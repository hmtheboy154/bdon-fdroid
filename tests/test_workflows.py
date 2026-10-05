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
import ast
import re
import sys
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


def _job_condition(path: pathlib.Path, job: str) -> str | None:
    """A job's ``if:`` expression, joined back into one line.

    Line-based on purpose. ``yaml`` is not importable in the daily job or in
    the plain-python unit job, and the whole point of these checks is that they
    run everywhere the suite runs.
    """
    lines = _lines(path)
    in_jobs = False
    for index, (_number, text) in enumerate(lines):
        if _key(text) == "jobs" and not text.lstrip(" ").startswith("-"):
            in_jobs = True
            continue
        if not in_jobs or _is_item(text):
            continue
        if _key(text) != job:
            continue
        # The job's keys sit two levels in; take its `if:` and fold back the
        # continuation lines that immediately follow it.
        for offset, (n2, t2) in enumerate(lines[index + 1 :], start=1):
            if t2.strip() and _indent(t2) <= 2:
                return None  # the next job; ours had no `if:`
            if _key(t2) == "if" and _indent(t2) == 4:
                expr = t2.split(":", 1)[1].strip()
                # A block scalar indicator (`>-`, `>`, `>-2`) is YAML's, not the
                # expression's; the continuation lines carry the value.
                expr = re.sub(r"^[>|][-+0-9]*\s*", "", expr)
                for _n3, t3 in lines[index + 1 + offset :]:
                    if t3.strip() and _indent(t3) > 4 and not t3.strip().startswith("#"):
                        expr = expr.rstrip() + " " + t3.strip()
                    else:
                        break
                return expr
    return None


