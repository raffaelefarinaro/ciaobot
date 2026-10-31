"""Checks for the verified server-host identity/ownership contract (#1023, B1).

The pure checks — command parsing, argv rendering, the constant contract and the
strict ownership-record decoder — run on every platform, using ``PurePosixPath``
macOS argv and Windows-safe temp record paths. The bundle inspector and the
ownership verifier run under an injected runner and a mocked ``sys.platform``
(so the ``codesign`` probes are proved without a real host), and the
macOS-only integration builds the real universal host into a fresh tmpdir and
verifies it. Nothing here installs a bundle, touches ``~/Applications``, an
engine, ``launchctl``, Login Items or TCC, or opens a permission prompt.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
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
    DeveloperIDSigning,
    EXECUTABLE_NAME,
    HOST_PROTOCOL,
    HOST_PROTOCOL_KEY,
    HOST_REVISION,
    ICON_NAME,
    SCHEMA_VERSION,
    SIGNATURE_REJECTED,
    SIGNED_BUNDLE_FILES,
    SIGNED_HOST_REVISION,
    SIGNED_SCHEMA_VERSION,
    HostOwnership,
    ServerHostError,
    developer_id_requirement,
    host_move_argv,
    host_service_argv,
    inspect_host_bundle,
    parse_service_command,
    SUPERSEDED_HOST_PROTOCOLS,
    read_host_ownership,
    verify_owned_host,
    verify_superseded_host,
)

ROOT = Path(__file__).resolve().parents[1]
# A macOS bundle path as a plain POSIX string, so record and argv fixtures mean
# the same thing on Windows (where ``str(Path("/Users/..."))`` has backslashes).
POSIX_BUNDLE = f"/Users/x/Applications/{APP_NAME}"

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

# An ownership record names a macOS bundle by an absolute POSIX path, and the
# reader refuses a backslash. A bundle built under a Windows ``tmp_path`` can
# never be recorded, so tests that round-trip one through a record are macOS/
# Linux only. The inspection itself still runs on Windows.
requires_posix_bundle_path = pytest.mark.skipif(
    sys.platform == "win32", reason="a recorded bundle path is POSIX-only"
)

# The builder needs the Command Line Tools (xcrun swiftc, lipo); the inspector
# itself needs only /usr/bin/codesign.
HAS_TOOLS = (
    sys.platform == "darwin"
    and shutil.which("xcrun") is not None
    and shutil.which("lipo") is not None
    and os.path.exists(server_host.CODESIGN)
)
requires_macos_tools = pytest.mark.skipif(
    not HAS_TOOLS, reason="the native host build requires macOS Command Line Tools"
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_CDHASH = {arch: hashlib.sha1(arch.encode()).hexdigest() for arch in ARCHITECTURES}
#: The only team any test may use: a fake, never a real Developer ID team.
_TEST_TEAM = "TESTTEAM01"


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
    source = HOST_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"^let HOST_PROTOCOL_REVISION = (\d+)$", source, re.MULTILINE)
    assert match is not None
    assert int(match.group(1)) == server_host.HOST_PROTOCOL
    match = re.search(r"^let STOP_GRACE_SECONDS = ([0-9.]+)$", source, re.MULTILINE)
    assert match is not None
    assert float(match.group(1)) == server_host.STOP_GRACE_SECONDS


def test_the_protocol_carrying_the_move_operation_is_the_host_protocol() -> None:
    # The Swift host has one protocol revision, and the ``run-move`` operation
    # belongs to it. The engine refuses a host below MOVE_HOST_PROTOCOL, so the
    # two must not drift apart: a host that speaks the current protocol
    # carries the move.
    source = HOST_SOURCE.read_text(encoding="utf-8")
    assert "case runMove(" in source
    assert server_host.MOVE_HOST_PROTOCOL == server_host.HOST_PROTOCOL


def test_the_hosted_child_is_an_accepted_direct_supervise_command() -> None:
    # The Swift host appends its fixed SUPERVISOR_ARGUMENTS to --python. Bind
    # to that source rather than a copy here: the child it launches must be a
    # direct shape this parser accepts and a subcommand ciao.cli really has.
    from ciao import cli

    source = HOST_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"^let SUPERVISOR_ARGUMENTS = \[(.*)\]$", source, re.MULTILINE)
    assert match is not None
    arguments = re.findall(r'"([^"]*)"', match.group(1))
    child = parse_service_command(["/usr/bin/python3", *arguments])
    assert child.mode == "direct"
    assert child.argv[-3:] == ("-m", "ciao.cli", "supervise")
    assert cli.build_parser().parse_args(list(child.argv[-1:])).command == "supervise"


def test_the_sealed_file_set_is_the_builder_layout() -> None:
    layout = BUILD._layout(PurePosixPath("/out"))
    built = {
        PurePosixPath(layout[key]).relative_to(PurePosixPath(layout["app"])).as_posix()
        for key in ("info_plist", "executable", "icon")
    }
    assert server_host.REQUIRED_BUNDLE_FILES == built | {
        "Contents/_CodeSignature/CodeResources"
    }


def test_exit_timeout_outlasts_stop_grace_which_outlasts_supervisor() -> None:
    from ciao import supervise

    assert supervise.STOP_GRACE_S < server_host.STOP_GRACE_SECONDS
    assert server_host.STOP_GRACE_SECONDS < server_host.EXIT_TIMEOUT_SECONDS


def test_defaults_are_home_anchored_at_call_time(home_dir: Path) -> None:
    # Resolved when called, so the per-test home isolation applies and no test
    # can read the developer's real record through the default.
    assert server_host.default_bundle_path() == home_dir / "Applications" / APP_NAME
    assert server_host.default_ownership_path() == (
        home_dir / ".local" / "state" / "ciaobot" / "server-host.json"
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
    host = f"{POSIX_BUNDLE}/Contents/MacOS/{EXECUTABLE_NAME}"
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


def test_parse_the_hosted_move_shape() -> None:
    # The workspace move runs under the host (#1658), so the host's second
    # hosted shape is a command the parser knows: it names the interpreter the
    # move runs, like the serve shape does.
    host = f"{POSIX_BUNDLE}/Contents/MacOS/{EXECUTABLE_NAME}"
    argv = [
        host,
        "run-move",
        "--python",
        "/usr/bin/python3",
        "--operation",
        "op-1234",
        "--state-dir",
        "/Users/x/.local/state/ciaobot/workspace-move",
    ]
    parsed = parse_service_command(argv)
    assert parsed.mode == "hosted"
    assert parsed.program == host
    assert parsed.python == "/usr/bin/python3"
    assert parsed.argv == tuple(argv)


@pytest.mark.parametrize(
    "bad",
    [
        # A relative state directory, a traversing one and a non-absolute
        # interpreter are all refusals.
        [
            "run-move",
            "--python",
            "/usr/bin/python3",
            "--operation",
            "op-1234",
            "--state-dir",
            "relative/state",
        ],
        [
            "run-move",
            "--python",
            "/usr/bin/python3",
            "--operation",
            "op-1234",
            "--state-dir",
            "/Users/x/../state",
        ],
        [
            "run-move",
            "--python",
            "relative/python3",
            "--operation",
            "op-1234",
            "--state-dir",
            "/Users/x/state",
        ],
        # The shape is exactly seven arguments: a missing or an extra one is a
        # refusal.
        [
            "run-move",
            "--python",
            "/usr/bin/python3",
            "--operation",
            "op-1234",
        ],
        [
            "run-move",
            "--python",
            "/usr/bin/python3",
            "--operation",
            "op-1234",
            "--state-dir",
            "/Users/x/state",
            "extra",
        ],
        # A non-python interpreter is refused, like the serve shape.
        [
            "run-move",
            "--python",
            "/bin/sh",
            "--operation",
            "op-1234",
            "--state-dir",
            "/Users/x/state",
        ],
    ],
)
def test_parse_rejects_invalid_hosted_move_shapes(bad: list[str]) -> None:
    host = f"{POSIX_BUNDLE}/Contents/MacOS/{EXECUTABLE_NAME}"
    with pytest.raises(ServerHostError) as caught:
        parse_service_command([host, *bad])
    assert caught.value.code == server_host.INVALID_COMMAND


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
        ["/usr/bin/../bin/python3", "-m", "ciao.cli", "run"],
        ["/opt//ciao", "run"],
        ["//opt/ciao", "run"],
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
        # The host runs `<python> -I -m ciao.cli supervise`: a shell is not one.
        [
            f"/x/{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}",
            "serve",
            "--python",
            "/bin/sh",
        ],
        [
            f"/x/../{APP_NAME}/Contents/MacOS/{EXECUTABLE_NAME}",
            "serve",
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
        PurePosixPath(POSIX_BUNDLE), PurePosixPath("/usr/bin/python3")
    )
    assert argv == (
        f"{POSIX_BUNDLE}/Contents/MacOS/{EXECUTABLE_NAME}",
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
        ("Applications/" + APP_NAME, "/usr/bin/python3"),
        (POSIX_BUNDLE, "python3"),
        (POSIX_BUNDLE, "/usr/bin/python3\x00"),
        ("/Users/x/Other.app", "/usr/bin/python3"),
        (POSIX_BUNDLE, "C:\\Python\\python.exe"),
        (POSIX_BUNDLE, "/bin/sh"),
        ("/Users/x/../y/" + APP_NAME, "/usr/bin/python3"),
        ("//Users/x/" + APP_NAME, "/usr/bin/python3"),
    ],
)
def test_host_service_argv_rejects_strict_absent_inputs(
    bundle: str, python: str
) -> None:
    with pytest.raises(ServerHostError) as caught:
        host_service_argv(PurePosixPath(bundle), PurePosixPath(python))
    assert caught.value.code == server_host.INVALID_COMMAND


# ── syntax: host_move_argv ──────────────────────────────────────────────────


def test_host_move_argv_round_trips_through_the_parser() -> None:
    argv = host_move_argv(
        PurePosixPath(POSIX_BUNDLE),
        PurePosixPath("/usr/bin/python3"),
        "op-1234",
        PurePosixPath("/Users/x/.local/state/ciaobot/workspace-move"),
    )
    assert argv == (
        f"{POSIX_BUNDLE}/Contents/MacOS/{EXECUTABLE_NAME}",
        "run-move",
        "--python",
        "/usr/bin/python3",
        "--operation",
        "op-1234",
        "--state-dir",
        "/Users/x/.local/state/ciaobot/workspace-move",
    )
    parsed = parse_service_command(list(argv))
    assert parsed.mode == "hosted"
    assert parsed.python == "/usr/bin/python3"


@pytest.mark.parametrize(
    "bundle, python, operation, state_dir",
    [
        # A bundle the host does not own, an interpreter it would refuse.
        ("Applications/" + APP_NAME, "/usr/bin/python3", "op-1", "/state"),
        (POSIX_BUNDLE, "python3", "op-1", "/state"),
        (POSIX_BUNDLE, "/bin/sh", "op-1", "/state"),
        # An operation id that is not a plain identifier: the host's own
        # parser reads a leading dash as an option.
        (POSIX_BUNDLE, "/usr/bin/python3", "--state-dir", "/state"),
        (POSIX_BUNDLE, "/usr/bin/python3", "", "/state"),
        (POSIX_BUNDLE, "/usr/bin/python3", "op/1", "/state"),
        # A state directory that is not absolute and normalized.
        (POSIX_BUNDLE, "/usr/bin/python3", "op-1", "relative/state"),
        (POSIX_BUNDLE, "/usr/bin/python3", "op-1", "/Users/x/../state"),
    ],
)
def test_host_move_argv_rejects_strict_absent_inputs(
    bundle: str, python: str, operation: str, state_dir: str
) -> None:
    with pytest.raises(ServerHostError) as caught:
        host_move_argv(
            PurePosixPath(bundle),
            PurePosixPath(python),
            operation,
            PurePosixPath(state_dir),
        )
    assert caught.value.code == server_host.INVALID_COMMAND


# ── ownership: the strict record decoder ─────────────────────────────────────


def _record(bundle: str = POSIX_BUNDLE) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "bundle_path": bundle,
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
    record = _write_record(tmp_path / "record.json", _record())
    parsed = read_host_ownership(record)
    assert parsed.bundle_path == POSIX_BUNDLE
    assert parsed.bundle_id == BUNDLE_ID
    assert parsed.host_revision == HOST_REVISION
    assert parsed.host_protocol == HOST_PROTOCOL
    assert parsed.executable_sha256 == _HASH_A
    assert dict(parsed.per_arch_cdhashes) == _CDHASH
    assert set(parsed.bundle_files) == set(_record()["bundle_files"])
    assert parsed.to_record() == _record()


def test_a_snapshot_cannot_be_edited_after_it_is_read(tmp_path: Path) -> None:
    parsed = read_host_ownership(_write_record(tmp_path / "record.json", _record()))
    with pytest.raises(TypeError):
        parsed.bundle_files["Contents/Info.plist"] = _HASH_B  # type: ignore[index]
    with pytest.raises(TypeError):
        parsed.per_arch_cdhashes["arm64"] = "0" * 40  # type: ignore[index]
    # to_record hands out plain copies a writer can serialize.
    assert isinstance(parsed.to_record()["bundle_files"], dict)


def test_read_record_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(tmp_path / "absent.json")
    assert caught.value.code == server_host.MISSING_RECORD


def test_an_unexaminable_record_is_not_a_missing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A record the reader cannot stat is not proof that none was written.
    def denied(path: object) -> os.stat_result:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(server_host.os, "lstat", denied)
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(tmp_path / "record.json")
    assert caught.value.code == server_host.INVALID_OWNERSHIP


def test_a_record_swapped_after_its_checks_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The checks and the read must be about the same inode.
    record = _write_record(tmp_path / "record.json", _record())
    other = _write_record(tmp_path / "other.json", _record())
    real_lstat = os.lstat
    monkeypatch.setattr(
        server_host.os,
        "lstat",
        lambda path: real_lstat(other) if Path(path) == record else real_lstat(path),
    )
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


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
    # Schema 2 is the signed record and is read on purpose; the fixture names the
    # policy *after* it, so this keeps testing an unsupported schema.
    document = _record()
    document["schema"] = server_host.SIGNED_SCHEMA_VERSION + 1
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
        # Two different digests for the one executable.
        lambda d: d.__setitem__("executable_sha256", _HASH_B),
        lambda d: d.__setitem__("bundle_path", "relative/app"),
        lambda d: d.__setitem__("bundle_path", "C:\\app"),
        # A record must name the host bundle, not a parent such as the home.
        lambda d: d.__setitem__("bundle_path", "/Users/x"),
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
        "Contents/Resources/extra.txt",
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bundle paths")
@pytest.mark.parametrize("via", ["direct", "record_alias", "bundle_alias"])
def test_read_record_rejects_record_inside_the_bundle(tmp_path: Path, via: str) -> None:
    real = Path(os.path.realpath(tmp_path)) / "real"
    bundle = real / APP_NAME
    (bundle / "Contents").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    # A symlinked ancestor on either path must not hide the overlap.
    record_dir = (alias if via == "record_alias" else real) / APP_NAME / "Contents"
    recorded = (alias if via == "bundle_alias" else real) / APP_NAME
    record = _write_record(
        record_dir / "server-host.json", _record(recorded.as_posix())
    )
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP
    assert "outside the bundle" in str(caught.value)


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


@requires_posix_uid
def test_read_record_rejects_group_or_other_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner/mode contract is macOS-only in production (`if sys.platform ==
    # "darwin"`). Native Linux CI cannot run that branch, so mock the platform
    # to exercise the contract on any POSIX host; it is the tested behaviour on
    # the macOS job all the same.
    monkeypatch.setattr(sys, "platform", "darwin")
    record = _write_record(tmp_path / "record.json", _record(), private=False)
    os.chmod(record, 0o644)
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


@requires_posix_uid
def test_read_record_rejects_a_foreign_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other half of the darwin branch: a record another account owns is not
    # this install's proof, even with owner-only mode bits.
    monkeypatch.setattr(sys, "platform", "darwin")
    record = _write_record(tmp_path / "record.json", _record())
    real_uid = os.getuid()
    monkeypatch.setattr(server_host.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


# ── identity/ownership: the bundle inspector and the verifier ───────────────

_PLIST_REL = "Contents/Info.plist"
_EXE_REL = f"Contents/MacOS/{EXECUTABLE_NAME}"
_ICON_REL = f"Contents/Resources/{ICON_NAME}"
_CODESIGN_REL = "Contents/_CodeSignature/CodeResources"
_APP_TICKET_REL = "Contents/CodeResources"


def _make_bundle(root: Path, *, name: str = APP_NAME) -> Path:
    bundle = root / name
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "Resources").mkdir(parents=True)
    (bundle / "Contents" / "_CodeSignature").mkdir(parents=True)
    (bundle / _EXE_REL).write_bytes(b"universal-host-binary")
    (bundle / _ICON_REL).write_bytes(b"indigo-icns-bytes")
    (bundle / _CODESIGN_REL).write_bytes(b"sealed-resources")
    with (bundle / _PLIST_REL).open("wb") as handle:
        plistlib.dump(BUILD.bundle_info(), handle)
    return bundle


_CODESIGN = server_host.CODESIGN


class _FakeRunner:
    """Records every native probe and answers the fixed ``codesign`` outputs.

    The bundle display mirrors real ``codesign -dv --verbose=4`` output from a
    built host: ``Identifier=``, ``Format=app bundle with Mach-O universal (...)``
    and ``Signature=adhoc`` on stderr. A non-ad-hoc answer prints the fields a
    Developer ID signed host really carries; every signed-policy probe has an
    explicit switch, so a test never gets blanket success for a command it did
    not reason about. Per-slice overrides let one architecture fail on its own,
    and ``display_text`` / ``requirement_text`` / ``entitlements_raw`` supply the
    exact bytes or lines a contradictory or duplicated answer needs.

    Every probe must be the *complete* argv the contract allows: the absolute
    ``/usr/bin/codesign``, the exact flags, the architecture, the expected
    requirement text and the right target (the bundle for a bundle probe, this
    bundle's executable for a slice probe). Anything else raises, so dropping
    ``--strict``, ``--all-architectures``, ``--xml``, ``--arch`` or ``-r-``, or
    probing a different path, cannot stay green. ``expected_team`` is the team
    the caller should have taken from the private record: the requirement text
    is built by the *builder*, not by the module under test, so a hardcoded or
    mistranslated team in :func:`developer_id_requirement` is refused here.
    """

    def __init__(
        self,
        *,
        verify_rc: int = 0,
        requirement_rc: int = 0,
        adhoc: bool = True,
        identifier: str = BUNDLE_ID,
        archs: str = "x86_64 arm64",
        arch_rc: int = 0,
        cdhashes: dict[str, str] | None = None,
        raises: BaseException | None = None,
        team_id: str = _TEST_TEAM,
        expected_team: str = _TEST_TEAM,
        runtime: bool = True,
        timestamp: bool = True,
        designated: str | None = None,
        entitlements: dict[str, object] | None = None,
        slices: dict[str, dict[str, Any]] | None = None,
        display_text: str | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.verify_rc = verify_rc
        self.requirement_rc = requirement_rc
        self.adhoc = adhoc
        self.identifier = identifier
        self.format = f"app bundle with Mach-O universal ({archs})"
        self.arch_rc = arch_rc
        self.cdhashes = dict(cdhashes or _CDHASH)
        self.raises = raises
        self.team_id = team_id
        self.expected_team = expected_team
        self.runtime = runtime
        self.timestamp = timestamp
        self.designated = (
            designated if designated is not None
            else BUILD.designated_requirement(_TEST_TEAM)
        )
        self.entitlements = (
            dict(entitlements)
            if entitlements is not None
            else {"com.apple.security.automation.apple-events": True}
        )
        self.slices = dict(slices or {})
        self.display_text = display_text
        #: The bundle under test in the current inspection, set by its probes.
        self.bundle: str | None = None

    def _slice(self, arch: str) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "identifier": self.identifier,
            "team_id": self.team_id,
            "runtime": self.runtime,
            "timestamp": self.timestamp,
            "cdhash": self.cdhashes.get(arch),
            "designated": self.designated,
            "entitlements": self.entitlements,
            "entitlements_raw": None,
            "display_text": None,
            "requirement_text": None,
            "display_rc": 0,
            "entitlements_rc": 0,
            "designated_rc": 0,
        }
        fields.update(self.slices.get(arch, {}))
        return fields

    def _requirement_argument(self) -> str:
        """The ``--test-requirement`` text for the expected team, from the builder."""
        return "=" + BUILD.designated_requirement(self.expected_team)

    def _note_bundle(self, target: str) -> None:
        target = target.replace(os.sep, "/")
        if not os.path.isabs(target) or os.path.basename(target) != APP_NAME:
            raise AssertionError(f"probe does not name the host bundle: {target!r}")
        # Pinned to one bundle: the first target binds, and a later probe aimed
        # at another bundle-shaped path is a broken contract, not a new subject.
        if self.bundle is not None and target != self.bundle:
            raise AssertionError(
                f"probe names {target!r}, not the bundle under test {self.bundle!r}"
            )
        self.bundle = target

    def _note_executable(self, target: str) -> None:
        target = target.replace(os.sep, "/")
        suffix = "/" + _EXE_REL
        if not os.path.isabs(target) or not target.endswith(suffix):
            raise AssertionError(f"probe does not name the host executable: {target!r}")
        expected = None if self.bundle is None else self.bundle + suffix
        if expected is not None and target != expected:
            raise AssertionError(f"probe names {target!r}, not {expected!r}")
        self.bundle = target[: -len(suffix)]

    def display_for(self, arch: str) -> str:
        """The default ``codesign -dv`` text of one slice, before any override."""
        fields = self._slice(arch)
        return self._display_text(
            identifier=str(fields["identifier"]),
            cdhash=fields["cdhash"],
            team_id=str(fields["team_id"]),
            runtime=bool(fields["runtime"]),
            timestamp=bool(fields["timestamp"]),
            with_format=False,
        )

    def _answer_verify(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        plain = [_CODESIGN, "--verify", "--strict"]
        signed = [*plain, "--all-architectures"]
        if args[:3] != plain:
            raise AssertionError(f"unexpected verify probe: {args}")
        if len(args) == 4:
            self._note_bundle(args[3])
            return subprocess.CompletedProcess(args, self.verify_rc, "", "")
        if args[:4] != signed:
            raise AssertionError(f"unexpected verify probe: {args}")
        if len(args) == 5:
            self._note_bundle(args[4])
            return subprocess.CompletedProcess(args, self.verify_rc, "", "")
        if len(args) == 7 and args[4] == "--test-requirement":
            if args[5] != self._requirement_argument():
                raise AssertionError(
                    "the requirement text is not the expected team's: " + repr(args[5])
                )
            self._note_bundle(args[6])
            return subprocess.CompletedProcess(args, self.requirement_rc, "", "")
        raise AssertionError(f"unexpected verify probe: {args}")

    def _answer_display(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if len(args) == 4:
            self._note_bundle(args[3])
            stderr = self._display_text(
                identifier=self.identifier,
                cdhash=None,
                team_id=self.team_id,
                runtime=self.runtime,
                timestamp=self.timestamp,
                with_format=True,
            )
            if self.display_text is not None:
                stderr = self.display_text
            return subprocess.CompletedProcess(args, 0, "", stderr)
        if len(args) == 6 and args[3] == "--arch":
            arch = args[4]
            if arch not in ARCHITECTURES:
                raise AssertionError(f"probe names an unknown architecture: {args}")
            self._note_executable(args[5])
            if self.arch_rc != 0:
                return subprocess.CompletedProcess(args, self.arch_rc, "", "")
            fields = self._slice(arch)
            if fields["display_rc"] != 0:
                return subprocess.CompletedProcess(args, fields["display_rc"], "", "")
            text = fields["display_text"]
            return subprocess.CompletedProcess(
                args, 0, "", self.display_for(arch) if text is None else str(text)
            )
        raise AssertionError(f"unexpected display probe: {args}")

    def _answer_entitlements(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        expected = [
            _CODESIGN,
            "-d",
            "--entitlements",
            ":-",
            "--xml",
            "--arch",
        ]
        if len(args) != 8 or args[:6] != expected:
            raise AssertionError(f"unexpected entitlement probe: {args}")
        arch = args[6]
        if arch not in ARCHITECTURES:
            raise AssertionError(f"probe names an unknown architecture: {args}")
        self._note_executable(args[7])
        fields = self._slice(arch)
        if fields["entitlements_rc"] != 0:
            return subprocess.CompletedProcess(
                args, fields["entitlements_rc"], "", ""
            )
        raw = fields["entitlements_raw"]
        if raw is not None:
            return subprocess.CompletedProcess(args, 0, str(raw), "")
        blob = fields["entitlements"]
        text = "" if blob is None else plistlib.dumps(blob).decode("utf-8")
        return subprocess.CompletedProcess(args, 0, text, "")

    def _answer_designated(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        expected = [_CODESIGN, "-d", "-r-", "--arch"]
        if len(args) != 6 or args[:4] != expected:
            raise AssertionError(f"unexpected designated requirement probe: {args}")
        arch = args[4]
        if arch not in ARCHITECTURES:
            raise AssertionError(f"probe names an unknown architecture: {args}")
        self._note_executable(args[5])
        fields = self._slice(arch)
        if fields["designated_rc"] != 0:
            return subprocess.CompletedProcess(args, fields["designated_rc"], "", "")
        text = fields["requirement_text"]
        if text is not None:
            return subprocess.CompletedProcess(args, 0, "", str(text))
        requirement = fields["designated"]
        if requirement is None:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", f"designated => {requirement}\n")

    def _display_text(
        self,
        *,
        identifier: str,
        cdhash: str | None,
        team_id: str,
        runtime: bool,
        timestamp: bool,
        with_format: bool,
    ) -> str:
        lines = [f"Identifier={identifier}"]
        if with_format:
            lines.append(f"Format={self.format}")
        flags = "0x10000(runtime)" if runtime else "0x0(none)"
        lines.append(
            f"CodeDirectory v=20500 size=400 flags={flags} hashes=1+3 "
            "location=embedded"
        )
        lines.append("Signature size=9000")
        if self.adhoc:
            lines.append("Signature=adhoc")
        else:
            lines.append("Authority=Developer ID Application: Ciaobot Test")
            lines.append(f"TeamIdentifier={team_id}")
            if timestamp:
                lines.append("Timestamp=Oct 30, 2026 at 10:00:00")
            if runtime:
                lines.append("Runtime Version=13.0.0")
        if cdhash:
            lines.append(f"CDHash={cdhash}")
        return "\n".join(lines) + "\n"

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(argv))
        self.kwargs.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises
        args = list(argv)
        if args[:2] == [_CODESIGN, "--verify"]:
            return self._answer_verify(args)
        if args[:3] == [_CODESIGN, "-dv", "--verbose=4"]:
            return self._answer_display(args)
        if args[:4] == [_CODESIGN, "-d", "--entitlements", ":-"]:
            return self._answer_entitlements(args)
        if args[:3] == [_CODESIGN, "-d", "-r-"]:
            return self._answer_designated(args)
        raise AssertionError(f"unexpected native probe: {args}")


def _install_record(path: Path, snapshot: HostOwnership) -> Path:
    return _write_record(path, snapshot.to_record())


def test_inspect_bundle_snapshot_and_probe_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Needs no uid: the happy path reads no record. It exercises the bundle
    # collection (the O_NOFOLLOW/inode read) on native Windows too.
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
        [_CODESIGN, "--verify", "--strict", canonical_bundle],
        [_CODESIGN, "-dv", "--verbose=4", canonical_bundle],
        [_CODESIGN, "-dv", "--verbose=4", "--arch", "arm64", executable],
        [_CODESIGN, "-dv", "--verbose=4", "--arch", "x86_64", executable],
    ]
    for kwargs in runner.kwargs:
        assert "shell" not in kwargs
        assert kwargs["timeout"] == server_host.NATIVE_TIMEOUT_SECONDS
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False


def test_the_probe_fake_answers_only_the_complete_argv(tmp_path: Path) -> None:
    # The fake is the seam that proves the native contract, so it must refuse
    # every weakened form itself: otherwise dropping a flag from the production
    # probe would still be answered and the suite would stay green.
    bundle = _make_bundle(tmp_path)
    canonical_bundle = os.fspath(Path(os.path.realpath(bundle)))
    executable = f"{canonical_bundle}/{_EXE_REL}"
    arch = ARCHITECTURES[0]
    requirement = "=" + BUILD.designated_requirement(_TEST_TEAM)

    accepted = [
        [_CODESIGN, "--verify", "--strict", canonical_bundle],
        [_CODESIGN, "--verify", "--strict", "--all-architectures", canonical_bundle],
        [
            _CODESIGN,
            "--verify",
            "--strict",
            "--all-architectures",
            "--test-requirement",
            requirement,
            canonical_bundle,
        ],
        [_CODESIGN, "-dv", "--verbose=4", canonical_bundle],
        [_CODESIGN, "-dv", "--verbose=4", "--arch", arch, executable],
        [_CODESIGN, "-d", "--entitlements", ":-", "--xml", "--arch", arch, executable],
        [_CODESIGN, "-d", "-r-", "--arch", arch, executable],
    ]
    runner = _FakeRunner(adhoc=False)
    for argv in accepted:
        runner(argv)
    assert runner.commands == accepted

    elsewhere = os.fspath(tmp_path / "elsewhere" / APP_NAME)
    malformed = [
        # `--strict` dropped from either verification.
        [_CODESIGN, "--verify", canonical_bundle],
        [_CODESIGN, "--verify", "--all-architectures", canonical_bundle],
        [
            _CODESIGN,
            "--verify",
            "--strict",
            "--all-architectures",
            canonical_bundle,
            "extra",
        ],
        # `--all-architectures` dropped from, or the requirement missing in, the
        # external requirement probe.
        [
            _CODESIGN,
            "--verify",
            "--strict",
            "--test-requirement",
            requirement,
            canonical_bundle,
        ],
        [
            _CODESIGN,
            "--verify",
            "--strict",
            "--all-architectures",
            "--test-requirement",
            canonical_bundle,
        ],
        # Another team's requirement text, or the text without its `=` prefix.
        [
            _CODESIGN,
            "--verify",
            "--strict",
            "--all-architectures",
            "--test-requirement",
            "=" + BUILD.designated_requirement("OTHERTEAM1"),
            canonical_bundle,
        ],
        [
            _CODESIGN,
            "--verify",
            "--strict",
            "--all-architectures",
            "--test-requirement",
            BUILD.designated_requirement(_TEST_TEAM),
            canonical_bundle,
        ],
        # The bundle probe pointing at the executable, and the slice probe at
        # the bundle.
        [_CODESIGN, "--verify", "--strict", executable],
        [_CODESIGN, "-dv", "--verbose=4", executable],
        [_CODESIGN, "-dv", "--verbose=4", "--arch", arch, canonical_bundle],
        # `--verbose=4` or `--arch` dropped from the display read.
        [_CODESIGN, "-dv", canonical_bundle],
        [_CODESIGN, "-dv", "--verbose=4", "--arch", "arm64e", executable],
        # `--xml` or `--arch` dropped from the entitlement read.
        [_CODESIGN, "-d", "--entitlements", ":-", "--arch", arch, executable],
        [_CODESIGN, "-d", "--entitlements", ":-", "--xml", executable],
        [
            _CODESIGN,
            "-d",
            "--entitlements",
            ":-",
            "--xml",
            "--arch",
            arch,
            canonical_bundle,
        ],
        # `-r-` dropped from the designated requirement read.
        [_CODESIGN, "-d", "--arch", arch, executable],
        [_CODESIGN, "-d", "-r-", arch, executable],
        # A slice probe for another bundle than the one under test.
        [_CODESIGN, "-dv", "--verbose=4", "--arch", arch, f"{elsewhere}/{_EXE_REL}"],
        # A bundle probe for another bundle-shaped path after the first target
        # was bound: the fake is pinned to the bundle under test.
        [_CODESIGN, "-dv", "--verbose=4", elsewhere],
        [_CODESIGN, "--verify", "--strict", elsewhere],
        # Not a read-only codesign probe at all.
        ["/usr/bin/lipo", "-info", executable],
        [_CODESIGN, "--sign", "-", canonical_bundle],
        [_CODESIGN, "--verify", "--strict", canonical_bundle, "--deep"],
    ]
    for argv in malformed:
        with pytest.raises(AssertionError):
            runner(argv)


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


@pytest.mark.parametrize("name", ["pipe", "CodeResources"])
def test_inspect_rejects_a_special_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # A stray FIFO and a FIFO at a sealed name: the second reaches the
    # special-file branch rather than the unexpected-name one.
    monkeypatch.setattr(sys, "platform", "darwin")
    if not hasattr(os, "mkfifo"):
        pytest.skip("no mkfifo on this platform")
    bundle = _make_bundle(tmp_path)
    fifo = bundle / "Contents" / "_CodeSignature" / name
    fifo.unlink(missing_ok=True)
    os.mkfifo(fifo)
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
            lambda b: (b / _PLIST_REL).write_bytes(
                b"<?xml version='1.0'?><plist><dict><key>a</key>"
            ),
            server_host.INSPECTION_FAILED,
        ),
        (
            lambda b: (b / _PLIST_REL).write_bytes(
                b"<?xml version='1.0'?><plist version='1.0'><dict>"
                b"<key>a</key><integer>zz</integer></dict></plist>"
            ),
            server_host.INSPECTION_FAILED,
        ),
        # `plistlib.loads` raises AttributeError on a malformed <date>, and
        # LookupError on a bogus encoding declaration; both must refuse.
        (
            lambda b: (b / _PLIST_REL).write_bytes(
                b"<plist version='1.0'><date>zz</date></plist>"
            ),
            server_host.INSPECTION_FAILED,
        ),
        (
            lambda b: (b / _PLIST_REL).write_bytes(
                b"<?xml version='1.0' encoding='bogus'?><plist version='1.0'>"
                b"<dict/></plist>"
            ),
            server_host.INSPECTION_FAILED,
        ),
        (
            lambda b: _rewrite_plist(b, bundle_id="local.other"),
            server_host.INSPECTION_FAILED,
        ),
        (
            lambda b: _rewrite_plist(b, revision=HOST_REVISION + 1),
            server_host.INSPECTION_FAILED,
        ),
        (lambda b: _rewrite_plist(b, protocol=3), server_host.INSPECTION_FAILED),
        # A plist <true/> is not a protocol number, and a string is not one
        # either, although "2" is the protocol's own text.
        (lambda b: _rewrite_plist(b, protocol=True), server_host.INSPECTION_FAILED),
        (lambda b: _rewrite_plist(b, protocol="2"), server_host.INSPECTION_FAILED),
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
        (
            lambda b: (b / "Contents" / "_CodeSignature" / "CodeSignature").write_text(
                "x"
            ),
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
    runner = _FakeRunner()
    with pytest.raises(ServerHostError) as caught:
        inspect_host_bundle(bundle, runner=runner)
    assert caught.value.code == code
    # Every refusal above happens while reading the tree or the plist, before
    # the first native probe: a malformed bundle never reaches codesign.
    assert runner.commands == []


def test_inspect_refuses_a_plist_the_parser_escapes_as_indexerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A malformed XML/binary plist can make plistlib raise IndexError, which the
    # module must convert to an inspection_failed refusal rather than let escape.
    monkeypatch.setattr(sys, "platform", "darwin")

    def explode(_raw: bytes) -> object:
        raise IndexError("list index out of range")

    monkeypatch.setattr(server_host.plistlib, "loads", explode)
    bundle = _make_bundle(tmp_path)
    runner = _FakeRunner()
    with pytest.raises(ServerHostError) as caught:
        inspect_host_bundle(bundle, runner=runner)
    assert caught.value.code == server_host.INSPECTION_FAILED
    assert runner.commands == []


def test_inspect_rejects_a_bundle_with_another_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # host_service_argv only renders a bundle named APP_NAME, so the inspector
    # must not prove one under another name.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path, name="Other.app")
    runner = _FakeRunner()
    with pytest.raises(ServerHostError) as caught:
        inspect_host_bundle(bundle, runner=runner)
    assert caught.value.code == server_host.INSPECTION_FAILED
    assert runner.commands == []


@pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: 0)() == 0,
    reason="POSIX permissions, not as root",
)
def test_inspect_refuses_an_unlistable_stray_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # os.walk silently skips a directory it cannot list, so without an onerror
    # refusal an unreadable stray directory would never be checked.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    hidden = bundle / "Contents" / "Hidden"
    hidden.mkdir()
    hidden.chmod(0)
    try:
        with pytest.raises(ServerHostError) as caught:
            inspect_host_bundle(bundle, runner=_FakeRunner())
    finally:
        hidden.chmod(0o700)
    assert caught.value.code == server_host.INSPECTION_FAILED


def _rewrite_plist(
    bundle: Path,
    *,
    bundle_id: str | None = None,
    revision: int | None = None,
    protocol: object = None,
    minimum: str | None = None,
) -> None:
    document = BUILD.bundle_info()
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


@pytest.mark.parametrize(
    "runner, code",
    [
        (_FakeRunner(verify_rc=1), server_host.INSPECTION_FAILED),
        (_FakeRunner(adhoc=False), server_host.INSPECTION_FAILED),
        (_FakeRunner(identifier="local.other"), server_host.INSPECTION_FAILED),
        (_FakeRunner(archs="arm64"), server_host.INSPECTION_FAILED),
        (_FakeRunner(archs="x86_64 arm64 arm64e"), server_host.INSPECTION_FAILED),
        (_FakeRunner(archs="x86_64 x86_64"), server_host.INSPECTION_FAILED),
        (
            _FakeRunner(raises=subprocess.TimeoutExpired(_CODESIGN, 10)),
            server_host.INSPECTION_FAILED,
        ),
        (
            _FakeRunner(raises=FileNotFoundError(_CODESIGN)),
            server_host.INSPECTION_FAILED,
        ),
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
    record = _install_record(
        tmp_path / "server-host.json",
        inspect_host_bundle(bundle_a, runner=_FakeRunner()),
    )
    # One runner per bundle: the fake is pinned to the bundle it was given, so a
    # second bundle is inspected through its own runner.
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(bundle_b, ownership_path=record, runner=_FakeRunner())
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
        assert command[0] == _CODESIGN
        assert command[1] in ("--verify", "-dv")
        assert "--sign" not in command


# ── ownership: the superseded host this engine can still replace ────────────


def _make_superseded_bundle(root: Path, revision: int = 3) -> Path:
    """A host bundle of the superseded revision, with the old identity fields."""
    bundle = root / APP_NAME
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "Resources").mkdir(parents=True)
    (bundle / "Contents" / "_CodeSignature").mkdir(parents=True)
    (bundle / _EXE_REL).write_bytes(b"universal-host-binary")
    (bundle / _ICON_REL).write_bytes(b"indigo-icns-bytes")
    (bundle / _CODESIGN_REL).write_bytes(b"sealed-resources")
    document = BUILD.bundle_info()
    document["CFBundleVersion"] = str(revision)
    document["CFBundleShortVersionString"] = str(revision)
    document[HOST_PROTOCOL_KEY] = SUPERSEDED_HOST_PROTOCOLS[revision]
    with (bundle / _PLIST_REL).open("wb") as handle:
        plistlib.dump(document, handle)
    return bundle


def _superseded_record(bundle: Path, revision: int = 3) -> dict[str, Any]:
    """The ownership record a revision-3 installation would have written.

    The digests are the ones the bundle's own bytes hash to, so the only thing
    that separates this record from a current one is its revision and protocol.
    """
    document = _record(os.fspath(bundle))
    document["host_revision"] = revision
    document["host_protocol"] = SUPERSEDED_HOST_PROTOCOLS[revision]
    document["executable_sha256"] = hashlib.sha256(b"universal-host-binary").hexdigest()
    document["bundle_files"] = {
        _PLIST_REL: hashlib.sha256((bundle / _PLIST_REL).read_bytes()).hexdigest(),
        _EXE_REL: hashlib.sha256((bundle / _EXE_REL).read_bytes()).hexdigest(),
        _ICON_REL: hashlib.sha256((bundle / _ICON_REL).read_bytes()).hexdigest(),
        _CODESIGN_REL: hashlib.sha256((bundle / _CODESIGN_REL).read_bytes()).hexdigest(),
    }
    return document


@requires_posix_uid
@pytest.mark.parametrize("revision", [2, 3])
def test_a_superseded_revision_record_is_read_as_a_superseded_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: int
) -> None:
    # The current reader refuses anything that is not the current revision:
    # a superseded-revision record is not proof that the installed bundle is the host
    # this engine speaks to. The superseded reader is the one that answers
    # "is this bundle ours?", so the installer can replace it (#1659).
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_superseded_bundle(tmp_path, revision)
    runner = _FakeRunner()
    record = _write_record(
        tmp_path / "server-host.json",
        _superseded_record(bundle, revision),
    )

    # The current reader refuses it.
    with pytest.raises(ServerHostError) as caught:
        read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(bundle, ownership_path=record, runner=runner)
    assert caught.value.code == server_host.INVALID_OWNERSHIP

    # The superseded reader accepts it, and answers with the same identity.
    superseded = verify_superseded_host(bundle, ownership_path=record, runner=runner)
    assert superseded.host_revision == revision
    assert superseded.host_protocol == SUPERSEDED_HOST_PROTOCOLS[revision]
    assert superseded.bundle_path == os.fspath(bundle)
    assert superseded.bundle_id == BUNDLE_ID


@requires_posix_uid
def test_a_record_naming_the_other_superseded_revision_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each revision is proven against its own bytes: a revision-2 record cannot
    # vouch for a revision-3 bundle.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_superseded_bundle(tmp_path, 3)
    record = _write_record(tmp_path / "server-host.json", _superseded_record(bundle, 2))
    with pytest.raises(ServerHostError):
        verify_superseded_host(bundle, ownership_path=record, runner=_FakeRunner())


@requires_posix_uid
@pytest.mark.parametrize("revision", [1, 4, 5])
def test_only_the_superseded_revision_set_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: int
) -> None:
    # Revisions 2 and 3 are superseded. Anything else - a revision this engine
    # never shipped, the current one or a future one - is refused by both readers.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_superseded_bundle(tmp_path)
    document = _superseded_record(bundle)
    document["host_revision"] = revision
    record = _write_record(tmp_path / "server-host.json", document)
    with pytest.raises(ServerHostError):
        verify_superseded_host(bundle, ownership_path=record, runner=_FakeRunner())


@requires_posix_uid
def test_a_superseded_host_must_still_match_its_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The proof is not "the record says revision 3": the bundle's bytes must
    # still match that record exactly, exactly as verify_owned_host requires.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_superseded_bundle(tmp_path)
    runner = _FakeRunner()
    record = _write_record(
        tmp_path / "server-host.json",
        _superseded_record(bundle),
    )
    (bundle / _ICON_REL).write_bytes(b"tampered-icon")
    with pytest.raises(ServerHostError) as caught:
        verify_superseded_host(bundle, ownership_path=record, runner=runner)
    assert caught.value.code == server_host.NOT_OWNED


@requires_posix_uid
def test_a_superseded_host_with_no_record_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bundle with no record is never ours: there is nothing to prove it.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_superseded_bundle(tmp_path)
    with pytest.raises(ServerHostError) as caught:
        verify_superseded_host(
            bundle, ownership_path=tmp_path / "absent.json", runner=_FakeRunner()
        )
    assert caught.value.code == server_host.MISSING_RECORD


@requires_posix_uid
def test_a_superseded_host_whose_record_names_another_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record must name this bundle. A record that names another path is
    # not proof about this one, exactly as for the current host.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_superseded_bundle(tmp_path)
    document = _superseded_record(bundle)
    document["bundle_path"] = POSIX_BUNDLE
    record = _write_record(tmp_path / "server-host.json", document)
    with pytest.raises(ServerHostError) as caught:
        verify_superseded_host(bundle, ownership_path=record, runner=_FakeRunner())
    assert caught.value.code == server_host.NOT_OWNED


# ── macOS-only integration: the real universal host ─────────────────────────


@requires_macos_tools
def test_real_build_snapshot_record_and_reverify(tmp_path: Path) -> None:
    """Build the real host into scratch, record it privately, verify it.

    The record lives outside the bundle in the same scratch dir. A change beside
    the sealed app (the archive, an engine-side sidecar) does not disturb
    verification; a tampered resource is refused by the signature check, and the
    same tampered bundle re-signed ad hoc in scratch is a well-formed stranger
    that only the record refuses. No installed app, engine, launchd or
    permission surface is touched; only the scratch copy is ever re-signed.
    """
    result = BUILD.build(tmp_path / "build")
    app: Path = result["paths"]["app"]
    archive: Path = result["paths"]["archive"]

    snapshot = inspect_host_bundle(app)
    assert set(snapshot.bundle_files) == server_host.REQUIRED_BUNDLE_FILES
    assert snapshot.executable_sha256 == result["metadata"]["executable_sha256"]
    assert dict(snapshot.per_arch_cdhashes) == result["metadata"]["per_arch_cdhashes"]

    record = _install_record(tmp_path / "server-host.json", snapshot)
    assert stat.S_IMODE(record.stat().st_mode) == 0o600

    archive.write_bytes(archive.read_bytes() + b"sidecar-change")
    (app.parent / "engine-sidecar.json").write_text('{"engine": "changed"}')
    assert verify_owned_host(app, ownership_path=record) == snapshot

    (app / _ICON_REL).write_bytes(b"tampered")
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(app, ownership_path=record)
    assert caught.value.code == server_host.INSPECTION_FAILED

    subprocess.run(
        [_CODESIGN, "--force", "--sign", "-", os.fspath(app)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    assert inspect_host_bundle(app).bundle_files != snapshot.bundle_files
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(app, ownership_path=record)
    assert caught.value.code == server_host.NOT_OWNED


# ── the spaced executable name and the retired one ──

_EXE_REL_CURRENT = f"Contents/MacOS/{EXECUTABLE_NAME}"


def test_parse_hosted_shape_accepts_the_spaced_executable_name() -> None:
    # The host executable is named "Ciaobot Server". The argv is a list, so the
    # space is part of one element and never a word split.
    host = f"{POSIX_BUNDLE}/{_EXE_REL_CURRENT}"
    parsed = parse_service_command([host, "serve", "--python", "/usr/bin/python3"])
    assert parsed.mode == "hosted"
    assert parsed.program == host
    assert parsed.python == "/usr/bin/python3"
    rendered = host_service_argv(
        PurePosixPath(POSIX_BUNDLE), PurePosixPath("/usr/bin/python3")
    )
    assert rendered == (host, "serve", "--python", "/usr/bin/python3")
    assert parse_service_command(list(rendered)).program == host


def test_parse_refuses_the_retired_executable_name() -> None:
    legacy = f"{POSIX_BUNDLE}/Contents/MacOS/CiaobotServerHost"  # host revision 1's name (#1249)
    with pytest.raises(ServerHostError) as caught:
        parse_service_command([legacy, "serve", "--python", "/usr/bin/python3"])
    assert caught.value.code == server_host.INVALID_COMMAND


# ── the explicitly signed (revision 5) host policy (#2329) ──────────────────

SIGNED = DeveloperIDSigning(_TEST_TEAM)
#: A second fake team: one team everywhere would let a hardcoded team survive.
_OTHER_TEAM = "OTHERTEAM1"
SIGNED_OTHER = DeveloperIDSigning(_OTHER_TEAM)

_PLIST_DOCTYPE = (
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
)


def _make_signed_bundle(
    root: Path, *, revision: int = SIGNED_HOST_REVISION
) -> Path:
    """A Developer ID signed host bundle: the four files plus the stapled ticket."""
    bundle = root / APP_NAME
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "Resources").mkdir(parents=True)
    (bundle / "Contents" / "_CodeSignature").mkdir(parents=True)
    (bundle / _EXE_REL).write_bytes(b"developer-id-host-binary")
    (bundle / _ICON_REL).write_bytes(b"indigo-icns-bytes")
    (bundle / _CODESIGN_REL).write_bytes(b"sealed-resources")
    (bundle / _APP_TICKET_REL).write_bytes(b"notarization-ticket")
    with (bundle / _PLIST_REL).open("wb") as handle:
        plistlib.dump(BUILD.bundle_info(revision), handle)
    return bundle


def _signed_record(bundle: Path) -> dict[str, Any]:
    """A schema-2 record whose digests are the bundle's own bytes."""
    document = _record(os.fspath(bundle))
    document["schema"] = SIGNED_SCHEMA_VERSION
    document["host_revision"] = SIGNED_HOST_REVISION
    document["executable_sha256"] = hashlib.sha256(
        b"developer-id-host-binary"
    ).hexdigest()
    document["bundle_files"] = {
        name: hashlib.sha256((bundle / name).read_bytes()).hexdigest()
        for name in (
            _PLIST_REL,
            _EXE_REL,
            _ICON_REL,
            _CODESIGN_REL,
            _APP_TICKET_REL,
        )
    }
    document["signing"] = {"kind": "developer-id", "team_id": _TEST_TEAM}
    return document


def _inspect_signed(
    bundle: Path, runner: _FakeRunner, *, signing: DeveloperIDSigning = SIGNED
) -> HostOwnership:
    return inspect_host_bundle(
        bundle,
        runner=runner,
        revision=SIGNED_HOST_REVISION,
        protocol=HOST_PROTOCOL,
        signing=signing,
    )


def _entitlements_xml(*values: str) -> str:
    """The XML blob ``codesign --xml`` prints, declaring the key once per value.

    ``plistlib`` keeps only the last value of a repeated dictionary key, so this
    is the evidence a parsed dictionary cannot show on its own.
    """
    entries = "".join(
        "\t<key>com.apple.security.automation.apple-events</key>\n"
        f"\t<{value}/>\n"
        for value in values
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n" + entries + "</dict>\n"
        "</plist>\n"
    )


def test_constants_match_the_builder_for_the_signed_policy() -> None:
    # The builder signs what this runtime will accept: the signed revision and
    # the designated requirement must be identical, or a released archive would
    # fail its own contract. The parity is asserted here rather than importing a
    # build script into runtime code.
    assert server_host.SIGNED_HOST_REVISION == BUILD.SIGNED_HOST_REVISION
    assert server_host.HOST_REVISION == HOST_REVISION == 4
    assert server_host.SIGNED_SCHEMA_VERSION == 2
    assert (
        developer_id_requirement(_TEST_TEAM)
        == BUILD.designated_requirement(_TEST_TEAM)
    )
    for key in ("com.apple.security.automation.apple-events",):
        assert server_host.SIGNED_ENTITLEMENTS[key] is True
    assert set(server_host.SIGNED_ENTITLEMENTS) == set(BUILD.RELEASE_ENTITLEMENTS)
    assert server_host.SIGNED_BUNDLE_FILES == server_host.REQUIRED_BUNDLE_FILES | {
        "Contents/CodeResources"
    }


@pytest.mark.parametrize(
    "team",
    ["testteam01", "TESTTEAM0", "TESTTEAM012", "", "TEST TEAM1", "TESTTEAM01\n"],
)
def test_developer_id_team_must_be_ten_uppercase_characters(team: str) -> None:
    # A team is interpolated into literal requirement source, so it is validated
    # before any text is built.
    with pytest.raises(ServerHostError):
        developer_id_requirement(team)
    with pytest.raises(ServerHostError):
        DeveloperIDSigning(team)


def test_the_developer_id_requirement_is_stable_across_certificate_renewal() -> None:
    # Team, bundle and the two Developer ID extensions: no leaf fingerprint, no
    # CDHash, so a renewal within the team keeps the same logical identity.
    requirement = developer_id_requirement(_TEST_TEAM)
    assert f'identifier "{BUNDLE_ID}"' in requirement
    assert "anchor apple generic" in requirement
    assert "1.2.840.113635.100.6.2.6" in requirement
    assert "1.2.840.113635.100.6.1.13" in requirement
    assert f'certificate leaf[subject.OU] = "{_TEST_TEAM}"' in requirement
    assert "cdhash" not in requirement.lower()
    assert "H\"" not in requirement


@requires_posix_bundle_path
def test_signed_record_round_trips_with_expected_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_signed_bundle(tmp_path)
    snapshot = _inspect_signed(bundle, _FakeRunner(adhoc=False))

    assert snapshot.schema == SIGNED_SCHEMA_VERSION
    assert snapshot.host_revision == SIGNED_HOST_REVISION
    assert snapshot.host_protocol == HOST_PROTOCOL
    assert snapshot.signing == SIGNED
    assert set(snapshot.bundle_files) == SIGNED_BUNDLE_FILES
    assert snapshot.bundle_files[_APP_TICKET_REL] == hashlib.sha256(
        b"notarization-ticket"
    ).hexdigest()

    document = snapshot.to_record()
    assert set(document) == set(_record()) | {"signing"}
    assert document["signing"] == {"kind": "developer-id", "team_id": _TEST_TEAM}

    parsed = read_host_ownership(
        _write_record(tmp_path / "server-host.json", document)
    )
    assert parsed == snapshot
    assert parsed.to_record() == document
    # Immutable after the proof, like the legacy snapshot.
    with pytest.raises(TypeError):
        parsed.bundle_files[_PLIST_REL] = _HASH_B  # type: ignore[index]
    with pytest.raises(TypeError):
        parsed.per_arch_cdhashes["arm64"] = "0" * 40  # type: ignore[index]


@requires_posix_uid
def test_signed_policy_binds_the_team_from_the_private_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every successful signed case above used one fake team, so a team hardcoded
    # inside the requirement builder could have survived. A second team signs a
    # bundle end to end here, and the external `--test-requirement` text must
    # carry the team the private record names.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_signed_bundle(tmp_path)
    other_requirement = BUILD.designated_requirement(_OTHER_TEAM)
    assert other_requirement != BUILD.designated_requirement(_TEST_TEAM)

    runner = _FakeRunner(
        adhoc=False,
        team_id=_OTHER_TEAM,
        expected_team=_OTHER_TEAM,
        designated=other_requirement,
    )
    snapshot = _inspect_signed(bundle, runner, signing=SIGNED_OTHER)

    assert snapshot.signing == SIGNED_OTHER
    assert snapshot.to_record()["signing"] == {
        "kind": "developer-id",
        "team_id": _OTHER_TEAM,
    }
    probe = next(
        command for command in runner.commands if "--test-requirement" in command
    )
    assert probe[probe.index("--test-requirement") + 1] == "=" + other_requirement
    assert f'certificate leaf[subject.OU] = "{_OTHER_TEAM}"' in probe[5]

    # The record carries the team, and the same record verifies the same bundle.
    record = _install_record(tmp_path / "server-host.json", snapshot)
    assert verify_owned_host(bundle, ownership_path=record, runner=runner) == snapshot

    # A bundle signed by the other team is not this record's host, and vice
    # versa: the expected team comes from the record, never from the bundle.
    for signing, bundle_team in ((SIGNED, _OTHER_TEAM), (SIGNED_OTHER, _TEST_TEAM)):
        runner = _FakeRunner(
            adhoc=False,
            team_id=bundle_team,
            expected_team=signing.team_id,
            designated=BUILD.designated_requirement(bundle_team),
        )
        with pytest.raises(ServerHostError) as caught:
            _inspect_signed(bundle, runner, signing=signing)
        assert caught.value.code == SIGNATURE_REJECTED, (signing, bundle_team)


@requires_posix_bundle_path
def test_record_rejects_schema_revision_signing_confusion(tmp_path: Path) -> None:
    # One field of a valid signed record is broken at a time. Every case must be
    # a refusal: the schema, the revision and the signing identity are one
    # policy, and no half of it can stand in for the other.
    bundle = _make_signed_bundle(tmp_path)

    def _without_signing(document: dict[str, Any]) -> None:
        document.pop("signing")

    def _legacy_schema_with_signing(document: dict[str, Any]) -> None:
        document["schema"] = SCHEMA_VERSION
        document["host_revision"] = HOST_REVISION

    def _legacy_files(document: dict[str, Any]) -> None:
        for name in (_APP_TICKET_REL,):
            document["bundle_files"].pop(name)

    def _legacy_record_with_ticket(document: dict[str, Any]) -> None:
        document["schema"] = SCHEMA_VERSION
        document["host_revision"] = HOST_REVISION
        document.pop("signing")
        document["bundle_files"][_APP_TICKET_REL] = _HASH_B

    mutations: list[Any] = [
        _without_signing,
        lambda d: d["signing"].__setitem__("extra", "value"),
        lambda d: d["signing"].pop("team_id"),
        lambda d: d["signing"].__setitem__("kind", "apple-development"),
        lambda d: d["signing"].__setitem__("team_id", "testteam01"),
        lambda d: d["signing"].__setitem__("team_id", "TESTTEAM0"),
        lambda d: d["signing"].__setitem__("team_id", "TESTTEAM012"),
        lambda d: d["signing"].__setitem__("team_id", 1234567890),
        lambda d: d["signing"].__setitem__("team_id", True),
        lambda d: d.__setitem__("signing", None),
        lambda d: d.__setitem__("signing", [_TEST_TEAM]),
        _legacy_schema_with_signing,
        lambda d: d.__setitem__("host_revision", HOST_REVISION),
        lambda d: d.__setitem__("host_revision", SIGNED_HOST_REVISION + 1),
        lambda d: d.__setitem__("host_protocol", 3),
        lambda d: d.__setitem__("schema", True),
        lambda d: d.__setitem__("schema", SIGNED_SCHEMA_VERSION + 1),
        _legacy_files,
        _legacy_record_with_ticket,
    ]
    for mutate in mutations:
        document = _signed_record(bundle)
        mutate(document)
        record = _write_record(tmp_path / "server-host.json", document)
        with pytest.raises(ServerHostError) as caught:
            read_host_ownership(record)
        assert caught.value.code in (
            server_host.INVALID_OWNERSHIP,
            server_host.UNSUPPORTED_SCHEMA,
        ), mutate

    # Control: the untouched signed record is accepted, so the loop above is not
    # passing because every schema-2 record is refused.
    accepted = read_host_ownership(
        _write_record(tmp_path / "control.json", _signed_record(bundle))
    )
    assert accepted.signing == SIGNED


def test_signed_host_requires_external_developer_id_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The proof is the external requirement codesign evaluates against the
    # certificate chain, not a display-text `TeamIdentifier=` claim: the probe is
    # observed at the seam, before any snapshot exists.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_signed_bundle(tmp_path)
    runner = _FakeRunner(adhoc=False)
    snapshot = _inspect_signed(bundle, runner)

    requirement_probes = [
        command for command in runner.commands if "--test-requirement" in command
    ]
    assert len(requirement_probes) == 1
    probe = requirement_probes[0]
    expected = "=" + developer_id_requirement(_TEST_TEAM)
    assert probe[probe.index("--test-requirement") + 1] == expected
    assert "--all-architectures" in probe
    assert probe[-1] == os.fspath(Path(os.path.realpath(bundle)))
    assert snapshot.signing == SIGNED

    # The requirement comes before the first per-slice evidence is used.
    first_slice = next(
        index
        for index, command in enumerate(runner.commands)
        if "--arch" in command
    )
    assert runner.commands.index(probe) < first_slice

    # A wrong team, a wrong certificate chain and a wrong identifier each
    # disprove the policy, even when every other field looks right.
    for runner in (
        _FakeRunner(adhoc=False, requirement_rc=1),
        _FakeRunner(adhoc=False, team_id="OTHERTEAM1"),
        _FakeRunner(adhoc=False, identifier="local.other"),
        _FakeRunner(adhoc=False, verify_rc=1),
    ):
        with pytest.raises(ServerHostError) as caught:
            _inspect_signed(bundle, runner)
        assert caught.value.code == SIGNATURE_REJECTED


def test_signed_host_checks_policy_on_every_architecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One bad slice cannot hide behind the other: the policy is checked per
    # architecture, and the integrity probes cover every architecture.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_signed_bundle(tmp_path)

    for arch in ARCHITECTURES:
        for override in (
            {"team_id": "OTHERTEAM1"},
            {"identifier": "local.other"},
            {"runtime": False},
            {"timestamp": False},
            {"cdhash": "nothex"},
            {"display_rc": 1},
        ):
            runner = _FakeRunner(adhoc=False, slices={arch: override})
            with pytest.raises(ServerHostError) as caught:
                _inspect_signed(bundle, runner)
            assert caught.value.code == SIGNATURE_REJECTED, (arch, override)

    runner = _FakeRunner(adhoc=False)
    _inspect_signed(bundle, runner)
    verify_commands = [
        command for command in runner.commands if command[:2] == [_CODESIGN, "--verify"]
    ]
    assert verify_commands
    for command in verify_commands:
        assert "--all-architectures" in command
    probed = [
        command[command.index("--arch") + 1]
        for command in runner.commands
        if "--arch" in command
    ]
    assert sorted(probed) == sorted(ARCHITECTURES * 3)


def test_signed_host_requires_minimal_apple_events_entitlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The build's exact entitlement set is the only one accepted; a missing,
    # false, coerced, extra, runtime-relaxing or malformed blob is refused.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_signed_bundle(tmp_path)

    for entitlements in (
        None,
        {},
        {"com.apple.security.automation.apple-events": False},
        {"com.apple.security.automation.apple-events": 1},
        {"com.apple.security.automation.apple-events": "true"},
        {"com.apple.security.cs.allow-jit": True},
        {"com.apple.security.cs.disable-library-validation": True},
        {"com.apple.security.cs.get-task-allow": True},
        {
            "com.apple.security.automation.apple-events": True,
            "com.apple.security.cs.allow-jit": True,
        },
    ):
        for arch in ARCHITECTURES:
            runner = _FakeRunner(
                adhoc=False, slices={arch: {"entitlements": entitlements}}
            )
            with pytest.raises(ServerHostError) as caught:
                _inspect_signed(bundle, runner)
            assert caught.value.code == SIGNATURE_REJECTED, (arch, entitlements)

    for raw in ("not a plist", plistlib.dumps(["a list"]).decode("utf-8")):
        runner = _FakeRunner(
            adhoc=False, slices={ARCHITECTURES[0]: {"entitlements_raw": raw}}
        )
        with pytest.raises(ServerHostError) as caught:
            _inspect_signed(bundle, runner)
        assert caught.value.code == SIGNATURE_REJECTED

    # The builder's release entitlement set is exactly the accepted one: the
    # usage strings stay in Info.plist and are not part of this policy.
    assert set(BUILD.RELEASE_ENTITLEMENTS) == set(server_host.SIGNED_ENTITLEMENTS)
    assert BUILD.RELEASE_ENTITLEMENTS == dict(server_host.SIGNED_ENTITLEMENTS)
    _inspect_signed(bundle, _FakeRunner(adhoc=False))


def test_signed_host_requires_stable_designated_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The embedded requirement must be the stable team-scoped one the builder
    # signs with. A code-specific or leaf-pinned requirement is refused: it
    # would make every rebuild a different logical identity.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_signed_bundle(tmp_path)
    canonical = BUILD.designated_requirement(_TEST_TEAM)
    assert canonical == developer_id_requirement(_TEST_TEAM)

    # The builder's own text (with its `/* exists */` annotations) passes.
    _inspect_signed(bundle, _FakeRunner(adhoc=False, designated=canonical))
    # So does the same requirement spelled with an explicit `exists`, which is
    # one of the two spellings codesign prints for the same test.
    spelled = re.sub(r"\s*/\* exists \*/", " exists", canonical)
    assert spelled != canonical
    _inspect_signed(bundle, _FakeRunner(adhoc=False, designated=spelled))

    for designated in (
        None,
        f'cdhash H"{_CDHASH["arm64"]}"',
        'certificate leaf = H"0123456789abcdef"',
        f'identifier "{BUNDLE_ID}"',
        BUILD.designated_requirement("OTHERTEAM1"),
        f'{canonical} or certificate leaf = H"0123456789abcdef"',
    ):
        for arch in ARCHITECTURES:
            runner = _FakeRunner(
                adhoc=False, slices={arch: {"designated": designated}}
            )
            with pytest.raises(ServerHostError) as caught:
                _inspect_signed(bundle, runner)
            assert caught.value.code == SIGNATURE_REJECTED, (arch, designated)

    runner = _FakeRunner(
        adhoc=False, slices={ARCHITECTURES[0]: {"designated_rc": 1}}
    )
    with pytest.raises(ServerHostError) as caught:
        _inspect_signed(bundle, runner)
    assert caught.value.code == SIGNATURE_REJECTED


def test_signed_host_refuses_duplicate_or_conflicting_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `codesign -dv` output is evidence, not a claim. A field the policy consumes
    # may be printed once and only once: a repeated value is a contradiction even
    # when both copies agree, because the policy cannot know which one the bundle
    # really carries. `Authority` is the exception — a Developer ID chain prints
    # it once per certificate.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_signed_bundle(tmp_path)
    arch = ARCHITECTURES[0]
    other = ARCHITECTURES[1]
    text = _FakeRunner(adhoc=False).display_for(arch)

    code_directory = next(
        line for line in text.splitlines() if line.startswith("CodeDirectory")
    )
    # Each entry is what gets appended to the slice's display: one or more lines.
    repeated = [
        # Repeats that agree with the line already printed.
        f"Identifier={BUNDLE_ID}",
        f"TeamIdentifier={_TEST_TEAM}",
        "Signature=Developer ID Application: Ciaobot Test\n"
        "Signature=Developer ID Application: Ciaobot Test",
        "Runtime Version=13.0.0",
        "Timestamp=Oct 30, 2026 at 11:00:00",
        f"CDHash={_CDHASH[arch]}",
        code_directory,
        # Repeats that contradict it.
        "Identifier=local.other",
        "Signature=adhoc\nSignature=adhoc",
        "TeamIdentifier=OTHERTEAM1",
        "Runtime Version=12.0.0",
        "Timestamp=",
        f"CDHash={_CDHASH[other]}",
        "CodeDirectory v=20500 size=400 flags=0x0(none) hashes=1+3 "
        "location=embedded",
    ]
    for line in repeated:
        runner = _FakeRunner(
            adhoc=False, slices={arch: {"display_text": text + line + "\n"}}
        )
        with pytest.raises(ServerHostError) as caught:
            _inspect_signed(bundle, runner)
        assert caught.value.code == SIGNATURE_REJECTED, line

    # The bundle's own display is the same evidence, so it is refused the same
    # way: a second Identifier or Signature line would silently win.
    bundle_text = _FakeRunner(adhoc=False)._display_text(
        identifier=BUNDLE_ID,
        cdhash=None,
        team_id=_TEST_TEAM,
        runtime=True,
        timestamp=True,
        with_format=True,
    )
    format_line = next(
        line for line in bundle_text.splitlines() if line.startswith("Format=")
    )
    for line in (
        f"Identifier={BUNDLE_ID}",
        "Identifier=local.other",
        "Signature=adhoc",
        # Format decides the architecture set, so a repeat of it is the same
        # contradiction: agreeing first, then contradicting.
        format_line,
        "Format=app bundle with Mach-O universal (arm64)",
        "Format=app bundle",
    ):
        runner = _FakeRunner(adhoc=False, display_text=bundle_text + line + "\n")
        with pytest.raises(ServerHostError) as caught:
            _inspect_signed(bundle, runner)
        assert caught.value.code == SIGNATURE_REJECTED, line

    # Control: one Format line per bundle, as real codesign prints it, is fine.
    _inspect_signed(bundle, _FakeRunner(adhoc=False))

    # Authority repeats legitimately, so a real chain's display is still accepted.
    authorities = text + "".join(
        "Authority=Developer ID Certification Authority\nAuthority=Apple Root CA\n"
        for _ in range(3)
    )
    _inspect_signed(
        bundle, _FakeRunner(adhoc=False, slices={arch: {"display_text": authorities}})
    )

    # One designated requirement, from one line: a second line is a second
    # identity, whether it repeats the first or names another team.
    canonical = BUILD.designated_requirement(_TEST_TEAM)
    for first, second in (
        (canonical, canonical),
        (canonical, BUILD.designated_requirement(_OTHER_TEAM)),
        (BUILD.designated_requirement(_OTHER_TEAM), canonical),
    ):
        runner = _FakeRunner(
            adhoc=False,
            slices={
                arch: {
                    "requirement_text": (
                        f"designated => {first}\ndesignated => {second}\n"
                    )
                }
            },
        )
        with pytest.raises(ServerHostError) as caught:
            _inspect_signed(bundle, runner)
        assert caught.value.code == SIGNATURE_REJECTED, (first, second)

    # A duplicated entitlement key is lost by the parser: it keeps the last
    # value, so `false` then `true` would read as the accepted `true`. The
    # declared key count is the evidence, and it is a refusal, never a crash.
    assert plistlib.loads(
        _entitlements_xml("false", "true").encode("utf-8")
    ) == {"com.apple.security.automation.apple-events": True}
    for values in (("false", "true"), ("true", "false"), ("true", "true")):
        for slice_arch in ARCHITECTURES:
            runner = _FakeRunner(
                adhoc=False,
                slices={slice_arch: {"entitlements_raw": _entitlements_xml(*values)}},
            )
            with pytest.raises(ServerHostError) as caught:
                _inspect_signed(bundle, runner)
            assert caught.value.code == SIGNATURE_REJECTED, (slice_arch, values)

    # Control: the same blob with each key declared once is accepted, so the
    # refusals above are about duplication and not about the XML shape.
    _inspect_signed(
        bundle,
        _FakeRunner(
            adhoc=False,
            slices={arch: {"entitlements_raw": _entitlements_xml("true")}},
        ),
    )


def _plist_document(
    inner: str, *, declaration: str = _PLIST_DOCTYPE, after_dict: str = "\n"
) -> str:
    """A complete XML plist whose ``<dict>`` holds ``inner`` verbatim."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + declaration
        + '<plist version="1.0">\n<dict>\n'
        + inner
        + "</dict>"
        + after_dict
        + "</plist>\n"
    )


def test_signed_host_entitlements_are_a_strict_xml_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The entitlement read is a strict structural allowlist over the whole
    # document, not a count of keys the parser happened to keep: plistlib
    # flattens an unknown wrapper, so a wrapped `false` key followed by the
    # permitted direct one would otherwise read as the accepted dictionary.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_signed_bundle(tmp_path)
    key = "com.apple.security.automation.apple-events"
    allowed = f"\t<key>{key}</key>\n\t<true/>\n"

    wrapped = _plist_document(
        f"\t<foo><key>{key}</key><false/></foo>\n" + allowed
    )
    # The premise: the parser really does flatten the wrapper to the allowed set.
    assert plistlib.loads(wrapped.encode("utf-8")) == {key: True}

    entity = _plist_document(
        "\t<key>&evt;</key>\n\t<true/>\n",
        declaration=f'<!DOCTYPE plist [<!ENTITY evt "{key}">]>\n',
    )
    refused = {
        # A key and value inside an unknown wrapper, with and without the
        # permitted direct pair beside it.
        "wrapped with direct key": wrapped,
        "wrapped only": _plist_document(
            f"\t<foo><key>{key}</key><true/></foo>\n"
        ),
        # A nested dictionary, whose inner key must not stand in for a top-level
        # one, with and without the permitted direct pair.
        "nested dict with direct key": _plist_document(
            f"\t<key>outer</key><dict><key>{key}</key><true/></dict>\n" + allowed
        ),
        "nested dict only": _plist_document(
            f"\t<key>outer</key><dict><key>{key}</key><true/></dict>\n"
        ),
        # CDATA and a comment are invisible to a text scan but not to the shape.
        "cdata key": _plist_document(f"\t<key><![CDATA[{key}]]></key>\n\t<true/>\n"),
        "comment beside the value": _plist_document(
            f"\t<key>{key}</key><!-- value -->\n\t<true/>\n"
        ),
        # An entity reference; plistlib rejects the declaration as well, and the
        # allowlist refuses the document before any value is trusted.
        "entity key": entity,
        "undefined entity": _plist_document("\t<key>&evt;</key>\n\t<true/>\n"),
        # Numeric character references: expat expands them without ever calling
        # an entity handler, so `&#46;` would spell the permitted key literally.
        "decimal character reference key": _plist_document(
            "\t<key>com&#46;apple.security.automation.apple-events</key>\n\t<true/>\n"
        ),
        "hex character reference key": _plist_document(
            "\t<key>com&#x2E;apple.security.automation.apple-events</key>\n\t<true/>\n"
        ),
        # Non-whitespace text after the dictionary is inside the plist but
        # outside the one dictionary the shape allows.
        "stray text after the dictionary": _plist_document(
            allowed, after_dict="stray\n"
        ),
        "stray text hugging the dictionary": _plist_document(allowed, after_dict=" x "),
    }
    for label, document in refused.items():
        runner = _FakeRunner(
            adhoc=False,
            slices={ARCHITECTURES[0]: {"entitlements_raw": document}},
        )
        with pytest.raises(ServerHostError) as caught:
            _inspect_signed(bundle, runner)
        assert caught.value.code == SIGNATURE_REJECTED, label

    # Control: the exact shape codesign --xml prints is accepted — the key
    # written literally with no character reference, and only whitespace after
    # the dictionary — so the cases above are refused for their shape and not
    # because everything is.
    _inspect_signed(
        bundle,
        _FakeRunner(
            adhoc=False,
            slices={
                ARCHITECTURES[0]: {
                    "entitlements_raw": _plist_document(allowed, after_dict="\n")
                }
            },
        ),
    )


@requires_posix_uid
def test_signed_ticket_is_recorded_and_stray_files_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_signed_bundle(tmp_path)
    runner = _FakeRunner(adhoc=False)
    snapshot = _inspect_signed(bundle, runner)
    record = _install_record(tmp_path / "server-host.json", snapshot)

    # The ticket is ordinary immutable record material: the same bytes verify,
    # an altered or deleted ticket does not.
    assert verify_owned_host(bundle, ownership_path=record, runner=runner) == snapshot
    (bundle / _APP_TICKET_REL).write_bytes(b"altered-ticket")
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(bundle, ownership_path=record, runner=runner)
    assert caught.value.code == server_host.NOT_OWNED
    (bundle / _APP_TICKET_REL).unlink()
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(bundle, ownership_path=record, runner=runner)
    assert caught.value.code == server_host.INSPECTION_FAILED
    (bundle / _APP_TICKET_REL).write_bytes(b"notarization-ticket")

    # The signature's own resource is still required, and nothing else is
    # allowed in: no free-form file list exists.
    (bundle / _CODESIGN_REL).unlink()
    with pytest.raises(ServerHostError) as caught:
        _inspect_signed(bundle, _FakeRunner(adhoc=False))
    assert caught.value.code == server_host.INSPECTION_FAILED
    (bundle / _CODESIGN_REL).write_bytes(b"sealed-resources")

    (bundle / "Contents" / "Resources" / "extra.txt").write_text("x")
    with pytest.raises(ServerHostError) as caught:
        _inspect_signed(bundle, _FakeRunner(adhoc=False))
    assert caught.value.code == server_host.INSPECTION_FAILED
    (bundle / "Contents" / "Resources" / "extra.txt").unlink()

    (bundle / "Contents" / "Frameworks").mkdir()
    with pytest.raises(ServerHostError) as caught:
        _inspect_signed(bundle, _FakeRunner(adhoc=False))
    assert caught.value.code == server_host.INSPECTION_FAILED
    (bundle / "Contents" / "Frameworks").rmdir()

    # A production revision-4 bundle does not grow a ticket: the legacy policy
    # keeps exactly its four files.
    legacy = _make_bundle(tmp_path / "legacy")
    (legacy / _APP_TICKET_REL).write_bytes(b"notarization-ticket")
    with pytest.raises(ServerHostError) as caught:
        inspect_host_bundle(legacy, runner=_FakeRunner())
    assert caught.value.code == server_host.INSPECTION_FAILED


@requires_posix_uid
def test_owned_revision_four_remains_runtime_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The production host a live install already has: a schema-1 record still
    # verifies through the same call, runs no signed-policy probe and writes
    # nothing. The wheel can update without changing the host.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path)
    runner = _FakeRunner()
    snapshot = inspect_host_bundle(bundle, runner=runner)
    record = _install_record(tmp_path / "server-host.json", snapshot)
    assert snapshot.schema == SCHEMA_VERSION
    assert snapshot.signing is None

    before = sorted(path.as_posix() for path in tmp_path.rglob("*"))
    verified = verify_owned_host(bundle, ownership_path=record, runner=runner)
    after = sorted(path.as_posix() for path in tmp_path.rglob("*"))

    assert verified == snapshot
    assert verified.schema == SCHEMA_VERSION
    assert verified.signing is None
    assert before == after
    commands = runner.commands[len(runner.commands) // 2 :]
    for command in commands:
        assert command[0] == _CODESIGN
        assert "--test-requirement" not in command
        assert "--all-architectures" not in command
        assert "--entitlements" not in command
        assert "-r-" not in command
        assert "--sign" not in command


@requires_posix_uid
def test_signed_verification_never_downgrades_to_adhoc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record selects the signed policy. An ad-hoc signature — even over the
    # very bytes the record hashes — disproves it, and there is no second
    # attempt at the legacy policy.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_signed_bundle(tmp_path)
    snapshot = _inspect_signed(bundle, _FakeRunner(adhoc=False))
    record = _install_record(tmp_path / "server-host.json", snapshot)
    canonical_bundle = os.fspath(Path(os.path.realpath(bundle)))
    requirement_probe = [
        _CODESIGN,
        "--verify",
        "--strict",
        "--all-architectures",
        "--test-requirement",
        "=" + developer_id_requirement(_TEST_TEAM),
        canonical_bundle,
    ]

    # Every inspection of a record's bundle goes through one policy entry point.
    # Counting probes alone cannot catch a retry that fails during file
    # collection or plist validation before issuing any probe, so the entry
    # point itself is observed: exactly one call, with the record's policy.
    inspections: list[dict[str, Any]] = []
    inspect = server_host.inspect_host_bundle

    def spy(bundle_path: Path, **kwargs: Any) -> HostOwnership:
        inspections.append({"bundle_path": bundle_path, **kwargs})
        return inspect(bundle_path, **kwargs)

    monkeypatch.setattr(server_host, "inspect_host_bundle", spy)

    adhoc = _FakeRunner(adhoc=True, requirement_rc=1)
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(bundle, ownership_path=record, runner=adhoc)
    assert caught.value.code == SIGNATURE_REJECTED
    assert inspections == [
        {
            "bundle_path": bundle,
            "runner": adhoc,
            "revision": SIGNED_HOST_REVISION,
            "protocol": HOST_PROTOCOL,
            "signing": SIGNED,
        }
    ]
    # The complete signed probe sequence, and nothing else: no display read, no
    # per-slice probe and no entitlement read, so the legacy ad-hoc inspection
    # (`_require_signed_universal` and `_per_arch_cdhashes`) was never entered.
    # A fallback that merely failed would still have run those probes.
    assert adhoc.commands == [
        [_CODESIGN, "--verify", "--strict", "--all-architectures", canonical_bundle],
        requirement_probe,
    ]
    assert all("--sign" not in command for command in adhoc.commands)

    # Re-signed bytes change the file hashes too; the policy is still what
    # refuses, so the reason is the signature and not merely the record match.
    (bundle / _ICON_REL).write_bytes(b"ad-hoc-resign")
    resign = _FakeRunner(adhoc=True)
    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(bundle, ownership_path=record, runner=resign)
    assert caught.value.code == SIGNATURE_REJECTED
    # Still one inspection, still with the signed policy, never a second attempt
    # under the legacy one.
    assert inspections == [
        {
            "bundle_path": bundle,
            "runner": adhoc,
            "revision": SIGNED_HOST_REVISION,
            "protocol": HOST_PROTOCOL,
            "signing": SIGNED,
        },
        {
            "bundle_path": bundle,
            "runner": resign,
            "revision": SIGNED_HOST_REVISION,
            "protocol": HOST_PROTOCOL,
            "signing": SIGNED,
        },
    ]
    # The signed policy rejected the display's ad-hoc signature before any
    # slice was inspected; the legacy policy's own verify and per-slice probes
    # (no `--all-architectures`) never ran.
    assert resign.commands == [
        [_CODESIGN, "--verify", "--strict", "--all-architectures", canonical_bundle],
        requirement_probe,
        [_CODESIGN, "-dv", "--verbose=4", canonical_bundle],
    ]
    assert all("--arch" not in command for command in resign.commands)


@requires_posix_uid
def test_signature_rejection_and_probe_unavailability_are_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A completed probe that disproves the policy is `signature_rejected`; a
    # probe that cannot start or hangs is `inspection_failed`. Neither
    # authorizes activation, but a caller can tell them apart.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_signed_bundle(tmp_path)
    record = _install_record(
        tmp_path / "server-host.json",
        _inspect_signed(bundle, _FakeRunner(adhoc=False)),
    )

    with pytest.raises(ServerHostError) as caught:
        verify_owned_host(
            bundle,
            ownership_path=record,
            runner=_FakeRunner(adhoc=False, requirement_rc=1),
        )
    assert caught.value.code == SIGNATURE_REJECTED

    for raises in (
        subprocess.TimeoutExpired(_CODESIGN, server_host.NATIVE_TIMEOUT_SECONDS),
        FileNotFoundError(_CODESIGN),
        OSError("the probe could not start"),
    ):
        with pytest.raises(ServerHostError) as caught:
            verify_owned_host(
                bundle, ownership_path=record, runner=_FakeRunner(raises=raises)
            )
        assert caught.value.code == server_host.INSPECTION_FAILED


def test_signed_policy_needs_an_explicit_team_and_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The policy is named by the caller and never learned from the bundle: the
    # signed revision without a team, a team under a legacy revision, and a
    # future revision are all refusals before any probe runs.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_signed_bundle(tmp_path)
    legacy = _make_bundle(tmp_path / "legacy")

    runner = _FakeRunner(adhoc=False)
    with pytest.raises(ServerHostError) as caught:
        inspect_host_bundle(
            bundle,
            runner=runner,
            revision=SIGNED_HOST_REVISION,
            protocol=HOST_PROTOCOL,
        )
    assert caught.value.code == server_host.INSPECTION_FAILED
    assert runner.commands == []

    for revision, protocol in (
        (HOST_REVISION, HOST_PROTOCOL),
        (SIGNED_HOST_REVISION + 1, HOST_PROTOCOL),
        (SIGNED_HOST_REVISION, HOST_PROTOCOL + 1),
    ):
        runner = _FakeRunner(adhoc=False)
        with pytest.raises(ServerHostError):
            inspect_host_bundle(
                legacy if revision == HOST_REVISION else bundle,
                runner=runner,
                revision=revision,
                protocol=protocol,
                signing=SIGNED,
            )
        assert runner.commands == []

    # A signed record cannot vouch for a revision-4 bundle either: the plist and
    # the policy disagree.
    with pytest.raises(ServerHostError):
        inspect_host_bundle(
            legacy,
            runner=_FakeRunner(adhoc=False),
            revision=SIGNED_HOST_REVISION,
            protocol=HOST_PROTOCOL,
            signing=SIGNED,
        )
