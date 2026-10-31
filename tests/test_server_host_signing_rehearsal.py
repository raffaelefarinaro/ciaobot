"""Checks for the signed server host signing rehearsal (#2331).

The rehearsal is CI orchestration: a temporary signing keychain, the explicit
Developer ID release build, a signed candidate manifest and an independent
verification of the final extracted app. None of that can run here, so every
native tool is a strict fake that recognizes complete argv and fails on
anything else. The build, notarization, keychain and upload paths in this file
never touch a real certificate, a real keychain, the network or a release.

The checks here are the ones a static YAML read cannot make: the argument and
side-effect order, the fail-closed behaviour of each stage, unconditional and
cancellation-aware keychain cleanup, credentials never reaching an output, an
argv-free credential path, a signer prepared without signing credentials, and a
verification that runs against the extracted archive rather than the build
directory.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
REHEARSAL_SCRIPT = ROOT / "scripts" / "rehearse-server-host-signing.py"

TEAM_ID = "ABCDE12345"
IDENTITY = "Developer ID Application: Ciaobot Rehearsal (ABCDE12345)"
VERSION = "1.7.0"
WHEEL_NAME = f"ciaobot-{VERSION}-py3-none-any.whl"
SOURCE_SHA = "485617bba4b5f0200c41a9618f45385782ba7214"

PRIOR_DEFAULT = "/Users/runner/Library/Keychains/login.keychain-db"
PRIOR_SEARCH = [
    "/Users/runner/Library/Keychains/login.keychain-db",
    "/Library/Keychains/System.keychain",
]

P12_BYTES = b"fake-pkcs12-material-not-a-certificate"
P12_PASSWORD = "p12-import-password-4F7A"
NOTARY_KEY_PEM = (
    b"-----BEGIN PRIVATE KEY-----\n"
    b"ZmFrZS1hcHAtc3RvcmUtY29ubmVjdC1rZXktbWF0ZXJpYWw=\n"
    b"-----END PRIVATE KEY-----\n"
)
NOTARY_KEY_ID = "ABCD123XYZ"
NOTARY_ISSUER_ID = "12345678-1234-1234-1234-1234567890AB"
TAURI_KEY = "tauri-private-key-7B3E"
TAURI_PASSWORD = "tauri-key-password-1D6F"
SENTINEL_KEY_MATERIAL = "SENTINEL-API-KEY-MATERIAL-6E1F"
SENTINEL_KEY_PEM = (
    b"-----BEGIN PRIVATE KEY-----\n"
    + base64.b64encode(SENTINEL_KEY_MATERIAL.encode())
    + b"\n-----END PRIVATE KEY-----\n"
)
SENTINELS = {
    "SERVER_HOST_SIGNING_IDENTITY": "SENTINEL-IDENTITY-4F7A",
    "SERVER_HOST_CERTIFICATE_PASSWORD": "SENTINEL-CERT-PASSWORD-9C2D",
    # Key id and issuer are identifiers with a required shape, not secrets; the
    # leak checks still require them not to be echoed anywhere.
    "SERVER_HOST_NOTARY_KEY_ID": "SENTKEY123",
    "SERVER_HOST_NOTARY_ISSUER_ID": "9A8B7C6D-1234-4ABC-9DEF-0123456789AB",
    "SERVER_HOST_NOTARY_KEY_P8_BASE64": base64.b64encode(SENTINEL_KEY_PEM).decode(),
    "TAURI_SIGNING_PRIVATE_KEY": "SENTINEL-TAURI-KEY-5A1C",
    "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": "SENTINEL-TAURI-PASSWORD-2E8B",
}
SENTINEL_CERTIFICATE_TEXT = base64.b64encode(b"SENTINEL-CERTIFICATE-8A44").decode()

# Native-shaped fixture data. The bundle identifier, protocol and minimum macOS
# version are the runtime contract's; the signature details are written out
# literally so a shared change fails these tests instead of moving the fake.
BUNDLE_ID = "local.ciaobot.server"
APP_NAME = "Ciaobot Server.app"
BUNDLE_DISPLAY = (
    f"Identifier={BUNDLE_ID}\n"
    "Format=app bundle with Mach-O universal (arm64 x86_64)\n"
    "Signature=Developer ID Application\n"
)
CODE_DIRECTORY = (
    "v=20500 size=400 flags=0x10000(runtime) hashes=4+3 location=embedded"
)
ENTITLEMENTS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0">\n<dict>\n'
    "\t<key>com.apple.security.automation.apple-events</key>\n\t<true/>\n"
    "</dict>\n</plist>\n"
)
PARTITION_LIST = "apple-tool:,apple:,codesign:"
ENTITLEMENTS_PATH = ROOT / "native" / "server-host" / "ServerHost.entitlements.plist"


def _app_of(path: Path) -> Path:
    """The app bundle a path lives in; fails if it is not inside one."""
    for candidate in (path, *path.parents):
        if candidate.name.endswith(".app"):
            return candidate
    raise AssertionError(f"not inside an app bundle: {path}")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve annotations through sys.modules[cls.__module__].
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


REHEARSAL = _load("rehearse_server_host_signing", REHEARSAL_SCRIPT)
SERVER_HOST = REHEARSAL.server_host
# Captured before any test patches it: the fake plays the installed wheel's
# verifier, while a test can still prove the helper never calls it in-process.
REAL_VERIFY_OWNED_HOST = SERVER_HOST.verify_owned_host


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the in-process bundle verifier run as it would on the runner."""
    if sys.platform == "win32":
        pytest.skip("mocking darwin needs a POSIX uid")
    monkeypatch.setattr(sys, "platform", "darwin")


def _environment(**overrides: str | None) -> dict[str, str]:
    values: dict[str, str | None] = {
        "SERVER_HOST_SIGNING_IDENTITY": IDENTITY,
        "SERVER_HOST_TEAM_ID": TEAM_ID,
        "SERVER_HOST_CERTIFICATE_P12_BASE64": base64.b64encode(P12_BYTES).decode(),
        "SERVER_HOST_CERTIFICATE_PASSWORD": P12_PASSWORD,
        "SERVER_HOST_NOTARY_KEY_P8_BASE64": base64.b64encode(NOTARY_KEY_PEM).decode(),
        "SERVER_HOST_NOTARY_KEY_ID": NOTARY_KEY_ID,
        "SERVER_HOST_NOTARY_ISSUER_ID": NOTARY_ISSUER_ID,
        "TAURI_SIGNING_PRIVATE_KEY": TAURI_KEY,
        "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": TAURI_PASSWORD,
        "PATH": "/usr/bin:/bin",
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    """A scratch output path, a candidate wheel and a prepared interpreter."""
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    wheel = dist / WHEEL_NAME
    wheel.write_bytes(b"candidate-wheel-bytes")
    interpreter = tmp_path / "engine-check" / "bin" / "python"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)
    return wheel, interpreter