def _step_script(path: pathlib.Path, name: str) -> str | None:
    """The shell script of the step with this name, de-indented.

    A ``run: |`` block's content is already uniformly indented by the YAML
    parser, so stripping that common indent returns the script exactly as bash
    would receive it.
    """
    all_lines = dict(_lines(path))
    for number, _indent_level, key, body in _steps(_lines(path)):
        if key != "name" or all_lines[number].split(":", 1)[1].strip() != name:
            continue
        for position, (_n, text) in enumerate(body):
            if _key(text) != "run":
                continue
            rest = body[position + 1 :]
            if not rest:
                return text.split("|", 1)[1] if "|" in text else ""
            width = _indent(rest[0][1])
            return "\n".join(t[width:] if len(t) > width else t.strip() for _, t in rest)
    return None


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

    NAMES = ("REPO_URL", "REDIRECTOR_URL", "PAGES_ORIGIN", "REPO_URL_VARIABLE",
             "REPO_ADDRESS")

    def setUp(self):
        self.paths = sorted(WORKFLOWS.glob("*.yml"))
        if not self.paths:
            self.skipTest("no workflow files found")

    @staticmethod
    def _written_to_env(path):
        """Names written to ``$GITHUB_ENV``, which propagates to later steps.

        This is the other legitimate way to bridge a value into the shell, so a
        name published this way must not be reported as unbridged.
        """
        names = set()
        for number, text in _lines(path):
            if "GITHUB_ENV" not in text:
                continue
            match = re.search(r"\b([A-Z_][A-Z0-9_]*)=", text)
            if match:
                names.add(match.group(1))
        return names

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
                } | self._written_to_env(path)
                env_bridged = self._written_to_env(path)
                for line_number, text in self._code_lines(body):
                    for name in self.NAMES:
                        if name not in bridged or name in env_bridged:
                            continue
                        for _match in re.finditer(r"\$\{?" + name + r"(?=[}:\s\"])", text):
                            with self.subTest(workflow=path.name, line=line_number, name=name):
                                self.assertIn(
                                    f"{name}: ${{{{", "\n".join(t for _, t in body),
                                    f"{path.name}:{line_number} reads ${{{name}}} but "
                                    f"nothing bridges it into the shell",
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


class RedirectorCheckTests(unittest.TestCase):
    """The check that the redirector still hands out the APK.

    This step failed on every run for a while and nothing noticed, because the
    other step in the job was checking a different host. The rules here are the
    ones whose absence made that possible.
    """

    def setUp(self):
        self.path = WORKFLOWS / "conformance.yml"
        self.source = self.path.read_text(encoding="utf-8")
        self.block = self._step_named("Check the redirector serves the APK")

    def _step_named(self, name: str) -> str:
        """The body of the step with this name.

        A step's ``name:`` is the item's own key, not a line in its body, so it
        has to be read from the item line rather than from the body.
        """
        all_lines = dict(_lines(self.path))
        for number, _indent, key, body in _steps(_lines(self.path)):
            if key != "name":
                continue
            if all_lines[number].split(":", 1)[1].strip() == name:
                return "\n".join(t for _, t in body)
        self.fail(f"no step named {name!r} in {self.path.name}")

    def test_no_workflow_uses_bare_urllib(self):
        """Cloudflare 403s urllib's default User-Agent.

        urllib sends ``Python-urllib/x.y``, a signature Cloudflare's managed
        rules reject, so a bare urlopen fails against any address served by the
        redirector while working against GitHub Pages. That asymmetry is what
        made this look like a live-site outage instead of a script bug. curl is
        already the idiom in these files; keep it that way.
        """
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertNotIn("urllib.request", text)

    def test_the_index_comes_from_the_published_address(self):
        # REDIRECTOR_URL is the origin; the repository is under it at
        # /fdroid/repo. Fetching "$REDIRECTOR_URL/index-v2.json" asks a path
        # that does not exist, which is a 404 that reads like a broken site.
        self.assertIn('curl -fsSL "$address/index-v2.json"', self.block)
        self.assertNotIn("$REDIRECTOR_URL/index-v2.json", self.block)

    def test_the_apk_is_probed_through_the_published_address(self):
        self.assertIn("address=\"$REPO_ADDRESS\"", self.block)
        self.assertIn("'%{redirect_url}' \"$address/$apk\"", self.block)

    def test_the_address_is_resolved_once_and_shared(self):
        # Two steps each deriving the address is how they came to disagree about
        # which host was being checked.
        self.assertIn('echo "REPO_ADDRESS=$address" >> "$GITHUB_ENV"', self.source)
        self.assertEqual(
            self.source.count('address="$REPO_ADDRESS"'),
            2,
            "both the verify step and the redirector step should read the "
            "resolved address rather than deriving their own",
        )

    def test_the_conformance_job_checks_the_address_users_were_given(self):
        # It used to read inputs.repo_url only, which is empty on push and
        # schedule, so the job fell back to a Pages address nobody adds.
        self.assertIn("REPO_URL_VARIABLE: ${{ vars.REPO_URL }}", self.source)
        self.assertNotIn("REPO_URL: ${{ inputs.repo_url }}", self.source)

    def test_the_redirector_is_still_required(self):
        """...and none of this may make the check conditional again.

        Scoped to the guard itself rather than the whole step: asserting on
        "exit 1" anywhere in the block passes even after the guard has been
        turned into a skip, because the later error branches still exit 1.
        """
        guard = re.search(r'if \[ -z "\$REDIRECTOR_URL" \].*?fi', self.block, re.S)
        self.assertIsNotNone(guard, "the unset-REDIRECTOR_URL guard is gone")
        block = guard.group(0)
        self.assertIn("::error::", block, "the guard no longer reports the problem")
        self.assertIn("exit 1", block, "the guard reports but does not fail the run")


class LiveSiteJobGateTests(unittest.TestCase):
    """The live-site check has to actually run.

    It was gated on ``schedule || inputs.repo_url != ''``, and ``inputs`` is empty
    on push and pull_request - so the job that verifies the published address and
    the redirector was skipped on every ordinary push and ran once a week. The
    defect it exists to catch could not be caught any sooner than a week after
    being introduced.
    """

    #: Every comparison the gate is allowed to make. A new one has to be added
    #: here deliberately, rather than silently falling through the evaluator.
    VALUES = {
        "github.event_name": "",
        "inputs.repo_url": "",
        "vars.REPO_URL": "",
        "vars.REDIRECTOR_URL": "",
    }

    def setUp(self):
        self.path = WORKFLOWS / "conformance.yml"
        self.condition = _job_condition(self.path, "published")
        self.assertIsNotNone(self.condition, "the published job has no if: condition")

    def _runs(self, event: str, repo_url: str = "", repo_var: str = "",
              redirector: str = "") -> bool:
        """Evaluate the gate the way Actions would, term by term."""
        values = dict(self.VALUES, **{
            "github.event_name": event,
            "inputs.repo_url": repo_url,
            "vars.REPO_URL": repo_var,
            "vars.REDIRECTOR_URL": redirector,
        })
        clauses = [c.strip() for c in self.condition.split("||") if c.strip()]
        self.assertGreaterEqual(
            len(clauses), 1, f"could not read the gate: {self.condition!r}"
        )
        for clause in clauses:
            match = re.fullmatch(r"([\w.]+)\s*(!=|==)\s*(.+)", clause)
            self.assertIsNotNone(match, f"unguarded clause in the gate: {clause!r}")
            left, operator, raw_right = (g.strip() for g in match.groups())
            self.assertIn(left, values, f"the gate reads an unmodelled value: {left}")
            right = raw_right.strip("\"'")
            if operator == "==":
                if (values[left] if left in values else left) == right:
                    return True
            elif right == "":
                if values.get(left, "") != "":
                    return True
            elif values.get(left, "") != right:
                return True
        return False

    def test_it_runs_on_a_push(self):
        self.assertTrue(self._runs("push", repo_var="https://x.example.com/fdroid/repo"))

    def test_it_runs_on_the_weekly_schedule(self):
        self.assertTrue(self._runs("schedule"))

    def test_it_runs_on_a_manual_dispatch_with_an_address(self):
        self.assertTrue(self._runs("workflow_dispatch", repo_url="https://x.example.com/fdroid/repo"))

    def test_it_skips_a_push_for_a_fork_with_nothing_configured(self):
        # Not a failure: a fork that has published nothing should not go red on
        # every push it makes.
        self.assertFalse(self._runs("push"))

    def test_it_runs_when_only_the_redirector_is_configured(self):
        self.assertTrue(self._runs("push", redirector="https://x.example.com"))

    def test_it_runs_on_a_pull_request_when_configured(self):
        self.assertTrue(self._runs("pull_request", repo_var="https://x.example.com/fdroid/repo"))

    def test_it_does_not_hard_fail_a_pull_request_from_a_fork(self):
        self.assertFalse(self._runs("pull_request"))

    def test_it_is_not_left_weekly_only(self):
        """The exact regression, asserted directly.

        Compared for equality rather than substring: the widened gate does still
        *contain* the old two terms, as the first two of four, so a substring
        check would fail on the correct file.
        """
        flattened = " ".join(self.condition.split())
        self.assertNotEqual(
            flattened, "${{ github.event_name == 'schedule' || inputs.repo_url != '' }}"
        )
        self.assertGreaterEqual(len(self.condition.split("||")), 3)


class ReleaseCommitGateTests(unittest.TestCase):
    """The daily job must not commit a timestamp change.

    ``lastChecked`` moves on every run by design, so diffing the whole file made
    the workflow commit every single time - four identical-looking
    "chore: record release" commits in a row, with a real new release buried
    among them.
    """

    def setUp(self):
        self.path = WORKFLOWS / "update.yml"
        self.block = _step_script(self.path, "Commit the updated release history")
        self.assertIsNotNone(self.block, "no such step in update.yml")

    def test_it_compares_the_release_list_not_the_whole_file(self):
        self.assertIn('json.loads(blob).get("releases")', self.block)
        self.assertIn('json.load(open("releases.json")).get("releases")', self.block)

    def test_it_no_longer_commits_on_a_whole_file_diff(self):
        # This is what made every run a commit.
        self.assertNotIn("git diff --quiet -- releases.json", self.block)

    def test_it_reports_through_an_if_condition_so_set_e_does_not_abort(self):
        # The comparison exits non-zero when the history changed, which under
        # `set -e` would kill the step unless it sits in a condition.
        self.assertRegex(self.block, r"if python3 - <<'PY'")
        self.assertIn("sys.exit(0 if previous == current else 1)", self.block)

    def test_a_first_run_with_no_recorded_history_still_commits(self):
        # git show fails when the file is not in HEAD; that must read as
        # "nothing recorded yet" rather than aborting the step.
        self.assertIn("previous = None  # first run", self.block)
        self.assertIn("except Exception:", self.block)


class StdlibOnlyTests(unittest.TestCase):
    """Nothing in the project may import anything outside the standard library.

    This is the project's own hard rule - the daily job must not grow a
    ``pip install`` step - and it has no natural enforcement. It was broken by a
    ``import yaml`` in a test, which passed locally and failed in CI: the
    ``unit`` job and the daily ``update`` job both run the suite under plain
    ``python3`` with no third-party packages available, so the daily job stopped
    publishing before anything looked wrong.
    """

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    #: Allowed even though it is not stdlib: it is an optional test-only
    #: dependency, imported lazily behind a skipUnless, and is only installed in
    #: the throwaway virtualenv the conformance job creates.
    OPTIONAL = {"fdroidserver"}

    @staticmethod
    def _sources():
        root = StdlibOnlyTests.ROOT
        return sorted(list((root / "tests").glob("*.py")) +
                      list((root / "bdon_fdroid").glob("*.py")))

    @classmethod
    def _imports(cls):
        """Every module name imported anywhere in the project.

        Parsed with ``ast`` rather than scanned with a regex, so a module name
        mentioned in a docstring or a comment - which is exactly how this rule
        gets described - is not mistaken for an import of it.
        """
        for path in cls._sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        yield path, node.lineno, alias.name.split(".")[0]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    if node.module:
                        yield path, node.lineno, node.module.split(".")[0]

    def test_nothing_imports_a_package_that_is_not_installed(self):
        allowed = set(sys.stdlib_module_names) | {"bdon_fdroid", "tests"} | self.OPTIONAL
        offenders = [
            f"{path.relative_to(self.ROOT)}:{number} imports {name!r}"
            for path, number, name in self._imports()
            if name not in allowed
        ]
        self.assertEqual(
            offenders, [],
            "non-stdlib imports, which fail in the daily and unit jobs:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_optional_dependency_is_still_optional(self):
        """The one allowed non-stdlib import has to stay optional.

        If ``fdroidserver`` ever became required, the allow-list above would be
        hiding a real dependency instead of documenting a guarded one - and the
        daily job would break in exactly the way this class exists to prevent.
        """
        source = (self.ROOT / "tests" / "test_conformance.py").read_text(encoding="utf-8")
        self.assertIn("def _fdroidserver_available()", source)
        self.assertIn("except ImportError", source)
        # Both conformance classes are gated on it.
        self.assertGreaterEqual(
            source.count("skipUnless(_fdroidserver_available()"), 2
        )

    def test_the_guard_is_decided_by_importing_the_optional_module(self):
        # Proves the allow-list is honest: the skip really does depend on the
        # import failing, so the daily job is unaffected.
        self.assertNotIn("fdroidserver", sys.stdlib_module_names)
        try:
            import fdroidserver  # noqa: F401
        except ImportError:
            return  # the common case in the daily and unit jobs
        self.skipTest("fdroidserver is installed here, so the skip cannot be exercised")


if __name__ == "__main__":
    unittest.main()
