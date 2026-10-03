"""Checks for the verified server-host identity/ownership contract (#1023, B1).

The pure checks — command parsing, argv rendering, the constant contract and the
strict ownership-record decoder — run on every platform, using ``PurePosixPath``
macOS argv and Windows-safe temp record paths. The bundle inspector and the
ownership verifier run under an injected runner and a mocked ``sys.platform``
(so the ``codesign``/``lipo`` probes are proved without a real host), and the
macOS-only integration builds the real universal host into a fresh tmpdir and
verifies it. Nothing here installs a bundle, touches ``~/Applications``, an
engine, ``launchctl``, Login Items or TCC, or opens a permission prompt.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
from types import ModuleType
from typing import Any

import pytest

from ciao import server_host
from ciao.server_host import (
    APP_NAME,
    ARCHITECTURES,
    BUNDLE_ID,
    EXECUTABLE_NAME,
    HOST_PROTOCOL,
    HOST_PROTOCOL_KEY,
    HOST_REVISION,
    ICON_NAME,
    MINIMUM_SYSTEM_VERSION,
    SCHEMA_VERSION,
    HostOwnership,
    ServerHostError,
    host_service_argv,
    inspect_host_bundle,
    parse_service_command,
    read_host_ownership,
    verify_owned_host,
)

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build-server-host.py"
HOST_SOURCE = ROOT / "native" / "server-host" / "ServerHost.swift"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = _load("build_server_host_contract", BUILD_SCRIPT)

# The uid/mode half of the record reader is macOS-only in production. These
# tests mock ``sys.platform`` to ``darwin`` and so must not run on a native
# Windows interpreter, where ``os.getuid`` does not exist.
requires_posix_uid = pytest.mark.skipif(
    sys.platform == "win32", reason="mocking darwin needs a POSIX uid"
)

HAS_TOOLS = (
    sys.platform == "darwin"
    and shutil.which("xcrun") is not None
    and shutil.which("codesign") is not None
    and shutil.which("lipo") is not None
)
requires_macos_tools = pytest.mark.skipif(
    not HAS_TOOLS, reason="the native host build requires macOS Command Line Tools"
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_CDHASH = {arch: hashlib.sha1(arch.encode()).hexdigest() for arch in ARCHITECTURES}


# ── the constant contract ───────────────────────────────────────────────────


def test_constants_match_the_real_builder() -> None:
    for name in (
        "APP_NAME",
        "BUNDLE_ID",
        "EXECUTABLE_NAME",
        "ICON_NAME",
        "HOST_REVISION",
    ):
        assert getattr(server_host, name) == getattr(BUILD, name), name
    assert server_host.HOST_PROTOCOL == BUILD.HOST_PROTOCOL_REVISION
    assert server_host.HOST_PROTOCOL_KEY == BUILD.HOST_PROTOCOL_KEY
    assert server_host.MINIMUM_SYSTEM_VERSION == BUILD.MACOS_DEPLOYMENT_TARGET


def test_constants_agree_with_the_swift_host() -> None:
    import re

    source = HOST_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"^let HOST_PROTOCOL_REVISION = (\d+)$", source, re.MULTILINE)
    assert match is not None
    assert int(match.group(1)) == server_host.HOST_PROTOCOL
    match = re.search(r"^let STOP_GRACE_SECONDS = ([0-9.]+)$", source, re.MULTILINE)
    assert match is not None
    assert float(match.group(1)) == server_host.STOP_GRACE_SECONDS


def test_exit_timeout_outlasts_stop_grace_which_outlasts_supervisor() -> None:
    from ciao import supervise

    assert supervise.STOP_GRACE_S < server_host.STOP_GRACE_SECONDS
    assert server_host.STOP_GRACE_SECONDS < server_host.EXIT_TIMEOUT_SECONDS


def test_defaults_are_hardcoded_user_paths() -> None:
    # The defaults are absolute, home-anchored and hardcoded; the tests'
    # redirected HOME only proves the shape, not the machine's real home.
    assert server_host.DEFAULT_BUNDLE_PATH.is_absolute()
    assert server_host.DEFAULT_BUNDLE_PATH.name == APP_NAME
    assert server_host.DEFAULT_BUNDLE_PATH.parent.name == "Applications"
    assert server_host.DEFAULT_OWNERSHIP_PATH.is_absolute()
    assert server_host.DEFAULT_OWNERSHIP_PATH.name == "server-host.json"
    assert server_host.DEFAULT_OWNERSHIP_PATH.parts[-3:] == (
        "state",
        "ciaobot",
        "server-host.json",
    )
    assert server_host.SCHEMA_VERSION == 1
    assert server_host.NATIVE_TIMEOUT_SECONDS == 10


# ── syntax: parse_service_command ───────────────────────────────────────────


@pytest.mark.parametrize(
    "argv, program, python",
    [
        (
            ["/usr/bin/python3", "-m", "ciao.cli", "run"],
            "/usr/bin/python3",
            "/usr/bin/python3",
        ),
        (
            ["/usr/bin/python3", "-I", "-m", "ciao.cli", "supervise"],
            "/usr/bin/python3",
            "/usr/bin/python3",
        ),
        (
            ["/opt/homebrew/bin/python3.12", "-m", "ciao.cli", "run"],
            "/opt/homebrew/bin/python3.12",
            "/opt/homebrew/bin/python3.12",
        ),
        (
            ["/usr/bin/python", "-m", "ciao.cli", "run"],
            "/usr/bin/python",
            "/usr/bin/python",
        ),
        (["/opt/ciao/bin/ciao", "run"], "/opt/ciao/bin/ciao", ""),
        (["/opt/ciao/bin/ciao", "supervise"], "/opt/ciao/bin/ciao", ""),
    ],
)
def test_parse_direct_shapes(argv: list[str], program: str, python: str) -> None:
    parsed = parse_service_command(argv)
    assert parsed.mode == "direct"
    assert parsed.program == program
    assert parsed.python == python
    assert parsed.argv == tuple(argv)


def test_parse_hosted_shape() -> None:
    host = f"/Users/x/Applications/{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}"
    parsed = parse_service_command([host, "serve", "--python", "/usr/bin/python3"])
    assert parsed.mode == "hosted"
    assert parsed.program == host
    assert parsed.python == "/usr/bin/python3"
    assert parsed.argv == (host, "serve", "--python", "/usr/bin/python3")


def test_parse_hosted_is_syntax_only_not_ownership() -> None:
    # A host basename that does not exist is accepted: naming the host is not
    # proof the host is ours. Ownership is a separate call.
    host = f"/nonexistent/{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}"
    parsed = parse_service_command([host, "serve", "--python", "/usr/bin/python3"])
    assert parsed.mode == "hosted"
    assert parsed.program == host


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-sequence",
        b"bytes",
        [],
        [1, 2],
        [None],
        ["/usr/bin/python3"],
        ["/usr/bin/python3", "run"],
        ["/usr/bin/python3", "-m", "ciao.cli", "run", "extra"],
        ["/usr/bin/python3", "-I", "-I", "-m", "ciao.cli", "run"],
        ["/usr/bin/python3", "-m", "ciao.cli"],
        ["/usr/bin/python3", "-m", "ciao.cli", "bogus"],
        ["/usr/bin/python3", "-I", "-m", "other.cli", "run"],
        ["/usr/bin/python3", "-X", "-m", "ciao.cli", "run"],
        ["python3", "-m", "ciao.cli", "run"],
        ["python3", "-I", "-m", "ciao.cli", "run"],
        ["relative/ciao", "run"],
        ["/opt/ciao", "run", "extra"],
        ["/opt/ciao", "bogus"],
        ["/opt/something", "run"],
        ["C:\\ciao\\ciao.exe", "run"],
        ["/usr/bin/python3", "-m", "ciao.cli", "run\x00"],
    ],
)
def test_parse_rejects_invalid_direct_shapes(bad: object) -> None:
    with pytest.raises(ServerHostError) as caught:
        parse_service_command(bad)
    assert caught.value.code == server_host.INVALID_COMMAND


@pytest.mark.parametrize(
    "bad",
    [
        ["host", "serve", "--python", "/usr/bin/python3"],
        [
            f"/x/{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}",
            "serve",
            "--python",
            "relative/python",
        ],
        [
            f"/x/{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}",
            "serve",
            "python",
            "/usr/bin/python3",
        ],
        [f"/x/{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}", "serve", "--python"],
        [
            f"/x/{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}",
            "serve",
            "--python",
            "/usr/bin/python3",
            "extra",
        ],
        [f"/x/{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}", "request-accessibility"],
        [
            f"/x/{APP_NAME}/Contents/MacOS/OtherHost",
            "serve",
            "--python",
            "/usr/bin/python3",
        ],
        [
            f"/x/{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}",
            "serve",
            "--python",
            "/usr/bin/python3",
            "--python",
            "/usr/bin/python3",
        ],
    ],
)
def test_parse_rejects_invalid_hosted_shapes(bad: object) -> None:
    with pytest.raises(ServerHostError) as caught:
        parse_service_command(bad)
    assert caught.value.code == server_host.INVALID_COMMAND


# ── syntax: host_service_argv ───────────────────────────────────────────────


def test_host_service_argv_round_trips_through_the_parser() -> None:
    argv = host_service_argv(
        Path(f"/Users/x/Applications/{APP_NAME}"), Path("/usr/bin/python3")
    )
    assert argv == (
        f"/Users/x/Applications/{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}",
        "serve",
        "--python",
        "/usr/bin/python3",
    )
    parsed = parse_service_command(list(argv))
    assert parsed.mode == "hosted"
    assert parsed.python == "/usr/bin/python3"


@pytest.mark.parametrize(
    "bundle, python",
    [
        (Path("Applications/" + APP_NAME), Path("/usr/bin/python3")),
        (Path(f"/Users/x/{APP_NAME}"), Path("python3")),
        (Path(f"/Users/x/{APP_NAME}"), Path("/usr/bin/python3\x00")),
        (Path("/Users/x/Other.app"), Path("/usr/bin/python3")),
        (Path(f"/Users/x/{APP_NAME}"), Path("C:\\Python\\python.exe")),
    ],
)
def test_host_service_argv_rejects_strict_absent_inputs(
    bundle: Path, python: Path
) -> None:
    with pytest.raises(ServerHostError) as caught:
        host_service_argv(bundle, python)
    assert caught.value.code == server_host.INVALID_COMMAND


# ── ownership: the strict record decoder ─────────────────────────────────────


def _record(bundle: Path | None = None) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "bundle_path": str(bundle or Path(f"/Users/x/Applications/{APP_NAME}")),
        "bundle_id": BUNDLE_ID,
        "host_revision": HOST_REVISION,
        "host_protocol": HOST_PROTOCOL,
        "executable_sha256": _HASH_A,
        "per_arch_cdhashes": dict(_CDHASH),
        "bundle_files": {
            "Contents/Info.plist": _HASH_A,
            f"Contents/MacOS/{EXECUTABLE_NAME}": _HASH_A,
            f"Contents/Resources/{ICON_NAME}": _HASH_B,
            "Contents/_CodeSignature/CodeResources": _HASH_B,
        },
    }


def _write_record(path: Path, document: object, *, private: bool = True) -> Path:
    text = document if isinstance(document, str) else json.dumps(document)
    path.write_text(text, encoding="utf-8")
    if private and sys.platform != "win32":
        os.chmod(path, 0o600)
    return path


def test_read_record_round_trips(tmp_path: Path) -> None:
    bundle = tmp_path / APP_NAME
    bundle.mkdir()
    record = _write_record(tmp_path / "record.json", _record(bundle))
    parsed = read_host_ownership(record)
    assert parsed.bundle_path == str(bundle)
    assert parsed.bundle_id == BUNDLE_ID
    assert parsed.host_revision == HOST_REVISION
    assert parsed.host_protocol == HOST_PROTOCOL
    assert parsed.executable_sha256 == _HASH_A
    assert dict(parsed.per_arch_cdhashes) == _CDHASH
    assert set(parsed.bundle_files) == set(_record(bundle)["bundle_files"])
    assert parsed.to_record() == _record(bundle)


def test_read_record_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(tmp_path / "absent.json")
    assert caught.value.code == server_host.MISSING_RECORD


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '"a string"',
        "{",
    ],
)
def test_read_record_rejects_malformed(tmp_path: Path, raw: str) -> None:
    record = _write_record(tmp_path / "record.json", raw)
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


def test_read_record_rejects_duplicate_keys(tmp_path: Path) -> None:
    text = json.dumps(_record()).replace('"schema": 1', '"schema": 1, "schema": 1', 1)
    record = _write_record(tmp_path / "record.json", text)
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


def test_read_record_rejects_nested_duplicate_keys(tmp_path: Path) -> None:
    document = _record()
    raw = json.dumps(document)
    raw = raw.replace(
        '"Contents/_CodeSignature/CodeResources": "',
        '"Contents/_CodeSignature/CodeResources": "x", "Contents/_CodeSignature/CodeResources": "',
        1,
    )
    record = _write_record(tmp_path / "record.json", raw)
    with pytest.raises(ServerHostError):
        read_host_ownership(record)


def test_read_record_rejects_bool_schema(tmp_path: Path) -> None:
    document = _record()
    document["schema"] = True
    record = _write_record(tmp_path / "record.json", document)
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.UNSUPPORTED_SCHEMA


def test_read_record_rejects_newer_schema(tmp_path: Path) -> None:
    document = _record()
    document["schema"] = SCHEMA_VERSION + 1
    record = _write_record(tmp_path / "record.json", document)
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.UNSUPPORTED_SCHEMA


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.__setitem__("bundle_id", "local.other"),
        lambda d: d.__setitem__("host_revision", HOST_REVISION + 1),
        lambda d: d.__setitem__("host_protocol", HOST_PROTOCOL + 1),
        lambda d: d.__setitem__("host_revision", True),
        lambda d: d.__setitem__("executable_sha256", "ABC"),
        lambda d: d.__setitem__("executable_sha256", "a" * 63),
        lambda d: d.__setitem__("executable_sha256", "A" * 64),
        lambda d: d.__setitem__("bundle_path", "relative/app"),
        lambda d: d.__setitem__("bundle_path", "C:\\app"),
        lambda d: d.__setitem__("per_arch_cdhashes", {"arm64": _CDHASH["arm64"]}),
        lambda d: d.__setitem__("per_arch_cdhashes", {**dict(_CDHASH), "arm64": "x"}),
        lambda d: d.__setitem__(
            "per_arch_cdhashes", {**dict(_CDHASH), "arm64": "A" * 40}
        ),
        lambda d: d.__setitem__("bundle_files", {}),
        lambda d: d.__setitem__("bundle_files", {"Contents/Info.plist": _HASH_A}),
        lambda d: d.__setitem__("extra", "unexpected"),
        lambda d: d.pop("bundle_id"),
    ],
)
def test_read_record_rejects_identity_and_shape(tmp_path: Path, mutate: Any) -> None:
    document = _record()
    mutate(document)
    record = _write_record(tmp_path / "record.json", document)
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code in (
        server_host.INVALID_OWNERSHIP,
        server_host.UNSUPPORTED_SCHEMA,
    )


@pytest.mark.parametrize(
    "name",
    [
        "",
        "/absolute/path",
        "../escape",
        "Contents/../../escape",
        "Contents\\Info.plist",
        "Contents/MacOS/./x",
        "Contents//Info.plist",
        "Contents/MacOS/trailing/",
    ],
)
def test_read_record_rejects_unsafe_file_names(tmp_path: Path, name: str) -> None:
    document = _record()
    document["bundle_files"][name] = _HASH_A
    record = _write_record(tmp_path / "record.json", document)
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


def test_read_record_rejects_missing_required_sealed_file(tmp_path: Path) -> None:
    document = _record()
    del document["bundle_files"]["Contents/_CodeSignature/CodeResources"]
    record = _write_record(tmp_path / "record.json", document)
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


def test_read_record_rejects_record_inside_the_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / APP_NAME
    bundle.mkdir()
    record = bundle / "Contents" / "server-host.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    _write_record(record, _record(bundle))
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


def test_read_record_rejects_a_symlink(tmp_path: Path) -> None:
    real = _write_record(tmp_path / "real.json", _record())
    link = tmp_path / "link.json"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(link)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


def test_read_record_rejects_a_directory(tmp_path: Path) -> None:
    directory = tmp_path / "record.json"
    directory.mkdir()
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(directory)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_read_record_rejects_group_or_other_readable(tmp_path: Path) -> None:
    record = _write_record(tmp_path / "record.json", _record(), private=False)
    os.chmod(record, 0o644)
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


# ── identity/ownership: the bundle inspector and the verifier ───────────────

_PLIST_REL = "Contents/Info.plist"
_EXE_REL = f"Contents/MacOS/{EXECUTABLE_NAME}"
_ICON_REL = f"Contents/Resources/{ICON_NAME}"
_CODESIGN_REL = "Contents/_CodeSignature/CodeResources"


def _production_plist() -> dict[str, Any]:
    return {
        "CFBundleName": "Ciaobot Server",
        "CFBundleDisplayName": "Ciaobot Server",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": ICON_NAME,
        "CFBundleVersion": str(HOST_REVISION),
        "CFBundleShortVersionString": str(HOST_REVISION),
        HOST_PROTOCOL_KEY: HOST_PROTOCOL,
        "LSMinimumSystemVersion": MINIMUM_SYSTEM_VERSION,
        "LSUIElement": True,
    }


def _make_bundle(root: Path, *, name: str = APP_NAME) -> Path:
    bundle = root / name
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "Resources").mkdir(parents=True)
    (bundle / "Contents" / "_CodeSignature").mkdir(parents=True)
    (bundle / _EXE_REL).write_bytes(b"universal-host-binary")
    (bundle / _ICON_REL).write_bytes(b"indigo-icns-bytes")
    (bundle / _CODESIGN_REL).write_bytes(b"sealed-resources")
    with (bundle / _PLIST_REL).open("wb") as handle:
        plistlib.dump(_production_plist(), handle)
    return bundle


class _FakeRunner:
    """Records every native probe and answers the fixed codesign/lipo outputs."""

    def __init__(
        self,
        *,
        verify_rc: int = 0,
        adhoc: bool = True,
        archs: str = "arm64 x86_64\n",
        arch_rc: int = 0,
        cdhashes: dict[str, str] | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.verify_rc = verify_rc
        self.adhoc = adhoc
        self.archs = archs
        self.arch_rc = arch_rc
        self.cdhashes = dict(cdhashes or _CDHASH)

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(argv))
        self.kwargs.append(dict(kwargs))
        args = list(argv)
        if args[:2] == ["codesign", "--verify"]:
            return subprocess.CompletedProcess(args, self.verify_rc, "", "")
        if args[:2] == ["codesign", "-dv"]:
            if "--arch" in args:
                arch = args[args.index("--arch") + 1]
                if self.arch_rc != 0:
                    return subprocess.CompletedProcess(args, self.arch_rc, "", "")
                stderr = f"CDHash={self.cdhashes[arch]}\n"
                return subprocess.CompletedProcess(args, 0, "", stderr)
            stderr = "Format=bundle\n"
            if self.adhoc:
                stderr += "Signature=adhoc\n"
            return subprocess.CompletedProcess(args, 0, "", stderr)
        if args[:2] == ["lipo", "-archs"]:
            return subprocess.CompletedProcess(args, 0, self.archs, "")
        raise AssertionError(f"unexpected native probe: {args}")


def _install_record(path: Path, snapshot: HostOwnership) -> Path:
    return _write_record(path, snapshot.to_record())


@requires_posix_uid
def test_inspect_bundle_snapshot_and_probe_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    runner = _FakeRunner()

    snapshot = inspect_host_bundle(bundle, runner=runner)

    assert snapshot.bundle_path == os.fspath(Path(os.path.realpath(bundle)))
    assert snapshot.bundle_id == BUNDLE_ID
    assert snapshot.host_revision == HOST_REVISION
    assert snapshot.host_protocol == HOST_PROTOCOL
    assert snapshot.schema == SCHEMA_VERSION
    assert (
        snapshot.executable_sha256
        == hashlib.sha256((bundle / _EXE_REL).read_bytes()).hexdigest()
    )
    assert dict(snapshot.per_arch_cdhashes) == _CDHASH
    assert set(snapshot.bundle_files) == {
        _PLIST_REL,
        _EXE_REL,
        _ICON_REL,
        _CODESIGN_REL,
    }

    # Exact fixed, shell-free, bounded probes in a fixed order.
    executable = os.fspath(Path(os.path.realpath(bundle)) / _EXE_REL)
    canonical_bundle = os.fspath(Path(os.path.realpath(bundle)))
    assert runner.commands == [
        ["codesign", "--verify", "--strict", canonical_bundle],
        ["codesign", "-dv", "--verbose=4", canonical_bundle],
        ["lipo", "-archs", executable],
        ["codesign", "-dv", "--verbose=4", "--arch", "arm64", executable],
        ["codesign", "-dv", "--verbose=4", "--arch", "x86_64", executable],
    ]
    for kwargs in runner.kwargs:
        assert "shell" not in kwargs
        assert kwargs["timeout"] == server_host.NATIVE_TIMEOUT_SECONDS
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False


@pytest.mark.parametrize("platform_name", ["linux", "win32"])
def test_inspect_refuses_non_macos_before_any_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform_name)
    bundle = _make_bundle(tmp_path)
    runner = _FakeRunner()
    with pytest.raises(ServerHostError) as caught:
        inspect_host_bundle(bundle, runner=runner)
    assert caught.value.code == server_host.UNSUPPORTED_PLATFORM
    assert runner.commands == []


def test_inspect_rejects_a_relative_bundle_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ServerHostError) as caught:
        inspect_host_bundle(Path("relative/Ciaobot Server.app"), runner=_FakeRunner())
    assert caught.value.code == server_host.INSPECTION_FAILED


def test_inspect_rejects_a_symlinked_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    link = tmp_path / "linked.app"
    try:
        link.symlink_to(bundle)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(ServerHostError) as caught:
        inspect_host_bundle(link, runner=_FakeRunner())
    assert caught.value.code == server_host.INSPECTION_FAILED


def test_inspect_rejects_a_symlinked_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    icon = bundle / _ICON_REL
    target = tmp_path / "elsewhere"
    target.write_bytes(b"elsewhere")
    icon.unlink()
    try:
        icon.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(ServerHostError) as caught:
        inspect_host_bundle(bundle, runner=_FakeRunner())
    assert caught.value.code == server_host.INSPECTION_FAILED


def test_inspect_rejects_a_special_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    if not hasattr(os, "mkfifo"):
        pytest.skip("no mkfifo on this platform")
    bundle = _make_bundle(tmp_path)
    os.mkfifo(bundle / "Contents" / "_CodeSignature" / "pipe")
    with pytest.raises(ServerHostError) as caught:
        inspect_host_bundle(bundle, runner=_FakeRunner())
    assert caught.value.code == server_host.INSPECTION_FAILED


@pytest.mark.parametrize(
    "mutate, code",
    [
        (
            lambda b: (b / _PLIST_REL).write_bytes(b"not a plist"),
            server_host.INSPECTION_FAILED,
        ),
        (
            lambda b: _rewrite_plist(b, bundle_id="local.other"),
            server_host.INSPECTION_FAILED,
        ),
        (lambda b: _rewrite_plist(b, revision=2), server_host.INSPECTION_FAILED),
        (lambda b: _rewrite_plist(b, protocol=2), server_host.INSPECTION_FAILED),
        (lambda b: _rewrite_plist(b, minimum="12.0"), server_host.INSPECTION_FAILED),
        (lambda b: (b / _ICON_REL).unlink(), server_host.INSPECTION_FAILED),
        (
            lambda b: (b / "Contents" / "Resources" / "extra.txt").write_text("x"),
            server_host.INSPECTION_FAILED,
        ),
        (
            lambda b: (b / "Contents" / "Frameworks").mkdir(),
            server_host.INSPECTION_FAILED,
        ),
    ],
)
def test_inspect_rejects_broken_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: Any, code: str
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    mutate(bundle)
    with pytest.raises(ServerHostError) as caught:
        inspect_host_bundle(bundle, runner=_FakeRunner())
    assert caught.value.code == code


def _rewrite_plist(
    bundle: Path,
    *,
    bundle_id: str | None = None,
    revision: int | None = None,
    protocol: int | None = None,
    minimum: str | None = None,
) -> None:
    document = _production_plist()
    if bundle_id is not None:
        document["CFBundleIdentifier"] = bundle_id
    if revision is not None:
        document["CFBundleVersion"] = str(revision)
        document["CFBundleShortVersionString"] = str(revision)
    if protocol is not None:
        document[HOST_PROTOCOL_KEY] = protocol
    if minimum is not None:
        document["LSMinimumSystemVersion"] = minimum
    with (bundle / _PLIST_REL).open("wb") as handle:
        plistlib.dump(document, handle)


@requires_posix_uid
@pytest.mark.parametrize(
    "runner, code",
    [
        (_FakeRunner(verify_rc=1), server_host.INSPECTION_FAILED),
        (_FakeRunner(adhoc=False), server_host.INSPECTION_FAILED),
        (_FakeRunner(archs="arm64\n"), server_host.INSPECTION_FAILED),
        (_FakeRunner(archs="arm64 x86_64 arm64e\n"), server_host.INSPECTION_FAILED),
        (_FakeRunner(arch_rc=1), server_host.INSPECTION_FAILED),
        (
            _FakeRunner(cdhashes={"arm64": "nothex", "x86_64": _CDHASH["x86_64"]}),
            server_host.INSPECTION_FAILED,
        ),
        (
            _FakeRunner(cdhashes={"arm64": "A" * 40, "x86_64": _CDHASH["x86_64"]}),
            server_host.INSPECTION_FAILED,
        ),
    ],
)
def test_inspect_rejects_signature_and_slice_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: _FakeRunner,
    code: str,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    with pytest.raises(ServerHostError) as caught:
        inspect_host_bundle(bundle, runner=runner)
    assert caught.value.code == code


@requires_posix_uid
def test_verify_owned_host_accepts_the_matching_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    runner = _FakeRunner()
    snapshot = inspect_host_bundle(bundle, runner=runner)
    record = _install_record(tmp_path / "server-host.json", snapshot)

    commands_before = len(runner.commands)
    verified = verify_owned_host(bundle, ownership_path=record, runner=runner)
    assert verified == snapshot
    # The verifier runs the same read-only probes; nothing signs.
    assert all(command[1] != "--sign" for command in runner.commands[commands_before:])


@requires_posix_uid
def test_verify_owned_host_refuses_a_changed_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    runner = _FakeRunner()
    snapshot = inspect_host_bundle(bundle, runner=runner)
    record = _install_record(tmp_path / "server-host.json", snapshot)

    (bundle / _ICON_REL).write_bytes(b"tampered-icon")
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(bundle, ownership_path=record, runner=runner)
    assert caught.value.code == server_host.NOT_OWNED


@requires_posix_uid
def test_verify_owned_host_refuses_a_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    runner = _FakeRunner()
    snapshot = inspect_host_bundle(bundle, runner=runner)
    record = _install_record(tmp_path / "server-host.json", snapshot)

    (bundle / "Contents" / "Resources" / "extra.txt").write_text("new")
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(bundle, ownership_path=record, runner=runner)
    assert caught.value.code == server_host.INSPECTION_FAILED


@requires_posix_uid
def test_verify_owned_host_refuses_a_deleted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    runner = _FakeRunner()
    snapshot = inspect_host_bundle(bundle, runner=runner)
    record = _install_record(tmp_path / "server-host.json", snapshot)

    (bundle / _CODESIGN_REL).unlink()
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(bundle, ownership_path=record, runner=runner)
    assert caught.value.code == server_host.INSPECTION_FAILED


@requires_posix_uid
def test_verify_owned_host_refuses_a_path_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle_a = _make_bundle(tmp_path / "a")
    bundle_b = _make_bundle(tmp_path / "b")
    runner = _FakeRunner()
    record = _install_record(
        tmp_path / "server-host.json", inspect_host_bundle(bundle_a, runner=runner)
    )
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(bundle_b, ownership_path=record, runner=runner)
    assert caught.value.code == server_host.NOT_OWNED


@requires_posix_uid
def test_verify_owned_host_refuses_a_missing_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(
            bundle, ownership_path=tmp_path / "absent.json", runner=_FakeRunner()
        )
    assert caught.value.code == server_host.MISSING_RECORD


@requires_posix_uid
def test_verify_owned_host_never_writes_or_signs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    runner = _FakeRunner()
    snapshot = inspect_host_bundle(bundle, runner=runner)
    record = _install_record(tmp_path / "server-host.json", snapshot)

    before = sorted(p.as_posix() for p in tmp_path.rglob("*"))
    verify_owned_host(bundle, ownership_path=record, runner=runner)
    after = sorted(p.as_posix() for p in tmp_path.rglob("*"))
    assert before == after
    for command in runner.commands:
        assert command[1] in ("--verify", "-dv", "-archs")
        assert "--sign" not in command


# ── macOS-only integration: the real universal host ─────────────────────────


@requires_macos_tools
def test_real_build_snapshot_record_and_reverify(tmp_path: Path) -> None:
    """Build the real host into scratch, record it privately, verify it.

    The record lives outside the bundle in the same scratch dir; a change to the
    archive sidecar (outside the sealed app) does not disturb verification, and a
    tampered copy of the app is refused. No installed app, engine, launchd or
    permission surface is touched.
    """
    result = BUILD.build(tmp_path / "build")
    app: Path = result["paths"]["app"]
    archive: Path = result["paths"]["archive"]

    snapshot = inspect_host_bundle(app)
    assert set(snapshot.bundle_files) >= {
        _PLIST_REL,
        _EXE_REL,
        _ICON_REL,
        _CODESIGN_REL,
    }
    assert set(snapshot.per_arch_cdhashes) == set(ARCHITECTURES)
    assert snapshot.executable_sha256 == result["metadata"]["executable_sha256"]
    assert dict(snapshot.per_arch_cdhashes) == result["metadata"]["per_arch_cdhashes"]

    record = _install_record(tmp_path / "server-host.json", snapshot)
    assert record.is_file()

    # An engine-sidecar file outside the sealed app may change; the host has not.
    archive.write_bytes(archive.read_bytes() + b"sidecar-change")
    verified = verify_owned_host(app, ownership_path=record)
    assert verified == snapshot

    # A tampered copy of the same app is refused.
    tampered = tmp_path / "Tampered.app"
    shutil.copytree(app, tampered, symlinks=True)
    (tampered / _ICON_REL).write_bytes(b"tampered")
    with pytest.raises(ServerHostError):
        verify_owned_host(tampered, ownership_path=record)

    # Never installed anywhere near the real app path, and nothing signed.
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
