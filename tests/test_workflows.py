"""Structural checks on the workflow files.

A GitHub Actions workflow that is not valid YAML *schema* still parses as YAML,
so nothing local complains: ``yaml.safe_load`` accepts it, ``actionlint`` is not
available, and the file looks fine.  GitHub only rejects it when the workflow is
validated, which surfaces as a failed run in the UI and an email - not as a red
build of anything you were working on.

That is exactly what happened to ``redirector.yml``: a comment dedented one level
too far left a step containing nothing but a ``name:``, and the whole file
stopped being a valid workflow.  The Worker was already deployed by hand, so
nothing appeared broken and the failure went unnoticed.

No YAML parser is used, because the daily job may not gain a dependency (see
AGENTS.md).  These checks are line-based, which is enough for the structural
mistakes that actually occur and keeps the file readable.
"""

import pathlib
import re
import unittest

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"

#: A step has to say what it does. GitHub's error for a step with only a name is
#: "There's not enough info to determine what you meant", so match that rather
#: than trying to model the whole schema.
ACTION_KEYS = ("run", "uses")


def _lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """(1-based line number, text) for every line, tabs expanded."""
    out = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        out.append((number, raw.expandtabs(2)))
    return out


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def _is_item(text: str) -> bool:
    return text.lstrip(" ").startswith("- ")


def _key(text: str) -> str:
    """The mapping key at the start of a line, or '' if there isn't one.

    A list item is ``- key: value``, so the marker has to come off first.
    Without that, every step reports an empty key and the checks that depend on
    it quietly pass on nothing.
    """
    stripped = text.lstrip(" ")
    if stripped.startswith("- "):
        stripped = stripped[2:].lstrip(" ")
    match = re.match(r"([A-Za-z_][\w-]*):", stripped)
    return match.group(1) if match else ""


def _steps(lines: list[tuple[int, str]]) -> list[tuple[int, int, str, list[tuple[int, str]]]]:
    """Every ``steps:`` list item as (line, indent, key, its owned lines).

    Owned lines are those more deeply indented than the ``- `` marker, up to the
    next item at the same indent. A ``run: |`` block is included: its body is
    more deeply indented still, and is content rather than schema.

    Comments at the item's own indent are deliberately *kept* rather than
    terminating the body. Dedenting a comment that way is the mistake these
    checks exist for, so it has to stay visible to the step above it.
    """
    found = []
    in_steps = False
    steps_indent = 0
    index = 0
    while index < len(lines):
        number, text = lines[index]
        if _key(text) == "steps" and not text.lstrip(" ").startswith("-"):
            in_steps = True
            steps_indent = _indent(text)
            index += 1
            continue
        if in_steps and _is_item(text) and _indent(text) > steps_indent:
            item_indent = _indent(text)
            body = []
            cursor = index + 1
            while cursor < len(lines):
                nxt_number, nxt_text = lines[cursor]
                stripped = nxt_text.strip()
                is_comment = stripped.startswith("#")
                if stripped and not is_comment and _indent(nxt_text) <= item_indent:
                    break
                if _is_item(nxt_text) and _indent(nxt_text) == item_indent:
                    break
                body.append((nxt_number, nxt_text))
                cursor += 1
            found.append((number, item_indent, _key(text), body))
            index = cursor
            continue
        if in_steps and text.strip() and _indent(text) <= steps_indent and not _is_item(text):
            in_steps = False
        index += 1
    return found


class StepShapeTests(unittest.TestCase):
    def setUp(self):
        self.paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
        if not self.paths:
            self.skipTest("no workflow files found")

    def test_every_workflow_file_exists_where_actions_looks(self):
        for path in self.paths:
            self.assertEqual(path.suffix, ".yml", f"{path.name} should use .yml")

    def test_no_step_lacks_a_run_or_a_uses(self):
        for path in self.paths:
            for number, indent, key, body in _steps(_lines(path)):
                if not key:
                    continue  # a bare "- |" block scalar
                # The action may be the item's own key ("- uses: actions/...") or
                # sit in the body ("- name: ... / run: ..."). Both are valid;
                # having neither is the error.
                keys = {key} | {_key(text) for _, text in body}
                if any(action in keys for action in ACTION_KEYS):
                    continue
                with self.subTest(workflow=path.name, line=number, step=key):
                    message = (
                        f"{path.name}:{number} step {key!r} has no "
                        f"{' or '.join(ACTION_KEYS)}; GitHub rejects the whole file"
                    )
                    # The usual cause is a comment dedented to the item level: it
                    # reads as though it introduces the *next* step and leaves
                    # this one with only a name. Name it, because the file looks
                    # fine and the error is reported far from the cause.
                    level = [n for n, t in body if t.strip().startswith("#")]
                    if level:
                        message += (
                            f". A comment at the item level on line(s) "
                            f"{', '.join(map(str, level))} is the likely cause: a "
                            f"comment introducing the next step belongs *before* "
                            f"the '- ', not at the same indent as it"
                        )
                    self.fail(message)

    def test_a_named_step_puts_its_comment_inside_the_step(self):
        """The same rule stated positively, so the fix is obvious from the test.

        A comment at the same indent as a ``- `` marker is not *always* wrong -
        one introducing the following step is normal and legal. It is only
        ambiguous when it leaves the step above it without a ``run`` or
        ``uses:``, which the check above reports with the line number.
        """
        source = (WORKFLOWS / "redirector.yml").read_text(encoding="utf-8")
        self.assertIn(
            "      - name: Run the Worker tests\n"
            "        # Never deploy code that does not pass its own tests.\n"
            "        run: node --test worker/\n",
            source,
        )