def _digest(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


class FakeChild:
    """One in-flight tool process, as the rehearsal's tracker sees it."""

    def __init__(self, events: list[tuple[str, str]], family: str, *, ignore_terminate: bool = False) -> None:
        self.events = events
        self.family = family
        self.ignore_terminate = ignore_terminate
        self.terminated = False
        self.killed = False
        self.waits = 0

    def terminate(self) -> None:
        self.terminated = True
        self.events.append(("terminate", self.family))

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.ignore_terminate and self.waits == 1:
            raise subprocess.TimeoutExpired(self.family, timeout or 0)
        self.events.append(("reaped", self.family))
        return 0

    def kill(self) -> None:
        self.killed = True
        self.events.append(("kill", self.family))


class FakeToolchain:
    """A strict native toolchain: complete argv, realistic output, no fallbacks.

    Every invocation is recorded. An argv that is not exactly one of the shapes
    the rehearsal is allowed to run raises, so a changed command fails here
    instead of silently doing something else. The ``fault`` mapping applies only
    to the matching artifact stage, which is how a test can prove the helper
    verifies the extracted archive rather than the build directory.
    """

    def __init__(
        self,
        *,
        team_id: str = TEAM_ID,
        identity: str = IDENTITY,
        notary_key: bytes = NOTARY_KEY_PEM,
        key_id: str = NOTARY_KEY_ID,
        issuer_id: str = NOTARY_ISSUER_ID,
        tauri_key: str = TAURI_KEY,
        tauri_password: str = TAURI_PASSWORD,
        fault: dict[str, Any] | None = None,
        notary_status: Any = "Accepted",
        notary_stdout: str | None = None,
        notary_rc: int = 0,
        create_rc: int = 0,
        settings_rc: int = 0,
        unlock_rc: int = 0,
        import_rc: int = 0,
        partition_rc: int = 0,
        default_set_rc: int = 0,
        search_set_rc: int = 0,
        store_rc: int = 0,
        delete_rc: int = 0,
        restore_search_rc: int = 0,
        restore_default_rc: int = 0,
        identity_listed: bool = True,
        sign_rc: int = 0,
        ticket: bytes | None = b"stapled-ticket-bytes",
        verify_rc: int = 0,
        npm_rc: int = 0,
        signer_rc: int = 0,
        manifest_verify_rc: int = 0,
        ownership_rc: int = 0,
        manifest_team: str | None = None,
        manifest_sha256: str | None = None,
        manifest_size: int | None = None,
        drop_host_entry: bool = False,
        mutate_archive_after_sign: bool = False,
        on_command: Any = None,
        children: Any = None,
        child_ignore_terminate: bool = False,
    ) -> None:
        self.team_id = team_id
        self.identity = identity
        self.notary_key = notary_key
        self.key_id = key_id
        self.issuer_id = issuer_id
        self.tauri_key = tauri_key
        self.tauri_password = tauri_password
        self.fault = fault or {}
        self.notary_status = notary_status
        self.notary_stdout = notary_stdout
        self.notary_rc = notary_rc
        self.create_rc = create_rc
        self.settings_rc = settings_rc
        self.unlock_rc = unlock_rc
        self.import_rc = import_rc
        self.partition_rc = partition_rc
        self.default_set_rc = default_set_rc
        self.search_set_rc = search_set_rc
        self.store_rc = store_rc
        self.delete_rc = delete_rc
        self.restore_search_rc = restore_search_rc
        self.restore_default_rc = restore_default_rc
        self.identity_listed = identity_listed
        self.sign_rc = sign_rc
        self.ticket = ticket
        self.verify_rc = verify_rc
        self.npm_rc = npm_rc
        self.signer_rc = signer_rc
        self.manifest_verify_rc = manifest_verify_rc
        self.ownership_rc = ownership_rc
        self.manifest_team = manifest_team
        self.manifest_sha256 = manifest_sha256
        self.manifest_size = manifest_size
        self.drop_host_entry = drop_host_entry
        self.mutate_archive_after_sign = mutate_archive_after_sign
        self.on_command = on_command
        self.children = (
            children if children is not None else REHEARSAL._ActiveChildren()
        )
        self.child_ignore_terminate = child_ignore_terminate
        self.events: list[tuple[str, str]] = []
        self.built_app: Path | None = None
        self.zip_path: Path | None = None
        self.scratch: Path | None = None
        self.manifest_path: Path | None = None
        self.commands: list[list[str]] = []
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.profile: str | None = None
        self.keychain: str | None = None
        self.notary_key_path: Path | None = None
        self.keychain_password: str | None = None
        self.archive: Path | None = None
        self.signer_prefix: Path | None = None
        self._signed = False

    # -- plumbing ----------------------------------------------------------

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(cmd))
        self.calls.append((list(cmd), dict(kwargs)))
        family = self._family(cmd)
        child = FakeChild(
            self.events, family, ignore_terminate=self.child_ignore_terminate
        )
        self.children.register(child)
        self.events.append(("start", family))
        try:
            if self.on_command is not None:
                self.on_command(list(cmd), dict(kwargs))
            return self._dispatch(cmd, kwargs)
        finally:
            self.children.clear()

    def _family(self, cmd: list[str]) -> str:
        tool = Path(cmd[0]).name
        if tool == "security":
            return f"security:{cmd[1]}" + (":set" if "-s" in cmd else "")
        if tool == "xcrun":
            return f"xcrun:{cmd[1]}:{cmd[2]}"
        if tool == "codesign":
            return "codesign:" + (cmd[1] if cmd[1] != "--verify" else "verify")
        if tool == "lipo":
            return f"lipo:{cmd[1]}"
        if tool in ("ditto", "spctl", "npm", "tauri"):
            return tool
        if cmd[1] == "-c":
            return "ownership"
        return "manifest:" + cmd[3]

    def _dispatch(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        tool = Path(cmd[0]).name
        if tool == "security":
            return self._security(cmd, kwargs)
        if tool == "xcrun":
            return self._xcrun(cmd, kwargs)
        if tool == "codesign":
            return self._codesign(cmd, kwargs)
        if tool == "lipo":
            return self._lipo(cmd, kwargs)
        if tool == "ditto":
            assert len(cmd) == 6, cmd
            assert Path(cmd[4]) == self.built_app, cmd
            assert Path(cmd[5]) == self.built_app.parent / ".host-notarize.zip", cmd
            self.zip_path = Path(cmd[5])
            Path(cmd[5]).write_bytes(b"temporary-zip")
            return self._done(cmd, kwargs)
        if tool == "spctl":
            self._expect(
                cmd,
                ["spctl", "--assess", "--type", "execute", "--verbose=2"],
                length=6,
            )
            assert Path(cmd[-1]).name == APP_NAME, cmd
            return self._done(
                cmd, kwargs, returncode=self._stage_rc("gatekeeper", cmd[-1])
            )
        if tool == "npm":
            return self._npm_install(cmd, kwargs)
        if tool == "tauri":
            return self._signer(cmd, kwargs)
        if cmd[1:2] in (["-m"], ["-c"]):
            return self._python(cmd, kwargs)
        raise AssertionError(f"unexpected rehearsal argv: {cmd}")

    def _expect(
        self, cmd: list[str], prefix: list[str], *, length: int | None = None
    ) -> None:
        """Assert ``cmd`` starts with ``prefix`` and has the expected length."""
        assert cmd[: len(prefix)] == prefix, f"unexpected argv: {cmd}"
        if length is not None:
            assert len(cmd) == length, f"unexpected argv length: {cmd}"

    def _done(
        self,
        cmd: list[str],
        kwargs: dict[str, Any],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        if returncode != 0 and kwargs.get("check"):
            raise subprocess.CalledProcessError(
                returncode, cmd, output=stdout, stderr=stderr
            )
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    def _stage(self, path: str | Path) -> str:
        text = str(path)
        if "verify-extracted" in text:
            return "extracted"
        if "verify-final" in text:
            return "final"
        if ".host-verify" in text:
            return "build-extract"
        return "built"

    def _fault(self, path: str | Path) -> dict[str, Any]:
        if self.fault.get("stage", "extracted") != self._stage(path):
            return {}
        return self.fault

    def _stage_rc(self, name: str, path: str | Path) -> int:
        return int(self._fault(path).get(name, 0))

    # -- security ----------------------------------------------------------

    def _security(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        action = cmd[1]
        if action == "list-keychains" and "-s" not in cmd:
            self._expect(cmd, ["security", "list-keychains", "-d", "user"], length=4)
            listed = "".join(f'    "{path}"\n' for path in PRIOR_SEARCH)
            return self._done(cmd, kwargs, stdout=listed)
        if action == "default-keychain" and "-s" not in cmd:
            self._expect(cmd, ["security", "default-keychain", "-d", "user"], length=4)
            return self._done(cmd, kwargs, stdout=f'    "{PRIOR_DEFAULT}"\n')
        if action == "list-keychains":
            # The only search-list mutation allowed: the temporary keychain, and
            # the whole prior list when it is put back.
            self._expect(
                cmd, ["security", "list-keychains", "-d", "user", "-s"]
            )
            targets = cmd[5:]
            assert len(cmd) == 5 + len(targets), cmd
            assert targets in ([self.keychain], PRIOR_SEARCH), cmd
            if targets == PRIOR_SEARCH:
                self.events.append(("restore", "security:list-keychains"))
                return self._done(cmd, kwargs, returncode=self.restore_search_rc)
            return self._done(cmd, kwargs, returncode=self.search_set_rc)
        if action == "default-keychain":
            self._expect(
                cmd, ["security", "default-keychain", "-d", "user", "-s"], length=6
            )
            assert cmd[-1] in (self.keychain, PRIOR_DEFAULT), cmd
            restoring = cmd[-1] == PRIOR_DEFAULT
            if restoring:
                self.events.append(("restore", "security:default-keychain"))
            rc = self.restore_default_rc if restoring else self.default_set_rc
            return self._done(cmd, kwargs, returncode=rc)
        if action == "create-keychain":
            # The keychain password is never on argv: create-keychain reads it
            # twice from stdin.
            self._expect(cmd, ["security", "create-keychain"], length=3)
            assert "-p" not in cmd, f"the keychain password reached argv: {cmd}"
            self.keychain = cmd[-1]
            assert Path(cmd[-1]).name == "rehearsal.keychain-db"
            assert Path(cmd[-1]).parent.name == "credentials"
            self.scratch = Path(cmd[-1]).parent.parent
            password = (kwargs.get("input") or "").split("\n")
            assert len(password) == 3 and password[0] == password[1] and password[0]
            self.keychain_password = password[0]
            if self.create_rc:
                return self._done(cmd, kwargs, returncode=self.create_rc)
            Path(cmd[-1]).write_bytes(b"fake-keychain")
            return self._done(cmd, kwargs)
        if action == "set-keychain-settings":
            self._expect(
                cmd, ["security", "set-keychain-settings", "-lut", "600"], length=5
            )
            return self._done(cmd, kwargs, returncode=self.settings_rc)
        if action == "unlock-keychain":
            # The keychain password is read from stdin, never passed with -p.
            self._expect(cmd, ["security", "unlock-keychain"], length=3)
            assert "-p" not in cmd, f"the keychain password reached argv: {cmd}"
            assert (kwargs.get("input") or "").split("\n")[0] == self.keychain_password
            return self._done(cmd, kwargs, returncode=self.unlock_rc)
        if action == "import":
            assert len(cmd) == 9, cmd
            assert Path(cmd[2]) == Path(self.keychain).parent / "certificate.p12", cmd
            assert cmd[3:5] == ["-k", str(self.keychain)], cmd
            assert cmd[5] == "-P" and cmd[6], cmd
            assert cmd[7:] == ["-T", "/usr/bin/codesign"], cmd
            return self._done(cmd, kwargs, returncode=self.import_rc)
        if action == "set-key-partition-list":
            # The password is not on argv (-k is deprecated and omitted); the
            # keychain is already unlocked and its password is fed on stdin.
            self._expect(
                cmd,
                [
                    "security",
                    "set-key-partition-list",
                    "-S",
                    PARTITION_LIST,
                    "-s",
                ],
                length=6,
            )
            assert "-k" not in cmd, f"the keychain password reached argv: {cmd}"
            assert (kwargs.get("input") or "").split("\n")[0] == self.keychain_password
            return self._done(cmd, kwargs, returncode=self.partition_rc)
        if action == "find-identity":
            assert cmd[:5] == [
                "security",
                "find-identity",
                "-v",
                "-p",
                "codesigning",
            ], cmd
            assert cmd[5] == self.keychain, cmd
            if not self.identity_listed:
                return self._done(cmd, kwargs, stdout="     0 valid identities found\n")
            return self._done(
                cmd,
                kwargs,
                stdout=(
                    f'  1) 0123456789ABCDEF0123456789ABCDEF01234567 "{self.identity}"\n'
                    "     1 valid identities found\n"
                ),
            )
        if action == "delete-keychain":
            self._expect(cmd, ["security", "delete-keychain"], length=3)
            self.events.append(("restore", "security:delete-keychain"))
            if self.delete_rc:
                return self._done(cmd, kwargs, returncode=self.delete_rc)
            Path(cmd[-1]).unlink(missing_ok=True)
            return self._done(cmd, kwargs)
        raise AssertionError(f"unexpected security argv: {cmd}")

    # -- xcrun -------------------------------------------------------------

    def _xcrun(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "swiftc":
            self._expect(cmd, ["xcrun", "swiftc", cmd[2]], length=8)
            assert cmd[3] == "-target" and cmd[5] == "-O" and cmd[6] == "-o", cmd
            Path(cmd[7]).write_bytes(b"thin-binary")
            return self._done(cmd, kwargs)
        if cmd[1] == "notarytool":
            if cmd[2] == "store-credentials":
                # The App Store Connect API key: the private key is a path into
                # the private scratch directory, the key id and issuer are
                # identifiers, and no credential is an argv value.
                assert cmd[0:4] == [
                    "xcrun",
                    "notarytool",
                    "store-credentials",
                    cmd[3],
                ], cmd
                assert len(cmd) == 13, cmd
                assert cmd[4] == "--key", cmd
                key_path = Path(cmd[5])
                assert key_path.name == "notary-key.p8", cmd
                assert key_path.parent.name == "credentials", cmd
                assert cmd[6] == "--key-id" and cmd[7] == self.key_id, cmd
                assert cmd[8] == "--issuer" and cmd[9] == self.issuer_id, cmd
                assert cmd[10] == "--keychain" and cmd[11] == self.keychain, cmd
                assert cmd[12] == "--no-validate", cmd
                assert key_path == Path(self.keychain).parent / "notary-key.p8", cmd
                # The decoded key is a private file with the exact bytes.
                assert key_path.read_bytes() == self.notary_key
                assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
                self.profile = cmd[3]
                self.notary_key_path = key_path
                return self._done(cmd, kwargs, returncode=self.store_rc)
            assert cmd[2] == "submit", cmd
            # Exactly the merged builder's submission shape, profile only.
            assert cmd[4] == "--keychain-profile" and cmd[5] == self.profile, cmd
            assert cmd[6:] == [
                "--wait",
                "--timeout",
                "30m",
                "--output-format",
                "json",
            ], cmd
            assert len(cmd) == 11, cmd
            assert Path(cmd[3]) == self.zip_path, cmd
            assert self.notary_key not in [token.encode() for token in cmd]
            stdout = (
                self.notary_stdout
                if self.notary_stdout is not None
                else json.dumps({"id": "submission-1", "status": self.notary_status})
            )
            return self._done(cmd, kwargs, returncode=self.notary_rc, stdout=stdout)
        if cmd[1] == "stapler":
            assert cmd[2] in ("staple", "validate"), cmd
            assert len(cmd) == 4, cmd
            target = Path(cmd[3])
            assert target.name == APP_NAME, cmd
            assert target.parent.name != "credentials", cmd
            if cmd[2] == "staple":
                if self.ticket is not None:
                    ticket = target / "Contents" / "CodeResources"
                    ticket.parent.mkdir(parents=True, exist_ok=True)
                    ticket.write_bytes(self.ticket)
                return self._done(cmd, kwargs)
            return self._done(
                cmd, kwargs, returncode=self._stage_rc("staple_validate", target)
            )
        raise AssertionError(f"unexpected xcrun argv: {cmd}")

    # -- codesign ----------------------------------------------------------

    def _codesign(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        requirement = SERVER_HOST.developer_id_requirement(self.team_id)
        if cmd[1] == "--force":
            # The one release signature: identity, runtime, timestamp, the
            # tracked entitlements and the exact canonical requirement.
            assert len(cmd) == 12, cmd
            assert cmd[2:6] == ["--sign", self.identity, "--options", "runtime"], cmd
            assert cmd[6] == "--timestamp" and cmd[7] == "--entitlements", cmd
            assert Path(cmd[8]) == ENTITLEMENTS_PATH, cmd
            assert cmd[9] == "--requirements", cmd
            assert cmd[10] == f"=designated => {requirement}", cmd
            app = Path(cmd[-1])
            assert app.name == APP_NAME, cmd
            assert app.parent.name == "build", cmd
            assert app.parent.parent == self.scratch, cmd
            self.built_app = app
            seal = app / "Contents" / "_CodeSignature" / "CodeResources"
            seal.parent.mkdir(parents=True, exist_ok=True)
            seal.write_bytes(b"sealed-resources")
            self._signed = True
            return self._done(cmd, kwargs, returncode=self.sign_rc)
        if cmd[1] == "--verify":
            assert cmd[2] == "--strict", cmd
            if cmd[3] == "--all-architectures":
                # server_host's signed-policy verification: strict on every
                # slice, then the external expected requirement.
                if "--test-requirement" in cmd:
                    assert len(cmd) == 7 and cmd[4] == "--test-requirement", cmd
                    assert cmd[5] == f"={requirement}", cmd
                else:
                    assert len(cmd) == 5, cmd
                return self._done(
                    cmd, kwargs, returncode=self._stage_rc("verify", cmd[-1])
                )
            if cmd[3] == "--test-requirement":
                # The builder's external expected requirement check.
                assert len(cmd) == 7, cmd
                assert cmd[4] == f"={requirement}", cmd
                assert cmd[5] == "--all-architectures", cmd
                return self._done(
                    cmd, kwargs, returncode=self._stage_rc("verify", cmd[-1])
                )
            assert len(cmd) == 4, cmd
            return self._done(
                cmd, kwargs, returncode=self._stage_rc("verify", cmd[-1])
            )
        if cmd[1] == "-dv":
            if "--arch" in cmd:
                assert len(cmd) == 6 and cmd[2] == "--verbose=4", cmd
                assert _app_of(Path(cmd[-1])).name == APP_NAME, cmd
                arch = cmd[4]
                return self._done(cmd, kwargs, stderr=self._slice_display(arch, cmd[-1]))
            # The runtime's bundle display.
            assert len(cmd) == 4 and cmd[2] == "--verbose=4", cmd
            assert cmd[-1].endswith(".app"), cmd
            return self._done(cmd, kwargs, stderr=BUNDLE_DISPLAY)
        if cmd[1] == "-d" and cmd[2] == "--entitlements":
            assert cmd[3:6] == [":-", "--xml", "--arch"], cmd
            assert len(cmd) == 8, cmd
            assert _app_of(Path(cmd[-1])).name == APP_NAME, cmd
            fault = self._fault(cmd[-1])
            if "entitlements" in fault:
                return self._done(cmd, kwargs, stdout=fault["entitlements"])
            return self._done(cmd, kwargs, stdout=ENTITLEMENTS_XML)
        if cmd[1] == "-d" and cmd[2] == "-r-":
            assert cmd[3] == "--arch" and len(cmd) == 6, cmd
            assert _app_of(Path(cmd[-1])).name == APP_NAME, cmd
            return self._done(
                cmd,
                kwargs,
                stdout=f"designated => {SERVER_HOST.developer_id_requirement(self.team_id)}\n",
            )
        raise AssertionError(f"unexpected codesign argv: {cmd}")

    def _slice_display(self, arch: str, path: str) -> str:
        fault = self._fault(path)
        identifier = fault.get("identifier", BUNDLE_ID)
        team_id = fault.get("team_id", self.team_id)
        code_directory = fault.get("code_directory", CODE_DIRECTORY)
        runtime_version = fault.get("runtime_version", "13.0.0")
        timestamp = fault.get("timestamp", "Oct 30, 2026 at 12:00:00")
        cdhash = fault.get("cdhash", {}).get(arch, hashlib.sha1(arch.encode()).hexdigest())
        lines: list[str] = []
        if identifier is not None:
            lines.append(f"Identifier={identifier}")
        if code_directory is not None:
            lines.append(f"CodeDirectory {code_directory}")
        lines.append("Signature=Developer ID Application")
        lines.append(f"TeamIdentifier={team_id}")
        if cdhash is not None:
            lines.append(f"CDHash={cdhash}")
        if runtime_version is not None:
            lines.append(f"Runtime Version={runtime_version}")
        if timestamp is not None:
            lines.append(f"Timestamp={timestamp}")
        return "\n".join(lines) + "\n"

    # -- lipo --------------------------------------------------------------

    def _lipo(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "-create":
            assert cmd[:3] == ["lipo", "-create", "-output"] and len(cmd) == 6, cmd
            Path(cmd[3]).write_bytes(b"universal-binary")
            return self._done(cmd, kwargs)
        assert cmd[1] == "-archs" and len(cmd) == 3, cmd
        return self._done(cmd, kwargs, stdout="arm64 x86_64\n")

    # -- signer preparation and manifest -----------------------------------

    def _npm_install(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        assert cmd[1] == "install", cmd
        assert cmd[2] == "--prefix" and len(cmd) == 11, cmd
        assert Path(cmd[3]) == self.scratch / "signer", cmd
        assert cmd[4:] == [
            "--no-save",
            "--no-audit",
            "--no-fund",
            "--ignore-scripts",
            "--loglevel",
            "error",
            REHEARSAL.MANIFEST_SIGNER,
        ], cmd
        env = kwargs.get("env") or {}
        # Preparation never sees a signing credential.
        for name in ("TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD"):
            assert name not in env, f"signer preparation received {name}"
        assert "PYTHONPATH" not in env
        assert env.get("npm_config_audit") == "false"
        assert env.get("npm_config_fund") == "false"
        assert env.get("npm_config_ignore_scripts") == "true"
        if self.npm_rc:
            return self._done(cmd, kwargs, returncode=self.npm_rc)
        self.signer_prefix = Path(cmd[3])
        binary = self.signer_prefix / "node_modules" / ".bin" / "tauri"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        return self._done(cmd, kwargs)

    def _signer(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        assert self.signer_prefix is not None, "the signer was not prepared first"
        expected = str(self.signer_prefix / "node_modules" / ".bin" / "tauri")
        assert cmd[0] == expected, f"the signer is not the prepared executable: {cmd}"
        assert cmd[1:] == ["signer", "sign", str(self._manifest_path())], cmd
        env = kwargs.get("env") or {}
        assert env.get("TAURI_SIGNING_PRIVATE_KEY") == self.tauri_key
        assert env.get("TAURI_SIGNING_PRIVATE_KEY_PASSWORD") == self.tauri_password
        if self.signer_rc:
            return self._done(cmd, kwargs, returncode=self.signer_rc)
        manifest = Path(cmd[3])
        manifest.with_name(manifest.name + ".sig").write_text(
            "untrusted comment: fake candidate signature\n"
        )
        if self.mutate_archive_after_sign:
            assert self.archive is not None
            self.archive.write_bytes(self.archive.read_bytes() + b"tampered")
        return self._done(cmd, kwargs)

    def _manifest_path(self) -> Path:
        assert self._manifest_out is not None
        return self._manifest_out

    # -- prepared python ---------------------------------------------------

    def _python(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == ["-m", "ciao.release_manifest"]:
            if cmd[3] == "build":
                return self._manifest_build(cmd, kwargs)
            assert cmd[3] == "verify", cmd
            assert len(cmd) == 6, cmd
            return self._done(
                cmd,
                kwargs,
                returncode=self.manifest_verify_rc,
                stdout=f"verified {VERSION}: trusted comment\n",
            )
        assert cmd[1] == "-c", cmd
        assert cmd[2] == REHEARSAL.OWNERSHIP_SCRIPT, cmd
        assert len(cmd) == 5, cmd
        assert cmd[0] not in ("python", "python3", sys.executable), (
            "ownership must be verified by the prepared interpreter"
        )
        assert Path(cmd[3]) == self.scratch / "verify-extracted" / APP_NAME, cmd
        assert (
            Path(cmd[4])
            == self.scratch / "home" / ".local" / "state" / "ciaobot" / "server-host.json"
        ), cmd
        env = kwargs.get("env") or {}
        assert "PYTHONPATH" not in env
        assert env.get("HOME", "") == str(self.scratch / "home"), env
        if self.ownership_rc:
            return self._done(cmd, kwargs, returncode=self.ownership_rc)
        # The fake plays the installed wheel: it runs the real verifier against
        # the record the rehearsal just wrote.
        try:
            REAL_VERIFY_OWNED_HOST(
                Path(cmd[3]), ownership_path=Path(cmd[4]), runner=self
            )
        except Exception:
            return self._done(cmd, kwargs, returncode=1)
        return self._done(cmd, kwargs)

    def _manifest_build(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        assert cmd[4:6] == ["--version", VERSION], cmd
        out = Path(cmd[cmd.index("--out") + 1])
        archive = Path(cmd[cmd.index("--server-host") + 1])
        team_id = cmd[cmd.index("--server-host-team-id") + 1]
        wheel = Path(cmd[-1])
        assert len(cmd) == 13, cmd
        assert out == self.scratch / "ciaobot-engine-manifest.json", cmd
        assert archive == self.scratch / "build" / REHEARSAL.ARCHIVE_NAME, cmd
        assert team_id == self.team_id, cmd
        assert wheel.name == WHEEL_NAME, cmd
        assert cmd[0] not in ("python", "python3", sys.executable), (
            "the manifest must be built by the prepared interpreter"
        )
        env = kwargs.get("env") or {}
        assert "PYTHONPATH" not in env
        assert env.get("HOME", "").endswith("/home"), env
        self.archive = archive
        self._manifest_out = out
        archive_sha, archive_size = _digest(archive)
        wheel_sha, wheel_size = _digest(wheel)
        artifacts: list[dict[str, Any]] = [
            {
                "filename": wheel.name,
                "kind": "wheel",
                "platform": "any",
                "arch": "any",
                "sha256": wheel_sha,
                "size": wheel_size,
            }
        ]
        if not self.drop_host_entry:
            artifacts.append(
                {
                    "filename": archive.name,
                    "kind": "server-host",
                    "platform": "macos",
                    "arch": "universal",
                    "sha256": self.manifest_sha256 or archive_sha,
                    "size": self.manifest_size or archive_size,
                    "bundle_id": BUNDLE_ID,
                    "host_revision": SERVER_HOST.SIGNED_HOST_REVISION,
                    "host_protocol": SERVER_HOST.HOST_PROTOCOL,
                    "signing": {
                        "kind": SERVER_HOST.DEVELOPER_ID_SIGNING_KIND,
                        "team_id": self.manifest_team or team_id,
                    },
                }
            )
        manifest = {
            "schema": 1,
            "version": VERSION,
            "tag": f"v{VERSION}",
            "created": "2026-10-30T00:00:00+00:00",
            "artifacts": artifacts,
        }
        out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return self._done(cmd, kwargs, stdout=str(out))

    _manifest_out: Path | None = None


def _run_rehearsal(
    tmp_path: Path,
    fake: FakeToolchain,
    *,
    environ: dict[str, str] | None = None,
    platform_name: str = "darwin",
    name: str = "rehearsal",
) -> REHEARSAL.RehearsalResult:
    wheel, interpreter = _paths(tmp_path)
    return REHEARSAL.rehearse(
        output=tmp_path / name,
        python=interpreter,
        wheels=[wheel],
        source_sha=SOURCE_SHA,
        environ=_environment() if environ is None else environ,
        runner=fake,
        children=fake.children,
        platform_name=platform_name,
    )


def _families(commands: list[list[str]]) -> list[str]:
    """A readable family per recorded command, in order."""
    families: list[str] = []
    for cmd in commands:
        tool = Path(cmd[0]).name
        if tool == "security":
            families.append(f"security:{cmd[1]}" + (":set" if "-s" in cmd else ""))
        elif tool == "xcrun":
            families.append(f"xcrun:{cmd[1]}:{cmd[2]}")
        elif tool == "codesign":
            families.append("codesign:" + (cmd[1] if cmd[1] != "--verify" else "verify"))
        elif tool == "lipo":
            families.append(f"lipo:{cmd[1]}")
        elif tool == "ditto":
            families.append("ditto")
        elif tool == "spctl":
            families.append("spctl")
        elif tool == "npm":
            families.append("npm:install")
        elif tool == "tauri":
            families.append("tauri:signer")
        elif cmd[1] == "-c":
            families.append("ownership")
        else:
            families.append("manifest:" + cmd[3])
    return families


def _index(families: list[str], value: str) -> int:
    return families.index(value)


def _manifest_builds(commands: list[list[str]]) -> list[list[str]]:
    return [cmd for cmd in commands if cmd[1:4] == ["-m", "ciao.release_manifest", "build"]]


def _assert_no_adhoc(commands: list[list[str]]) -> None:
    for cmd in commands:
        if cmd[:3] == ["codesign", "--force", "--sign"]:
            assert cmd[3] != "-", "the rehearsal fell back to an ad-hoc signature"
            assert cmd[3] == IDENTITY, "the signing identity came from somewhere else"


# --- input validation -------------------------------------------------------


def test_missing_input_fails_before_keychain_or_build(
    tmp_path: Path, macos: None
) -> None:
    for name in REHEARSAL.REQUIRED_ENV:
        fake = FakeToolchain()
        environ = _environment(**{name: None})
        with pytest.raises(REHEARSAL.RehearsalError) as caught:
            _run_rehearsal(tmp_path, fake, environ=environ)
        message = str(caught.value)
        assert name in message, message
        # The name is reported; no value is, and nothing ran.
        for value in SENTINELS.values():
            assert value not in message
        assert fake.commands == []
        assert not (tmp_path / "rehearsal").exists()


def test_missing_input_message_names_every_absent_input(
    tmp_path: Path, macos: None
) -> None:
    environ = _environment(
        SERVER_HOST_TEAM_ID=None, SERVER_HOST_NOTARY_ISSUER_ID=None
    )
    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake, environ=environ)
    message = str(caught.value)
    assert "SERVER_HOST_TEAM_ID" in message
    assert "SERVER_HOST_NOTARY_ISSUER_ID" in message
    assert fake.commands == []


def test_rehearsal_refuses_existing_output_and_non_macos_before_writes(
    tmp_path: Path, macos: None
) -> None:
    existing = tmp_path / "occupied"
    existing.mkdir()
    marker = existing / "keep-me.txt"
    marker.write_text("untouched", encoding="utf-8")

    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        REHEARSAL.rehearse(
            output=existing,
            python=_paths(tmp_path)[1],
            wheels=[_paths(tmp_path)[0]],
            source_sha=SOURCE_SHA,
            environ=_environment(),
            runner=fake,
            platform_name="darwin",
        )
    assert "existing output" in str(caught.value)
    assert marker.read_text(encoding="utf-8") == "untouched"
    assert fake.commands == []

    target = tmp_path / "symlink-target"
    target.mkdir()
    links = tmp_path / "links"
    links.mkdir()
    dangling = links / "rehearsal"
    dangling.symlink_to(target, target_is_directory=True)
    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError):
        REHEARSAL.rehearse(
            output=dangling,
            python=_paths(tmp_path)[1],
            wheels=[_paths(tmp_path)[0]],
            source_sha=SOURCE_SHA,
            environ=_environment(),
            runner=fake,
            platform_name="darwin",
        )
    assert list(target.iterdir()) == []
    assert fake.commands == []

    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake, platform_name="linux", name="never-created")
    assert "macOS" in str(caught.value)
    assert fake.commands == []
    assert not (tmp_path / "never-created").exists()

    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError):
        REHEARSAL.rehearse(
            output=Path("relative-output"),
            python=_paths(tmp_path)[1],
            wheels=[_paths(tmp_path)[0]],
            source_sha=SOURCE_SHA,
            environ=_environment(),
            runner=fake,
            platform_name="darwin",
        )
    assert fake.commands == []

    # Credentials and candidate files never land in the repository.
    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        REHEARSAL.rehearse(
            output=REHEARSAL.REPO / "rehearsal-scratch",
            python=_paths(tmp_path)[1],
            wheels=[_paths(tmp_path)[0]],
            source_sha=SOURCE_SHA,
            environ=_environment(),
            runner=fake,
            platform_name="darwin",
        )
    assert "inside the repository" in str(caught.value)
    assert fake.commands == []
    assert not (REHEARSAL.REPO / "rehearsal-scratch").exists()


def _workflow_cleanup(scratch: Path) -> bool:
    """The workflow's rule: delete only the path this run recorded."""
    import shutil

    receipt = scratch.with_name(scratch.name + REHEARSAL.OWNERSHIP_RECEIPT_SUFFIX)
    if receipt.is_file() and receipt.read_text(encoding="utf-8").strip() == str(scratch):
        shutil.rmtree(scratch)
        receipt.unlink()
        return True
    if receipt.is_file():
        receipt.unlink()
    return False


def test_scratch_cleanup_is_ownership_scoped(tmp_path: Path, macos: None) -> None:
    # A pre-existing directory that already contains the marker is still not
    # ours: exclusive mkdir refuses it, no receipt is written, and the
    # workflow's cleanup leaves it alone.
    foreign = tmp_path / "rehearsal"
    foreign.mkdir()
    sentinel = foreign / "someone-elses-file"
    sentinel.write_text("keep", encoding="utf-8")
    (foreign / REHEARSAL.OWNERSHIP_MARKER).write_text("planted", encoding="utf-8")
    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError):
        _run_rehearsal(tmp_path, fake)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert _workflow_cleanup(foreign) is False
    assert foreign.is_dir()

    # A pre-existing receipt is never adopted, even without a directory there.
    planted = tmp_path / "planted"
    planted_receipt = planted.with_name(
        planted.name + REHEARSAL.OWNERSHIP_RECEIPT_SUFFIX
    )
    planted_receipt.write_text(str(planted), encoding="utf-8")
    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake, name="planted")
    assert "ownership receipt" in str(caught.value)
    assert not planted.exists()
    assert planted_receipt.exists()

    # A planted directory with a matching receipt is still not ours: the
    # workflow clears the receipt before the helper starts, so the refusal
    # leaves no receipt and cleanup leaves the directory alone.
    combined = tmp_path / "combined"
    combined.mkdir()
    combined_marker = combined / REHEARSAL.OWNERSHIP_MARKER
    combined_marker.write_text("planted", encoding="utf-8")
    combined_receipt = combined.with_name(
        combined.name + REHEARSAL.OWNERSHIP_RECEIPT_SUFFIX
    )
    combined_receipt.write_text(str(combined), encoding="utf-8")
    combined_receipt.unlink()  # the helper step's pre-clean
    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError):
        _run_rehearsal(tmp_path, fake, name="combined")
    assert _workflow_cleanup(combined) is False
    assert combined_marker.read_text(encoding="utf-8") == "planted"

    # A directory this run created is recorded by path, and cleanup removes
    # exactly that directory and the receipt.
    owned = tmp_path / "owned"
    fake = FakeToolchain()
    _run_rehearsal(tmp_path, fake, name="owned")
    receipt = owned.with_name(owned.name + REHEARSAL.OWNERSHIP_RECEIPT_SUFFIX)
    assert (owned / REHEARSAL.OWNERSHIP_MARKER).is_file()
    assert receipt.read_text(encoding="utf-8") == str(owned)
    assert _workflow_cleanup(owned) is True
    assert not owned.exists()
    assert not receipt.exists()


def test_no_ownership_receipt_when_the_marker_write_fails(
    tmp_path: Path, macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An interruption (or failure) between mkdir and the marker leaves our empty
    # directory behind, but writes no receipt, so cleanup never adopts it.
    real_write = REHEARSAL._write_private

    def fail_marker(path: Path, data: bytes) -> None:
        if Path(path).name == REHEARSAL.OWNERSHIP_MARKER:
            raise OSError("simulated interruption")
        real_write(path, data)

    monkeypatch.setattr(REHEARSAL, "_write_private", fail_marker)
    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError):
        _run_rehearsal(tmp_path, fake)
    output = tmp_path / "rehearsal"
    receipt = output.with_name(output.name + REHEARSAL.OWNERSHIP_RECEIPT_SUFFIX)
    assert output.is_dir()
    assert not receipt.exists()
    assert _workflow_cleanup(output) is False
    assert output.is_dir()


def test_rehearsal_requires_exactly_one_candidate_wheel(
    tmp_path: Path, macos: None
) -> None:
    wheel, interpreter = _paths(tmp_path)
    second = wheel.with_name("ciaobot-1.7.0-cp313-none-any.whl")
    second.write_bytes(b"second-wheel")
    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        REHEARSAL.rehearse(
            output=tmp_path / "rehearsal",
            python=interpreter,
            wheels=[wheel, second],
            source_sha=SOURCE_SHA,
            environ=_environment(),
            runner=fake,
            platform_name="darwin",
        )
    assert "exactly one candidate wheel" in str(caught.value)
    assert fake.commands == []


def test_rehearsal_refuses_a_changed_manifest_trust_root(
    tmp_path: Path, macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The installed wheel must verify with the unchanged release key. A build
    # that changed the trust root is not the release being rehearsed.
    monkeypatch.setattr(
        REHEARSAL.release_manifest, "RELEASE_PUBLIC_KEY", "another-public-key"
    )
    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake)
    assert "release key" in str(caught.value)
    assert fake.commands == []
    assert not (tmp_path / "rehearsal").exists()


# --- argv: no credential except the documented p12 import residual ----------


def test_secrets_never_appear_in_spawned_argv(tmp_path: Path, macos: None) -> None:
    fake = FakeToolchain()
    _run_rehearsal(tmp_path, fake)
    assert fake.keychain_password, "the fake never saw the keychain password"
    import_argv = next(cmd for cmd in fake.commands if cmd[1:2] == ["import"])
    key_text = NOTARY_KEY_PEM.decode()
    key_base64 = base64.b64encode(NOTARY_KEY_PEM).decode()
    for cmd in fake.commands:
        joined = "\n".join(cmd)
        assert key_base64 not in joined, f"the encoded notary key is in argv: {cmd}"
        assert fake.keychain_password not in cmd, f"the keychain password is in argv: {cmd}"
        assert TAURI_KEY not in cmd and TAURI_PASSWORD not in cmd
        # The API key is a file path in the scratch directory, never its bytes.
        assert key_text not in joined, f"the notary key contents are in argv: {cmd}"
        assert SENTINEL_KEY_MATERIAL not in joined
    # The p12 import password is the one accepted residual, and only there.
    carriers = [cmd for cmd in fake.commands if P12_PASSWORD in cmd]
    assert carriers == [import_argv], carriers


def test_notary_commands_use_only_the_api_key_options(
    tmp_path: Path, macos: None
) -> None:
    fake = FakeToolchain()
    _run_rehearsal(tmp_path, fake)
    options = {
        token
        for cmd in fake.commands
        if cmd[1:2] == ["notarytool"]
        for token in cmd
        if token.startswith("--")
    }
    # Exactly the API-key store shape and the profile-only submission shape:
    # no account address, no password and no other option is possible.
    assert options == {
        "--key",
        "--key-id",
        "--issuer",
        "--keychain",
        "--no-validate",
        "--keychain-profile",
        "--wait",
        "--timeout",
        "--output-format",
    }
    key_values = [cmd[cmd.index("--key") + 1] for cmd in fake.commands if "--key" in cmd]
    assert len(key_values) == 1
    # The value is a path in the private scratch directory, and it is gone
    # after cleanup (the fake checked its 0600 mode and exact bytes at store time).
    assert Path(key_values[0]).name == "notary-key.p8"
    assert not Path(key_values[0]).exists()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"SERVER_HOST_NOTARY_KEY_P8_BASE64": "not base64 !!"}, "P8_BASE64"),
        ({"SERVER_HOST_NOTARY_KEY_P8_BASE64": base64.b64encode(b"").decode()}, "P8_BASE64"),
        (
            {"SERVER_HOST_NOTARY_KEY_P8_BASE64": base64.b64encode(b"not a pem").decode()},
            "PEM",
        ),
        (
            {
                "SERVER_HOST_NOTARY_KEY_P8_BASE64": base64.b64encode(
                    b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
                ).decode()
            },
            "PEM",
        ),
        ({"SERVER_HOST_NOTARY_KEY_ID": "too-short"}, "NOTARY_KEY_ID"),
        ({"SERVER_HOST_NOTARY_KEY_ID": "ABCD123XY!"}, "NOTARY_KEY_ID"),
        ({"SERVER_HOST_NOTARY_ISSUER_ID": "not-a-uuid"}, "NOTARY_ISSUER_ID"),
        (
            {"SERVER_HOST_NOTARY_ISSUER_ID": "123456781234123412341234567890AB"},
            "NOTARY_ISSUER_ID",
        ),
    ],
)
def test_malformed_notary_api_key_fails_before_any_tool(
    tmp_path: Path,
    macos: None,
    overrides: dict[str, str],
    expected: str,
) -> None:
    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake, environ=_environment(**overrides))
    message = str(caught.value)
    assert expected in message, message
    # Fixed text only: no value from the key material or the identifiers.
    assert NOTARY_KEY_PEM.decode() not in message
    assert fake.commands == []
    assert not (tmp_path / "rehearsal").exists()


