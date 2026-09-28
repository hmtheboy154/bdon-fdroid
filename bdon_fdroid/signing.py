"""Wrap the index files in signed JARs.

F-Droid does not trust a plain ``index-v2.json``: it downloads ``entry.jar`` and
``index-v2.json``, verifies the JAR signature against the repository key whose
fingerprint the user installed the repo with, and then checks that the SHA-256 of
``index-v2.json`` matches the value inside the signed ``entry.json``.

Signing is done with the JDK's ``jarsigner`` rather than a Python
implementation, because the format (PKCS#7 signature blocks inside a ZIP) is
fiddly and ``jarsigner`` is already present on GitHub's runners.  The algorithms
match what ``fdroidserver`` itself uses, see
``fdroidserver/signindex.py::sign_jar``:

* ``index-v2.jar`` and ``entry.jar`` - SHA-256 / SHA256withRSA, matching the
  index-v2 scheme,
* ``index-v1.jar`` - SHA-1 / SHA1withRSA, because Android before 4.3 cannot
  verify anything stronger and old clients still exist.

Passwords are passed via ``-storepass:env`` / ``-keypass:env`` so they never
appear in the process list.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile

#: name -> (digest algorithm, signature algorithm)
ALGORITHMS = {
    "entry.jar": ("SHA-256", "SHA256withRSA"),
    "index-v2.jar": ("SHA-256", "SHA256withRSA"),
    "index-v1.jar": ("SHA1", "SHA1withRSA"),
}


class SigningError(RuntimeError):
    """Raised when a key is missing, unusable, or signing fails."""


class KeyStore:
    """The repository signing key, as loaded from disk."""

    def __init__(self, path: str, alias: str, password: str) -> None:
        self.path = os.path.abspath(path)
        self.alias = alias
        self.password = password
        if not os.path.exists(self.path):
            raise SigningError(
                f"keystore {self.path} does not exist. Generate one with "
                "scripts/init-repo-key.sh, or set KEYSTORE_PATH."
            )
        if not password:
            raise SigningError("the keystore password is empty (set KEYSTORE_PASS)")

    def _env(self) -> dict:
        env = dict(os.environ)
        # jarsigner reads both the store and the key password from here.
        env["BDON_KEYSTORE_PASS"] = self.password
        return env

    def fingerprint(self) -> str:
        """The SHA-256 fingerprint of the signing certificate, lower-case hex.

        F-Droid users need this to add the repository safely; it is shown on the
        published landing page and used in the ``?fingerprint=`` repo URL.
        """
        result = subprocess.run(
            [
                "keytool",
                "-list",
                "-v",
                "-keystore",
                self.path,
                "-storepass:env",
                "BDON_KEYSTORE_PASS",
                "-alias",
                self.alias,
            ],
            capture_output=True,
            text=True,
            env=self._env(),
        )
        if result.returncode != 0:
            raise SigningError(
                f"keytool could not read {self.path} (alias {self.alias!r}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("SHA256:"):
                return stripped.split(":", 1)[1].replace(":", "").strip().lower()
        raise SigningError(
            f"no SHA-256 fingerprint found in keytool output for alias {self.alias!r}"
        )


def build_jar(payload_name: str, payload: bytes, dest: str) -> str:
    """Create a JAR containing exactly one entry, ``payload_name``."""
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        # A fixed timestamp keeps the output byte-identical between runs when
        # the payload is unchanged, so signing cannot introduce a spurious diff.
        info = zipfile.ZipInfo(payload_name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, payload)
    return dest


def sign_jar(jar_path: str, keystore: KeyStore, jar_name: str) -> None:
    """Sign ``jar_path`` in place using the algorithms F-Droid expects."""
    try:
        digest_alg, sig_alg = ALGORITHMS[jar_name]
    except KeyError as exc:  # pragma: no cover - programming error
        raise SigningError(f"no algorithm configured for {jar_name}") from exc

    if shutil.which("jarsigner") is None:
        raise SigningError(
            "jarsigner not found on PATH. Install a JDK (GitHub's ubuntu-latest "
            "runner includes one) or set JAVA_HOME."
        )

    command = [
        "jarsigner",
        "-keystore",
        keystore.path,
        "-storepass:env",
        "BDON_KEYSTORE_PASS",
        "-keypass:env",
        "BDON_KEYSTORE_PASS",
        "-digestalg",
        digest_alg,
        "-sigalg",
        sig_alg,
        jar_path,
        keystore.alias,
    ]
    result = subprocess.run(command, capture_output=True, text=True, env=keystore._env())
    if result.returncode != 0:
        raise SigningError(
            f"jarsigner failed for {jar_path} (exit {result.returncode}):\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def verify_jar(jar_path: str, jar_name: str) -> None:
    """Self-check that what we produced really is a validly signed JAR.

    Two things are checked: that the JAR has the shape F-Droid expects (one
    payload entry plus a signature), and - except for ``index-v1.jar`` - that
    ``jarsigner`` accepts the signature.

    ``index-v1.jar`` is signed with SHA-1 on purpose, mirroring
    ``fdroidserver``, because Android before 4.3 cannot verify anything
    stronger.  Modern JDKs refuse to *verify* SHA-1 signatures (they consider
    the algorithm disabled), so the exit status is not meaningful for that one
    file; the structural check still applies.
    """
    with zipfile.ZipFile(jar_path) as archive:
        names = archive.namelist()
    payload = [name for name in names if not name.startswith("META-INF/")]
    signature_files = [name for name in names if name.endswith((".SF", ".RSA", ".DSA", ".EC"))]
    if len(payload) != 1:
        raise SigningError(
            f"{os.path.basename(jar_path)} must contain exactly one payload entry, "
            f"found {payload}"
        )
    if not any(name.endswith(".SF") for name in signature_files):
        raise SigningError(f"{os.path.basename(jar_path)} has no .SF signature file")
    if not any(name.endswith((".RSA", ".DSA", ".EC")) for name in signature_files):
        raise SigningError(
            f"{os.path.basename(jar_path)} has no signature block "
            "(.RSA/.DSA/.EC)"
        )

    if ALGORITHMS[jar_name][0] == "SHA1":
        return

    # Not -strict: a F-Droid repository key is self-signed and untimestamped, both
    # of which -strict rejects, yet F-Droid's own verifier (BouncyCastle, matching
    # the certificate fingerprint) accepts them.  What matters here is simply that
    # the signature over our payload is intact.
    result = subprocess.run(
        ["jarsigner", "-verify", jar_path], capture_output=True, text=True
    )
    output = (result.stdout + result.stderr).strip()
    if "jar verified" not in output:
        raise SigningError(
            f"jarsigner could not verify {os.path.basename(jar_path)}:\n{output}"
        )


def sign_index_files(
    root: str, payloads: dict[str, bytes], keystore: KeyStore
) -> list[str]:
    """Create and sign the signed wrappers for every index payload.

    ``payloads`` maps a JSON filename to its bytes.  Returns the paths written.
    """
    written: list[str] = []
    for json_name, payload in payloads.items():
        jar_name = json_name.replace(".json", ".jar")
        jar_path = os.path.join(root, jar_name)
        build_jar(json_name, payload, jar_path)
        sign_jar(jar_path, keystore, jar_name)
        verify_jar(jar_path, jar_name)
        written.append(jar_path)
    return written
