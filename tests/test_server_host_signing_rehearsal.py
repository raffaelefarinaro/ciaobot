"""Checks for the signed server host signing rehearsal (#2331).

The rehearsal is CI orchestration: a temporary signing keychain, the explicit
Developer ID release build, a signed candidate manifest and an independent
verification of the final extracted app. None of that can run here, so every
native tool is a strict fake that recognizes exact argv and fails on anything
else. The build, notarization, keychain and upload paths in this file never
touch a real certificate, a real keychain, the network or a release.

The checks here are the ones a static YAML read cannot make: the argument and
side-effect order, the fail-closed behaviour of each stage, unconditional
keychain cleanup, credentials never reaching an output or an exception, and a
verification that runs against the extracted archive rather than the build
directory.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
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
SENTINELS = {
    "SERVER_HOST_SIGNING_IDENTITY": "SENTINEL-IDENTITY-4F7A",
    "SERVER_HOST_CERTIFICATE_PASSWORD": "SENTINEL-CERT-PASSWORD-9C2D",
    "SERVER_HOST_NOTARY_APPLE_ID": "sentinel-apple-id-7b3e@example.invalid",
    "SERVER_HOST_NOTARY_APP_PASSWORD": "SENTINEL-NOTARY-PASSWORD-1D6F",
    "TAURI_SIGNING_PRIVATE_KEY": "SENTINEL-TAURI-KEY-5A1C",
    "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": "SENTINEL-TAURI-PASSWORD-2E8B",
}
SENTINEL_CERTIFICATE_TEXT = base64.b64encode(b"SENTINEL-CERTIFICATE-8A44").decode()

# Native-shaped fixture data. The bundle identifier, protocol and minimum macOS
# version are the runtime contract's; the signature details are written out
# literally so a shared change fails these tests instead of moving the fake.
BUNDLE_ID = "local.ciaobot.server"
EXECUTABLE = "Ciaobot Server"
APP_NAME = "Ciaobot Server.app"
BUNDLE_DISPLAY = (
    f"Identifier={BUNDLE_ID}\n"
    "Format=app bundle with Mach-O universal (arm64 x86_64)\n"
    "Signature=Developer ID Application\n"
)
CODE_DIRECTORY = (
    "v=20500 size=400 flags=0x10000(runtime) hashes=4+3 location=embedded"
)
NO_RUNTIME_FLAGS = "v=20500 size=400 flags=0x0(none) hashes=4+3 location=embedded"
ENTITLEMENTS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0">\n<dict>\n'
    "\t<key>com.apple.security.automation.apple-events</key>\n\t<true/>\n"
    "</dict>\n</plist>\n"
)


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
        "SERVER_HOST_CERTIFICATE_PASSWORD": "p12-import-password",
        "SERVER_HOST_NOTARY_APPLE_ID": "release@example.invalid",
        "SERVER_HOST_NOTARY_APP_PASSWORD": "app-specific-password",
        "TAURI_SIGNING_PRIVATE_KEY": "tauri-private-key",
        "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": "tauri-key-password",
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


class FakeToolchain:
    """A strict native toolchain: exact argv, realistic output, no fallbacks.

    Every invocation is recorded. Unknown argv raises, so a changed command in
    the rehearsal fails here instead of silently running something else. The
    ``fault`` mapping applies only to the matching artifact stage, which is how
    a test can prove the helper verifies the extracted archive rather than the
    build directory.
    """

    def __init__(
        self,
        *,
        team_id: str = TEAM_ID,
        identity: str = IDENTITY,
        fault: dict[str, Any] | None = None,
        notary_status: Any = "Accepted",
        notary_stdout: str | None = None,
        notary_rc: int = 0,
        import_rc: int = 0,
        create_rc: int = 0,
        sign_rc: int = 0,
        identity_listed: bool = True,
        ticket: bytes | None = b"stapled-ticket-bytes",
        verify_rc: int = 0,
        manifest_team: str | None = None,
        manifest_sha256: str | None = None,
        manifest_size: int | None = None,
        drop_host_entry: bool = False,
        mutate_archive_after_sign: bool = False,
        signer_rc: int = 0,
    ) -> None:
        self.team_id = team_id
        self.identity = identity
        self.fault = fault or {}
        self.notary_status = notary_status
        self.notary_stdout = notary_stdout
        self.notary_rc = notary_rc
        self.import_rc = import_rc
        self.create_rc = create_rc
        self.sign_rc = sign_rc
        self.identity_listed = identity_listed
        self.ticket = ticket
        self.verify_rc = verify_rc
        self.manifest_team = manifest_team
        self.manifest_sha256 = manifest_sha256
        self.manifest_size = manifest_size
        self.drop_host_entry = drop_host_entry
        self.mutate_archive_after_sign = mutate_archive_after_sign
        self.signer_rc = signer_rc
        self.commands: list[list[str]] = []
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.profile: str | None = None
        self.keychain: str | None = None
        self.archive: Path | None = None

    # -- plumbing ----------------------------------------------------------

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(cmd))
        self.calls.append((list(cmd), dict(kwargs)))
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
            Path(cmd[5]).write_bytes(b"temporary-zip")
            return self._done(cmd, kwargs)
        if tool == "spctl":
            return self._done(cmd, kwargs, returncode=self._stage_rc("gatekeeper", cmd[-1]))
        if tool == "npx":
            return self._signer(cmd, kwargs)
        if cmd[0].endswith("python") and cmd[1:3] == ["-m", "ciao.release_manifest"]:
            return self._manifest(cmd, kwargs)
        raise AssertionError(f"unexpected rehearsal argv: {cmd}")

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
            listed = "".join(f'    "{path}"\n' for path in PRIOR_SEARCH)
            return self._done(cmd, kwargs, stdout=listed)
        if action == "default-keychain" and "-s" not in cmd:
            return self._done(cmd, kwargs, stdout=f'    "{PRIOR_DEFAULT}"\n')
        if action in (
            "create-keychain",
            "set-keychain-settings",
            "unlock-keychain",
            "set-key-partition-list",
        ) or "-s" in cmd:
            if action == "create-keychain":
                if self.create_rc:
                    return self._done(cmd, kwargs, returncode=self.create_rc)
                self.keychain = cmd[-1]
                Path(cmd[-1]).write_bytes(b"fake-keychain")
            return self._done(cmd, kwargs)
        if action == "import":
            return self._done(cmd, kwargs, returncode=self.import_rc)
        if action == "find-identity":
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
            Path(cmd[-1]).unlink(missing_ok=True)
            return self._done(cmd, kwargs)
        raise AssertionError(f"unexpected security argv: {cmd}")

    # -- xcrun -------------------------------------------------------------

    def _xcrun(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "swiftc":
            Path(cmd[7]).write_bytes(b"thin-binary")
            return self._done(cmd, kwargs)
        if cmd[1] == "notarytool":
            if cmd[2] == "store-credentials":
                self.profile = cmd[3]
                self.keychain = cmd[cmd.index("--keychain") + 1]
                return self._done(cmd, kwargs)
            assert cmd[2] == "submit", cmd
            assert cmd[cmd.index("--keychain-profile") + 1] == self.profile, cmd
            stdout = (
                self.notary_stdout
                if self.notary_stdout is not None
                else json.dumps({"id": "submission-1", "status": self.notary_status})
            )
            return self._done(
                cmd, kwargs, returncode=self.notary_rc, stdout=stdout
            )
        if cmd[1] == "stapler":
            target = Path(cmd[3])
            if cmd[2] == "staple":
                if self.ticket is not None:
                    ticket = target / "Contents" / "CodeResources"
                    ticket.parent.mkdir(parents=True, exist_ok=True)
                    ticket.write_bytes(self.ticket)
                return self._done(cmd, kwargs)
            assert cmd[2] == "validate", cmd
            return self._done(
                cmd, kwargs, returncode=self._stage_rc("staple_validate", target)
            )
        raise AssertionError(f"unexpected xcrun argv: {cmd}")

    # -- codesign ----------------------------------------------------------

    def _codesign(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "--force":
            app = Path(cmd[-1])
            seal = app / "Contents" / "_CodeSignature" / "CodeResources"
            seal.parent.mkdir(parents=True, exist_ok=True)
            seal.write_bytes(b"sealed-resources")
            return self._done(cmd, kwargs, returncode=self.sign_rc)
        if cmd[1] == "--verify":
            return self._done(
                cmd, kwargs, returncode=max(self.verify_rc, self._stage_rc("verify", cmd[-1]))
            )
        if cmd[1] == "-dv":
            if "--arch" in cmd:
                arch = cmd[cmd.index("--arch") + 1]
                return self._done(cmd, kwargs, stderr=self._slice_display(arch, cmd[-1]))
            assert cmd[-1].endswith(".app"), cmd
            return self._done(cmd, kwargs, stderr=BUNDLE_DISPLAY)
        if cmd[1] == "-d" and cmd[2] == "--entitlements":
            arch = cmd[cmd.index("--arch") + 1]
            fault = self._fault(cmd[-1])
            if "entitlements" in fault:
                return self._done(cmd, kwargs, stdout=fault["entitlements"])
            assert arch in SERVER_HOST.ARCHITECTURES, cmd
            return self._done(cmd, kwargs, stdout=ENTITLEMENTS_XML)
        if cmd[1] == "-d" and cmd[2] == "-r-":
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
            Path(cmd[3]).write_bytes(b"universal-binary")
            return self._done(cmd, kwargs)
        assert cmd[1] == "-archs", cmd
        return self._done(cmd, kwargs, stdout="arm64 x86_64\n")

    # -- manifest, signer --------------------------------------------------

    def _manifest(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        subcommand = cmd[3]
        if subcommand == "build":
            return self._manifest_build(cmd, kwargs)
        assert subcommand == "verify", cmd
        return self._done(cmd, kwargs, stdout=f"verified {VERSION}: trusted comment\n")

    def _manifest_build(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--out") + 1])
        archive = Path(cmd[cmd.index("--server-host") + 1])
        team_id = cmd[cmd.index("--server-host-team-id") + 1]
        wheel = Path(cmd[-1])
        self.archive = archive
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

    def _signer(
        self, cmd: list[str], kwargs: dict[str, Any]
    ) -> subprocess.CompletedProcess[str]:
        assert cmd[1:5] == ["--yes", REHEARSAL.MANIFEST_SIGNER, "signer", "sign"], cmd
        manifest = Path(cmd[5])
        if self.signer_rc:
            return self._done(cmd, kwargs, returncode=self.signer_rc)
        manifest.with_name(manifest.name + ".sig").write_text(
            "untrusted comment: fake candidate signature\n"
        )
        if self.mutate_archive_after_sign:
            assert self.archive is not None
            self.archive.write_bytes(self.archive.read_bytes() + b"tampered")
        return self._done(cmd, kwargs)


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
        elif tool == "npx":
            families.append("npx")
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
        SERVER_HOST_TEAM_ID=None, SERVER_HOST_NOTARY_APP_PASSWORD=None
    )
    fake = FakeToolchain()
    with pytest.raises(REHEARSAL.RehearsalError) as caught:
        _run_rehearsal(tmp_path, fake, environ=environ)
    message = str(caught.value)
    assert "SERVER_HOST_TEAM_ID" in message
    assert "SERVER_HOST_NOTARY_APP_PASSWORD" in message
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
    manifest_verify = _index(families, "manifest:verify")
    assert sign < notary < staple < validate < manifest_build < manifest_verify

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
    assert not (tmp_path / "rehearsal" / REHEARSAL.VERIFICATION_NAME).exists()


def test_manifest_is_signed_with_the_pinned_signer_and_existing_key(
    tmp_path: Path, macos: None
) -> None:
    fake = FakeToolchain()
    _run_rehearsal(tmp_path, fake)
    signers = [cmd for cmd in fake.commands if Path(cmd[0]).name == "npx"]
    assert len(signers) == 1
    assert signers[0][2] == "@tauri-apps/cli@2.11.4"
    signer_call = next(
        kwargs for cmd, kwargs in fake.calls if Path(cmd[0]).name == "npx"
    )
    assert signer_call["env"]["TAURI_SIGNING_PRIVATE_KEY"] == "tauri-private-key"
    assert "PYTHONPATH" not in signer_call["env"]

    # The installed clean-wheel verifier runs the manifest, not the checkout.
    for cmd, kwargs in fake.calls:
        if cmd[1:3] == ["-m", "ciao.release_manifest"]:
            assert "PYTHONPATH" not in kwargs["env"]
            assert kwargs["env"]["HOME"].startswith(str(tmp_path / "rehearsal"))
            assert kwargs["cwd"] == str(tmp_path / "rehearsal")
            # The verifier's built-in release key is the trust root; the
            # rehearsal never substitutes another one.
            assert "--public-key" not in cmd


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


@pytest.mark.parametrize(
    ("fake_kwargs", "keychain_created"),
    [
        ({}, True),
        ({"create_rc": 1}, False),
        ({"import_rc": 1}, True),
        ({"sign_rc": 1}, True),
        ({"notary_status": "Invalid"}, True),
        ({"fault": {"stage": "extracted", "gatekeeper": 1}}, True),
        ({"manifest_team": "ZZZZZZZZZZ"}, True),
        ({"signer_rc": 1}, True),
    ],
)
def test_cleanup_restores_default_keychain_search_list_and_deletes_credentials(
    tmp_path: Path, macos: None, fake_kwargs: dict[str, Any], keychain_created: bool
) -> None:
    fake = FakeToolchain(**fake_kwargs)
    if fake_kwargs:
        with pytest.raises(REHEARSAL.RehearsalError):
            _run_rehearsal(tmp_path, fake)
    else:
        _run_rehearsal(tmp_path, fake)

    # Whatever the rehearsal changed, it puts back. A failure before the
    # keychain exists changed nothing, so it restores nothing and deletes nothing.
    restored_search, restored_default = _restore_commands(fake.commands)
    deletions = [cmd for cmd in fake.commands if cmd[1:2] == ["delete-keychain"]]
    if not keychain_created:
        assert restored_search == []
        assert restored_default == []
        assert deletions == []
        assert not (tmp_path / "rehearsal" / "credentials").exists()
        return

    assert restored_search == PRIOR_SEARCH
    assert restored_default == [PRIOR_DEFAULT]
    assert len(deletions) == 1
    deleted = deletions[0][-1]
    assert deleted.startswith(str(tmp_path / "rehearsal"))

    # The ephemeral keychain was default and sole search target before import.
    families = _families(fake.commands)
    default_set = next(
        index
        for index, cmd in enumerate(fake.commands)
        if cmd[:4] == ["security", "default-keychain", "-d", "user"] and "-s" in cmd
    )
    import_index = _index(families, "security:import")
    assert default_set < import_index
    keychain = fake.commands[default_set][-1]
    assert keychain.startswith(str(tmp_path / "rehearsal"))
    assert keychain == fake.keychain

    # Credential material is gone from every path, and cleanup ran last.
    credentials = tmp_path / "rehearsal" / "credentials"
    assert not credentials.exists()
    assert not (credentials / "certificate.p12").exists()
    assert fake.commands[-1][1:2] == ["delete-keychain"]
    # Nothing outside the fresh scratch directory is deleted.
    for cmd in fake.commands:
        if cmd[1:2] in (["delete-keychain"],):
            assert str(tmp_path) in cmd[-1]


def test_keychain_and_notary_profile_are_fresh_per_run(
    tmp_path: Path, macos: None
) -> None:
    runs: list[tuple[str, str]] = []
    for name in ("first", "second"):
        fake = FakeToolchain()
        _run_rehearsal(tmp_path, fake, name=name)
        create = next(
            cmd for cmd in fake.commands if cmd[1:2] == ["create-keychain"]
        )
        store = next(
            cmd for cmd in fake.commands if cmd[2:3] == ["store-credentials"]
        )
        password = create[3]
        profile = store[store.index("store-credentials") + 1]
        assert len(password) >= 32, "the keychain password is not random enough"
        assert profile.startswith(REHEARSAL.PROFILE_PREFIX)
        assert profile != REHEARSAL.PROFILE_PREFIX, "the profile name is not random"
        runs.append((password, profile))
    assert runs[0] != runs[1], "two runs reused the same keychain password or profile"


# --- credentials ------------------------------------------------------------

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
    "fake_kwargs",
    [
        {"import_rc": 1},
        {"notary_status": "Invalid"},
        {"fault": {"stage": "extracted", "gatekeeper": 1}},
        {"manifest_team": "ZZZZZZZZZZ"},
        {"signer_rc": 1},
    ],
)
def test_authentication_values_do_not_leak_through_logs_or_exceptions(
    tmp_path: Path,
    macos: None,
    capsys: pytest.CaptureFixture[str],
    fake_kwargs: dict[str, Any],
) -> None:
    fake = FakeToolchain(
        **fake_kwargs, identity=SENTINELS["SERVER_HOST_SIGNING_IDENTITY"]
    )
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
        environ=_sentinel_environment(),
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error:" in captured.err
    text = captured.out + captured.err
    for value in SENTINELS.values():
        assert value not in text
    assert "SENTINEL-CERTIFICATE-8A44" not in text

    report = tmp_path / "rehearsal" / REHEARSAL.VERIFICATION_NAME
    summary = tmp_path / "rehearsal" / REHEARSAL.SUMMARY_NAME
    assert not report.exists(), "an incomplete run wrote a verification report"
    assert not summary.exists()
    if (tmp_path / "rehearsal").exists():
        for path in (tmp_path / "rehearsal").rglob("*"):
            if path.is_file():
                haystack = path.read_bytes()
                for value in SENTINELS.values():
                    assert value.encode() not in haystack, path


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
    fake = FakeToolchain(identity=SENTINELS["SERVER_HOST_SIGNING_IDENTITY"])
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
    verified = SERVER_HOST.verify_owned_host(
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