@pytest.mark.parametrize(
    ("fake_kwargs", "stage"),
    [
        ({}, None),
        ({"create_rc": 1}, "keychain"),
        ({"store_rc": 1}, "keychain"),
        ({"sign_rc": 1}, "build"),
        ({"notary_status": "Invalid"}, "build"),
        ({"manifest_verify_rc": 1}, "manifest_verification"),
        ({"restore_search_rc": 1}, "cleanup"),
    ],
)
def test_p8_key_is_private_and_deleted_on_every_path(
    tmp_path: Path, macos: None, fake_kwargs: dict[str, Any], stage: str | None
) -> None:
    fake = FakeToolchain(**fake_kwargs)
    if stage is None:
        _run_rehearsal(tmp_path, fake)
    else:
        with pytest.raises(REHEARSAL.RehearsalError):
            _run_rehearsal(tmp_path, fake)
        report = json.loads(
            (tmp_path / "rehearsal" / REHEARSAL.VERIFICATION_NAME).read_text(
                encoding="utf-8"
            )
        )
        assert report["failure"]["stage"] == stage

    output = tmp_path / "rehearsal"
    credentials = output / "credentials"
    assert not credentials.exists(), fake_kwargs
    # The decoded key is deleted wherever it was written, not only where the
    # rehearsal was supposed to put it, and its bytes never survive anywhere in
    # the scratch tree.
    assert not list(output.rglob("*.p8")), fake_kwargs
    for path in output.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert NOTARY_KEY_PEM not in data, path
            assert SENTINEL_KEY_MATERIAL.encode() not in data, path
    if fake.notary_key_path is not None:
        assert fake.notary_key_path.name == "notary-key.p8"


