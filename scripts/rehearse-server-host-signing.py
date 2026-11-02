"""Rehearse an explicitly signed, notarized server host release (#2331).

This is a CI orchestration tool, not an installer and not a publisher. It runs
the opt-in Developer ID build of ``scripts/build-server-host.py`` inside a
temporary signing keychain, checks the resulting archive by extracting it
again, authenticates the exact bytes with a signed candidate manifest, and
writes a sanitized runner-local report. It never uploads a release asset, never
runs ``actions/upload-artifact``, and never distributes anything: the Apple
notary submission the release builder performs is the only network write.

Every input is explicit. The signing identity, the expected team, the exported
certificate, its import password, the App Store Connect API key and the key id
and issuer identifiers are named CI inputs, read from the environment by name
only. Missing inputs are reported by name; values never reach an error, a log
line, a report or a step summary. A malformed or missing identity is never an
excuse to run the default ad-hoc build: this tool has exactly one build path and
it is the signed one.

The temporary keychain and notary profile live under the fresh ``--output``
directory, are made the user default and the only signing-identity search
target for the duration of the build, and are restored and deleted on every
path, including build, notarization, manifest and verification failures and a
SIGTERM/SIGINT interruption. Credentials stay out of argv: the keychain password
reaches ``security`` on stdin, the App Store Connect API key reaches
``notarytool`` as the path to an owner-only file in the private scratch
directory, and submissions use only the keychain profile name. One documented
residual remains: ``security import -P`` takes the p12 passphrase in a single
short-lived process because Apple's CLI has no stdin input for it. The decoded
key, the p12, the keychain and the profile are deleted in the same unconditional
cleanup.

Every failure after the scratch directory exists leaves an explicitly
incomplete ``verification.json`` and ``summary.txt`` naming only the failing
stage and its code, so a failed run can never be mistaken for a verified one.
The pinned manifest signer is installed in its own process with no signing
credential in the environment, and ownership is verified by the prepared
interpreter rather than this process's imports.

```sh
python scripts/rehearse-server-host-signing.py \\
    --output "$RUNNER_TEMP/server-host-signing-rehearsal" \\
    --python "$RUNNER_TEMP/engine-check/bin/python" \\
    --source-sha "$GITHUB_SHA" \\
    --wheel dist/ciaobot-1.7.0-py3-none-any.whl
```

Import-safe on Windows and Linux: the macOS-only native tools are injected and
never reached until the platform and input checks pass.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import importlib.util
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
from types import ModuleType
from typing import Any, Callable, Iterator, Mapping

from ciao import release_manifest, server_host

REPO = Path(__file__).resolve().parents[1]
BUILDER_SCRIPT = REPO / "scripts" / "build-server-host.py"

#: The pinned standalone minisign signer, unchanged from the production release.
#: It is installed ahead of signing, in its own process with no signing
#: credential in the environment, and then invoked by path.
MANIFEST_SIGNER = "@tauri-apps/cli@2.11.4"
SIGNER_BIN = Path("node_modules") / ".bin" / "tauri"

#: Marks the scratch directory this run created, and the sibling receipt that
#: records the exact path this run created. The workflow deletes only a path the
#: helper recorded, so a pre-existing directory (even one that already contains
#: a marker) is never adopted or removed.
OWNERSHIP_MARKER = ".rehearsal-owned"
OWNERSHIP_RECEIPT_SUFFIX = ".owned"

#: How long a terminated child gets to exit before it is killed and reaped.
CHILD_REAP_SECONDS = 10

#: Runs the ownership round trip in the prepared interpreter, so the verifier
#: that reads the record is the installed wheel and never an editable checkout.
OWNERSHIP_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "from ciao.server_host import verify_owned_host\n"
    "verify_owned_host(Path(sys.argv[1]), ownership_path=Path(sys.argv[2]))\n"
)

#: The notary keychain profile is a random name, never a fixed one that could
#: collide with a profile already on the runner.
PROFILE_PREFIX = "ciaobot-rehearsal-"

#: Bounded native steps. The build itself bounds its own notary wait; these are
#: the outer deadlines for the keychain, manifest and signer processes.
KEYCHAIN_TIMEOUT_SECONDS = 120
MANIFEST_TIMEOUT_SECONDS = 300
SIGNER_TIMEOUT_SECONDS = 900

#: Where the rehearsal writes its sanitized, nonsecret report and the summary
#: the workflow may append to the job's step summary. Both stay runner-local.
VERIFICATION_NAME = "verification.json"
SUMMARY_NAME = "summary.txt"
REPORT_KIND = "server-host-signing-rehearsal"

#: The owner-only record written under the scratch home and read back by the
#: runtime contract. It is proof that the staged snapshot interoperates with
#: ``verify_owned_host``, not a live install or a TCC result.
RECORD_RELATIVE = Path(".local") / "state" / "ciaobot" / "server-host.json"

#: The one fixed archive name the manifest and every consumer pin.
ARCHIVE_NAME = "ciaobot-server-host-macos-universal-v1.tar.gz"

#: The release manifest verifier key the candidate manifest must be checked
#: with. The rehearsal refuses an installed wheel that trusts another key: a
#: build that changed the trust root is not the release it claims to rehearse.
EXPECTED_RELEASE_PUBLIC_KEY = (
    "RWSDUnIeQDnpmnNJiTjLmN6XOVFqgn1A0EXvTVG7AJIZXJxhyFN9osxm"
)

#: The exact CI input names. The helper reads these by name and nothing else:
#: there is no ``CIAO_*`` knob and no credential auto-discovery.
IDENTITY_ENV = "SERVER_HOST_SIGNING_IDENTITY"
TEAM_ENV = "SERVER_HOST_TEAM_ID"
CERTIFICATE_ENV = "SERVER_HOST_CERTIFICATE_P12_BASE64"
CERTIFICATE_PASSWORD_ENV = "SERVER_HOST_CERTIFICATE_PASSWORD"
NOTARY_KEY_ENV = "SERVER_HOST_NOTARY_KEY_P8_BASE64"
NOTARY_KEY_ID_ENV = "SERVER_HOST_NOTARY_KEY_ID"
NOTARY_ISSUER_ID_ENV = "SERVER_HOST_NOTARY_ISSUER_ID"
TAURI_KEY_ENV = "TAURI_SIGNING_PRIVATE_KEY"
TAURI_PASSWORD_ENV = "TAURI_SIGNING_PRIVATE_KEY_PASSWORD"

REQUIRED_ENV = (
    IDENTITY_ENV,
    TEAM_ENV,
    CERTIFICATE_ENV,
    CERTIFICATE_PASSWORD_ENV,
    NOTARY_KEY_ENV,
    NOTARY_KEY_ID_ENV,
    NOTARY_ISSUER_ID_ENV,
    TAURI_KEY_ENV,
    TAURI_PASSWORD_ENV,
)

TEAM_ID_PATTERN = re.compile(r"[A-Z0-9]{10}")
#: The App Store Connect API key identifier is ten alphanumeric characters and
#: the issuer is a UUID. Both are identifiers, not secrets, and both are
#: validated before any tool runs so a malformed key fails closed.
NOTARY_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9]{10}")
NOTARY_ISSUER_ID_PATTERN = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
#: The one PEM shape an App Store Connect API key has.
NOTARY_KEY_HEADER = b"-----BEGIN PRIVATE KEY-----"
NOTARY_KEY_FOOTER = b"-----END PRIVATE KEY-----"
SOURCE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
WHEEL_PATTERN = re.compile(r"^ciaobot-(\d+\.\d+\.\d+)-[^/]+\.whl$")

#: The environment names a signed candidate manifest is allowed to take from
#: the parent process. Everything else is dropped so the manifest subprocess
#: cannot import the checkout or read an unrelated credential.
INHERITED_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")


class RehearsalError(RuntimeError):
    """A refusal or failure whose message is fixed text and carries no secret."""


@dataclass(frozen=True)
class _Credentials:
    """The named CI inputs, with a repr that never reveals one.

    ``repr=False`` on every field is deliberate: a credential must not appear in
    a traceback, a debug print, a test failure diff or a report because someone
    formatted this object.
    """

    identity: str = field(repr=False)
    team_id: str = field(repr=False)
    certificate: bytes = field(repr=False)
    certificate_password: str = field(repr=False)
    notary_key: bytes = field(repr=False)
    key_id: str = field(repr=False)
    issuer_id: str = field(repr=False)
    tauri_key: str = field(repr=False)
    tauri_password: str = field(repr=False)


@dataclass(frozen=True)
class RehearsalResult:
    """What a completed rehearsal proved, all of it nonsecret."""

    source_sha: str
    archive_name: str
    archive_size: int
    archive_sha256: str
    report_path: Path
    summary: tuple[str, ...]


def _run(
    runner: Callable[..., Any],
    cmd: list[str],
    *,
    failure: str,
    timeout: int,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> Any:
    """Run one bounded tool; every failure becomes a sanitized error.

    The argv, the captured output and the original exception stay out of the
    message and out of the exception chain. Credentials reach a tool through
    ``input_text`` (never argv) wherever the tool supports a prompt; the p12
    import password is the one documented argv residual.
    """
    try:
        return runner(
            cmd,
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=None if env is None else dict(env),
            cwd=None if cwd is None else str(cwd),
            input=None if input_text is None else input_text,
        )
    except subprocess.TimeoutExpired:
        raise RehearsalError(
            f"{failure}: the tool did not finish within {timeout} seconds"
        ) from None
    except subprocess.CalledProcessError:
        raise RehearsalError(failure) from None
    except OSError:
        raise RehearsalError(failure) from None


def _output_text(completed: Any) -> str:
    """The combined stdout/stderr of a completed tool, never echoed anywhere."""
    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    return f"{stdout}\n{stderr}"


def _quoted_paths(text: str) -> list[str]:
    """The paths in ``security``'s quoted list output, unquoted."""
    paths: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().rstrip(",")
        if not stripped:
            continue
        if stripped.startswith('"') and stripped.endswith('"'):
            stripped = stripped[1:-1]
        paths.append(stripped)
    return paths


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to a new owner-only regular file, refusing to follow a link."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        path.unlink(missing_ok=True)
        raise


