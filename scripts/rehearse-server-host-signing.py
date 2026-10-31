"""Rehearse an explicitly signed, notarized server host release (#2331).

This is a CI orchestration tool, not an installer and not a publisher. It runs
the opt-in Developer ID build of ``scripts/build-server-host.py`` inside a
temporary signing keychain, checks the resulting archive by extracting it
again, authenticates the exact bytes with a signed candidate manifest, and
writes a sanitized runner-local report. It never uploads a release asset, never
runs ``actions/upload-artifact``, and never distributes anything: the Apple
notary submission the release builder performs is the only network write.

Every input is explicit. The signing identity, the expected team, the exported
certificate, its import password and the notary credentials are named CI
inputs, read from the environment by name only. Missing inputs are reported by
name; values never reach an error, a log line, a report or a step summary. A
malformed or missing identity is never an excuse to run the default ad-hoc
build: this tool has exactly one build path and it is the signed one.

The temporary keychain and notary profile live under the fresh ``--output``
directory, are made the user default and the only signing-identity search
target for the duration of the build, and are restored and deleted on every
path, including build, notarization, manifest and verification failures.

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
import stat
import subprocess
import sys
import tarfile
from types import ModuleType
from typing import Any, Callable, Iterator, Mapping

from ciao import release_manifest, server_host

REPO = Path(__file__).resolve().parents[1]
BUILDER_SCRIPT = REPO / "scripts" / "build-server-host.py"

#: The pinned standalone minisign signer, unchanged from the production release.
MANIFEST_SIGNER = "@tauri-apps/cli@2.11.4"

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
NOTARY_APPLE_ID_ENV = "SERVER_HOST_NOTARY_APPLE_ID"
NOTARY_APP_PASSWORD_ENV = "SERVER_HOST_NOTARY_APP_PASSWORD"
TAURI_KEY_ENV = "TAURI_SIGNING_PRIVATE_KEY"
TAURI_PASSWORD_ENV = "TAURI_SIGNING_PRIVATE_KEY_PASSWORD"

REQUIRED_ENV = (
    IDENTITY_ENV,
    TEAM_ENV,
    CERTIFICATE_ENV,
    CERTIFICATE_PASSWORD_ENV,
    NOTARY_APPLE_ID_ENV,
    NOTARY_APP_PASSWORD_ENV,
    TAURI_KEY_ENV,
    TAURI_PASSWORD_ENV,
)

TEAM_ID_PATTERN = re.compile(r"[A-Z0-9]{10}")
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
    apple_id: str = field(repr=False)
    app_password: str = field(repr=False)
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
) -> Any:
    """Run one bounded tool; every failure becomes a sanitized error.

    The argv, the captured output and the original exception stay out of the
    message and out of the exception chain: credential-bearing values are in the
    environment and, for the keychain and notary import, on the argv itself.
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

    apple_id = values[NOTARY_APPLE_ID_ENV].strip()
    if "@" not in apple_id or any(char in apple_id for char in "\r\n\t"):
        raise RehearsalError(f"{NOTARY_APPLE_ID_ENV} is not an Apple ID address")

    return _Credentials(
        identity=identity,
        team_id=team_id,
        certificate=certificate,
        certificate_password=values[CERTIFICATE_PASSWORD_ENV],
        apple_id=apple_id,
        app_password=values[NOTARY_APP_PASSWORD_ENV],
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
    return wheel, match.group(1)


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


def _temporary_signing_keychain(
    output: Path,
    credentials: _Credentials,
    runner: Callable[..., Any],
) -> contextlib.AbstractContextManager[tuple[Path, str]]:
    """Create the ephemeral keychain and notary profile, and always undo them.

    The prior default keychain and the whole prior search list are captured
    before anything changes and restored in ``finally``. The keychain, the
    exported certificate and the notary profile all live under the fresh
    ``output`` directory; nothing outside it is deleted.
    """

    @contextlib.contextmanager
    def manage() -> Iterator[tuple[Path, str]]:
        credentials_dir = output / "credentials"
        credentials_dir.mkdir(mode=0o700, exist_ok=False)
        keychain = credentials_dir / "rehearsal.keychain-db"
        certificate_path = credentials_dir / "certificate.p12"
        keychain_password = secrets.token_urlsafe(32)
        profile = f"{PROFILE_PREFIX}{secrets.token_hex(8)}"

        try:
            previous_default = _default_keychain(runner)
            previous_search = _search_list(runner)
        except RehearsalError:
            shutil.rmtree(credentials_dir, ignore_errors=True)
            raise

        created = False
        default_changed = False
        search_changed = False
        try:
            _write_private(certificate_path, credentials.certificate)
            _run(
                runner,
                ["security", "create-keychain", "-p", keychain_password, str(keychain)],
                failure="the temporary signing keychain could not be created",
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
            created = True
            _run(
                runner,
                [
                    "security",
                    "set-keychain-settings",
                    "-lut",
                    "600",
                    str(keychain),
                ],
                failure="the temporary signing keychain settings could not be applied",
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
            _run(
                runner,
                ["security", "unlock-keychain", "-p", keychain_password, str(keychain)],
                failure="the temporary signing keychain could not be unlocked",
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
            _run(
                runner,
                ["security", "default-keychain", "-d", "user", "-s", str(keychain)],
                failure="the temporary signing keychain could not be made the default",
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
            default_changed = True
            _run(
                runner,
                ["security", "list-keychains", "-d", "user", "-s", str(keychain)],
                failure="the temporary signing keychain could not be searched",
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
            search_changed = True
            _run(
                runner,
                [
                    "security",
                    "import",
                    str(certificate_path),
                    "-k",
                    str(keychain),
                    "-P",
                    credentials.certificate_password,
                    # Only the signer may use the private key, and only without a
                    # graphical prompt. No other tool is trusted with it.
                    "-T",
                    "/usr/bin/codesign",
                ],
                failure="the signing certificate could not be imported",
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
            certificate_path.unlink(missing_ok=True)
            _run(
                runner,
                [
                    "security",
                    "set-key-partition-list",
                    "-S",
                    "apple-tool:,apple:,codesign:",
                    "-s",
                    "-k",
                    keychain_password,
                    str(keychain),
                ],
                failure="the signing key's access could not be bounded",
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
            listed = _run(
                runner,
                ["security", "find-identity", "-v", "-p", "codesigning", str(keychain)],
                failure="the temporary keychain's identities could not be listed",
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
            if not _identity_is_available(_output_text(listed), credentials.identity):
                raise RehearsalError(
                    "the named signing identity is not available in the temporary "
                    "keychain"
                )
            _run(
                runner,
                [
                    "xcrun",
                    "notarytool",
                    "store-credentials",
                    profile,
                    "--apple-id",
                    credentials.apple_id,
                    "--password",
                    credentials.app_password,
                    "--team-id",
                    credentials.team_id,
                    "--keychain",
                    str(keychain),
                    # Validation is an extra round trip; the submission itself is
                    # the real proof and the only permitted network write.
                    "--no-validate",
                ],
                failure="the notary credentials could not be stored in the temporary keychain",
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
            yield keychain, profile
        finally:
            problems: list[str] = []
            if search_changed:
                try:
                    _run(
                        runner,
                        [
                            "security",
                            "list-keychains",
                            "-d",
                            "user",
                            "-s",
                            *previous_search,
                        ],
                        failure="the prior keychain search list could not be restored",
                        timeout=KEYCHAIN_TIMEOUT_SECONDS,
                    )
                except RehearsalError as exc:
                    problems.append(str(exc))
            if default_changed:
                try:
                    _run(
                        runner,
                        [
                            "security",
                            "default-keychain",
                            "-d",
                            "user",
                            "-s",
                            previous_default,
                        ],
                        failure="the prior default keychain could not be restored",
                        timeout=KEYCHAIN_TIMEOUT_SECONDS,
                    )
                except RehearsalError as exc:
                    problems.append(str(exc))
            if created:
                try:
                    _run(
                        runner,
                        ["security", "delete-keychain", str(keychain)],
                        failure="the temporary signing keychain could not be deleted",
                        timeout=KEYCHAIN_TIMEOUT_SECONDS,
                    )
                except RehearsalError as exc:
                    problems.append(str(exc))
            shutil.rmtree(credentials_dir, ignore_errors=True)
            if problems and sys.exc_info()[0] is None:
                raise RehearsalError("; ".join(problems))

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
    temporary = path.with_name(f".{path.name}.rehearsal")
    try:
        _write_private(temporary, document.encode("utf-8"))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def _rehearse(
    *,
    output: Path,
    python: Path,
    wheels: list[Path],
    source_sha: str,
    environ: Mapping[str, str],
    runner: Callable[..., Any],
    platform_name: str,
) -> RehearsalResult:
    credentials = _credentials(environ)
    wheel, version = _single_wheel(wheels)
    _check_environment(
        platform_name=platform_name, source_sha=source_sha, output=output, python=python
    )

    output = output.expanduser()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(output, 0o700)
    home = output / "home"
    home.mkdir(mode=0o700)

    builder = _load_builder()
    signing = server_host.DeveloperIDSigning(credentials.team_id)

    steps: dict[str, bool] = {}
    with _temporary_signing_keychain(output, credentials, runner) as (
        _keychain,
        profile,
    ):
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
        steps["sign"] = True
        steps["notarize"] = True
        steps["staple"] = True
        steps["staple_validate"] = True
        steps["build"] = True

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
        ownership = _verify_candidate_archive(
            archive, output / "verify-extracted", signing, runner
        )
        steps["extracted_verification"] = True

        record_path = home / RECORD_RELATIVE
        _write_ownership_record(ownership, record_path)
        verified = server_host.verify_owned_host(
            Path(ownership.bundle_path),
            ownership_path=record_path,
            runner=runner,
        )
        if verified != ownership:
            raise RehearsalError("the scratch ownership round trip changed the snapshot")
        steps["ownership"] = True

        manifest = output / release_manifest.MANIFEST_NAME
        manifest_env = _clean_env(environ, home)
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
        steps["manifest_build"] = True
        _run(
            runner,
            [
                "npx",
                "--yes",
                MANIFEST_SIGNER,
                "signer",
                "sign",
                str(manifest),
            ],
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
        steps["manifest_signature"] = True
        signature = manifest.with_name(manifest.name + ".sig")
        if not signature.is_file():
            raise RehearsalError("the candidate manifest signature is missing")
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
        steps["manifest_verification"] = True

        # The digest is recomputed only after the manifest is signed, so the entry
        # is compared against the bytes as they stand at the end.
        archive_sha256, archive_size = _sha256_file(archive)
        if (
            metadata.get("archive_sha256") != archive_sha256
            or metadata.get("archive_size") != archive_size
        ):
            raise RehearsalError("the final archive changed after the build")
        _check_manifest(
            manifest.read_bytes(),
            team_id=credentials.team_id,
            wheel=wheel,
            archive=archive,
            archive_sha256=archive_sha256,
            archive_size=archive_size,
        )
        steps["archive"] = True

        # Re-extract exactly the authenticated bytes, independently of the first
        # extraction and of the build directory.
        final = _verify_candidate_archive(
            archive, output / "verify-final", signing, runner
        )
        if final.bundle_files != ownership.bundle_files:
            raise RehearsalError("the re-extracted app differs from the verified one")
        steps["reverified_archive"] = True

    report: dict[str, Any] = {
        "schema": 1,
        "kind": "server-host-signing-rehearsal",
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
        "steps": steps,
    }
    report_path = output / VERIFICATION_NAME
    _write_private(
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
        "steps: " + ", ".join(f"{name}=ok" for name in steps),
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
    platform_name: str | None = None,
) -> RehearsalResult:
    """Run the whole rehearsal with injected tools; fail closed on any stage.

    ``platform_name``, ``runner`` and ``environ`` are injection points for
    tests. They are not shipped bypass flags: the platform is checked against
    the same value the build uses, and there is no code path that builds
    without ``release_signing``.
    """
    effective_platform = sys.platform if platform_name is None else platform_name
    effective_runner = subprocess.run if runner is None else runner
    try:
        return _rehearse(
            output=output,
            python=python,
            wheels=wheels,
            source_sha=source_sha,
            environ=environ,
            runner=effective_runner,
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