def test_build_and_manifest_commands_never_carry_a_secret(
    tmp_path: Path, macos: None
) -> None:
    fake = FakeToolchain()
    _run_rehearsal(tmp_path, fake)
    for cmd in fake.commands:
        joined = " ".join(cmd)
        if cmd[1:2] == ["import"]:
            # The documented residual: the p12 passphrase on this one argv.
            continue
        assert P12_PASSWORD not in joined
        assert NOTARY_KEY_PEM.decode() not in joined
        assert fake.keychain_password not in joined
        assert TAURI_KEY not in joined


# --- the selected signed mode only -----------------------------------------


def test_explicit_release_mode_has_no_adhoc_fallback(
    tmp_path: Path, macos: None
) -> None:
    # A failed certificate import stops before any build.
    fake = FakeToolchain(import_rc=1)
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake, name="import")
    assert "import" in str(caught.value)
    assert _manifest_builds(fake.commands) == []
    assert not any(cmd[:3] == ["xcrun", "swiftc"] for cmd in fake.commands)

    # A named identity the temporary keychain does not hold is a refusal, not a
    # reason to sign with whatever is available.
    fake = FakeToolchain(identity_listed=False)
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake, name="identity")
    assert "signing identity" in str(caught.value)
    assert not any(cmd[:3] == ["codesign", "--force"] for cmd in fake.commands)

    # A signing failure never continues and never falls back.
    fake = FakeToolchain(sign_rc=1)
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake, name="sign")
    assert "signature could not be applied" in str(caught.value)
    assert _manifest_builds(fake.commands) == []
    _assert_no_adhoc(fake.commands)

    # A requirement the built app does not satisfy fails the build.
    fake = FakeToolchain(fault={"stage": "built", "verify": 1})
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake, name="requirement")
    assert "release build failed" in str(caught.value)
    assert _manifest_builds(fake.commands) == []
    _assert_no_adhoc(fake.commands)

    # The one signature is made with the explicitly named identity.
    fake = FakeToolchain()
    _run_rehearsal(tmp_path, fake, name="success")
    _assert_no_adhoc(fake.commands)