class TriggersTests(unittest.TestCase):
    def setUp(self):
        self.paths = sorted(WORKFLOWS.glob("*.yml"))
        if not self.paths:
            self.skipTest("no workflow files found")

    def test_the_daily_job_is_scheduled(self):
        source = (WORKFLOWS / "update.yml").read_text(encoding="utf-8")
        self.assertRegex(source, r"(?m)^\s*schedule:", "update.yml has no schedule:")
        self.assertRegex(source, r"(?m)^\s*- cron:", "update.yml has no cron entry")

    def test_every_workflow_declares_its_permissions(self):
        # Least privilege is the default expectation, and an absent block means
        # the default GITHUB_TOKEN scope is used instead of an explicit one.
        for path in self.paths:
            with self.subTest(workflow=path.name):
                self.assertIn(
                    "\npermissions:\n", f"\n{path.read_text(encoding='utf-8')}"
                )

    def test_every_job_pins_a_runner(self):
        for path in self.paths:
            for number, text in _lines(path):
                if _key(text) == "runs-on":
                    with self.subTest(workflow=path.name, line=number):
                        self.assertNotEqual(text.split(":", 1)[1].strip(), "")


class RepositoryVariableTests(unittest.TestCase):
    """Guards the bug that shipped a repository address only one client accepts.

    A repository *variable* reaches Actions only as ``${{ vars.NAME }}``. Once it
    is in a step's ``env:`` block the shell may read it normally - that bridge is
    the documented idiom, and the only way to do it. What is never correct is a
    shell read of a name that nothing bridged: the expansion is silently empty,
    the code falls through to its fallback, and the run goes green.

    That is not hypothetical. ``update.yml`` read ``address="${REPO_URL:-}"`` with
    no bridge, published the GitHub Pages address, and only the official F-Droid
    client could install from it.
    """

    NAMES = ("REPO_URL", "REDIRECTOR_URL", "PAGES_ORIGIN", "REPO_URL_VARIABLE")

    def setUp(self):
        self.paths = sorted(WORKFLOWS.glob("*.yml"))
        if not self.paths:
            self.skipTest("no workflow files found")

    def _code_lines(self, body):
        """Body lines with comments stripped out.

        Commenting the bug out of the way is the normal way to document it, and
        ``update.yml`` does exactly that, so a naive scan reports the explanation
        as if it were the mistake.
        """
        for number, text in body:
            if not text.lstrip(" ").startswith("#"):
                yield number, text

    def test_no_shell_reads_a_variable_nothing_bridged(self):
        for path in self.paths:
            for number, _indent, key, body in _steps(_lines(path)):
                if not key:
                    continue
                bridged = {
                    _key(text)
                    for _, text in self._code_lines(body)
                    if _key(text) in self.NAMES and text.split(":", 1)[1].strip()
                }
                for line_number, text in self._code_lines(body):
                    for name in self.NAMES:
                        if name not in bridged:
                            continue
                        for match in re.finditer(r"\$\{?" + name + r"(?=[}:\s\"])", text):
                            with self.subTest(workflow=path.name, line=line_number, name=name):
                                self.assertIn(
                                    f"{name}: ${{{{", "\n".join(t for _, t in body),
                                    f"{path.name}:{line_number} reads ${{{name}}} but no "
                                    f"env: entry bridges it",
                                )

    def test_a_repository_variable_is_bridged_through_vars(self):
        # The bridge itself must use the vars context. A step that reads
        # $REPO_URL is fine; one that defines REPO_URL: $REPO_URL is not.
        for path in self.paths:
            for number, _indent, key, body in _steps(_lines(path)):
                for line_number, text in self._code_lines(body):
                    name = _key(text)
                    if name not in ("REPO_URL", "REDIRECTOR_URL", "PAGES_ORIGIN"):
                        continue
                    value = text.split(":", 1)[1].strip()
                    with self.subTest(workflow=path.name, line=line_number, name=name):
                        self.assertNotEqual(value, "", f"{path.name}:{line_number} empty bridge")
                        self.assertRegex(
                            value,
                            r"\$\{\{\s*(vars|secrets|inputs|steps|github|env)\.",
                            f"{path.name}:{line_number} bridges {name} from {value!r}; "
                            f"a repository variable is only visible as "
                            f"{{{{ vars.{name} }}}}",
                        )

    def test_the_daily_job_still_reads_the_variable_the_right_way(self):
        source = (WORKFLOWS / "update.yml").read_text(encoding="utf-8")
        self.assertIn("REPO_URL_VARIABLE: ${{ vars.REPO_URL }}", source)
        self.assertIn("REDIRECTOR_URL: ${{ vars.REDIRECTOR_URL }}", source)
        # The exact expression that shipped the bug.
        self.assertNotIn('address="${REPO_URL:-}"', source)

    def test_half_configured_addresses_fail_the_run(self):
        """...and the guard that now stops it recurring quietly.

        Asserted structurally: an ``::error::`` annotation, a message naming
        REPO_URL, and a non-zero exit. Matching the wording instead would break
        every time the message is reworded, which is not the point of the guard.
        """
        source = (WORKFLOWS / "update.yml").read_text(encoding="utf-8")
        guard = re.search(r'if \[ -n "\$REDIRECTOR_URL" \].*?fi', source, re.S)
        self.assertIsNotNone(guard, "the half-configured guard is gone")
        block = guard.group(0)
        self.assertIn("::error::", block, "the guard does not annotate the failure")
        self.assertIn("REPO_URL", block)
        self.assertIn("exit 1", block, "the guard reports but does not fail the run")


if __name__ == "__main__":
    unittest.main()