def _replace_private(path: Path, data: bytes) -> None:
    """Write ``data`` owner-only at ``path``, replacing whatever is there."""
    temporary = path.with_name(f".{path.name}.rehearsal")
    try:
        _write_private(temporary, data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_builder() -> ModuleType:
    """The checked-out release builder, imported from its script path."""
    if not BUILDER_SCRIPT.is_file():
        raise RehearsalError(f"the release builder is missing: {BUILDER_SCRIPT.name}")
    spec = importlib.util.spec_from_file_location("build_server_host", BUILDER_SCRIPT)
    if spec is None or spec.loader is None:
        raise RehearsalError("the release builder could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wanted_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The CI inputs present in ``environ``, by name, for validation."""
    return {name: environ.get(name, "") for name in REQUIRED_ENV}


def _credentials(environ: Mapping[str, str]) -> _Credentials:
    """Validate every named input without ever printing a value."""
    values = _wanted_env(environ)
    missing = [name for name in REQUIRED_ENV if not values[name].strip()]
    if missing:
        raise RehearsalError(
            "missing required CI input(s): " + ", ".join(sorted(missing))
        )

    identity = values[IDENTITY_ENV]
    if identity.startswith("-") or identity != identity.strip():
        raise RehearsalError(
            f"{IDENTITY_ENV} must be a Developer ID identity, not an ad-hoc signature"
        )
    if any(char in identity for char in "\r\n\t"):
        raise RehearsalError(f"{IDENTITY_ENV} is malformed")

    team_id = values[TEAM_ENV].strip()
    if not TEAM_ID_PATTERN.fullmatch(team_id):
        raise RehearsalError(f"{TEAM_ENV} must be ten uppercase letters or digits")

    certificate_text = "".join(values[CERTIFICATE_ENV].split())
    try:
        certificate = base64.b64decode(certificate_text, validate=True)
    except (binascii.Error, ValueError):
        raise RehearsalError(
            f"{CERTIFICATE_ENV} is not valid base64-encoded PKCS#12 material"
        ) from None
    if not certificate:
        raise RehearsalError(f"{CERTIFICATE_ENV} decodes to nothing")

    key_id = values[NOTARY_KEY_ID_ENV].strip()
    if not NOTARY_KEY_ID_PATTERN.fullmatch(key_id):
        raise RehearsalError(
            f"{NOTARY_KEY_ID_ENV} must be ten alphanumeric characters"
        )
    issuer_id = values[NOTARY_ISSUER_ID_ENV].strip()
    if not NOTARY_ISSUER_ID_PATTERN.fullmatch(issuer_id):
        raise RehearsalError(f"{NOTARY_ISSUER_ID_ENV} must be a UUID")

    key_text = "".join(values[NOTARY_KEY_ENV].split())
    try:
        notary_key = base64.b64decode(key_text, validate=True)
    except (binascii.Error, ValueError):
        raise RehearsalError(
            f"{NOTARY_KEY_ENV} is not valid base64-encoded PEM key material"
        ) from None
    if not notary_key:
        raise RehearsalError(f"{NOTARY_KEY_ENV} decodes to nothing")
    stripped_key = notary_key.lstrip()
    if not stripped_key.startswith(NOTARY_KEY_HEADER) or not stripped_key.rstrip(
        b"\r\n"
    ).endswith(NOTARY_KEY_FOOTER):
        raise RehearsalError(
            f"{NOTARY_KEY_ENV} is not an App Store Connect API key PEM "
            f"(expected '{NOTARY_KEY_HEADER.decode()}' ... "
            f"'{NOTARY_KEY_FOOTER.decode()}')"
        )

    return _Credentials(
        identity=identity,
        team_id=team_id,
        certificate=certificate,
        certificate_password=values[CERTIFICATE_PASSWORD_ENV],
        notary_key=notary_key,
        key_id=key_id,
        issuer_id=issuer_id,
        tauri_key=values[TAURI_KEY_ENV],
        tauri_password=values[TAURI_PASSWORD_ENV],
    )


def _single_wheel(wheels: list[Path]) -> tuple[Path, str]:
    """The one candidate wheel and the release version its name carries."""
    if len(wheels) != 1:
        raise RehearsalError(
            "the rehearsal needs exactly one candidate wheel, "
            f"got {len(wheels)}"
        )
    wheel = wheels[0]
    if not wheel.is_file():
        raise RehearsalError(f"the candidate wheel is missing: {wheel.name}")
    match = WHEEL_PATTERN.fullmatch(wheel.name)
    if match is None:
        raise RehearsalError(
            "the candidate wheel is not named ciaobot-<version>-<tags>.whl"
        )
    # Resolve at this boundary. The workflow names the candidate relative to the
    # checkout (``--wheel dist/ciaobot-*.whl``) while the manifest subprocess runs
    # with the scratch output as its cwd, where that path names nothing; every
    # later use must name the exact file this run verified (#41221404339).
    return wheel.resolve(), match.group(1)


def _check_environment(
    *, platform_name: str, source_sha: str, output: Path, python: Path
) -> None:
    """Every refusal that must happen before the output directory exists."""
    if platform_name != "darwin":
        raise RehearsalError(
            "the Ciaobot Server host requires macOS; this platform cannot rehearse it"
        )
    if sys.version_info < (3, 12):
        raise RehearsalError("the rehearsal requires Python 3.12+")
    if release_manifest.RELEASE_PUBLIC_KEY != EXPECTED_RELEASE_PUBLIC_KEY:
        raise RehearsalError(
            "the installed manifest verifier does not use the release key"
        )
    if not SOURCE_SHA_PATTERN.fullmatch(source_sha):
        raise RehearsalError("the source SHA must be a lowercase 40-hex commit id")
    if not output.is_absolute():
        raise RehearsalError("the output path must be absolute")
    # lexists() also refuses a dangling symlink, which resolve() would follow.
    if os.path.lexists(output):
        raise RehearsalError(f"refusing to reuse an existing output path: {output}")
    # An ownership receipt is written only by a run that created its directory;
    # a pre-existing one is never adopted or overwritten.
    receipt = output.with_name(output.name + OWNERSHIP_RECEIPT_SUFFIX)
    if os.path.lexists(receipt):
        raise RehearsalError(
            f"refusing to reuse an existing ownership receipt: {receipt}"
        )
    if output.resolve().is_relative_to(REPO):
        raise RehearsalError(
            "refusing to rehearse inside the repository; use a scratch directory"
        )
    if not python.is_absolute() or not python.is_file():
        raise RehearsalError(
            "the prepared interpreter must be an absolute path to an existing file"
        )


def _clean_env(
    environ: Mapping[str, str], home: Path, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """A scrubbed subprocess environment: no checkout import, no stray secret.

    ``PYTHONPATH``/``PYTHONHOME`` are dropped so the manifest subprocess runs the
    installed wheel, never an accidental editable checkout import, and ``HOME``
    points at the scratch home so no real user state is written.
    """
    env = {name: environ[name] for name in INHERITED_ENV if name in environ}
    env["HOME"] = str(home)
    if extra:
        env.update(extra)
    return env


def _search_list(runner: Callable[..., Any]) -> list[str]:
    completed = _run(
        runner,
        ["security", "list-keychains", "-d", "user"],
        failure="the current keychain search list could not be read",
        timeout=KEYCHAIN_TIMEOUT_SECONDS,
    )
    paths = _quoted_paths(_output_text(completed))
    if not paths:
        # Restoring an empty or unparsable list would silently change the
        # runner's keychain state, so the rehearsal refuses before it changes it.
        raise RehearsalError("the current keychain search list is empty")
    return paths


def _default_keychain(runner: Callable[..., Any]) -> str:
    completed = _run(
        runner,
        ["security", "default-keychain", "-d", "user"],
        failure="the current default keychain could not be read",
        timeout=KEYCHAIN_TIMEOUT_SECONDS,
    )
    paths = _quoted_paths(_output_text(completed))
    if len(paths) != 1:
        raise RehearsalError("the current default keychain is ambiguous")
    return paths[0]


def _identity_is_available(text: str, identity: str) -> bool:
    """Whether ``security find-identity`` lists exactly the named identity."""
    return any(
        quoted == identity
        for line in text.splitlines()
        for quoted in re.findall(r'"([^"]*)"', line)
    )


class _ActiveChildren:
    """The one tool process that is currently running, and how to stop it.

    A signal handler must not restore keychain state while a ``security`` or
    ``notarytool`` child is still applying its own change. The handler asks this
    registry to terminate and reap the child first; production registers the
    real ``Popen`` and tests register a handle with the same protocol.

    The lock is re-entrant because the handler runs on the same thread that may
    be holding it while a process is being created. ``spawn`` holds it across
    create-and-register, so a signal delivered in that window defers instead of
    deadlocking, and the process is terminated as soon as it is registered.
    """

    def __init__(self) -> None:
        self._child: Any = None
        self._lock = threading.RLock()
        self._spawning = False
        self._deferred: str | None = None

    def register(self, child: Any) -> None:
        with self._lock:
            self._child = child

    def clear(self) -> None:
        with self._lock:
            self._child = None

    def spawn(self, factory: Callable[[], Any]) -> Any:
        """Create and register a child; stop it if a signal arrived meanwhile."""
        deferred: str | None = None
        with self._lock:
            self._spawning = True
            try:
                child = factory()
                # Keep ``_spawning`` set across registration: a signal delivered
                # between the factory returning and this assignment must see
                # "still spawning" (and defer), never a free handler that
                # restores while the child runs unregistered.
                self._child = child
            finally:
                # Clear the flag and collect a deferral under the same lock, so
                # no signal window can leave a deferred interruption unread.
                self._spawning = False
                deferred = self._deferred
                self._deferred = None
        if deferred is not None:
            outcome = self.stop()
            suffix = (
                "; the interrupted tool could not be reaped"
                if outcome == "unreaped"
                else ""
            )
            raise RehearsalError(
                f"the rehearsal was interrupted by {deferred}{suffix}"
            ) from None
        return child

    def stop(self, interrupt: str | None = None) -> str | None:
        """Terminate and reap the active child.

        Returns ``None`` when there was no child or it was reaped, ``"spawning"``
        when a process is still being created and cannot be stopped yet, and
        ``"unreaped"`` when a killed child still has not exited.
        """
        with self._lock:
            if self._spawning:
                if interrupt is not None:
                    self._deferred = interrupt
                return "spawning"
            child, self._child = self._child, None
        if child is None:
            return None
        try:
            child.terminate()
        except OSError:
            pass
        try:
            child.wait(timeout=CHILD_REAP_SECONDS)
            return None
        except subprocess.TimeoutExpired:
            pass
        try:
            child.kill()
        except OSError:
            pass
        try:
            child.wait(timeout=CHILD_REAP_SECONDS)
            return None
        except subprocess.TimeoutExpired:
            return "unreaped"


class _ProcessRunner:
    """``subprocess.run`` with the active child registered for the handler."""

    def __init__(self, children: _ActiveChildren) -> None:
        self._children = children

    def __call__(
        self,
        cmd: list[str],
        *,
        check: bool = False,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        process = self._children.spawn(
            lambda: subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=None if env is None else dict(env),
                cwd=None if cwd is None else str(cwd),
            )
        )
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise subprocess.TimeoutExpired(cmd, timeout) from None
        finally:
            self._children.clear()
        if check and process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode, cmd, output=stdout, stderr=stderr
            )
        return subprocess.CompletedProcess(
            cmd, process.returncode, stdout=stdout, stderr=stderr
        )


class _SigningKeychain:
    """The ephemeral keychain and everything needed to undo it.

    Nothing here is auto-detected or reused: the keychain password and the
    notary profile name are random per run, and every credential lives under the
    fresh scratch directory. ``restore`` is idempotent and re-entrancy safe, so
    it can run from ``finally``, from a signal handler and again afterwards.

    Each "changed" flag is set *before* the command that changes it. A tool that
    is killed or times out after applying its change has then still been marked
    as changed, so the restore always runs; restoring a value that was never
    changed is a harmless no-op.
    """

    def __init__(
        self,
        output: Path,
        credentials: _Credentials,
        runner: Callable[..., Any],
    ) -> None:
        self.runner = runner
        self.credentials = credentials
        self.credentials_dir = output / "credentials"
        self.keychain = self.credentials_dir / "rehearsal.keychain-db"
        self.certificate = self.credentials_dir / "certificate.p12"
        self.notary_key = self.credentials_dir / "notary-key.p8"
        self.keychain_password = secrets.token_urlsafe(32)
        self.profile = f"{PROFILE_PREFIX}{secrets.token_hex(8)}"
        self.previous_default = ""
        self.previous_search: list[str] = []
        self.created = False
        self.default_changed = False
        self.search_changed = False
        self._restoring = False

    def prepare(self) -> None:
        """Create the keychain, import the certificate and store the profile."""
        self.credentials_dir.mkdir(mode=0o700, exist_ok=False)
        try:
            self.previous_default = _default_keychain(self.runner)
            self.previous_search = _search_list(self.runner)
        except RehearsalError:
            shutil.rmtree(self.credentials_dir, ignore_errors=True)
            raise
        self._write_certificate()
        # ``create-keychain`` prompts twice and reads both lines from stdin, so
        # the keychain password never appears on the command line.
        self.created = True
        _run(
            self.runner,
            ["security", "create-keychain", str(self.keychain)],
            failure="the temporary signing keychain could not be created",
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            input_text=f"{self.keychain_password}\n{self.keychain_password}\n",
        )
        _run(
            self.runner,
            ["security", "set-keychain-settings", "-lut", "600", str(self.keychain)],
            failure="the temporary signing keychain settings could not be applied",
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
        _run(
            self.runner,
            ["security", "unlock-keychain", str(self.keychain)],
            failure="the temporary signing keychain could not be unlocked",
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            input_text=f"{self.keychain_password}\n",
        )
        self.default_changed = True
        _run(
            self.runner,
            ["security", "default-keychain", "-d", "user", "-s", str(self.keychain)],
            failure="the temporary signing keychain could not be made the default",
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
        self.search_changed = True
        _run(
            self.runner,
            ["security", "list-keychains", "-d", "user", "-s", str(self.keychain)],
            failure="the temporary signing keychain could not be searched",
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
        _run(
            self.runner,
            [
                "security",
                "import",
                str(self.certificate),
                "-k",
                str(self.keychain),
                # The one accepted residual: Apple's CLI offers no file or
                # stdin input for the p12 passphrase. It is visible in the
                # process list to the same user for the duration of this one
                # short-lived import, on an ephemeral runner, and nowhere else.
                "-P",
                self.credentials.certificate_password,
                # Only the signer may use the private key, without a prompt.
                "-T",
                "/usr/bin/codesign",
            ],
            failure="the signing certificate could not be imported",
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
        self.certificate.unlink(missing_ok=True)
        _run(
            self.runner,
            [
                "security",
                "set-key-partition-list",
                "-S",
                "apple-tool:,apple:,codesign:",
                "-s",
                str(self.keychain),
            ],
            failure="the signing key's access could not be bounded",
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            # -k is deprecated; the password is fed to the prompt instead.
            input_text=f"{self.keychain_password}\n",
        )
        listed = _run(
            self.runner,
            ["security", "find-identity", "-v", "-p", "codesigning", str(self.keychain)],
            failure="the temporary keychain's identities could not be listed",
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
        if not _identity_is_available(_output_text(listed), self.credentials.identity):
            raise RehearsalError(
                "the named signing identity is not available in the temporary "
                "keychain"
            )
        _run(
            self.runner,
            [
                "xcrun",
                "notarytool",
                "store-credentials",
                self.profile,
                # A file path, not the key contents: no secret is an argv value.
                "--key",
                str(self.notary_key),
                "--key-id",
                self.credentials.key_id,
                "--issuer",
                self.credentials.issuer_id,
                "--keychain",
                str(self.keychain),
                # Validation is an extra round trip; the submission itself is
                # the real proof and the only permitted network write.
                "--no-validate",
            ],
            failure="the notary credentials could not be stored in the temporary keychain",
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )

    def _write_certificate(self) -> None:
        _write_private(self.certificate, self.credentials.certificate)
        # The decoded App Store Connect API key: owner-only, private scratch
        # directory, and deleted in the same unconditional cleanup as the
        # keychain, the profile and the p12.
        _write_private(self.notary_key, self.credentials.notary_key)

    @property
    def restoring(self) -> bool:
        """Whether a restore is already running on this session."""
        return self._restoring

    def restore(self) -> list[str]:
        """Put back the prior keychain state and delete every credential.

        Each step is attempted independently and every failure is collected as
        fixed text: a p12 that cannot be unlinked never stops the API key from
        being deleted, and a restore command that fails keeps its flag set so a
        retry still attempts it. Returns the problems instead of raising, so the
        caller can combine them with whatever failure is already in flight.
        """
        if self._restoring:
            return []
        self._restoring = True
        try:
            problems: list[str] = []
            if self.search_changed:
                try:
                    _run(
                        self.runner,
                        [
                            "security",
                            "list-keychains",
                            "-d",
                            "user",
                            "-s",
                            *self.previous_search,
                        ],
                        failure="the prior keychain search list could not be restored",
                        timeout=KEYCHAIN_TIMEOUT_SECONDS,
                    )
                except RehearsalError as exc:
                    problems.append(str(exc))
                else:
                    # Only a successful restore clears the flag; a retry still
                    # attempts a setting that failed.
                    self.search_changed = False
            if self.default_changed:
                try:
                    _run(
                        self.runner,
                        [
                            "security",
                            "default-keychain",
                            "-d",
                            "user",
                            "-s",
                            self.previous_default,
                        ],
                        failure="the prior default keychain could not be restored",
                        timeout=KEYCHAIN_TIMEOUT_SECONDS,
                    )
                except RehearsalError as exc:
                    problems.append(str(exc))
                else:
                    self.default_changed = False
            if self.created:
                try:
                    _run(
                        self.runner,
                        ["security", "delete-keychain", str(self.keychain)],
                        failure="the temporary signing keychain could not be deleted",
                        timeout=KEYCHAIN_TIMEOUT_SECONDS,
                    )
                except RehearsalError as exc:
                    # The postcondition is what matters: a keychain that is not
                    # there is gone, even if the delete command reported an error.
                    if os.path.lexists(self.keychain):
                        problems.append(str(exc))
                    else:
                        self.created = False
                else:
                    self.created = False
            # Independent credential deletion: one failure never stops the next.
            for path, failure in (
                (self.certificate, "the exported certificate could not be deleted"),
                (self.notary_key, "the decoded notary key could not be deleted"),
            ):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    problems.append(failure)
            try:
                shutil.rmtree(self.credentials_dir)
            except FileNotFoundError:
                pass
            except OSError:
                problems.append("the credential directory could not be removed")
            return problems
        finally:
            self._restoring = False


def _temporary_signing_keychain(
    output: Path,
    credentials: _Credentials,
    runner: Callable[..., Any],
    children: _ActiveChildren,
    progress: "_Progress | None" = None,
) -> contextlib.AbstractContextManager[tuple[Path, str]]:
    """Create the ephemeral keychain and notary profile, and always undo them.

    The prior default keychain and the whole prior search list are captured
    before anything changes and restored on success, on failure and on SIGTERM
    or SIGINT. A cleanup problem is reported together with whatever failure was
    already in flight; it is never swallowed because another exception won.
    """

    @contextlib.contextmanager
    def manage() -> Iterator[tuple[Path, str]]:
        session = _SigningKeychain(output, credentials, runner)
        previous_handlers: dict[int, Any] = {}
        state: dict[str, Any] = {"interrupting": False, "deferred_signal": None}

        def interrupted(signum: int, _frame: Any) -> None:
            name = signal.Signals(signum).name
            # Never start a second restore: one may already be running on the
            # ordinary cleanup path. Defer it and let the running restore finish;
            # the run then fails after cleanup, with the key deleted.
            if state["interrupting"] or session.restoring:
                state["deferred_signal"] = name
                return
            state["interrupting"] = True
            # Never restore while the interrupted tool may still be applying a
            # change: terminate it and reap it first.
            outcome = children.stop(name)
            if outcome == "spawning":
                # The process is still being created; spawn() stops it as soon
                # as it is registered and raises the interruption itself.
                state["deferred_signal"] = name
                return
            problems = session.restore()
            suffix = (
                "; cleanup also failed: " + "; ".join(problems) if problems else ""
            )
            if outcome == "unreaped":
                suffix += "; the interrupted tool could not be reaped"
            if state["deferred_signal"] is not None:
                suffix += (
                    f"; a later signal ({state['deferred_signal']}) was deferred "
                    "until cleanup finished"
                )
            raise RehearsalError(
                f"the rehearsal was interrupted by {name}{suffix}"
            ) from None

        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                previous_handlers[signum] = signal.signal(signum, interrupted)
            except (ValueError, OSError):
                # Not the main thread, or a platform without the signal.
                pass
        try:
            session.prepare()
            yield session.keychain, session.profile
        except BaseException as exc:
            problems = session.restore()
            if problems:
                detail = (
                    str(exc)
                    if isinstance(exc, RehearsalError)
                    else "the rehearsal failed"
                )
                raise RehearsalError(
                    f"{detail}; cleanup also failed: " + "; ".join(problems)
                ) from None
            raise
        else:
            if progress is not None:
                # Only a clean body reaches here; a failure keeps its own stage.
                progress.enter("cleanup")
            problems = session.restore()
            if state["deferred_signal"] is not None:
                suffix = (
                    "; cleanup also failed: " + "; ".join(problems)
                    if problems
                    else ""
                )
                raise RehearsalError(
                    "the rehearsal was interrupted by "
                    f"{state['deferred_signal']} after cleanup finished{suffix}"
                ) from None
            if problems:
                raise RehearsalError(
                    "the rehearsal finished but cleanup failed: "
                    + "; ".join(problems)
                ) from None
        finally:
            for signum, handler in previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):
                    pass

    return manage()


def _extract_archive(archive: Path, destination: Path) -> Path:
    """Unpack exactly the app subtree into a fresh directory and return it."""
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if member.issym() or member.islnk():
                    raise RehearsalError("the archive holds a link")
            try:
                tar.extractall(destination, filter="data")
            except tarfile.FilterError:
                raise RehearsalError("the archive holds an unsafe member") from None
    except (tarfile.TarError, OSError):
        raise RehearsalError("the candidate archive could not be extracted") from None
    extracted = destination / server_host.APP_NAME
    if not extracted.is_dir():
        raise RehearsalError("the candidate archive holds no server host app")
    return extracted


def _verify_candidate_archive(
    archive: Path,
    destination: Path,
    signing: server_host.DeveloperIDSigning,
    runner: Callable[..., Any],
) -> server_host.HostOwnership:
    """Extract ``archive`` and prove the app in it under the signed policy.

    This is the check that has to run against the final extracted app: strict
    signature, the external expected requirement, team, hardened runtime, secure
    timestamp, entitlements, the stable designated requirement, the exact file
    set including the notarization ticket, staple validity and Gatekeeper. An
    inspection of the build directory alone would never exercise the archive.
    """
    extracted = _extract_archive(archive, destination)
    try:
        ownership = server_host.inspect_host_bundle(
            extracted,
            runner=runner,
            revision=server_host.SIGNED_HOST_REVISION,
            protocol=server_host.HOST_PROTOCOL,
            signing=signing,
        )
    except server_host.ServerHostError as exc:
        raise RehearsalError(f"the extracted app is not the signed host: {exc}") from None
    ticket = extracted / "Contents" / "CodeResources"
    if not ticket.is_file():
        raise RehearsalError("the extracted app carries no notarization ticket")
    _run(
        runner,
        ["xcrun", "stapler", "validate", str(extracted)],
        failure="the extracted app's stapled ticket could not be validated",
        timeout=KEYCHAIN_TIMEOUT_SECONDS,
    )
    _run(
        runner,
        ["spctl", "--assess", "--type", "execute", "--verbose=2", str(extracted)],
        failure="Gatekeeper rejected the extracted app",
        timeout=KEYCHAIN_TIMEOUT_SECONDS,
    )
    return ownership


def _write_ownership_record(snapshot: server_host.HostOwnership, path: Path) -> None:
    """Write the schema-2 record owner-only at ``path``, atomically."""
    document = json.dumps(snapshot.to_record(), indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    _replace_private(path, document.encode("utf-8"))


def _manifest_artifacts(manifest_bytes: bytes) -> list[dict[str, Any]]:
    try:
        payload: Any = json.loads(manifest_bytes)
    except ValueError:
        raise RehearsalError("the signed manifest is not JSON") from None
    if not isinstance(payload, dict):
        raise RehearsalError("the signed manifest is not an object")
    if payload.get("schema") != release_manifest.SCHEMA_VERSION:
        raise RehearsalError("the signed manifest has an unsupported schema")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise RehearsalError("the signed manifest lists no artifacts")
    if not all(isinstance(artifact, dict) for artifact in artifacts):
        raise RehearsalError("the signed manifest has a malformed artifact")
    return [artifact for artifact in artifacts]


def _check_manifest(
    manifest_bytes: bytes,
    *,
    team_id: str,
    wheel: Path,
    archive: Path,
    archive_sha256: str,
    archive_size: int,
) -> None:
    """Prove the signed manifest authenticates exactly these final bytes.

    The signature is verified by the installed ``ciao.release_manifest verify``
    gate before this runs; this reads that same file and refuses a different
    team, a different archive name, size or digest, or a manifest that does not
    describe the single candidate wheel.
    """
    artifacts = _manifest_artifacts(manifest_bytes)
    hosts = [entry for entry in artifacts if entry.get("kind") == "server-host"]
    if len(hosts) != 1:
        raise RehearsalError("the signed manifest does not name exactly one server host")
    host = hosts[0]
    if host.get("filename") != ARCHIVE_NAME or archive.name != ARCHIVE_NAME:
        raise RehearsalError("the signed manifest names a different archive")
    if host.get("host_revision") != server_host.SIGNED_HOST_REVISION:
        raise RehearsalError("the signed manifest is not the signed host revision")
    if host.get("host_protocol") != server_host.HOST_PROTOCOL:
        raise RehearsalError("the signed manifest is not the host protocol")
    expected_signing = {
        "kind": server_host.DEVELOPER_ID_SIGNING_KIND,
        "team_id": team_id,
    }
    if host.get("signing") != expected_signing:
        raise RehearsalError("the signed manifest does not authenticate the expected team")
    if host.get("sha256") != archive_sha256 or host.get("size") != archive_size:
        raise RehearsalError(
            "the signed manifest does not authenticate the final archive bytes"
        )
    wheels = [entry for entry in artifacts if entry.get("kind") == "wheel"]
    if len(wheels) != 1:
        raise RehearsalError("the signed manifest does not name exactly one wheel")
    wheel_sha256, wheel_size = _sha256_file(wheel)
    if (
        wheels[0].get("filename") != wheel.name
        or wheels[0].get("sha256") != wheel_sha256
        or wheels[0].get("size") != wheel_size
    ):
        raise RehearsalError("the signed manifest does not describe the candidate wheel")


@dataclass
class _Progress:
    """The stage a failure belongs to, and the steps that did complete.

    A stage name and its derived code are fixed text: a failed run can say where
    it stopped without ever exposing a credential, an argv or tool output.
    """

    stage: str = "prepare"
    steps: dict[str, bool] = field(default_factory=dict)

    def enter(self, stage: str) -> None:
        self.stage = stage

    def done(self, step: str) -> None:
        self.steps[step] = True

    @property
    def code(self) -> str:
        return f"{self.stage}_failed"


def _write_failure_report(
    output: Path, progress: _Progress, source_sha: str
) -> None:
    """Leave an explicitly incomplete report so a failed run is never mistaken
    for a verified one. Only the stage, its code and completed step names."""
    report = {
        "schema": 1,
        "kind": REPORT_KIND,
        "complete": False,
        "source_sha": source_sha,
        "failure": {"stage": progress.stage, "code": progress.code},
        "steps": dict(progress.steps),
    }
    summary = (
        f"server host signing rehearsal: FAILED at {progress.stage} "
        f"({progress.code})",
        f"source sha: {source_sha}",
        "steps: " + ", ".join(f"{name}=ok" for name in progress.steps),
    )
    try:
        _replace_private(
            output / VERIFICATION_NAME,
            (json.dumps(report, indent=2, sort_keys=True) + "\n").encode(),
        )
        (output / SUMMARY_NAME).write_text(
            "\n".join(summary) + "\n", encoding="utf-8"
        )
    except OSError:
        # The run already failed. A report that cannot be written is not a
        # second failure worth masking the sanitized error with.
        pass


def _prepare_manifest_signer(
    output: Path,
    runner: Callable[..., Any],
    environ: Mapping[str, str],
    home: Path,
) -> Path:
    """Install the pinned signer in its own process, before any credential.

    The install is a separate command with a scrubbed environment: no signing
    key, no password, no checkout on ``PYTHONPATH``, install scripts disabled.
    Only the later invocation receives the manifest signing secrets.
    """
    prefix = output / "signer"
    env = _clean_env(
        environ,
        home,
        {
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "npm_config_ignore_scripts": "true",
        },
    )
    _run(
        runner,
        [
            "npm",
            "install",
            "--prefix",
            str(prefix),
            "--no-save",
            "--no-audit",
            "--no-fund",
            "--ignore-scripts",
            "--loglevel",
            "error",
            MANIFEST_SIGNER,
        ],
        failure="the pinned manifest signer could not be prepared",
        timeout=SIGNER_TIMEOUT_SECONDS,
        env=env,
        cwd=output,
    )
    binary = prefix / SIGNER_BIN
    if not binary.is_file():
        raise RehearsalError("the prepared manifest signer is missing")
    return binary


def _verify_scratch_ownership(
    python: Path,
    bundle: Path,
    record: Path,
    *,
    env: Mapping[str, str],
    cwd: Path,
    runner: Callable[..., Any],
) -> None:
    """Read the record back with the prepared interpreter's installed wheel.

    Running it through ``--python`` is what proves the verifier is the packaged
    one: an editable checkout on the helper's own ``sys.path`` cannot stand in.
    """
    _run(
        runner,
        [str(python), "-c", OWNERSHIP_SCRIPT, str(bundle), str(record)],
        failure=(
            "the scratch ownership round trip could not be verified by the "
            "installed wheel"
        ),
        timeout=MANIFEST_TIMEOUT_SECONDS,
        env=env,
        cwd=cwd,
    )


def _rehearse(
    *,
    output: Path,
    python: Path,
    wheels: list[Path],
    source_sha: str,
    environ: Mapping[str, str],
    runner: Callable[..., Any],
    children: _ActiveChildren,
    platform_name: str,
) -> RehearsalResult:
    credentials = _credentials(environ)
    wheel, version = _single_wheel(wheels)
    _check_environment(
        platform_name=platform_name, source_sha=source_sha, output=output, python=python
    )

    output = output.expanduser()
    receipt = output.with_name(output.name + OWNERSHIP_RECEIPT_SUFFIX)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(output, 0o700)
    progress = _Progress()
    try:
        # The marker is written immediately after creation, and then the exact
        # created path is recorded in a sibling receipt. The workflow deletes
        # only the recorded path, so a pre-existing directory (even with a
        # marker) is never adopted. An interruption between these two writes
        # leaves no receipt, so the directory is not deleted either.
        _write_private(
            output / OWNERSHIP_MARKER, b"ciaobot server host signing rehearsal\n"
        )
        _write_private(receipt, str(output).encode("utf-8"))
        home = output / "home"
        home.mkdir(mode=0o700)
        return _run_rehearsal_stages(
            output=output,
            python=python,
            wheel=wheel,
            version=version,
            source_sha=source_sha,
            environ=environ,
            runner=runner,
            children=children,
            platform_name=platform_name,
            credentials=credentials,
            home=home,
            progress=progress,
        )
    except BaseException:
        _write_failure_report(output, progress, source_sha)
        raise


def _run_rehearsal_stages(
    *,
    output: Path,
    python: Path,
    wheel: Path,
    version: str,
    source_sha: str,
    environ: Mapping[str, str],
    runner: Callable[..., Any],
    children: _ActiveChildren,
    platform_name: str,
    credentials: _Credentials,
    home: Path,
    progress: _Progress,
) -> RehearsalResult:
    builder = _load_builder()
    signing = server_host.DeveloperIDSigning(credentials.team_id)
    manifest_env = _clean_env(environ, home)

    progress.enter("keychain")
    with _temporary_signing_keychain(
        output, credentials, runner, children, progress
    ) as (
        _keychain,
        profile,
    ):
        progress.enter("build")
        try:
            build = builder.build(
                output / "build",
                platform_name=platform_name,
                runner=runner,
                release_signing=builder.ReleaseSigning(
                    identity=credentials.identity,
                    team_id=credentials.team_id,
                    keychain_profile=profile,
                ),
            )
        except builder.BuildError as exc:
            raise RehearsalError(f"the release build failed: {exc}") from None
        except RehearsalError:
            raise
        except Exception:
            raise RehearsalError("the release build failed") from None
        # The builder exits only after an exact ``Accepted`` notary status and a
        # stapled, validated, re-extracted app; those are its contract, not an
        # inference made here.
        progress.done("sign")
        progress.done("notarize")
        progress.done("staple")
        progress.done("staple_validate")
        progress.done("build")

        archive = Path(build["paths"]["archive"])
        metadata = build["metadata"]
        if archive.name != ARCHIVE_NAME:
            raise RehearsalError("the release build produced an unexpected archive name")
        if metadata.get("host_revision") != server_host.SIGNED_HOST_REVISION:
            raise RehearsalError("the release build did not build the signed host revision")
        if set(metadata.get("per_arch_cdhashes", {})) != set(
            server_host.ARCHITECTURES
        ):
            raise RehearsalError("the release build did not sign every architecture")

        # Final extracted verification, before any manifest exists.
        progress.enter("extracted_verification")
        ownership = _verify_candidate_archive(
            archive, output / "verify-extracted", signing, runner
        )
        progress.done("extracted_verification")

        progress.enter("ownership")
        record_path = home / RECORD_RELATIVE
        _write_ownership_record(ownership, record_path)
        _verify_scratch_ownership(
            python,
            Path(ownership.bundle_path),
            record_path,
            env=manifest_env,
            cwd=output,
            runner=runner,
        )
        progress.done("ownership")

        progress.enter("manifest_build")
        manifest = output / release_manifest.MANIFEST_NAME
        _run(
            runner,
            [
                str(python),
                "-m",
                "ciao.release_manifest",
                "build",
                "--version",
                version,
                "--out",
                str(manifest),
                "--server-host",
                str(archive),
                "--server-host-team-id",
                credentials.team_id,
                str(wheel),
            ],
            failure="the candidate manifest could not be built",
            timeout=MANIFEST_TIMEOUT_SECONDS,
            env=manifest_env,
            cwd=output,
        )
        progress.done("manifest_build")

        progress.enter("manifest_signature")
        signer = _prepare_manifest_signer(output, runner, environ, home)
        progress.done("manifest_signer")
        _run(
            runner,
            [str(signer), "signer", "sign", str(manifest)],
            failure="the candidate manifest could not be signed",
            timeout=SIGNER_TIMEOUT_SECONDS,
            env=_clean_env(
                environ,
                home,
                {
                    TAURI_KEY_ENV: credentials.tauri_key,
                    TAURI_PASSWORD_ENV: credentials.tauri_password,
                },
            ),
            cwd=output,
        )
        signature = manifest.with_name(manifest.name + ".sig")
        if not signature.is_file():
            raise RehearsalError("the candidate manifest signature is missing")
        progress.done("manifest_signature")

        progress.enter("manifest_verification")
        _run(
            runner,
            [
                str(python),
                "-m",
                "ciao.release_manifest",
                "verify",
                str(manifest),
                str(signature),
            ],
            failure="the installed manifest verifier refused the candidate manifest",
            timeout=MANIFEST_TIMEOUT_SECONDS,
            env=manifest_env,
            cwd=output,
        )
        progress.done("manifest_verification")

        # The digest is recomputed only after the manifest is signed, so the entry
        # is compared against the bytes as they stand at the end.
        progress.enter("archive_digest")
        archive_sha256, archive_size = _sha256_file(archive)
        if (
            metadata.get("archive_sha256") != archive_sha256
            or metadata.get("archive_size") != archive_size
        ):
            raise RehearsalError("the final archive changed after the build")
        progress.done("archive")

        progress.enter("manifest_authentication")
        _check_manifest(
            manifest.read_bytes(),
            team_id=credentials.team_id,
            wheel=wheel,
            archive=archive,
            archive_sha256=archive_sha256,
            archive_size=archive_size,
        )
        progress.done("manifest_authentication")

        # Re-extract exactly the authenticated bytes, independently of the first
        # extraction and of the build directory.
        progress.enter("final_extraction")
        final = _verify_candidate_archive(
            archive, output / "verify-final", signing, runner
        )
        if final.bundle_files != ownership.bundle_files:
            raise RehearsalError("the re-extracted app differs from the verified one")
        progress.done("final_extraction")

    progress.enter("report")
    report: dict[str, Any] = {
        "schema": 1,
        "kind": REPORT_KIND,
        "complete": True,
        "source_sha": source_sha,
        "policy": {
            "host_revision": server_host.SIGNED_HOST_REVISION,
            "host_protocol": server_host.HOST_PROTOCOL,
            "signing_kind": server_host.DEVELOPER_ID_SIGNING_KIND,
        },
        "notarization": {"status": "Accepted"},
        "archive": {
            "filename": ARCHIVE_NAME,
            "size": archive_size,
            "sha256": archive_sha256,
        },
        "steps": dict(progress.steps),
    }
    report_path = output / VERIFICATION_NAME
    _replace_private(
        report_path, (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    )

    summary = (
        "server host signing rehearsal: verified",
        f"source sha: {source_sha}",
        "policy: revision "
        f"{server_host.SIGNED_HOST_REVISION} / protocol {server_host.HOST_PROTOCOL} "
        f"({server_host.DEVELOPER_ID_SIGNING_KIND})",
        f"archive: {ARCHIVE_NAME} ({archive_size} bytes, sha256 {archive_sha256})",
        "notarization: Accepted",
        "steps: " + ", ".join(f"{name}=ok" for name in progress.steps),
    )
    (output / SUMMARY_NAME).write_text("\n".join(summary) + "\n", encoding="utf-8")
    return RehearsalResult(
        source_sha=source_sha,
        archive_name=ARCHIVE_NAME,
        archive_size=archive_size,
        archive_sha256=archive_sha256,
        report_path=report_path,
        summary=summary,
    )


def rehearse(
    *,
    output: Path,
    python: Path,
    wheels: list[Path],
    source_sha: str,
    environ: Mapping[str, str],
    runner: Callable[..., Any] | None = None,
    children: _ActiveChildren | None = None,
    platform_name: str | None = None,
) -> RehearsalResult:
    """Run the whole rehearsal with injected tools; fail closed on any stage.

    ``platform_name``, ``runner``, ``children`` and ``environ`` are injection
    points for tests. They are not shipped bypass flags: the platform is checked
    against the same value the build uses, and there is no code path that builds
    without ``release_signing``. In production the runner is the tracked
    ``_ProcessRunner``, so a signal can terminate and reap the active child
    before any keychain state is restored.
    """
    effective_platform = sys.platform if platform_name is None else platform_name
    if runner is None:
        tracker = _ActiveChildren()
        effective_runner: Callable[..., Any] = _ProcessRunner(tracker)
    else:
        effective_runner = runner
        tracker = children if children is not None else _ActiveChildren()
    try:
        return _rehearse(
            output=output,
            python=python,
            wheels=wheels,
            source_sha=source_sha,
            environ=environ,
            runner=effective_runner,
            children=tracker,
            platform_name=effective_platform,
        )
    except RehearsalError:
        raise
    except Exception:
        # Anything unexpected (including a tool fake's own assertion, whose text
        # may repeat argv) becomes fixed text: no secret can reach a log.
        raise RehearsalError("the signing rehearsal failed") from None


def main(
    argv: list[str] | None = None,
    *,
    runner: Callable[..., Any] | None = None,
    children: _ActiveChildren | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New absolute scratch directory for the rehearsal (must not exist)",
    )
    parser.add_argument(
        "--python",
        type=Path,
        required=True,
        help="Absolute path to the prepared clean Python 3.12+ interpreter",
    )
    parser.add_argument(
        "--source-sha",
        required=True,
        help="The exact commit this run checks out; recorded in the report",
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        nargs="+",
        required=True,
        help="Exactly one candidate engine wheel",
    )
    args = parser.parse_args(argv)

    try:
        result = rehearse(
            output=args.output,
            python=args.python,
            wheels=list(args.wheel),
            source_sha=args.source_sha,
            environ=os.environ if environ is None else environ,
            runner=runner,
            children=children,
        )
    except RehearsalError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for line in result.summary:
        print(line)
    print(f"report: {result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