# --- notarization -----------------------------------------------------------


@pytest.mark.parametrize(
    ("notary_status", "notary_stdout", "notary_rc", "expected"),
    [
        ("Invalid", None, 0, "Invalid"),
        ("Rejected", None, 0, "Rejected"),
        ("In Progress", None, 0, "In Progress"),
        (None, None, 0, "unrecognized"),
        (["Accepted"], None, 0, "unrecognized"),
        ("Accepted", "not json", 0, "JSON"),
        ("Accepted", None, 1, "exit status 1"),
        ("Accepted", '{"id": "submission-1"}', 0, "unrecognized"),
    ],
)
def test_notary_must_return_accepted_json(
    tmp_path: Path,
    macos: None,
    notary_status: Any,
    notary_stdout: str | None,
    notary_rc: int,
    expected: str,
) -> None:
    fake = FakeToolchain(
        notary_status=notary_status, notary_stdout=notary_stdout, notary_rc=notary_rc
    )
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake)
    message = str(caught.value)
    assert "notar" in message.lower(), message
    assert expected in message, message
    # Nothing is stapled, no manifest is written and no digest stage follows.
    families = _families(fake.commands)
    assert "xcrun:stapler:staple" not in families
    assert _manifest_builds(fake.commands) == []
    joined = "\n".join(" ".join(cmd) for cmd in fake.commands)
    assert "gh release" not in joined and "upload-artifact" not in joined


def test_notary_accepted_is_required_exactly(
    tmp_path: Path, macos: None
) -> None:
    # A run whose only difference is the exact ``Accepted`` status succeeds.
    fake = FakeToolchain(notary_status="Accepted")
    result = _run_rehearsal(tmp_path, fake)
    assert result.archive_sha256
    assert "xcrun:stapler:staple" in _families(fake.commands)


# --- ordering and the final extracted app ----------------------------------


def test_staple_and_final_extracted_verification_precede_manifest(
    tmp_path: Path, macos: None
) -> None:
    fake = FakeToolchain()
    result = _run_rehearsal(tmp_path, fake)
    families = _families(fake.commands)

    sign = _index(families, "codesign:--force")
    notary = _index(families, "xcrun:notarytool:submit")
    staple = _index(families, "xcrun:stapler:staple")
    validate = _index(families, "xcrun:stapler:validate")
    manifest_build = _index(families, "manifest:build")
    prepare = _index(families, "npm:install")
    signature = _index(families, "tauri:signer")
    manifest_verify = _index(families, "manifest:verify")
    ownership = _index(families, "ownership")
    assert (
        sign
        < notary
        < staple
        < validate
        < ownership
        < manifest_build
        < prepare
        < signature
        < manifest_verify
    )

    # The build archive exists before the helper verifies it, and that
    # verification happens on the helper's own extraction.
    build_extract_runs = [
        index
        for index, cmd in enumerate(fake.commands)
        if ".host-verify" in " ".join(cmd)
    ]
    extracted_runs = [
        index
        for index, cmd in enumerate(fake.commands)
        if "verify-extracted" in " ".join(cmd)
    ]
    assert build_extract_runs, "the builder never re-extracted its own archive"
    assert extracted_runs, "the helper never verified its own extraction"
    assert max(build_extract_runs) < min(extracted_runs)
    assert min(extracted_runs) < manifest_build
    assert max(extracted_runs) < manifest_build

    # And the authenticated archive is extracted and verified once more after
    # the manifest is signed.
    final_runs = [
        index
        for index, cmd in enumerate(fake.commands)
        if "verify-final" in " ".join(cmd)
    ]
    assert final_runs and min(final_runs) > manifest_verify
    assert (result.report_path).is_file()


@pytest.mark.parametrize(
    ("fake_kwargs", "expected"),
    [
        ({"ticket": None}, "Contents/CodeResources"),
        ({"fault": {"stage": "extracted", "team_id": "ZZZZZZZZZZ"}}, "team"),
        ({"fault": {"stage": "extracted", "gatekeeper": 1}}, "Gatekeeper"),
        ({"fault": {"stage": "extracted", "staple_validate": 1}}, "ticket"),
        ({"fault": {"stage": "extracted", "runtime_version": None}}, "runtime"),
        ({"fault": {"stage": "extracted", "timestamp": None}}, "timestamp"),
        ({"fault": {"stage": "extracted", "entitlements": "{}"}}, "entitlement"),
        (
            {"fault": {"stage": "extracted", "cdhash": {"arm64": None}}},
            "CDHash",
        ),
    ],
)
def test_final_extracted_app_evidence_is_required(
    tmp_path: Path,
    macos: None,
    fake_kwargs: dict[str, Any],
    expected: str,
) -> None:
    fake = FakeToolchain(**fake_kwargs)
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake)
    assert expected.lower() in str(caught.value).lower(), str(caught.value)
    assert _manifest_builds(fake.commands) == []


def test_verification_targets_the_extracted_app_not_the_build_directory(
    tmp_path: Path, macos: None
) -> None:
    # Gatekeeper accepts the builder's own re-extraction and rejects only the
    # helper's extraction. A helper that verified the build directory would pass.
    fake = FakeToolchain(fault={"stage": "extracted", "gatekeeper": 1})
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake)
    assert "Gatekeeper" in str(caught.value)
    build_extract_assessments = [
        cmd
        for cmd in fake.commands
        if cmd[0] == "spctl" and ".host-verify" in cmd[-1]
    ]
    assert build_extract_assessments, "the builder did not assess its own extraction"
    extracted_assessments = [
        cmd
        for cmd in fake.commands
        if cmd[0] == "spctl" and "verify-extracted" in cmd[-1]
    ]
    assert extracted_assessments, "the helper never assessed its extraction"


# --- the candidate manifest -------------------------------------------------


@pytest.mark.parametrize(
    ("fake_kwargs", "expected"),
    [
        ({"manifest_team": "ZZZZZZZZZZ"}, "team"),
        ({"manifest_sha256": "0" * 64}, "final archive bytes"),
        ({"manifest_size": 1}, "final archive bytes"),
        ({"drop_host_entry": True}, "server host"),
        ({"mutate_archive_after_sign": True}, "changed after the build"),
    ],
)
def test_manifest_authenticates_exact_final_archive_and_expected_team(
    tmp_path: Path,
    macos: None,
    fake_kwargs: dict[str, Any],
    expected: str,
) -> None:
    fake = FakeToolchain(**fake_kwargs)
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake)
    assert expected in str(caught.value), str(caught.value)
    # The installed verifier ran, and the post-manifest re-verification did not.
    assert any(cmd[1:4] == ["-m", "ciao.release_manifest", "verify"] for cmd in fake.commands)
    assert not any("verify-final" in " ".join(cmd) for cmd in fake.commands)


def test_manifest_signer_is_prepared_without_signing_credentials(
    tmp_path: Path, macos: None
) -> None:
    fake = FakeToolchain()
    _run_rehearsal(tmp_path, fake)

    installs = [cmd for cmd in fake.commands if Path(cmd[0]).name == "npm"]
    assert len(installs) == 1
    assert installs[0][2] == "--prefix"
    install_kwargs = next(
        kwargs for cmd, kwargs in fake.calls if Path(cmd[0]).name == "npm"
    )
    # Preparation is a separate process with no signing credential present.
    assert "TAURI_SIGNING_PRIVATE_KEY" not in install_kwargs["env"]
    assert "TAURI_SIGNING_PRIVATE_KEY_PASSWORD" not in install_kwargs["env"]
    assert install_kwargs["env"]["npm_config_ignore_scripts"] == "true"

    signers = [cmd for cmd in fake.commands if Path(cmd[0]).name == "tauri"]
    assert len(signers) == 1
    assert signers[0][0].endswith("node_modules/.bin/tauri")
    signer_kwargs = next(
        kwargs for cmd, kwargs in fake.calls if Path(cmd[0]).name == "tauri"
    )
    assert signer_kwargs["env"]["TAURI_SIGNING_PRIVATE_KEY"] == TAURI_KEY
    assert "PYTHONPATH" not in signer_kwargs["env"]

    # Preparation happens after the candidate manifest exists and before it is
    # signed; the install never runs in the signing step's environment.
    families = _families(fake.commands)
    assert _index(families, "manifest:build") < _index(families, "npm:install")
    assert _index(families, "npm:install") < _index(families, "tauri:signer")


# --- ownership is verified by the prepared interpreter ----------------------


def test_clean_wheel_ownership_verification_uses_the_prepared_interpreter(
    tmp_path: Path, macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("ownership was verified by the checkout, not the wheel")

    monkeypatch.setattr(REHEARSAL.server_host, "verify_owned_host", fail)
    fake = FakeToolchain()
    _run_rehearsal(tmp_path, fake)

    ownership_commands = [
        cmd for cmd in fake.commands if cmd[2:3] == [REHEARSAL.OWNERSHIP_SCRIPT]
    ]
    assert len(ownership_commands) == 1
    command = ownership_commands[0]
    _, interpreter = _paths(tmp_path)
    assert command[0] == str(interpreter)


# --- cleanup ----------------------------------------------------------------


def _restore_commands(commands: list[list[str]]) -> tuple[list[str], list[str]]:
    restored_search: list[str] = []
    restored_default: list[str] = []
    for cmd in commands:
        if cmd[:4] == ["security", "list-keychains", "-d", "user"] and "-s" in cmd:
            restored_search = cmd[cmd.index("-s") + 1 :]
        if cmd[:4] == ["security", "default-keychain", "-d", "user"] and "-s" in cmd:
            restored_default = [cmd[-1]]
    return restored_search, restored_default


def _prepared_session(
    tmp_path: Path, fake: FakeToolchain
) -> REHEARSAL._SigningKeychain:
    output = tmp_path / "rehearsal"
    output.mkdir()
    session = REHEARSAL._SigningKeychain(
        output, REHEARSAL._credentials(_environment()), fake
    )
    session.prepare()
    return session


def test_credential_deletion_survives_unlink_and_directory_failures(
    tmp_path: Path, macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeToolchain()
    session = _prepared_session(tmp_path, fake)
    assert session.notary_key.is_file()
    # A path that still holds the p12 (an import that failed before its early
    # unlink, for example) must not stop the API key from being deleted.
    session.certificate.write_bytes(P12_BYTES)

    real_unlink = Path.unlink

    def fail_certificate(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == session.certificate:
            raise OSError("simulated unlink failure")
        real_unlink(path, *args, **kwargs)

    def fail_rmtree(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("simulated directory removal failure")

    monkeypatch.setattr(Path, "unlink", fail_certificate)
    monkeypatch.setattr(REHEARSAL.shutil, "rmtree", fail_rmtree)

    problems = session.restore()
    assert any("certificate could not be deleted" in problem for problem in problems)
    assert any(
        "credential directory could not be removed" in problem for problem in problems
    )
    # Each deletion was attempted independently: the API key is gone even
    # though the p12 unlink and the directory removal both failed.
    assert not session.notary_key.exists()
    assert session.certificate.exists()
    # The keychain was still deleted and both prior settings restored.
    assert any(cmd[:2] == ["security", "delete-keychain"] for cmd in fake.commands)
    restored_search, restored_default = _restore_commands(fake.commands)
    assert restored_search == PRIOR_SEARCH
    assert restored_default == [PRIOR_DEFAULT]


def test_failed_restore_keeps_its_flag_and_retries(
    tmp_path: Path, macos: None
) -> None:
    fake = FakeToolchain(restore_search_rc=1)
    session = _prepared_session(tmp_path, fake)
    problems = session.restore()
    assert any("search list could not be restored" in problem for problem in problems)
    # The flag survives the failure so a retry still attempts the setting...
    assert session.search_changed is True
    # ...and the API key is deleted even though the restore command failed.
    assert not session.notary_key.exists()

    fake.restore_search_rc = 0
    assert session.restore() == []
    assert session.search_changed is False
    restores = [
        cmd
        for cmd in fake.commands
        if cmd[:5] == ["security", "list-keychains", "-d", "user", "-s"]
        and cmd[5:] == PRIOR_SEARCH
    ]
    assert len(restores) == 2


@pytest.mark.parametrize(
    ("fake_kwargs", "restored"),
    [
        ({}, "both"),
        # Failures before either keychain setting was touched change nothing.
        ({"create_rc": 1}, "none"),
        ({"settings_rc": 1}, "none"),
        ({"unlock_rc": 1}, "none"),
        # The default is flagged before its command; the search list only after.
        ({"default_set_rc": 1}, "default"),
        ({"search_set_rc": 1}, "both"),
        ({"import_rc": 1}, "both"),
        ({"partition_rc": 1}, "both"),
        ({"store_rc": 1}, "both"),
        ({"sign_rc": 1}, "both"),
        ({"notary_status": "Invalid"}, "both"),
        ({"fault": {"stage": "extracted", "staple_validate": 1}}, "both"),
        ({"fault": {"stage": "extracted", "gatekeeper": 1}}, "both"),
        ({"manifest_verify_rc": 1}, "both"),
        ({"ownership_rc": 1}, "both"),
        ({"manifest_team": "ZZZZZZZZZZ"}, "both"),
        ({"signer_rc": 1}, "both"),
        ({"npm_rc": 1}, "both"),
        # The cleanup itself can fail; it is still attempted in full.
        ({"restore_search_rc": 1}, "both"),
        ({"restore_default_rc": 1}, "both"),
        ({"delete_rc": 1}, "both"),
    ],
)
def test_cleanup_restores_default_keychain_search_list_and_deletes_credentials(
    tmp_path: Path, macos: None, fake_kwargs: dict[str, Any], restored: str
) -> None:
    fake = FakeToolchain(**fake_kwargs)
    if fake_kwargs:
        with pytest.raises(REHEARSAL.RehearsalError):
            _run_rehearsal(tmp_path, fake)
    else:
        _run_rehearsal(tmp_path, fake)

    # Whatever the rehearsal changed, it puts back. The deletion is always
    # attempted once creation was flagged, even if the create command failed.
    restored_search, restored_default = _restore_commands(fake.commands)
    if restored == "none":
        assert restored_search == []
        assert restored_default == []
    elif restored == "default":
        assert restored_search == []
        assert restored_default == [PRIOR_DEFAULT]
    else:
        assert restored_search == PRIOR_SEARCH
        assert restored_default == [PRIOR_DEFAULT]

    deletions = [cmd for cmd in fake.commands if cmd[1:2] == ["delete-keychain"]]
    assert len(deletions) == 1
    assert deletions[0][-1].startswith(str(tmp_path / "rehearsal"))
    assert fake.commands[-1][1:2] == ["delete-keychain"]
    for cmd in deletions:
        assert str(tmp_path) in cmd[-1]

    # Credential material is gone from every path, including the API key.
    credentials = tmp_path / "rehearsal" / "credentials"
    assert not credentials.exists()
    assert not (credentials / "certificate.p12").exists()
    assert not (credentials / "notary-key.p8").exists()

    if restored == "none":
        return

    # The ephemeral keychain was default and sole search target before import.
    default_set = next(
        index
        for index, cmd in enumerate(fake.commands)
        if cmd[:4] == ["security", "default-keychain", "-d", "user"] and "-s" in cmd
    )
    assert fake.commands[default_set][-1] == fake.keychain
    families = _families(fake.commands)
    if "security:import" in families:
        assert default_set < _index(families, "security:import")

def test_cleanup_failure_is_reported_alongside_an_inflight_failure(
    tmp_path: Path, macos: None
) -> None:
    fake = FakeToolchain(sign_rc=1, restore_search_rc=1)
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake)
    message = str(caught.value)
    # Both are reported, sanitized: the build failure and the cleanup failure.
    assert "signature could not be applied" in message
    assert "cleanup also failed" in message
    assert "search list could not be restored" in message
    for value in (
        P12_PASSWORD,
        NOTARY_KEY_PEM.decode(),
        SENTINEL_KEY_MATERIAL,
        TAURI_KEY,
        TAURI_PASSWORD,
    ):
        assert value not in message


def test_cleanup_failure_after_success_fails_the_run(
    tmp_path: Path, macos: None
) -> None:
    fake = FakeToolchain(restore_default_rc=1)
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake)
    assert "cleanup failed" in str(caught.value)
    assert "default keychain could not be restored" in str(caught.value)


def test_cleanup_only_failure_names_the_cleanup_stage(
    tmp_path: Path, macos: None
) -> None:
    fake = FakeToolchain(restore_search_rc=1)
    with pytest.raises(REHEARSAL.RehearsalError):
        _run_rehearsal(tmp_path, fake)
    report = json.loads(
        (tmp_path / "rehearsal" / REHEARSAL.VERIFICATION_NAME).read_text(encoding="utf-8")
    )
    assert report["complete"] is False
    assert report["failure"] == {"stage": "cleanup", "code": "cleanup_failed"}


def test_sigint_during_signing_restores_the_keychain(
    tmp_path: Path, macos: None
) -> None:
    interrupted = {"sent": False}

    def interrupt(cmd: list[str], _kwargs: dict[str, Any]) -> None:
        if cmd[:3] == ["codesign", "--force", "--sign"] and not interrupted["sent"]:
            interrupted["sent"] = True
            # Deliver SIGINT while the signing tool is running. The rehearsal's
            # handler must restore the keychain and fail this run; the default
            # handler would raise KeyboardInterrupt instead.
            signal.raise_signal(signal.SIGINT)

    fake = FakeToolchain(on_command=interrupt)
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake)
    assert "interrupted" in str(caught.value)
    assert interrupted["sent"]

    # The interrupted signing child was terminated and reaped before any
    # keychain state was restored.
    terminations = [event for event in fake.events if event[0] == "terminate"]
    assert terminations and terminations[0][1] == "codesign:--force"
    assert ("reaped", "codesign:--force") in fake.events
    first_restore = next(
        index for index, event in enumerate(fake.events) if event[0] == "restore"
    )
    assert fake.events.index(terminations[0]) < first_restore
    assert fake.events.index(("reaped", "codesign:--force")) < first_restore

    restored_search, restored_default = _restore_commands(fake.commands)
    assert restored_search == PRIOR_SEARCH
    assert restored_default == [PRIOR_DEFAULT]
    assert any(cmd[1:2] == ["delete-keychain"] for cmd in fake.commands)
    credentials = tmp_path / "rehearsal" / "credentials"
    assert not credentials.exists()
    assert not (credentials / "notary-key.p8").exists()


def test_sigterm_and_sigint_handlers_are_installed_during_the_rehearsal(
    tmp_path: Path, macos: None
) -> None:
    observed: dict[int, Any] = {}

    def record(cmd: list[str], _kwargs: dict[str, Any]) -> None:
        if cmd[:3] == ["codesign", "--force", "--sign"] and not observed:
            observed[signal.SIGTERM] = signal.getsignal(signal.SIGTERM)
            observed[signal.SIGINT] = signal.getsignal(signal.SIGINT)

    fake = FakeToolchain(on_command=record)
    _run_rehearsal(tmp_path, fake)
    assert observed[signal.SIGTERM] not in (signal.SIG_DFL, signal.SIG_IGN, None)
    assert observed[signal.SIGINT] not in (signal.SIG_DFL, signal.SIG_IGN, None)


def test_sigint_during_keychain_settings_terminates_the_child_first(
    tmp_path: Path, macos: None
) -> None:
    sent = {"once": False}

    def interrupt(cmd: list[str], _kwargs: dict[str, Any]) -> None:
        if cmd[1:2] == ["set-keychain-settings"] and not sent["once"]:
            sent["once"] = True
            # Interrupt while a keychain-setting command is applying its change.
            signal.raise_signal(signal.SIGINT)

    fake = FakeToolchain(on_command=interrupt)
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake)
    assert "interrupted" in str(caught.value)
    terminations = [event for event in fake.events if event[0] == "terminate"]
    assert terminations and terminations[0][1] == "security:set-keychain-settings"
    assert ("reaped", "security:set-keychain-settings") in fake.events
    first_restore = next(
        index for index, event in enumerate(fake.events) if event[0] == "restore"
    )
    assert fake.events.index(terminations[0]) < first_restore
    assert fake.events.index(("reaped", "security:set-keychain-settings")) < first_restore
    credentials = tmp_path / "rehearsal" / "credentials"
    assert not credentials.exists()
    assert not (credentials / "notary-key.p8").exists()


def test_child_that_ignores_terminate_is_killed_and_reaped(
    tmp_path: Path, macos: None
) -> None:
    sent = {"once": False}

    def interrupt(cmd: list[str], _kwargs: dict[str, Any]) -> None:
        if cmd[:3] == ["codesign", "--force", "--sign"] and not sent["once"]:
            sent["once"] = True
            signal.raise_signal(signal.SIGINT)

    fake = FakeToolchain(on_command=interrupt, child_ignore_terminate=True)
    with pytest.raises(REHEARSAL.RehearsalError):
        _run_rehearsal(tmp_path, fake)
    kills = [event for event in fake.events if event[0] == "kill"]
    assert kills and kills[0][1] == "codesign:--force"
    assert ("reaped", "codesign:--force") in fake.events


def test_second_signal_during_cleanup_is_deferred(
    tmp_path: Path, macos: None
) -> None:
    state = {"first": False, "second": False}

    def interrupt(cmd: list[str], _kwargs: dict[str, Any]) -> None:
        if cmd[:3] == ["codesign", "--force", "--sign"] and not state["first"]:
            state["first"] = True
            signal.raise_signal(signal.SIGINT)
        elif (
            state["first"]
            and not state["second"]
            and cmd[:5] == ["security", "list-keychains", "-d", "user", "-s"]
        ):
            state["second"] = True
            # A second signal while cleanup is running must not abandon it.
            signal.raise_signal(signal.SIGINT)

    fake = FakeToolchain(on_command=interrupt)
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake)
    assert "interrupted" in str(caught.value)
    # The deferred signal did not abort cleanup: no cleanup failure is reported.
    assert "cleanup also failed" not in str(caught.value)
    assert state["first"] and state["second"]
    restored_search, restored_default = _restore_commands(fake.commands)
    assert restored_search == PRIOR_SEARCH
    assert restored_default == [PRIOR_DEFAULT]
    assert any(cmd[:2] == ["security", "delete-keychain"] for cmd in fake.commands)
    assert not (tmp_path / "rehearsal" / "credentials").exists()


# --- sanitized failure report ----------------------------------------------


@pytest.mark.parametrize(
    ("fake_kwargs", "stage", "code"),
    [
        ({"notary_status": "Invalid"}, "build", "build_failed"),
        ({"fault": {"stage": "extracted", "gatekeeper": 1}}, "extracted_verification", "extracted_verification_failed"),
        ({"ownership_rc": 1}, "ownership", "ownership_failed"),
        ({"manifest_verify_rc": 1}, "manifest_verification", "manifest_verification_failed"),
        ({"manifest_team": "ZZZZZZZZZZ"}, "manifest_authentication", "manifest_authentication_failed"),
        ({"signer_rc": 1}, "manifest_signature", "manifest_signature_failed"),
    ],
)
def test_failure_writes_an_incomplete_sanitized_report(
    tmp_path: Path,
    macos: None,
    fake_kwargs: dict[str, Any],
    stage: str,
    code: str,
) -> None:
    fake = FakeToolchain(**_sentinel_fake_kwargs(), **fake_kwargs)
    wheel, interpreter = _paths(tmp_path)
    output = tmp_path / "rehearsal"
    exit_code = REHEARSAL.main(
        [
            "--output",
            str(output),
            "--python",
            str(interpreter),
            "--source-sha",
            SOURCE_SHA,
            "--wheel",
            str(wheel),
        ],
        runner=fake,
        children=fake.children,
        environ=_sentinel_environment(),
    )
    assert exit_code == 1

    report_path = output / REHEARSAL.VERIFICATION_NAME
    summary_path = output / REHEARSAL.SUMMARY_NAME
    assert report_path.is_file(), "a failed run left no failure report"
    assert summary_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["complete"] is False
    assert report["failure"] == {"stage": stage, "code": code}
    assert report["source_sha"] == SOURCE_SHA
    for value in (
        list(SENTINELS.values())
        + [P12_PASSWORD, SENTINEL_KEY_MATERIAL, TAURI_KEY, TAURI_PASSWORD]
    ):
        assert value not in report_path.read_text(encoding="utf-8")
        assert value not in summary_path.read_text(encoding="utf-8")
    assert IDENTITY not in report_path.read_text(encoding="utf-8")
    assert TEAM_ID not in report_path.read_text(encoding="utf-8")
    summary = summary_path.read_text(encoding="utf-8")
    assert "FAILED" in summary and stage in summary and code in summary


def test_success_report_and_failure_report_cannot_be_confused(
    tmp_path: Path, macos: None
) -> None:
    fake = FakeToolchain()
    result = _run_rehearsal(tmp_path, fake)
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["complete"] is True
    assert "failure" not in report


# --- keychain passwords stay off argv --------------------------------------


def test_keychain_and_notary_profile_are_fresh_per_run(
    tmp_path: Path, macos: None
) -> None:
    runs: list[tuple[str, str]] = []
    for name in ("first", "second"):
        fake = FakeToolchain()
        _run_rehearsal(tmp_path, fake, name=name)
        assert fake.keychain_password is not None
        assert len(fake.keychain_password) >= 32
        assert fake.profile is not None
        assert fake.profile.startswith(REHEARSAL.PROFILE_PREFIX)
        assert fake.profile != REHEARSAL.PROFILE_PREFIX
        runs.append((fake.keychain_password, fake.profile))
    assert runs[0] != runs[1], "two runs reused the same keychain password or profile"


def _sentinel_fake_kwargs() -> dict[str, str]:
    """The fake's expectations for the sentinel credential environment."""
    return {
        "identity": SENTINELS["SERVER_HOST_SIGNING_IDENTITY"],
        "notary_key": SENTINEL_KEY_PEM,
        "key_id": SENTINELS["SERVER_HOST_NOTARY_KEY_ID"],
        "issuer_id": SENTINELS["SERVER_HOST_NOTARY_ISSUER_ID"],
        "tauri_key": SENTINELS["TAURI_SIGNING_PRIVATE_KEY"],
        "tauri_password": SENTINELS["TAURI_SIGNING_PRIVATE_KEY_PASSWORD"],
    }


def _sentinel_environment(**overrides: str | None) -> dict[str, str]:
    values: dict[str, str | None] = dict(SENTINELS)
    values.update(
        {
            "SERVER_HOST_TEAM_ID": TEAM_ID,
            "SERVER_HOST_CERTIFICATE_P12_BASE64": SENTINEL_CERTIFICATE_TEXT,
            "PATH": "/usr/bin:/bin",
        }
    )
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


@pytest.mark.parametrize(
    ("fake_kwargs", "stage"),
    [
        ({"import_rc": 1}, "keychain"),
        ({"notary_status": "Invalid"}, "build"),
        ({"fault": {"stage": "extracted", "gatekeeper": 1}}, "extracted_verification"),
        ({"manifest_team": "ZZZZZZZZZZ"}, "manifest_authentication"),
        ({"signer_rc": 1}, "manifest_signature"),
        ({"restore_search_rc": 1}, "cleanup"),
    ],
)
def test_authentication_values_do_not_leak_through_logs_or_exceptions(
    tmp_path: Path,
    macos: None,
    capsys: pytest.CaptureFixture[str],
    fake_kwargs: dict[str, Any],
    stage: str,
) -> None:
    fake = FakeToolchain(**_sentinel_fake_kwargs(), **fake_kwargs)
    wheel, interpreter = _paths(tmp_path)
    exit_code = REHEARSAL.main(
        [
            "--output",
            str(tmp_path / "rehearsal"),
            "--python",
            str(interpreter),
            "--source-sha",
            SOURCE_SHA,
            "--wheel",
            str(wheel),
        ],
        runner=fake,
        children=fake.children,
        environ=_sentinel_environment(),
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error:" in captured.err
    # Each case reached the stage it is meant to exercise, rather than failing
    # early for an unrelated reason.
    report = json.loads(
        (tmp_path / "rehearsal" / REHEARSAL.VERIFICATION_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert report["failure"]["stage"] == stage

    text = captured.out + captured.err
    for value in SENTINELS.values():
        assert value not in text
    assert "SENTINEL-CERTIFICATE-8A44" not in text
    assert SENTINEL_KEY_MATERIAL not in text
    assert SENTINEL_KEY_PEM.decode() not in text

    for path in (tmp_path / "rehearsal").rglob("*"):
        if path.is_file():
            haystack = path.read_bytes()
            for value in list(SENTINELS.values()) + [SENTINEL_KEY_MATERIAL]:
                assert value.encode() not in haystack, path
            assert SENTINEL_KEY_PEM not in haystack, path


def test_fake_failure_argv_and_stderr_values_never_reach_output(
    tmp_path: Path, macos: None
) -> None:
    # A real tool failure carries its argv (the p12 password) and its stderr
    # (which may quote a credential) in the exception. Neither may reach a log,
    # an exception message or a runner-local report.
    stderr_secret = "SENTINEL-FAILURE-STDERR-3C9F"

    class LeakyFailure(FakeToolchain):
        def _security(self, cmd: list[str], kwargs: dict[str, Any]) -> Any:
            if cmd[1:2] == ["import"]:
                raise subprocess.CalledProcessError(
                    1, cmd, output=stderr_secret, stderr=f"failed: {stderr_secret}"
                )
            return super()._security(cmd, kwargs)

    fake = LeakyFailure()
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake)
    message = str(caught.value)
    assert stderr_secret not in message
    assert P12_PASSWORD not in message
    output = tmp_path / "rehearsal"
    for path in output.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert stderr_secret.encode() not in data, path
            assert P12_PASSWORD.encode() not in data, path

def test_credential_repr_is_empty(tmp_path: Path, macos: None) -> None:
    credentials = REHEARSAL._credentials(_sentinel_environment())
    rendered = repr(credentials)
    for value in SENTINELS.values():
        assert value not in rendered
    assert "SENTINEL-CERTIFICATE-8A44" not in rendered


# --- runner-local output ----------------------------------------------------


def test_candidate_files_stay_runner_local_and_only_sanitized_summary_is_emitted(
    tmp_path: Path,
    macos: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeToolchain(**_sentinel_fake_kwargs())
    wheel, interpreter = _paths(tmp_path)
    output = tmp_path / "rehearsal"
    exit_code = REHEARSAL.main(
        [
            "--output",
            str(output),
            "--python",
            str(interpreter),
            "--source-sha",
            SOURCE_SHA,
            "--wheel",
            str(wheel),
        ],
        runner=fake,
        children=fake.children,
        environ=_sentinel_environment(),
    )
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    assert "server host signing rehearsal: verified" in captured.out
    assert SOURCE_SHA in captured.out

    # No distribution: no release upload, no artifact upload, no network writer.
    joined = "\n".join(" ".join(cmd) for cmd in fake.commands)
    for forbidden in (
        "gh release",
        "upload-artifact",
        "curl ",
        "aws s3",
        "--clobber",
        "git push",
        "scp ",
    ):
        assert forbidden not in joined, forbidden

    # The summary and report are sanitized: no secret, no identity, no team.
    summary_text = (output / REHEARSAL.SUMMARY_NAME).read_text(encoding="utf-8")
    report_text = (output / REHEARSAL.VERIFICATION_NAME).read_text(encoding="utf-8")
    for text in (captured.out, captured.err, summary_text, report_text):
        for value in SENTINELS.values():
            assert value not in text
        assert IDENTITY not in text
        assert TEAM_ID not in text
    report = json.loads(report_text)
    assert set(report) == {
        "schema",
        "kind",
        "complete",
        "source_sha",
        "policy",
        "notarization",
        "archive",
        "steps",
    }
    assert report["complete"] is True
    assert report["source_sha"] == SOURCE_SHA
    assert report["notarization"] == {"status": "Accepted"}
    assert report["policy"] == {
        "host_revision": SERVER_HOST.SIGNED_HOST_REVISION,
        "host_protocol": SERVER_HOST.HOST_PROTOCOL,
        "signing_kind": SERVER_HOST.DEVELOPER_ID_SIGNING_KIND,
    }
    assert all(state is True for state in report["steps"].values())

    # Candidate files stay in the scratch directory, which the caller removes.
    assert (output / "build" / REHEARSAL.ARCHIVE_NAME).is_file()
    assert (output / "ciaobot-engine-manifest.json").is_file()
    assert (output / "ciaobot-engine-manifest.json.sig").is_file()
    assert (output / REHEARSAL.OWNERSHIP_MARKER).is_file()
    assert stat.S_IMODE((output / REHEARSAL.VERIFICATION_NAME).stat().st_mode) == 0o600


# --- scratch ownership round trip ------------------------------------------


def test_expected_team_round_trips_through_scratch_ownership(
    tmp_path: Path, macos: None
) -> None:
    fake = FakeToolchain()
    result = _run_rehearsal(tmp_path, fake)
    output = tmp_path / "rehearsal"
    record_path = output / "home" / REHEARSAL.RECORD_RELATIVE
    assert record_path.is_file()
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    document = json.loads(record_path.read_text(encoding="utf-8"))
    assert document["schema"] == SERVER_HOST.SIGNED_SCHEMA_VERSION
    assert document["host_revision"] == SERVER_HOST.SIGNED_HOST_REVISION
    assert document["host_protocol"] == SERVER_HOST.HOST_PROTOCOL
    assert document["signing"] == {
        "kind": SERVER_HOST.DEVELOPER_ID_SIGNING_KIND,
        "team_id": TEAM_ID,
    }
    assert Path(document["bundle_path"]).is_relative_to(output.resolve())
    assert Path(document["bundle_path"]).name == APP_NAME

    # The runtime verifier reads the record back under the signed policy and
    # accepts the same snapshot the rehearsal recorded.
    verified = REAL_VERIFY_OWNED_HOST(
        Path(document["bundle_path"]),
        ownership_path=record_path,
        runner=fake,
    )
    assert verified.to_record() == document
    assert verified.signing == SERVER_HOST.DeveloperIDSigning(TEAM_ID)

    # The fixture proof is runner-local: the record and the extracted app live
    # under the scratch directory, never in a real home.
    assert result.report_path.is_relative_to(output)
    assert (output / "verify-final" / APP_NAME).is_dir()


# --- entry point ------------------------------------------------------------


def test_entry_point_reports_missing_input_without_running(
    tmp_path: Path, macos: None, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel, interpreter = _paths(tmp_path)
    fake = FakeToolchain()
    exit_code = REHEARSAL.main(
        [
            "--output",
            str(tmp_path / "rehearsal"),
            "--python",
            str(interpreter),
            "--source-sha",
            SOURCE_SHA,
            "--wheel",
            str(wheel),
        ],
        runner=fake,
        environ=_environment(SERVER_HOST_TEAM_ID=None),
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "SERVER_HOST_TEAM_ID" in captured.err
    assert fake.commands == []
