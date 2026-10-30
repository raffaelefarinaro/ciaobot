"""Checks for the production Ciaobot Server native host and its builder (#1009).

The pure checks import the builder and the real ``ciao`` constants and run on
every platform. The macOS-only checks compile the actual ``ServerHost.swift``
and a small C child fixture once per module and drive the host with it, so the
spawn contract (argv, cwd, environment, process group, signal mask and
dispositions, stdio) is proved by executing the shipped host against a real
child. Nothing here launches the ``.app`` through LaunchServices, boots the
engine, installs a bundle, or touches permissions or TCC: the
``request-accessibility`` branch is deliberately never executed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from types import ModuleType
from collections.abc import Callable
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build-server-host.py"
HOST_SOURCE = ROOT / "native" / "server-host" / "ServerHost.swift"
ICON_PATH = ROOT / "ciao" / "stock" / "deploy" / "CiaobotServer.icns"

# The exact bytes PR #119 restored for the production host icon.
EXPECTED_ICON_SHA256 = "0dc70c6b612cd43a9e04fb79dd9f6ac3c02d19a976e2235845535617f8eb0f7d"

# The offline child fixture the macOS runtime tests compile and run under the
# host. It is NOT production code and never ships; it records the observable
# spawn contract and can spawn a descendant or react to SIGTERM. It is embedded
# here so the test suite stays within the six scoped files.
_HELPER_C_SOURCE = r"""
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>

static const char *env_or(const char *key, const char *fallback) {
    const char *v = getenv(key);
    return v ? v : fallback;
}

static void json_string(FILE *f, const char *s) {
    fputc('"', f);
    for (const char *p = s; *p; p++) {
        if (*p == '"' || *p == '\\') { fputc('\\', f); fputc(*p, f); }
        else if (*p == '\n') fputs("\\n", f);
        else fputc(*p, f);
    }
    fputc('"', f);
}

static const char *disposition(void *h) {
    if (h == SIG_DFL) return "dfl";
    if (h == SIG_IGN) return "ign";
    return "handler";
}

/* The signal state the child inherited from the host, read before the child
   installs anything of its own: the record reports what the host handed over. */
static sigset_t inherited_mask;
static struct sigaction inherited_term, inherited_intr;

static void capture_inherited(void) {
    sigemptyset(&inherited_mask);
    sigprocmask(SIG_BLOCK, NULL, &inherited_mask);
    sigaction(SIGTERM, NULL, &inherited_term);
    sigaction(SIGINT, NULL, &inherited_intr);
}

static void write_record(int argc, char **argv) {
    const char *path = getenv("HOST_RECORD");
    if (!path) return;
    FILE *f = fopen(path, "w");
    if (!f) return;
    int record_fd = fileno(f);
    unsigned int bits = 0;
    memcpy(&bits, &inherited_mask, sizeof(bits));
    struct sigaction term = inherited_term, intr = inherited_intr;
    int extra = -1;
    const char *extra_env = getenv("HOST_EXTRA_FD");
    if (extra_env) extra = atoi(extra_env);
    int extra_open = (extra >= 0 && fcntl(extra, F_GETFD) != -1) ? 1 : 0;
    char cwd[4096];
    if (!getcwd(cwd, sizeof(cwd))) cwd[0] = '\0';
    fprintf(f, "{");
    fprintf(f, "\"pgid\":%d,\"sid\":%d,\"pid\":%d,\"ppid\":%d,",
            (int)getpgid(0), (int)getsid(0), (int)getpid(), (int)getppid());
    fprintf(f, "\"cwd\":");
    json_string(f, cwd);
    fprintf(f, ",\"mask\":%u,", bits);
    fprintf(f, "\"term\":\"%s\",\"int\":\"%s\",",
            disposition((void *)term.sa_handler), disposition((void *)intr.sa_handler));
    fprintf(f, "\"extra_fd\":%d,\"extra_fd_open\":%s,", extra, extra_open ? "true" : "false");
    fprintf(f, "\"mark\":");
    json_string(f, env_or("CIAO_MARK", ""));
    fprintf(f, ",\"open_fds\":[");
    int first = 1;
    for (int fd = 0; fd < 64; fd++) {
        if (fd == record_fd) continue;
        if (fcntl(fd, F_GETFD) != -1) { fprintf(f, "%s%d", first ? "" : ",", fd); first = 0; }
    }
    fprintf(f, "],\"args\":[");
    for (int i = 1; i < argc; i++) {
        if (i > 1) fputc(',', f);
        json_string(f, argv[i]);
    }
    fprintf(f, "]}\n");
    fclose(f);
}

static void spawn_descendant(void) {
    const char *pidfile = getenv("HOST_DESCENDANT_PIDFILE");
    if (!pidfile) return;
    pid_t child = fork();
    if (child == 0) { execl("/bin/sleep", "sleep", "600", (char *)NULL); _exit(127); }
    if (child < 0) return;
    FILE *f = fopen(pidfile, "w");
    if (f) { fprintf(f, "%d %d\n", (int)child, (int)getpgid(child)); fclose(f); }
}

static volatile sig_atomic_t marked = 0;
static void on_term(int signum) {
    (void)signum;
    const char *marker = getenv("HOST_MARKER");
    if (marker && !marked) {
        marked = 1;
        FILE *f = fopen(marker, "w");
        if (f) { fputs("term\n", f); fclose(f); }
    }
    const char *action = env_or("HOST_TERM_ACTION", "exit0");
    if (strcmp(action, "exit") == 0) _exit(atoi(env_or("HOST_TERM_EXIT", "0")));
    if (strcmp(action, "abort") == 0) { raise(SIGABRT); return; }
    _exit(0);
}

int main(int argc, char **argv) {
    struct rlimit core = {0, 0};
    setrlimit(RLIMIT_CORE, &core);
    capture_inherited();
    const char *mode = env_or("HOST_MODE", "report");
    if (strcmp(mode, "report") == 0) {
        write_record(argc, argv);
        spawn_descendant();
        if (getenv("HOST_ECHO")) {
            fputs("child-stdout\n", stdout);
            fputs("child-stderr\n", stderr);
        }
        return atoi(env_or("HOST_EXIT", "0"));
    }
    if (strcmp(mode, "ignore_term") == 0) {
        signal(SIGTERM, SIG_IGN);
        signal(SIGINT, SIG_IGN);
    } else {
        struct sigaction sa;
        memset(&sa, 0, sizeof(sa));
        sa.sa_handler = on_term;
        sigemptyset(&sa.sa_mask);
        sigaction(SIGTERM, &sa, NULL);
        sigaction(SIGINT, &sa, NULL);
    }
    /* The record is what the tests wait on before signalling, so it is written
       only once the handlers are in place. Written first, a SIGTERM forwarded in
       the gap killed the child by the default action, and the host reported a
       clean stop (0) where the test expected the handler's exit code. */
    write_record(argc, argv);
    spawn_descendant();
    for (;;) pause();
    return 0;
}
"""


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = _load("build_server_host", BUILD_SCRIPT)

HAS_TOOLS = (
    sys.platform == "darwin"
    and shutil.which("xcrun") is not None
    and shutil.which("codesign") is not None
    and shutil.which("lipo") is not None
)
HAS_CC = sys.platform == "darwin" and shutil.which("cc") is not None
requires_macos_tools = pytest.mark.skipif(
    not HAS_TOOLS, reason="the native host build requires macOS Command Line Tools"
)


def _fake_runner(
    commands: list[list[str]] | None = None, archs: str = "arm64 x86_64\n"
) -> Any:
    """An injected runner that fakes the compiler, lipo and codesign outputs."""

    def run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if commands is not None:
            commands.append(list(cmd))
        if cmd[0] == "xcrun":
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"thin")
        elif cmd[:2] == ["lipo", "-create"]:
            Path(cmd[cmd.index("-output") + 1]).write_bytes(b"universal-binary")
        elif cmd[:2] == ["lipo", "-archs"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=archs)
        elif cmd[:2] == ["codesign", "-dv"]:
            arch = cmd[cmd.index("--arch") + 1]
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=f"CDHash=cd-{arch}\n")
        return subprocess.CompletedProcess(cmd, 0)

    return run


# --- identity -------------------------------------------------------------


def test_identity_is_distinct_from_pwa_retired_and_experiment() -> None:
    info = BUILD.bundle_info()
    assert info["CFBundleIdentifier"] == "local.ciaobot.server"
    assert info["CFBundleDisplayName"] == "Ciaobot Server"
    assert info["CFBundleName"] == "Ciaobot Server"
    assert info["CFBundleExecutable"] == "Ciaobot Server"
    assert info["LSUIElement"] is True

    from ciao import cli, macos_service

    reserved = {macos_service.DESKTOP_BUNDLE_ID, "local.ciaobot.menubar", *cli._OUR_BUNDLE_IDS}
    assert BUILD.BUNDLE_ID not in reserved
    # Not the experiment identity either.
    assert BUILD.BUNDLE_ID != "local.ciaobot.server-host-experiment"


def test_revision_is_independent_of_engine_version() -> None:
    info = BUILD.bundle_info()
    # The bundle version expresses the host revision, never the engine release.
    assert BUILD.HOST_REVISION == 4
    assert info["CFBundleVersion"] == "4"
    assert info["CFBundleShortVersionString"] == "4"
    assert info[BUILD.HOST_PROTOCOL_KEY] == 2
    assert info["LSMinimumSystemVersion"] == "13.0"

    from ciao import __version__

    # The engine's own version must not leak into the host identity fields.
    for value in (info["CFBundleVersion"], info["CFBundleShortVersionString"]):
        assert value != __version__
        assert __version__ not in str(value)


# --- the folder usage descriptions ------------------------------------------


_FOLDER_USAGE_KEYS = {
    "NSDesktopFolderUsageDescription": "Desktop",
    "NSDocumentsFolderUsageDescription": "Documents",
    "NSDownloadsFolderUsageDescription": "Downloads",
}

def _record_stat(
    record: Path, *, uid: int | None = None, mode: int = 0o600
) -> Callable[[str | os.PathLike[str]], os.stat_result]:
    """An ``lstat`` that gives the ownership record the simulated macOS owner and mode.

    The reader's owner/mode half is macOS-only in production, and these tests
    fake ``sys.platform`` to exercise it on any host. Two facts a real file
    cannot supply off macOS: ``os.getuid`` does not exist on Windows, and
    ``os.chmod`` cannot make POSIX owner-only bits there (``st_mode`` reports
    ``0o666``). This wrapper answers for the record path only - preserving the
    real file type, device and inode, size and timestamps, which the reader
    compares against the open descriptor and the record's bytes - and
    substitutes the owner and permission bits the simulated macOS expects.
    Every other path is delegated to the real ``lstat``.
    """
    real_lstat = os.lstat

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        info = real_lstat(path)
        if Path(path) != record:
            return info
        return os.stat_result(
            (
                (info.st_mode & 0o170000) | mode,
                info.st_ino,
                info.st_dev,
                info.st_nlink,
                info.st_uid if uid is None else uid,
                info.st_gid,
                info.st_size,
                info.st_atime,
                info.st_mtime,
                info.st_ctime,
            )
        )

    return lstat


def test_the_host_declares_the_folder_usage_strings() -> None:
    # macOS shows the explanation line it is given; with no key for a folder the
    # prompt appears with an empty line (#1577, #1659).
    info = BUILD.bundle_info()
    for key, folder in _FOLDER_USAGE_KEYS.items():
        text = info[key]
        assert isinstance(text, str) and text.strip() == text
        # One plain sentence: it names the folder, says who asks, and says the
        # access is scoped to a workspace the user chose.
        assert text.endswith(".")
        assert text.count(".") == 1, f"{key} is not one sentence: {text!r}"
        assert folder in text
        assert "Ciaobot Server" in text
        assert "workspace" in text
    # The two existing usage descriptions are untouched.
    assert info["NSAppleEventsUsageDescription"] == BUILD.APPLE_EVENTS_USAGE
    assert info["NSAccessibilityUsageDescription"] == BUILD.ACCESSIBILITY_USAGE


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the ownership record is a POSIX artifact: it names an absolute POSIX bundle path",
)
def test_a_bundle_with_the_folder_usage_strings_still_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The three new keys are part of the sealed plist, so they change the
    # bundle's digest. Adding them must not break the ownership contract: the
    # inspector reads the identity fields, and the record matches the bytes.
    # POSIX-only: the record names a POSIX bundle path and verify requires it
    # to equal the inspected one, which on Windows is a backslash path. The
    # uid/mode refusals still run on Windows through the two companions below.
    monkeypatch.setattr(sys, "platform", "darwin")
    from ciao import server_host

    # The record reader also checks the owner and the mode on macOS. The
    # record's own owner is this user on macOS and `0` on Windows, and the
    # simulated stat supplies owner-only mode, so both checks are the ones the
    # test means to exercise (the same isolation #1666 applied to the service
    # test).

    bundle = tmp_path / server_host.APP_NAME
    for relative in (
        "Contents/MacOS/Ciaobot Server",
        "Contents/Resources/CiaobotServer.icns",
        "Contents/_CodeSignature/CodeResources",
    ):
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"sealed")
    with (bundle / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(BUILD.bundle_info(), handle)

    snapshot = server_host.inspect_host_bundle(bundle, runner=_plist_runner())
    assert snapshot.host_revision == 4
    assert snapshot.host_protocol == 2
    # The sealed plist really carries the new keys.
    plist_bytes = (bundle / "Contents" / "Info.plist").read_bytes()
    assert snapshot.bundle_files["Contents/Info.plist"] == hashlib.sha256(
        plist_bytes
    ).hexdigest()
    for key in _FOLDER_USAGE_KEYS:
        assert key.encode() in plist_bytes

    # The record that vouches for it verifies the very same bytes.
    record = tmp_path / "server-host.json"
    record.write_text(json.dumps(snapshot.to_record()), encoding="utf-8")
    os.chmod(record, 0o600)
    owner = record.stat().st_uid
    monkeypatch.setattr(server_host, "_getuid", lambda: owner)
    monkeypatch.setattr(server_host.os, "lstat", _record_stat(record, uid=owner))
    verified = server_host.verify_owned_host(
        bundle, ownership_path=record, runner=_plist_runner()
    )
    assert verified == snapshot


def test_the_ownership_record_refuses_a_foreign_owner_on_a_simulated_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The synthetic stat must not turn the owner check into a formality: a
    # record whose simulated owner is not this user is refused, on Windows too.
    monkeypatch.setattr(sys, "platform", "darwin")
    from ciao import server_host

    record = tmp_path / "server-host.json"
    record.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server_host, "_getuid", lambda: 501)
    monkeypatch.setattr(server_host.os, "lstat", _record_stat(record, uid=502))

    with pytest.raises(server_host.ServerHostError) as caught:
        server_host.read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


def test_the_ownership_record_refuses_a_group_readable_mode_on_a_simulated_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ...and neither may it excuse a record that grants group or other access.
    monkeypatch.setattr(sys, "platform", "darwin")
    from ciao import server_host

    record = tmp_path / "server-host.json"
    record.write_text("{}", encoding="utf-8")
    owner = record.stat().st_uid
    monkeypatch.setattr(server_host, "_getuid", lambda: owner)
    monkeypatch.setattr(
        server_host.os, "lstat", _record_stat(record, uid=owner, mode=0o644)
    )

    with pytest.raises(server_host.ServerHostError) as caught:
        server_host.read_host_ownership(record)
    assert caught.value.code == server_host.INVALID_OWNERSHIP


def _plist_runner() -> Any:
    """A runner that answers the codesign probes for a fixture bundle."""
    codesign = "/usr/bin/codesign"

    def run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == [codesign, "-dv"]:
            if "--arch" in cmd:
                arch = cmd[cmd.index("--arch") + 1]
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="",
                    stderr=f"CDHash={hashlib.sha1(arch.encode()).hexdigest()}\n",
                )
            stderr = (
                "Identifier=local.ciaobot.server\n"
                "Format=app bundle with Mach-O universal (arm64 x86_64)\n"
                "Signature=adhoc\n"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=stderr)
        if cmd[:2] == [codesign, "--verify"]:
            return subprocess.CompletedProcess(cmd, 0)
        raise AssertionError(f"unexpected native probe: {cmd}")

    return run


# --- icon -----------------------------------------------------------------


def test_tracked_icon_matches_pinned_bytes() -> None:
    assert ICON_PATH.is_file()
    digest = hashlib.sha256(ICON_PATH.read_bytes()).hexdigest()
    assert digest == EXPECTED_ICON_SHA256


def test_icon_is_bundled_from_tracked_file(tmp_path: Path) -> None:
    result = BUILD.build(
        tmp_path / "host",
        platform_name="darwin",
        runner=_fake_runner(archs="x86_64 arm64\n"),
    )
    app: Path = result["paths"]["app"]
    assert (app / "Contents" / "Resources" / BUILD.ICON_NAME).read_bytes() == ICON_PATH.read_bytes()

    # No Python, config or sidecar content ships inside the sealed bundle.
    shipped = sorted(
        p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file()
    )
    assert shipped == [
        "Contents/Info.plist",
        "Contents/MacOS/Ciaobot Server",
        "Contents/Resources/CiaobotServer.icns",
    ]
    assert not any(name.endswith(".py") for name in shipped)


# --- pure builder API -----------------------------------------------------


def test_compile_commands_are_exact_and_both_arch() -> None:
    source = Path("/src/ServerHost.swift")
    arm = Path("/stage/Ciaobot Server.arm64")
    intel = Path("/stage/Ciaobot Server.x86_64")
    commands = BUILD.compile_commands(source, arm, intel)
    assert commands == [
        ["xcrun", "swiftc", str(source), "-target", "arm64-apple-macosx13.0", "-O", "-o", str(arm)],
        [
            "xcrun",
            "swiftc",
            str(source),
            "-target",
            "x86_64-apple-macosx13.0",
            "-O",
            "-o",
            str(intel),
        ],
    ]
    assert BUILD.ARM_TARGET == "arm64-apple-macosx13.0"
    assert BUILD.INTEL_TARGET == "x86_64-apple-macosx13.0"
    assert BUILD.MACOS_DEPLOYMENT_TARGET == "13.0"


@pytest.mark.parametrize("kind", ["file", "dir", "dangling_symlink"])
def test_builder_refuses_existing_output(tmp_path: Path, kind: str) -> None:
    output = tmp_path / "existing"
    if kind == "file":
        output.write_text("keep me")
    elif kind == "dir":
        output.mkdir()
        (output / "marker").write_text("keep me")
    else:
        try:
            output.symlink_to(tmp_path / "missing-target")
        except OSError:
            pytest.skip("symlinks unavailable on this platform")

    def _no_runner(cmd: list[str], **_kwargs: Any) -> None:
        raise AssertionError(f"compiler/signer must not run for existing output: {cmd}")

    with pytest.raises(BUILD.BuildError):
        BUILD.build(output, platform_name="darwin", runner=_no_runner)

    if kind == "file":
        assert output.read_text() == "keep me"
    elif kind == "dir":
        assert (output / "marker").read_text() == "keep me"
    else:
        assert output.is_symlink()
        assert not (tmp_path / "missing-target").exists()


def test_non_macos_build_fails_before_writes(tmp_path: Path) -> None:
    output = tmp_path / "never-created"

    def _no_runner(cmd: list[str], **_kwargs: Any) -> None:
        raise AssertionError(f"compiler must not run off macOS: {cmd}")

    for platform_name in ("win32", "linux"):
        with pytest.raises(BUILD.BuildError):
            BUILD.build(output, platform_name=platform_name, runner=_no_runner)
        assert not output.exists()


def test_injected_build_order_and_metadata(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    result = BUILD.build(
        tmp_path / "host",
        platform_name="darwin",
        runner=_fake_runner(commands),
    )

    # Universal assembly -> metadata/resources -> one signature -> strict verify
    # -> lipo verification -> per-arch extraction. Nothing signs before the
    # resources are in place.
    kinds = [cmd[0] if cmd[0] != "xcrun" else "swiftc" for cmd in commands]
    assert kinds[:3] == ["swiftc", "swiftc", "lipo"]
    first_sign = kinds.index("codesign")
    assert kinds[:first_sign] == ["swiftc", "swiftc", "lipo"]
    assert commands[first_sign][:4] == ["codesign", "--force", "--sign", "-"]
    assert commands[first_sign + 1] == ["codesign", "--verify", "--strict", str(result["paths"]["app"])]
    assert commands[first_sign + 2][:2] == ["lipo", "-archs"]

    metadata = result["metadata"]
    assert metadata["bundle_id"] == "local.ciaobot.server"
    assert metadata["host_revision"] == 4
    assert metadata["host_protocol"] == 2
    assert metadata["minimum_system_version"] == "13.0"
    assert metadata["architectures"] == ["arm64", "x86_64"]
    assert metadata["executable_sha256"] == hashlib.sha256(b"universal-binary").hexdigest()
    assert metadata["per_arch_cdhashes"] == {"arm64": "cd-arm64", "x86_64": "cd-x86_64"}
    # Public metadata names no local-machine path.
    for value in (metadata["executable_sha256"], metadata["archive_sha256"]):
        assert "/" not in value and "Users" not in value

    archive: Path = result["paths"]["archive"]
    assert archive.name == "ciaobot-server-host-macos-universal-v1.tar.gz"
    assert archive.is_file()


def test_failed_build_removes_only_its_own_output(tmp_path: Path) -> None:
    sibling = tmp_path / "keep-me.txt"
    sibling.write_text("unrelated")
    output = tmp_path / "host"

    def boom(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, cmd)

    with pytest.raises(subprocess.CalledProcessError):
        BUILD.build(output, platform_name="darwin", runner=boom)

    assert not output.exists()
    # A foreign file outside the created output is untouched.
    assert sibling.read_text() == "unrelated"


def test_archive_members_are_ordinary_and_path_safe(tmp_path: Path) -> None:
    result = BUILD.build(tmp_path / "host", platform_name="darwin", runner=_fake_runner())
    with tarfile.open(result["paths"]["archive"], "r:gz") as tar:
        members = tar.getmembers()
    for member in members:
        assert not member.issym() and not member.islnk()
        assert not member.name.startswith("/")
        assert ".." not in Path(member.name).parts
        assert member.uid == 0 and member.gid == 0
        assert member.uname == "" and member.gname == ""
    names = {m.name for m in members}
    assert "Ciaobot Server.app/Contents/MacOS/Ciaobot Server" in names
    # No thin staging files and no absolute paths leak into the archive.
    assert not any(n.endswith((".arm64", ".x86_64")) for n in names)
    # Only the app ships: the staging and verify scratch dirs are gone.
    assert sorted(p.name for p in (tmp_path / "host").iterdir()) == sorted(
        [BUILD.APP_NAME, BUILD.ARCHIVE_NAME]
    )


# --- release signing: Developer ID, hardened runtime, notarization ---------

RELEASE_TEAM_ID = "ABCDE12345"
RELEASE_IDENTITY = "Developer ID Application: Ciaobot Server (ABCDE12345)"
RELEASE_PROFILE = "ciaobot-notary"

# Native-shaped fixture data, written out literally and independently of the
# production constants: a shared change must fail these tests rather than move
# the builder and its fake together.
NATIVE_IDENTIFIER = "local.ciaobot.server"
NATIVE_ARCHS_LINE = "arm64 x86_64\n"
NATIVE_CODE_DIRECTORY = "v=20500 size=400 flags=0x10000(runtime) hashes=4+3 location=embedded"
NATIVE_NO_RUNTIME_FLAGS = "v=20500 size=400 flags=0x0(none) hashes=4+3 location=embedded"
NATIVE_ENTITLEMENTS = {"com.apple.security.automation.apple-events": True}
NATIVE_ENTITLEMENTS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    "<plist version=\"1.0\">\n<dict>\n"
    "\t<key>com.apple.security.automation.apple-events</key>\n\t<true/>\n"
    "</dict>\n</plist>\n"
)

# Unique values that must never appear in an error message or in CLI output.
SENTINEL_IDENTITY = "Developer ID Application: SENTINELIDENTITY4F7A (ABCDE12345)"
SENTINEL_PROFILE = "SENTINELPROFILE9C2D"
SENTINEL_TOOL_OUTPUT = "SENTINELTOOLOUTPUT7B3E"
SENTINEL_TOOL_ERROR = "SENTINELTOOLERROR1D6F"
SENTINEL_RAW_ARCHS = "SENTINELRAWARCHS5A1C"
SENTINEL_RAW_FIELD = "SENTINELRAWFIELD2E8B"

# Stand-in for whatever material ``xcrun stapler`` writes into the bundle. The
# tests never assert a ticket format or path: they assert that the bytes the
# staple step produced are still in the app when the final archive is written.
STAPLE_TICKET_RELATIVE = "Contents/TestStapleTicket"
STAPLE_TICKET_BYTES = b"stapled-ticket"


def _release_signing(**overrides: str) -> Any:
    values = {
        "identity": RELEASE_IDENTITY,
        "team_id": RELEASE_TEAM_ID,
        "keychain_profile": RELEASE_PROFILE,
    }
    values.update(overrides)
    return BUILD.ReleaseSigning(**values)


def _release_tool(cmd: list[str]) -> str:
    """The tool family for exactly-known release argv; fail on anything else."""

    def is_(prefix: list[str], length: int) -> bool:
        return cmd[: len(prefix)] == prefix and len(cmd) == length

    if is_(["xcrun", "swiftc"], 8):
        assert cmd[2].endswith("ServerHost.swift"), cmd
        assert cmd[3] == "-target" and cmd[5] == "-O" and cmd[6] == "-o", cmd
        return "swiftc"
    if is_(["lipo", "-create", "-output"], 6):
        return "lipo-create"
    if is_(["lipo", "-archs"], 3):
        return "archs"
    if is_(["codesign", "--force", "--sign"], 12):
        assert cmd[4:6] == ["--options", "runtime"], cmd
        assert cmd[6] == "--timestamp", cmd
        assert cmd[7] == "--entitlements", cmd
        assert cmd[9] == "--requirements", cmd
        return "sign"
    if is_(["codesign", "--verify", "--strict"], 4):
        return "verify"
    if is_(["codesign", "--verify", "--strict", "--test-requirement"], 7):
        assert cmd[4].startswith("="), cmd
        assert cmd[5] == "--all-architectures", cmd
        return "verify-expected"
    if is_(["codesign", "-dv", "--verbose=4", "--arch"], 6):
        return "display"
    if is_(["codesign", "-d", "--entitlements", ":-", "--xml", "--arch"], 8):
        return "entitlements"
    if is_(["ditto", "-c", "-k", "--keepParent"], 6):
        return "ditto"
    if is_(["xcrun", "notarytool", "submit"], 11):
        assert cmd[4] == "--keychain-profile", cmd
        assert cmd[6:] == ["--wait", "--timeout", "30m", "--output-format", "json"], cmd
        return "notary"
    if is_(["xcrun", "stapler", "staple"], 4):
        return "staple"
    if is_(["xcrun", "stapler", "validate"], 4):
        return "staple-validate"
    if is_(["spctl", "--assess", "--type", "execute", "--verbose=2"], 6):
        return "gatekeeper"
    raise AssertionError(f"unexpected release argv: {cmd}")


def _release_runner(
    commands: list[list[str]] | None = None,
    *,
    calls: list[tuple[list[str], dict[str, Any]]] | None = None,
    fault: dict[str, Any] | None = None,
    notary_status: Any = "Accepted",
    notary_returncode: int = 0,
    notary_stdout: str | None = None,
    staple_ticket: bytes | None = None,
    fail: Callable[[list[str]], bool] | None = None,
    timeout_at: Callable[[list[str]], bool] | None = None,
) -> Any:
    """A strict, native-shaped fake toolchain that rejects unexpected argv.

    ``fault`` overrides one slice/artifact fact and applies only to the selected
    ``stage`` (``"built"`` or ``"extracted"``) and, when it names one, the
    selected ``arch``, so artifact- and slice-targeted negative cases stay
    independent.
    """

    def complete(
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

    def for_stage(path: str, arch: str | None = None) -> dict[str, Any]:
        if not fault:
            return {}
        stage = "extracted" if ".host-verify" in path else "built"
        if fault.get("stage", "built") != stage:
            return {}
        target = fault.get("arch")
        if target is not None and target != arch:
            return {}
        return fault

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if commands is not None:
            commands.append(list(cmd))
        if calls is not None:
            calls.append((list(cmd), dict(kwargs)))
        family = _release_tool(cmd)
        if fail is not None and fail(cmd):
            raise subprocess.CalledProcessError(
                1, cmd, output=SENTINEL_TOOL_OUTPUT, stderr=SENTINEL_TOOL_ERROR
            )
        if timeout_at is not None and timeout_at(cmd):
            raise subprocess.TimeoutExpired(cmd, 1)
        if family == "swiftc":
            Path(cmd[7]).write_bytes(b"thin")
        elif family == "lipo-create":
            Path(cmd[3]).write_bytes(b"universal-binary")
        elif family in ("sign", "verify", "verify-expected"):
            pass
        elif family == "archs":
            return complete(
                cmd, kwargs, stdout=str(for_stage(cmd[2]).get("archs", NATIVE_ARCHS_LINE))
            )
        elif family == "display":
            arch, path = cmd[4], cmd[5]
            data = for_stage(path, arch)
            identifier = data.get("identifier", NATIVE_IDENTIFIER)
            code_directory = data.get("code_directory", NATIVE_CODE_DIRECTORY)
            runtime_version = data.get("runtime_version", "13.0.0")
            timestamp = data.get("timestamp", "Oct 30, 2026 at 12:00:00")
            cdhash = data.get("cdhash", f"native-{arch}")
            lines: list[str] = []
            if identifier is not None:
                lines.append(f"Identifier={identifier}")
            if code_directory is not None:
                lines.append(f"CodeDirectory {code_directory}")
            lines.append(f"Signature={data.get('signature', 'Developer ID Application')}")
            lines.append(f"TeamIdentifier={data.get('team_id', RELEASE_TEAM_ID)}")
            if cdhash is not None:
                lines.append(f"CDHash={cdhash}")
            if runtime_version is not None:
                lines.append(f"Runtime Version={runtime_version}")
            if timestamp is not None:
                lines.append(f"Timestamp={timestamp}")
            return complete(cmd, kwargs, stdout="", stderr="\n".join(lines) + "\n")
        elif family == "entitlements":
            entitlements = for_stage(cmd[7], cmd[6]).get(
                "entitlements", NATIVE_ENTITLEMENTS
            )
            if entitlements is None:
                return complete(cmd, kwargs)
            if isinstance(entitlements, str):
                return complete(cmd, kwargs, stdout=entitlements)
            return complete(
                cmd, kwargs, stdout=plistlib.dumps(entitlements).decode("utf-8")
            )
        elif family == "ditto":
            Path(cmd[5]).write_bytes(b"temporary-zip")
        elif family == "notary":
            stdout = (
                notary_stdout
                if notary_stdout is not None
                else json.dumps({"id": "submission-1", "status": notary_status})
            )
            return complete(cmd, kwargs, returncode=notary_returncode, stdout=stdout)
        elif family == "staple":
            if staple_ticket is not None:
                ticket = Path(cmd[3]) / STAPLE_TICKET_RELATIVE
                ticket.parent.mkdir(parents=True, exist_ok=True)
                ticket.write_bytes(staple_ticket)
        elif family == "staple-validate":
            stage = for_stage(cmd[3])
            return complete(cmd, kwargs, returncode=int(stage.get("staple_validate", 0)))
        elif family == "gatekeeper":
            stage = for_stage(cmd[5])
            return complete(cmd, kwargs, returncode=int(stage.get("gatekeeper", 0)))
        else:  # pragma: no cover - _release_tool already rejected this
            raise AssertionError(f"unhandled release argv: {cmd}")
        return complete(cmd, kwargs)

    return run


def _staple_index(commands: list[list[str]]) -> int:
    return next(
        index
        for index, cmd in enumerate(commands)
        if cmd[:3] == ["xcrun", "stapler", "staple"]
    )


def _cli_with_runner(monkeypatch: pytest.MonkeyPatch, runner: Any) -> None:
    """Keep the CLI path, inject only the toolchain runner into ``build``."""
    original = BUILD.build

    def build_with_runner(path: Path, *, release_signing: Any = None) -> dict[str, Any]:
        return original(
            path, platform_name="darwin", runner=runner, release_signing=release_signing
        )

    monkeypatch.setattr(BUILD, "build", build_with_runner)


def test_release_entitlements_plist_is_minimal() -> None:
    # The tracked entitlements file is the only entitlement source, and it stays
    # minimal: no sandbox, JIT, unsigned memory, library-validation-disable,
    # debug or broad filesystem entitlement.
    document = plistlib.loads(BUILD.ENTITLEMENTS_SOURCE.read_bytes())
    assert document == {"com.apple.security.automation.apple-events": True}
    assert BUILD.ENTITLEMENTS_SOURCE.name == "ServerHost.entitlements.plist"


def test_release_build_requires_complete_signing_configuration_before_writes(
    tmp_path: Path,
) -> None:
    output = tmp_path / "never-created"

    def _no_runner(cmd: list[str], **_kwargs: Any) -> None:
        raise AssertionError(f"no tool may run for an incomplete release config: {cmd}")

    for overrides in (
        {"identity": ""},
        {"identity": "   "},
        {"identity": "-"},
        {"team_id": ""},
        {"team_id": "SHORT"},
        {"team_id": "abcde12345"},
        {"team_id": "ABCDE1234"},
        {"team_id": "ABCDE123456"},
        {"team_id": "ABCDE 1234"},
        {"keychain_profile": ""},
        {"keychain_profile": "   "},
    ):
        with pytest.raises(BUILD.BuildError):
            BUILD.build(
                output,
                platform_name="darwin",
                runner=_no_runner,
                release_signing=_release_signing(**overrides),
            )
        assert not output.exists(), overrides


def test_release_build_rejects_a_missing_entitlements_plist_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "never-created"
    monkeypatch.setattr(BUILD, "ENTITLEMENTS_SOURCE", tmp_path / "missing.plist")

    def _no_runner(cmd: list[str], **_kwargs: Any) -> None:
        raise AssertionError(f"no tool may run without the entitlements file: {cmd}")

    with pytest.raises(BUILD.BuildError):
        BUILD.build(
            output,
            platform_name="darwin",
            runner=_no_runner,
            release_signing=_release_signing(),
        )
    assert not output.exists()


def test_release_build_rejects_adhoc_identity(tmp_path: Path) -> None:
    output = tmp_path / "never-created"
    commands: list[list[str]] = []

    with pytest.raises(BUILD.BuildError):
        BUILD.build(
            output,
            platform_name="darwin",
            runner=_release_runner(commands),
            release_signing=_release_signing(identity="-"),
        )
    # An ad-hoc signature can never satisfy the release contract, so it is
    # refused before anything is compiled, signed or written.
    assert commands == []
    assert not output.exists()


def test_release_build_signs_with_runtime_timestamp_entitlement_and_stable_requirement(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    result = BUILD.build(
        tmp_path / "host",
        platform_name="darwin",
        runner=_release_runner(commands),
        release_signing=_release_signing(),
    )

    signing = BUILD.ReleaseSigning(RELEASE_IDENTITY, RELEASE_TEAM_ID, RELEASE_PROFILE)
    # Frozen: the release inputs cannot be rebound after construction, and the
    # value compares and hashes by content.
    with pytest.raises(AttributeError):
        signing.identity = "someone else"  # type: ignore[misc]
    assert signing == BUILD.ReleaseSigning(
        RELEASE_IDENTITY, RELEASE_TEAM_ID, RELEASE_PROFILE
    )
    assert hash(signing) == hash(
        BUILD.ReleaseSigning(RELEASE_IDENTITY, RELEASE_TEAM_ID, RELEASE_PROFILE)
    )

    # Exactly one signature, applied once to the assembled app, and the exact
    # embedding argv: `--requirements` with a requirement-set literal is a
    # SIGNING option and must not be reused to check the artifact.
    expression = (
        f'identifier "{BUILD.BUNDLE_ID}"'
        " and anchor apple generic"
        " and certificate 1[field.1.2.840.113635.100.6.2.6] /* exists */"
        " and certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */"
        f' and certificate leaf[subject.OU] = "{RELEASE_TEAM_ID}"'
    )
    app = str(result["paths"]["app"])
    sign_indexes = [
        index for index, cmd in enumerate(commands) if cmd[:2] == ["codesign", "--force"]
    ]
    assert len(sign_indexes) == 1
    assert commands[sign_indexes[0]] == [
        "codesign",
        "--force",
        "--sign",
        RELEASE_IDENTITY,
        "--options",
        "runtime",
        "--timestamp",
        "--entitlements",
        str(BUILD.ENTITLEMENTS_SOURCE),
        "--requirements",
        f"=designated => {expression}",
        app,
    ]

    # Verification is the external seam: `--test-requirement` with a single
    # requirement (not the `=designated =>` set), evaluated for every slice.
    executable = str(result["paths"]["executable"])
    expected = f"={expression}"
    assert ["codesign", "--verify", "--strict", app] in commands
    assert [
        "codesign",
        "--verify",
        "--strict",
        "--test-requirement",
        expected,
        "--all-architectures",
        app,
    ] in commands
    # `--requirements` never appears on a verification command, and no verify
    # command omits the external requirement or the all-slices flag.
    verify_commands = [cmd for cmd in commands if cmd[:2] == ["codesign", "--verify"]]
    assert verify_commands
    for cmd in verify_commands:
        assert "--requirements" not in cmd
        if cmd != ["codesign", "--verify", "--strict", cmd[-1]]:
            assert "--test-requirement" in cmd and "--all-architectures" in cmd, cmd
    extracted_app = next(
        cmd[-1]
        for cmd in verify_commands
        if "--test-requirement" in cmd and ".host-verify" in cmd[-1]
    )
    assert [
        "codesign",
        "--verify",
        "--strict",
        "--test-requirement",
        expected,
        "--all-architectures",
        extracted_app,
    ] in commands

    # Both slices are inspected for identity, team, runtime, timestamp and
    # entitlements - for the built app and again for the re-extracted one.
    display_probes = [cmd for cmd in commands if cmd[:2] == ["codesign", "-dv"]]
    entitlement_probes = [
        cmd for cmd in commands if cmd[:2] == ["codesign", "-d"] and "--entitlements" in cmd
    ]
    for probes in (display_probes, entitlement_probes):
        built = [cmd for cmd in probes if cmd[-1] == executable]
        extracted = [cmd for cmd in probes if cmd[-1] != executable]
        assert [cmd[cmd.index("--arch") + 1] for cmd in built] == ["arm64", "x86_64"]
        assert [cmd[cmd.index("--arch") + 1] for cmd in extracted] == ["arm64", "x86_64"]
        assert all(".host-verify" in cmd[-1] for cmd in extracted)

    metadata = result["metadata"]
    assert metadata["host_revision"] == BUILD.SIGNED_HOST_REVISION == 5
    assert metadata["host_protocol"] == 2
    assert metadata["bundle_id"] == "local.ciaobot.server"
    assert metadata["per_arch_cdhashes"] == {
        "arm64": "native-arm64",
        "x86_64": "native-x86_64",
    }
    with (result["paths"]["app"] / "Contents" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["CFBundleVersion"] == "5"
    assert info["CFBundleShortVersionString"] == "5"
    assert info[BUILD.HOST_PROTOCOL_KEY] == 2


def test_release_native_tool_argv_and_order_are_exact(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    result = BUILD.build(
        tmp_path / "host",
        platform_name="darwin",
        runner=_release_runner(commands, staple_ticket=STAPLE_TICKET_BYTES),
        release_signing=_release_signing(),
    )
    app = str(result["paths"]["app"])

    ditto = next(cmd for cmd in commands if cmd[:2] == ["ditto", "-c"])
    zip_path = ditto[5]
    assert ditto == ["ditto", "-c", "-k", "--keepParent", app, zip_path]
    assert zip_path.endswith(".zip")
    assert Path(zip_path).parent == result["paths"]["app"].parent

    notary = [
        "xcrun",
        "notarytool",
        "submit",
        zip_path,
        "--keychain-profile",
        RELEASE_PROFILE,
        "--wait",
        "--timeout",
        "30m",
        "--output-format",
        "json",
    ]
    notary_index = commands.index(notary)
    staple_index = commands.index(["xcrun", "stapler", "staple", app])
    built_validate_index = commands.index(["xcrun", "stapler", "validate", app])
    sign_index = commands.index(
        next(cmd for cmd in commands if cmd[:2] == ["codesign", "--force"])
    )
    ditto_index = commands.index(ditto)
    assert sign_index < ditto_index < notary_index < staple_index
    assert staple_index < built_validate_index

    # The extracted app gets its own staple validation and Gatekeeper
    # assessment, after its own signature checks - never the built app's paths.
    extracted_app = next(
        cmd[-1]
        for cmd in commands
        if cmd[:3] == ["xcrun", "stapler", "validate"] and cmd[-1] != app
    )
    assert ".host-verify" in extracted_app
    extracted_validate = ["xcrun", "stapler", "validate", extracted_app]
    gatekeeper = ["spctl", "--assess", "--type", "execute", "--verbose=2", extracted_app]
    assert commands.count(extracted_validate) == 1
    assert commands.count(gatekeeper) == 1
    last_extracted_verify = max(
        index
        for index, cmd in enumerate(commands)
        if cmd[:2] == ["codesign", "--verify"] and ".host-verify" in cmd[-1]
    )
    assert last_extracted_verify < commands.index(extracted_validate)
    assert commands.index(extracted_validate) < commands.index(gatekeeper)


def test_release_fake_rejects_unexpected_argv() -> None:
    # The fake is strict: a wrong verification flag or a stray argument fails the
    # test instead of quietly returning success.
    runner = _release_runner()
    with pytest.raises(AssertionError):
        runner(
            ["codesign", "--verify", "--strict", "--requirements", "=designated => x", "/tmp/app"],
            check=True,
        )
    with pytest.raises(AssertionError):
        runner(["codesign", "--verify", "--strict", "/tmp/app", "--all-architectures"], check=True)
    with pytest.raises(AssertionError):
        runner(["codesign", "--deep", "--verify", "/tmp/app"], check=True)


def test_release_signed_subprocesses_are_bounded(tmp_path: Path) -> None:
    # Every subprocess the release mode adds - including the release-only
    # `lipo -archs` check - carries an explicit deadline, and the notary wait is
    # the longest one.
    calls: list[tuple[list[str], dict[str, Any]]] = []
    BUILD.build(
        tmp_path / "host",
        platform_name="darwin",
        runner=_release_runner(calls=calls),
        release_signing=_release_signing(),
    )
    assert calls
    signed_calls = [
        (cmd, kwargs)
        for cmd, kwargs in calls
        if not (cmd[0] == "xcrun" and cmd[1] == "swiftc")
        and cmd[:2] != ["lipo", "-create"]
    ]
    assert signed_calls
    for cmd, kwargs in signed_calls:
        assert kwargs.get("timeout"), cmd
    notary = next(
        kwargs for cmd, kwargs in calls if cmd[:3] == ["xcrun", "notarytool", "submit"]
    )
    other_timeouts = [
        kwargs["timeout"] for cmd, kwargs in signed_calls if cmd[:3] != ["xcrun", "notarytool", "submit"]
    ]
    assert notary["timeout"] > max(other_timeouts)
    # The release-only arch check is one of the bounded calls.
    arch_checks = [
        kwargs for cmd, kwargs in calls if cmd[:2] == ["lipo", "-archs"]
    ]
    assert len(arch_checks) == 2
    assert all(kwargs.get("timeout") for kwargs in arch_checks)


def test_release_build_rejects_wrong_team_or_certificate_requirement(
    tmp_path: Path,
) -> None:
    # A slice whose leaf certificate names another team...
    commands: list[list[str]] = []
    with pytest.raises(BUILD.BuildError):
        BUILD.build(
            tmp_path / "wrong-team",
            platform_name="darwin",
            runner=_release_runner(
                commands, fault={"team_id": "ZZZZZ99999", "stage": "built"}
            ),
            release_signing=_release_signing(),
        )
    assert not (tmp_path / "wrong-team").exists()
    # ...and one that does not satisfy the expected requirement at all are both
    # refused before the artifact is submitted for notarization.
    assert not any(cmd[:3] == ["xcrun", "notarytool", "submit"] for cmd in commands)

    commands = []
    with pytest.raises(BUILD.BuildError) as caught:
        BUILD.build(
            tmp_path / "wrong-certificate",
            platform_name="darwin",
            runner=_release_runner(
                commands,
                fail=lambda cmd: cmd[:2] == ["codesign", "--verify"] and "--test-requirement" in cmd,
            ),
            release_signing=_release_signing(),
        )
    assert "expected requirement" in str(caught.value)
    assert not (tmp_path / "wrong-certificate").exists()
    assert not any(cmd[:3] == ["xcrun", "notarytool", "submit"] for cmd in commands)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"notary_returncode": 1},
        {"notary_stdout": "not json"},
        {"notary_stdout": SENTINEL_TOOL_OUTPUT},
        {"notary_stdout": '{"id": "submission-1"}'},
        {"notary_status": "Invalid"},
        {"notary_status": "Rejected"},
        {"notary_status": "In Progress"},
        {"notary_status": None},
        {"notary_status": SENTINEL_TOOL_OUTPUT},
    ],
)
def test_release_build_requires_accepted_notarization_status(
    tmp_path: Path, kwargs: dict[str, Any]
) -> None:
    output = tmp_path / "host"
    commands: list[list[str]] = []
    with pytest.raises(BUILD.BuildError) as caught:
        BUILD.build(
            output,
            platform_name="darwin",
            runner=_release_runner(commands, **kwargs),
            release_signing=_release_signing(),
        )
    assert not output.exists()
    # The rejection never echoes the tool's output, even when that output is
    # where the status came from.
    message = str(caught.value)
    assert SENTINEL_TOOL_OUTPUT not in message
    # A submission that did not return exactly Accepted is never stapled.
    assert not any(cmd[:3] == ["xcrun", "stapler", "staple"] for cmd in commands)


@pytest.mark.parametrize("stage", ["sign", "submit", "staple", "extracted"])
def test_release_build_failure_removes_only_its_output(
    tmp_path: Path, stage: str
) -> None:
    sibling = tmp_path / "keep-me.txt"
    sibling.write_text("unrelated")
    output = tmp_path / "host"

    def failed(cmd: list[str]) -> bool:
        if stage == "sign":
            return cmd[:2] == ["codesign", "--force"]
        if stage == "submit":
            return cmd[:3] == ["xcrun", "notarytool", "submit"]
        if stage == "staple":
            return cmd[:3] == ["xcrun", "stapler", "staple"]
        return cmd[:2] == ["codesign", "--verify"] and ".host-verify" in cmd[-1]

    commands: list[list[str]] = []
    with pytest.raises(BUILD.BuildError) as caught:
        BUILD.build(
            output,
            platform_name="darwin",
            runner=_release_runner(commands, fail=failed),
            release_signing=_release_signing(),
        )

    # The failure is sanitized: no tool argv, captured output or raw chain.
    assert SENTINEL_TOOL_OUTPUT not in str(caught.value)
    assert SENTINEL_TOOL_ERROR not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    # Only this invocation's fresh output is removed.
    assert not output.exists()
    assert sibling.read_text() == "unrelated"
    # No later stage ran after the failure: no submission for a signing failure,
    # no staple for a rejected submission, no extraction for a staple failure.
    submitted = any(cmd[:3] == ["xcrun", "notarytool", "submit"] for cmd in commands)
    stapled = any(cmd[:3] == ["xcrun", "stapler", "staple"] for cmd in commands)
    extracted = any(".host-verify" in cmd[-1] for cmd in commands)
    if stage == "sign":
        assert not submitted and not stapled and not extracted
    elif stage == "submit":
        assert submitted and not stapled and not extracted
    elif stage == "staple":
        assert submitted and stapled and not extracted
    else:
        assert submitted and stapled and extracted
        assert not any(cmd[0] == "spctl" for cmd in commands)


_SLICE_FAULTS = [
    pytest.param("archs", "arm64\n", "must carry exactly", id="archs-missing"),
    pytest.param("archs", "arm64 x86_64 i386\n", "must carry exactly", id="archs-extra"),
    pytest.param("identifier", "com.example.other", "bundle identifier", id="identifier-wrong"),
    pytest.param("team_id", "ZZZZZ99999", "signing team", id="team-wrong"),
    pytest.param("signature", "adhoc", "ad-hoc", id="signature-adhoc"),
    pytest.param("code_directory", None, "no CodeDirectory", id="code-directory-missing"),
    pytest.param(
        "code_directory", NATIVE_NO_RUNTIME_FLAGS, "hardened-runtime", id="runtime-flag-missing"
    ),
    pytest.param("runtime_version", None, "hardened runtime", id="runtime-version-missing"),
    pytest.param("timestamp", None, "secure timestamp", id="timestamp-missing"),
    pytest.param("cdhash", None, "no CDHash", id="cdhash-missing"),
    pytest.param(
        "entitlements",
        {
            "com.apple.security.automation.apple-events": True,
            "com.apple.security.cs.disable-library-validation": True,
        },
        "entitlements",
        id="entitlements-extra",
    ),
    pytest.param("entitlements", {}, "entitlements", id="entitlements-missing"),
    pytest.param("entitlements", "not a plist", "entitlements", id="entitlements-unparseable"),
]

# The same slice facts, but faulted only in the second architecture: the arm64
# probe must pass and the x86_64 probe must be reached and rejected, for the
# built app and again for the extracted one.
_SECOND_SLICE_FAULTS = [
    pytest.param(
        "code_directory", NATIVE_NO_RUNTIME_FLAGS, "hardened-runtime", id="runtime-flag"
    ),
    pytest.param("identifier", "com.example.other", "bundle identifier", id="identifier"),
    pytest.param("timestamp", None, "secure timestamp", id="timestamp"),
    pytest.param(
        "entitlements",
        {
            "com.apple.security.automation.apple-events": True,
            "com.apple.security.cs.disable-library-validation": True,
        },
        "entitlements",
        id="entitlements",
    ),
]


@pytest.mark.parametrize("stage", ["built", "extracted"])
@pytest.mark.parametrize("field, value, fragment", _SLICE_FAULTS)
def test_release_build_rejects_bad_slice_evidence(
    tmp_path: Path, stage: str, field: str, value: Any, fragment: str
) -> None:
    # Every verification input the builder claims to check is rejected
    # independently, on the built app and again on the re-extracted one.
    output = tmp_path / "host"
    sibling = tmp_path / "keep-me.txt"
    sibling.write_text("kept")
    commands: list[list[str]] = []
    with pytest.raises(BUILD.BuildError) as caught:
        BUILD.build(
            output,
            platform_name="darwin",
            runner=_release_runner(commands, fault={field: value, "stage": stage}),
            release_signing=_release_signing(),
        )
    assert fragment in str(caught.value)
    assert not output.exists()
    assert sibling.read_text() == "kept"
    submitted = any(cmd[:3] == ["xcrun", "notarytool", "submit"] for cmd in commands)
    extracted = any(".host-verify" in cmd[-1] for cmd in commands)
    if stage == "built":
        # Nothing beyond the failed built-artifact checks ran.
        assert not submitted and not extracted
        assert not any(cmd[:2] == ["ditto", "-c"] for cmd in commands)
    else:
        # The artifact was notarized, stapled and archived; the extracted
        # re-verification is what rejected it, and no completed artifact remains.
        assert submitted and extracted
    assert not (tmp_path / BUILD.ARCHIVE_NAME).exists()


@pytest.mark.parametrize("stage", ["built", "extracted"])
@pytest.mark.parametrize("field, value, fragment", _SECOND_SLICE_FAULTS)
def test_release_build_enforces_second_slice_independently(
    tmp_path: Path, stage: str, field: str, value: Any, fragment: str
) -> None:
    output = tmp_path / "host"
    sibling = tmp_path / "keep-me.txt"
    sibling.write_text("kept")
    commands: list[list[str]] = []
    with pytest.raises(BUILD.BuildError) as caught:
        BUILD.build(
            output,
            platform_name="darwin",
            runner=_release_runner(
                commands, fault={field: value, "stage": stage, "arch": "x86_64"}
            ),
            release_signing=_release_signing(),
        )
    assert fragment in str(caught.value)
    assert "x86_64" in str(caught.value)
    # The arm64 probe passed and the x86_64 probe was reached on the same
    # artifact: the failure comes from the second slice, not the first.
    family = ["codesign", "-d"] if field == "entitlements" else ["codesign", "-dv"]
    probes = [
        cmd
        for cmd in commands
        if cmd[:2] == family and (".host-verify" in cmd[-1]) == (stage == "extracted")
    ]
    assert [cmd[cmd.index("--arch") + 1] for cmd in probes] == ["arm64", "x86_64"]
    assert not output.exists()
    assert sibling.read_text() == "kept"
    if stage == "built":
        assert not any(cmd[:3] == ["xcrun", "notarytool", "submit"] for cmd in commands)
    else:
        assert any(cmd[:3] == ["xcrun", "notarytool", "submit"] for cmd in commands)
    assert not (tmp_path / BUILD.ARCHIVE_NAME).exists()


@pytest.mark.parametrize(
    "field, fragment, reaches_gatekeeper",
    [
        ("staple_validate", "ticket could not be validated", False),
        ("gatekeeper", "Gatekeeper rejected", True),
    ],
)
def test_release_build_rejects_extracted_only_staple_or_gatekeeper_failure(
    tmp_path: Path, field: str, fragment: str, reaches_gatekeeper: bool
) -> None:
    output = tmp_path / "host"
    sibling = tmp_path / "keep-me.txt"
    sibling.write_text("kept")
    commands: list[list[str]] = []
    with pytest.raises(BUILD.BuildError) as caught:
        BUILD.build(
            output,
            platform_name="darwin",
            runner=_release_runner(commands, fault={field: 1, "stage": "extracted"}),
            release_signing=_release_signing(),
        )
    assert fragment in str(caught.value)
    # The built app's own staple validation passed; only the extracted one failed.
    validates = [cmd for cmd in commands if cmd[:3] == ["xcrun", "stapler", "validate"]]
    built = [cmd for cmd in validates if ".host-verify" not in cmd[-1]]
    extracted = [cmd for cmd in validates if ".host-verify" in cmd[-1]]
    assert len(built) == 1 and len(extracted) == 1
    assert commands.index(built[0]) < commands.index(extracted[0])
    assert any(cmd[:3] == ["xcrun", "notarytool", "submit"] for cmd in commands)
    assert _staple_index(commands) < commands.index(extracted[0])
    gatekeepers = [cmd for cmd in commands if cmd[0] == "spctl"]
    if reaches_gatekeeper:
        assert len(gatekeepers) == 1 and ".host-verify" in gatekeepers[0][-1]
        assert commands.index(extracted[0]) < commands.index(gatekeepers[0])
    else:
        assert not gatekeepers
    assert not output.exists()
    assert sibling.read_text() == "kept"
    assert not (tmp_path / BUILD.ARCHIVE_NAME).exists()


def test_release_build_staples_before_final_archive_and_digest(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    result = BUILD.build(
        tmp_path / "host",
        platform_name="darwin",
        runner=_release_runner(commands, staple_ticket=STAPLE_TICKET_BYTES),
        release_signing=_release_signing(),
    )

    archive: Path = result["paths"]["archive"]
    staple = _staple_index(commands)
    # One signature, and it happens before the ticket is stapled: nothing signs
    # after stapling.
    signing = [i for i, cmd in enumerate(commands) if cmd[:2] == ["codesign", "--force"]]
    assert len(signing) == 1 and signing[0] < staple
    validate = next(
        i for i, cmd in enumerate(commands) if cmd[:3] == ["xcrun", "stapler", "validate"]
    )
    extracted_verify = next(
        i
        for i, cmd in enumerate(commands)
        if cmd[:2] == ["codesign", "--verify"] and ".host-verify" in cmd[-1]
    )
    assert staple < validate < extracted_verify
    # The archive was written after the staple: it carries the ticket bytes.
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.getmember(f"{BUILD.APP_NAME}/{STAPLE_TICKET_RELATIVE}")
        assert tar.extractfile(member).read() == STAPLE_TICKET_BYTES  # type: ignore[union-attr]
    # The digests describe the final archive on disk, not an intermediate file.
    assert result["metadata"]["archive_sha256"] == hashlib.sha256(
        archive.read_bytes()
    ).hexdigest()
    assert result["metadata"]["archive_size"] == archive.stat().st_size


def test_release_archive_preserves_stapled_ticket(tmp_path: Path) -> None:
    result = BUILD.build(
        tmp_path / "host",
        platform_name="darwin",
        runner=_release_runner(staple_ticket=STAPLE_TICKET_BYTES),
        release_signing=_release_signing(),
    )
    app: Path = result["paths"]["app"]
    archive: Path = result["paths"]["archive"]

    # The staple material is still in the built app and in the archive, under
    # the fixed archive root, as an ordinary file with no link.
    assert (app / STAPLE_TICKET_RELATIVE).read_bytes() == STAPLE_TICKET_BYTES
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        ticket = tar.getmember(f"{BUILD.APP_NAME}/{STAPLE_TICKET_RELATIVE}")
        assert not ticket.issym() and not ticket.islnk()
        assert tar.extractfile(ticket).read() == STAPLE_TICKET_BYTES  # type: ignore[union-attr]
    for member in members:
        assert not member.issym() and not member.islnk()
        assert not member.name.startswith("/")
        assert ".." not in Path(member.name).parts
    assert all(m.name == BUILD.APP_NAME or m.name.startswith(f"{BUILD.APP_NAME}/") for m in members)
    # The temporary ZIP and the extraction scratch are gone from the output.
    assert sorted(p.name for p in (tmp_path / "host").iterdir()) == sorted(
        [BUILD.APP_NAME, BUILD.ARCHIVE_NAME]
    )


def test_release_build_verifies_final_extracted_app(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fails_only_on_the_extracted_app(cmd: list[str]) -> bool:
        return cmd[:2] == ["codesign", "--verify"] and ".host-verify" in cmd[-1]

    output = tmp_path / "host"
    with pytest.raises(BUILD.BuildError) as caught:
        BUILD.build(
            output,
            platform_name="darwin",
            runner=_release_runner(
                commands, fail=fails_only_on_the_extracted_app
            ),
            release_signing=_release_signing(),
        )
    assert "extracted app failed strict signature verification" in str(caught.value)
    assert not output.exists()

    # The original app passed, the artifact was stapled and archived, and only
    # the independent re-extraction failed: the failure is not the earlier
    # verification repeating.
    app = next(
        cmd[-1]
        for cmd in commands
        if cmd[:2] == ["codesign", "--verify"]
        and "--test-requirement" in cmd
        and ".host-verify" not in cmd[-1]
    )
    extracted = [cmd[-1] for cmd in commands if ".host-verify" in cmd[-1]]
    assert extracted and all(path != app for path in extracted)
    assert _staple_index(commands) < commands.index(
        next(cmd for cmd in commands if cmd[-1] == extracted[0])
    )
    # The extracted verification is the last stage reached: no extracted staple
    # validation, Gatekeeper assessment or completed artifact survives.
    assert not any(cmd[:3] == ["xcrun", "stapler", "validate"] and ".host-verify" in cmd[-1] for cmd in commands)
    assert not any(cmd[0] == "spctl" for cmd in commands)


@pytest.mark.parametrize(
    "argv, fragment",
    [
        (["--release"], "--release requires"),
        (["--release", "--signing-identity", RELEASE_IDENTITY], "--release requires"),
        (["--release", "--team-id", RELEASE_TEAM_ID], "--release requires"),
        (["--release", "--notary-profile", RELEASE_PROFILE], "--release requires"),
        (
            ["--release", "--signing-identity", RELEASE_IDENTITY, "--team-id", RELEASE_TEAM_ID],
            "--release requires",
        ),
        (["--signing-identity", RELEASE_IDENTITY], "require --release"),
        (["--team-id", RELEASE_TEAM_ID], "require --release"),
        (["--notary-profile", RELEASE_PROFILE], "require --release"),
        (
            [
                "--release",
                "--signing-identity",
                "-",
                "--team-id",
                RELEASE_TEAM_ID,
                "--notary-profile",
                RELEASE_PROFILE,
            ],
            "ad-hoc signature",
        ),
    ],
)
def test_release_cli_rejects_partial_configuration(
    tmp_path: Path, argv: list[str], fragment: str, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "host"
    with pytest.raises(SystemExit) as caught:
        BUILD.main(["--output", str(output), *argv])
    assert caught.value.code == 2, argv
    # The refusal names the real rule, so an unknown-option rejection cannot
    # stand in for it.
    assert fragment in capsys.readouterr().err, argv
    assert not output.exists(), argv


def test_release_cli_passes_complete_configuration_to_the_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "host"
    captured: dict[str, Any] = {}

    def fake_build(path: Path, *, release_signing: Any = None) -> dict[str, Any]:
        captured["path"] = path
        captured["signing"] = release_signing
        return {
            "paths": {"app": output / BUILD.APP_NAME, "archive": output / BUILD.ARCHIVE_NAME},
            "metadata": {"host_revision": BUILD.SIGNED_HOST_REVISION},
        }

    monkeypatch.setattr(BUILD, "build", fake_build)
    assert (
        BUILD.main(
            [
                "--output",
                str(output),
                "--release",
                "--signing-identity",
                RELEASE_IDENTITY,
                "--team-id",
                RELEASE_TEAM_ID,
                "--notary-profile",
                RELEASE_PROFILE,
            ]
        )
        == 0
    )
    signing = captured["signing"]
    assert isinstance(signing, BUILD.ReleaseSigning)
    assert (signing.identity, signing.team_id, signing.keychain_profile) == (
        RELEASE_IDENTITY,
        RELEASE_TEAM_ID,
        RELEASE_PROFILE,
    )
    printed = capsys.readouterr().out
    # Authentication-bearing values never reach the output.
    assert RELEASE_PROFILE not in printed
    assert RELEASE_IDENTITY not in printed


@pytest.mark.parametrize(
    "identity, profile",
    [
        (SENTINEL_IDENTITY + "\n", SENTINEL_PROFILE),
        (SENTINEL_IDENTITY, SENTINEL_PROFILE + "\n"),
    ],
    ids=["bad-identity", "bad-profile"],
)
def test_release_cli_never_echoes_signing_inputs_on_validation_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], identity: str, profile: str
) -> None:
    output = tmp_path / "host"
    with pytest.raises(SystemExit) as caught:
        BUILD.main(
            [
                "--output",
                str(output),
                "--release",
                "--signing-identity",
                identity,
                "--team-id",
                RELEASE_TEAM_ID,
                "--notary-profile",
                profile,
            ]
        )
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert SENTINEL_IDENTITY not in err
    assert SENTINEL_PROFILE not in err
    assert "Traceback" not in err
    assert not output.exists()


def test_release_cli_never_echoes_signing_inputs_on_signing_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "host"
    _cli_with_runner(
        monkeypatch, _release_runner(fail=lambda cmd: cmd[:2] == ["codesign", "--force"])
    )
    with pytest.raises(SystemExit) as caught:
        BUILD.main(
            [
                "--output",
                str(output),
                "--release",
                "--signing-identity",
                SENTINEL_IDENTITY,
                "--team-id",
                RELEASE_TEAM_ID,
                "--notary-profile",
                SENTINEL_PROFILE,
            ]
        )
    assert caught.value.code == 2
    err = capsys.readouterr().err
    for sentinel in (
        SENTINEL_IDENTITY,
        SENTINEL_PROFILE,
        SENTINEL_TOOL_OUTPUT,
        SENTINEL_TOOL_ERROR,
    ):
        assert sentinel not in err
    assert "Traceback" not in err
    assert "could not be applied" in err
    assert not output.exists()


def test_release_cli_never_echoes_notary_inputs_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "host"
    commands: list[list[str]] = []
    _cli_with_runner(
        monkeypatch,
        _release_runner(
            commands,
            timeout_at=lambda cmd: cmd[:3] == ["xcrun", "notarytool", "submit"],
        ),
    )
    with pytest.raises(SystemExit) as caught:
        BUILD.main(
            [
                "--output",
                str(output),
                "--release",
                "--signing-identity",
                SENTINEL_IDENTITY,
                "--team-id",
                RELEASE_TEAM_ID,
                "--notary-profile",
                SENTINEL_PROFILE,
            ]
        )
    assert caught.value.code == 2
    err = capsys.readouterr().err
    for sentinel in (
        SENTINEL_IDENTITY,
        SENTINEL_PROFILE,
        SENTINEL_TOOL_OUTPUT,
        SENTINEL_TOOL_ERROR,
    ):
        assert sentinel not in err
    assert "did not finish" in err
    assert "Traceback" not in err
    assert not any(cmd[:3] == ["xcrun", "stapler", "staple"] for cmd in commands)
    assert not output.exists()


@pytest.mark.parametrize("stage", ["sign", "notary-timeout", "notary-output"])
def test_release_failures_are_sanitized_and_unchained(tmp_path: Path, stage: str) -> None:
    commands: list[list[str]] = []
    if stage == "sign":
        runner = _release_runner(
            commands, fail=lambda cmd: cmd[:2] == ["codesign", "--force"]
        )
    elif stage == "notary-timeout":
        runner = _release_runner(
            commands,
            timeout_at=lambda cmd: cmd[:3] == ["xcrun", "notarytool", "submit"],
        )
    else:
        runner = _release_runner(commands, notary_stdout=SENTINEL_TOOL_OUTPUT)

    with pytest.raises(BUILD.BuildError) as caught:
        BUILD.build(
            tmp_path / "host",
            platform_name="darwin",
            runner=runner,
            release_signing=_release_signing(
                identity=SENTINEL_IDENTITY, keychain_profile=SENTINEL_PROFILE
            ),
        )

    error = caught.value
    text = str(error)
    for sentinel in (
        SENTINEL_IDENTITY,
        SENTINEL_PROFILE,
        SENTINEL_TOOL_OUTPUT,
        SENTINEL_TOOL_ERROR,
    ):
        assert sentinel not in text
    # Neither the tool argv nor the captured output is part of the message.
    assert "--sign" not in text
    assert "--keychain-profile" not in text
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert not (tmp_path / "host").exists()


@pytest.mark.parametrize(
    "status", [[], {}, 0, True], ids=["list", "object", "number", "bool"]
)
def test_release_cli_rejects_a_non_string_notary_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: Any,
) -> None:
    # Valid JSON whose status is not a string must fail sanitized, not raise
    # TypeError out of the status check.
    output = tmp_path / "host"
    commands: list[list[str]] = []
    _cli_with_runner(monkeypatch, _release_runner(commands, notary_status=status))
    with pytest.raises(SystemExit) as caught:
        BUILD.main(
            [
                "--output",
                str(output),
                "--release",
                "--signing-identity",
                SENTINEL_IDENTITY,
                "--team-id",
                RELEASE_TEAM_ID,
                "--notary-profile",
                SENTINEL_PROFILE,
            ]
        )
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "an unrecognized status" in err
    assert "TypeError" not in err
    assert "Traceback" not in err
    for sentinel in (SENTINEL_IDENTITY, SENTINEL_PROFILE, SENTINEL_TOOL_OUTPUT):
        assert sentinel not in err
    assert not any(cmd[:3] == ["xcrun", "stapler", "staple"] for cmd in commands)
    assert not output.exists()


@pytest.mark.parametrize(
    "raw_field, fragment",
    [
        ({"archs": SENTINEL_RAW_ARCHS + "\n"}, "must carry exactly"),
        ({"identifier": SENTINEL_RAW_FIELD}, "bundle identifier"),
        ({"team_id": SENTINEL_RAW_FIELD}, "signing team"),
        ({"entitlements": {SENTINEL_RAW_FIELD: True}}, "entitlements"),
    ],
    ids=["archs", "identifier", "team", "entitlements"],
)
def test_release_cli_never_echoes_raw_tool_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw_field: dict[str, Any],
    fragment: str,
) -> None:
    # Malformed native-tool fields (architecture, identifier, team, entitlement
    # keys) may not echo their content, the identity or the profile.
    output = tmp_path / "host"
    _cli_with_runner(
        monkeypatch, _release_runner(fault={**raw_field, "stage": "built"})
    )
    with pytest.raises(SystemExit) as caught:
        BUILD.main(
            [
                "--output",
                str(output),
                "--release",
                "--signing-identity",
                SENTINEL_IDENTITY,
                "--team-id",
                RELEASE_TEAM_ID,
                "--notary-profile",
                SENTINEL_PROFILE,
            ]
        )
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert fragment in err
    for sentinel in (
        SENTINEL_IDENTITY,
        SENTINEL_PROFILE,
        SENTINEL_RAW_ARCHS,
        SENTINEL_RAW_FIELD,
        SENTINEL_TOOL_OUTPUT,
        SENTINEL_TOOL_ERROR,
    ):
        assert sentinel not in err
    assert "Traceback" not in err
    assert not output.exists()


@requires_macos_tools
def test_build_archive_preserves_strict_signature(tmp_path: Path) -> None:
    result = BUILD.build(tmp_path / "host")
    app: Path = result["paths"]["app"]
    archive: Path = result["paths"]["archive"]

    codesign = subprocess.run(
        ["codesign", "--verify", "--strict", str(app)], capture_output=True, text=True
    )
    assert codesign.returncode == 0, codesign.stderr

    archs = subprocess.run(
        ["lipo", "-archs", str(app / "Contents" / "MacOS" / "Ciaobot Server")],
        capture_output=True,
        text=True,
    )
    assert set(archs.stdout.split()) == {"arm64", "x86_64"}

    # Re-extract to a fresh scratch dir and strict-verify without recompiling.
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(extracted, filter="fully_trusted")
    extracted_app = extracted / BUILD.APP_NAME
    reverify = subprocess.run(
        ["codesign", "--verify", "--strict", str(extracted_app)], capture_output=True, text=True
    )
    assert reverify.returncode == 0, reverify.stderr

    with (extracted_app / "Contents" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["CFBundleIdentifier"] == "local.ciaobot.server"
    assert info[BUILD.HOST_PROTOCOL_KEY] == 2

    # The per-arch CDHashes in the metadata are the real signed slices' hashes,
    # cross-checked against each slice thinned out of the extracted executable.
    executable = extracted_app / "Contents" / "MacOS" / BUILD.EXECUTABLE_NAME
    for arch, expected in result["metadata"]["per_arch_cdhashes"].items():
        thin = tmp_path / f"thin-{arch}"
        subprocess.run(
            ["lipo", "-thin", arch, "-output", str(thin), str(executable)],
            check=True,
            capture_output=True,
        )
        shown = subprocess.run(
            ["codesign", "-dv", "--verbose=4", str(thin)], capture_output=True, text=True
        )
        assert f"CDHash={expected}" in shown.stderr.splitlines()


@requires_macos_tools
def test_the_built_app_carries_the_folder_usage_strings(tmp_path: Path) -> None:
    # The real, signed bundle: the three folder usage keys are in its sealed
    # Info.plist, and the bundle version is the new host revision.
    result = BUILD.build(tmp_path / "host")
    app: Path = result["paths"]["app"]

    with (app / "Contents" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)

    assert info["CFBundleVersion"] == "4"
    assert info["CFBundleShortVersionString"] == "4"
    for key, folder in _FOLDER_USAGE_KEYS.items():
        text = info[key]
        assert isinstance(text, str)
        assert folder in text
        assert "Ciaobot Server" in text
        assert "workspace" in text
    # The two existing usage descriptions are still there.
    assert info["NSAppleEventsUsageDescription"] == BUILD.APPLE_EVENTS_USAGE
    assert info["NSAccessibilityUsageDescription"] == BUILD.ACCESSIBILITY_USAGE


# --- serve branch runtime (macOS only) ------------------------------------


def _host_target() -> str:
    machine = platform.machine()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
    return f"{arch}-apple-macosx13.0"


@pytest.fixture(scope="module")
def host_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not HAS_TOOLS:
        pytest.skip("the native host build requires macOS Command Line Tools")
    workdir = tmp_path_factory.mktemp("server-host-bin")
    binary = workdir / "Ciaobot Server"
    completed = subprocess.run(
        [
            "xcrun",
            "swiftc",
            str(HOST_SOURCE),
            "-target",
            _host_target(),
            "-O",
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    return binary


@pytest.fixture(scope="module")
def child_helper(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile the offline C child fixture once per module."""
    if not HAS_CC:
        pytest.skip("the child fixture needs the system C compiler")
    workdir = tmp_path_factory.mktemp("server-host-child")
    source = workdir / "helper.c"
    source.write_text(_HELPER_C_SOURCE, encoding="utf-8")
    helper = workdir / "helper"
    completed = subprocess.run(
        ["cc", "-O1", "-o", str(helper), str(source)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    return helper


def _write_fake_child(directory: Path, script: str, name: str = "fake_child.sh") -> Path:
    path = directory / name
    path.write_text(script)
    path.chmod(0o755)
    return path


def _child_env(**extra: Any) -> dict[str, str]:
    env = dict(os.environ)
    for key, value in extra.items():
        env[key] = str(value)
    return env


def _wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _read_json(path: Path, timeout: float = 5.0) -> dict[str, Any]:
    assert _wait_from_file(path, timeout=timeout), f"{path} was never written"
    parsed: dict[str, Any] = json.loads(path.read_text())
    return parsed


def _read_descendant(path: Path, timeout: float = 5.0) -> tuple[int, int]:
    assert _wait_from_file(path, timeout=timeout), f"{path} was never written"
    child, pgid = path.read_text().split()
    return int(child), int(pgid)


@requires_macos_tools
def test_host_rejects_invalid_argv(host_binary: Path, tmp_path: Path) -> None:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(host_binary), *args], capture_output=True, text=True, timeout=30)

    assert run([]).returncode == 2
    assert run(["serve"]).returncode == 2
    assert run(["serve", "--python"]).returncode == 2
    assert run(["serve", "--python", "relative/path"]).returncode == 2
    assert run(["serve", "--python", "/does/not/exist"]).returncode == 2
    # An existing but non-executable file is refused.
    plain = tmp_path / "plain.txt"
    plain.write_text("not executable")
    assert run(["serve", "--python", str(plain)]).returncode == 2
    # Repeated, unknown and extra arguments are refused.
    assert run(["serve", "--python", "/usr/bin/true", "--python", "/usr/bin/true"]).returncode == 2
    assert run(["serve", "--python", "/usr/bin/true", "extra"]).returncode == 2
    assert run(["serve", "--unknown"]).returncode == 2
    assert run(["request-accessibility", "extra"]).returncode == 2
    # Nothing was launched or prompted: every refusal is a plain exit 2.
    for result in (run([]), run(["bogus"])):
        assert result.returncode == 2
        assert result.stdout == ""


@requires_macos_tools
def test_host_fixed_supervisor_argv_and_lifetime(
    host_binary: Path, child_helper: Path, tmp_path: Path
) -> None:
    record = tmp_path / "record.json"
    env = _child_env(
        HOST_RECORD=record, HOST_MODE="report", HOST_ECHO="1", CIAO_MARK="inherited-value"
    )
    host = subprocess.Popen(
        [str(host_binary), "serve", "--python", str(child_helper)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    stdout, stderr = host.communicate(timeout=30)

    # The child's io is inherited, not captured by the host.
    assert stdout == "child-stdout\n"
    assert stderr == "child-stderr\n"
    # The host stays alive until the child exits and mirrors a clean status.
    assert host.returncode == 0

    rec = json.loads(record.read_text())
    # Exact fixed argv, and nothing else.
    assert rec["args"] == ["-I", "-m", "ciao.cli", "supervise"]
    # cwd and parent are inherited.
    assert rec["cwd"] == str(tmp_path)
    assert rec["ppid"] == host.pid
    # No permission or shell surface: only stdio is open in the child.
    assert rec["open_fds"] == [0, 1, 2]
    # The environment is inherited verbatim.
    assert rec["mark"] == "inherited-value"


@requires_macos_tools
def test_host_run_move_spawns_the_move_child(
    host_binary: Path, child_helper: Path, tmp_path: Path
) -> None:
    # The move job runs under the host, so a granted folder grant covers it
    # (#1658). The child's argv is the fixed move command plus the two
    # validated values - no arbitrary command and no shell.
    record = tmp_path / "record.json"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    operation = "op-1234"
    env = _child_env(HOST_RECORD=record, HOST_MODE="report", HOST_EXIT="0")
    host = subprocess.Popen(
        [
            str(host_binary),
            "run-move",
            "--python",
            str(child_helper),
            "--operation",
            operation,
            "--state-dir",
            str(state_dir),
        ],
        env=env,
        start_new_session=True,
    )
    host_pgid = os.getpgid(host.pid)
    try:
        assert host.wait(timeout=30) == 0
        rec = json.loads(record.read_text())
        # Exactly the fixed move argv, and nothing else.
        assert rec["args"] == [
            "-I",
            "-m",
            "ciao.workspace_move",
            "run",
            "--operation",
            operation,
            "--state-dir",
            str(state_dir),
        ]
        # The child shares the host's process group: the move job stays in
        # launchd's own job group, like the supervisor does.
        assert rec["pgid"] == host_pgid
        assert rec["sid"] == host_pgid
        assert rec["ppid"] == host.pid
        # No permission or shell surface: only stdio is open in the child.
        assert rec["open_fds"] == [0, 1, 2]
    finally:
        _kill_group_exact(host_pgid)


@requires_macos_tools
def test_host_run_move_mirrors_the_child_exit_code(
    host_binary: Path, child_helper: Path, tmp_path: Path
) -> None:
    # The move job's own exit status is what launchd and the move's log see,
    # so the host mirrors it exactly.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    for code in (0, 5, 78):
        result = subprocess.run(
            [
                str(host_binary),
                "run-move",
                "--python",
                str(child_helper),
                "--operation",
                "op-1234",
                "--state-dir",
                str(state_dir),
            ],
            capture_output=True,
            text=True,
            env=_child_env(
                HOST_RECORD=tmp_path / f"record-{code}.json",
                HOST_MODE="report",
                HOST_EXIT=str(code),
            ),
            timeout=30,
        )
        assert result.returncode == code


@requires_macos_tools
def test_host_rejects_an_invalid_run_move_argv(
    host_binary: Path, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    executable = tmp_path / "python"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    plain = tmp_path / "plain.txt"
    plain.write_text("not executable")

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(host_binary), *args], capture_output=True, text=True, timeout=30
        )

    valid = [
        "run-move",
        "--python",
        str(executable),
        "--operation",
        "op-1234",
        "--state-dir",
        str(state_dir),
    ]
    # A relative python, a missing --state-dir, a ".." in either value and an
    # extra argument are all refused before anything is spawned.
    assert run([*valid[:2], "relative/python", *valid[4:]]).returncode == 2
    assert run([*valid[:2], str(executable), "--operation", "op-1234"]).returncode == 2
    assert (
        run(
            [
                "run-move",
                "--python",
                str(executable),
                "--operation",
                "op-1234",
                "--state-dir",
                f"{state_dir}/../escape",
            ]
        ).returncode
        == 2
    )
    assert (
        run(
            [
                "run-move",
                "--python",
                f"{executable}/../python",
                "--operation",
                "op-1234",
                "--state-dir",
                str(state_dir),
            ]
        ).returncode
        == 2
    )
    assert run([*valid, "extra"]).returncode == 2
    assert run([*valid, "--python", str(executable)]).returncode == 2
    # A non-executable python is refused, like the serve branch.
    assert (
        run(
            [
                "run-move",
                "--python",
                str(plain),
                "--operation",
                "op-1234",
                "--state-dir",
                str(state_dir),
            ]
        ).returncode
        == 2
    )
    # The operation id must be a plain id, not a path or an option.
    assert (
        run(
            [
                "run-move",
                "--python",
                str(executable),
                "--operation",
                "../escape",
                "--state-dir",
                str(state_dir),
            ]
        ).returncode
        == 2
    )
    # An id shaped like an option is refused too: the child would read it as
    # one, and the engine never mints an id that starts with a dash.
    for bad_id in ("--state-dir", "-x", ".hidden", "op 1234", ""):
        assert (
            run(
                [
                    "run-move",
                    "--python",
                    str(executable),
                    "--operation",
                    bad_id,
                    "--state-dir",
                    str(state_dir),
                ]
            ).returncode
            == 2
        ), bad_id
    # Nothing was spawned: every refusal is a plain exit 2 with no output.
    for args in (
        [*valid[:2], "relative/python", *valid[4:]],
        [*valid, "extra"],
    ):
        result = run(args)
        assert result.returncode == 2
        assert result.stdout == ""


@requires_macos_tools
def test_host_child_resets_signal_state_and_drops_extra_fds(
    host_binary: Path, child_helper: Path, tmp_path: Path
) -> None:
    record = tmp_path / "record.json"
    # The test parent opens an extra fd and passes it to the host. The child must
    # not inherit it: CLOEXEC_DEFAULT closes everything but the re-inherited 0-2.
    read_fd, write_fd = os.pipe()
    try:
        env = _child_env(
            HOST_RECORD=record,
            HOST_MODE="report",
            HOST_EXTRA_FD=write_fd,
            CIAO_MARK="inherited-value",
        )
        result = subprocess.run(
            [str(host_binary), "serve", "--python", str(child_helper)],
            pass_fds=(write_fd,),
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env=env,
            timeout=30,
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert result.returncode == 0

    rec = json.loads(record.read_text())
    # The host's SIG_IGN dispositions are not leaked: the child starts clean.
    assert rec["term"] == "dfl"
    assert rec["int"] == "dfl"
    assert rec["mask"] == 0
    # The extra fd passed to the host is not open in the child.
    assert rec["extra_fd_open"] is False
    assert rec["open_fds"] == [0, 1, 2]
    # cwd and the environment are inherited from the host process; no custom
    # DYLD/PYTHONPATH knob is injected.
    assert rec["cwd"] == str(tmp_path)
    assert rec["mark"] == "inherited-value"


@requires_macos_tools
def test_host_descendants_share_job_group_and_die_with_it(
    host_binary: Path, child_helper: Path, tmp_path: Path
) -> None:
    # The host is started as a session/group leader, the way launchd starts a
    # job. The supervisor and its descendants must stay in that one group so
    # launchd's final job-group cleanup reaches them.
    record = tmp_path / "group.json"
    descendant_file = tmp_path / "descendant.pid"
    env = _child_env(
        HOST_RECORD=record,
        HOST_MODE="ignore_term",
        HOST_DESCENDANT_PIDFILE=descendant_file,
    )
    host = subprocess.Popen(
        [str(host_binary), "serve", "--python", str(child_helper)],
        env=env,
        start_new_session=True,
    )
    host_pgid = os.getpgid(host.pid)
    try:
        rec = _read_json(record)
        descendant_pid, descendant_pgid = _read_descendant(descendant_file)
        # The host is the group/session leader, and the child shares it.
        assert host_pgid == host.pid
        assert rec["pgid"] == host_pgid
        assert rec["sid"] == host_pgid
        assert descendant_pgid == host_pgid

        # Simulate launchd's final job-group cleanup, the authority when the
        # host exits or crashes. This is the whole point of dropping
        # Foundation.Process: with a separate child group, the supervisor would
        # survive here.
        os.killpg(host_pgid, signal.SIGKILL)
        assert host.wait(timeout=10) == -signal.SIGKILL
        # A zombie counts as dead: only a still-scheduled process is a survivor.
        assert _await_dead(rec["pid"]), "supervisor survived the job-group kill"
        assert _await_dead(descendant_pid), "descendant survived the job-group kill"
    finally:
        _kill_group_exact(host_pgid)


@requires_macos_tools
def test_host_mirrors_child_exit_and_signal(
    host_binary: Path, child_helper: Path, tmp_path: Path
) -> None:
    # A very fast exit must not be missed by the process-exit source.
    for _ in range(5):
        record = tmp_path / "fast.json"
        result = subprocess.run(
            [str(host_binary), "serve", "--python", str(child_helper)],
            capture_output=True,
            text=True,
            env=_child_env(HOST_RECORD=record, HOST_MODE="report", HOST_EXIT="7"),
            timeout=30,
        )
        assert result.returncode == 7

    # A child killed externally (not by a stop) mirrors 128 + signal.
    pid_file = tmp_path / "child.pid"
    longrun = _write_fake_child(
        tmp_path,
        "#!/bin/sh\necho $$ > \"" + str(pid_file) + "\"\nwhile :; do sleep 0.5; done\n",
        name="longrun.sh",
    )
    host = subprocess.Popen([str(host_binary), "serve", "--python", str(longrun)])
    try:
        assert _wait_from_file(pid_file)
        child_pid = int(pid_file.read_text().strip())
        os.kill(child_pid, signal.SIGKILL)
        assert host.wait(timeout=30) == 128 + signal.SIGKILL
    finally:
        _terminate_exact(host)
        _kill_exact_from_file(pid_file)


@requires_macos_tools
def test_host_forwards_stop_and_reaps(
    host_binary: Path, child_helper: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "got-term"
    record = tmp_path / "child.json"
    env = _child_env(HOST_RECORD=record, HOST_MODE="term", HOST_MARKER=marker)
    host = subprocess.Popen([str(host_binary), "serve", "--python", str(child_helper)], env=env)
    try:
        rec = _read_json(record)
        host.send_signal(signal.SIGTERM)
        # A child that exits 0 after the forwarded SIGTERM is a clean stop.
        assert host.wait(timeout=30) == 0
        assert marker.exists()
        # The SIGTERM was forwarded, and the child was reaped (not a zombie).
        assert _await_dead(rec["pid"])
    finally:
        _terminate_exact(host)


@requires_macos_tools
@pytest.mark.parametrize(
    "action, exit_code, expected",
    [
        ("exit", 5, 5),
        ("exit", 130, 0),
        ("exit", 143, 0),
        ("abort", 0, 128 + signal.SIGABRT),
    ],
)
def test_host_stop_status_mapping(
    host_binary: Path,
    child_helper: Path,
    tmp_path: Path,
    action: str,
    exit_code: int,
    expected: int,
) -> None:
    # A stop is a clean 0 for the codes the real supervisor answers a stop with:
    # 0, 130 (stop during backoff/start) and 143 (engine died by the forwarded
    # TERM), plus death by the forwarded SIGTERM. Any other non-zero exit or a
    # crash after the stop is preserved so launchd and logs see the failure.
    record = tmp_path / "child.json"
    env = _child_env(
        HOST_RECORD=record,
        HOST_MODE="term",
        HOST_TERM_ACTION=action,
        HOST_TERM_EXIT=exit_code,
    )
    host = subprocess.Popen([str(host_binary), "serve", "--python", str(child_helper)], env=env)
    try:
        _read_json(record)
        host.send_signal(signal.SIGTERM)
        assert host.wait(timeout=30) == expected
    finally:
        _terminate_exact(host)


@requires_macos_tools
@pytest.mark.parametrize("exit_code", [130, 143])
def test_host_without_stop_preserves_supervisor_codes(
    host_binary: Path, child_helper: Path, tmp_path: Path, exit_code: int
) -> None:
    # Without a stop request, 130 and 143 are ordinary child exits and pass
    # through unchanged; the clean-stop mapping must not swallow them.
    record = tmp_path / "child.json"
    result = subprocess.run(
        [str(host_binary), "serve", "--python", str(child_helper)],
        capture_output=True,
        text=True,
        env=_child_env(HOST_RECORD=record, HOST_MODE="report", HOST_EXIT=str(exit_code)),
        timeout=30,
    )
    assert result.returncode == exit_code


@requires_macos_tools
def test_host_stop_escalates_stalled_child(
    host_binary: Path, child_helper: Path, tmp_path: Path
) -> None:
    # A stalled supervisor ignores TERM. The host escalates to SIGKILL on the
    # tracked, unreaped child PID after the 35 s grace, reports the failed
    # shutdown, and reaps. It does NOT kill the shared group: launchd's final
    # job-group cleanup is what reaches the descendants.
    record = tmp_path / "child.json"
    descendant_file = tmp_path / "descendant.pid"
    env = _child_env(
        HOST_RECORD=record,
        HOST_MODE="ignore_term",
        HOST_DESCENDANT_PIDFILE=descendant_file,
    )
    host = subprocess.Popen(
        [str(host_binary), "serve", "--python", str(child_helper)],
        env=env,
        start_new_session=True,
    )
    host_pgid = os.getpgid(host.pid)
    try:
        rec = _read_json(record)
        descendant_pid, _ = _read_descendant(descendant_file)
        host.send_signal(signal.SIGTERM)
        started = time.time()
        assert host.wait(timeout=45) == 137
        # The grace is honoured (no early kill) and the host is the outer bound.
        assert 34 <= time.time() - started < 40
        # The supervisor was SIGKILLed and reaped; a zombie counts as dead.
        assert _await_dead(rec["pid"])
        # The standalone host cannot kill the shared group, so the descendant is
        # still there. This is honest, not a regression: launchd does it.
        assert _is_running(descendant_pid), "the host must not killpg the shared group"
        # Simulate that final launchd cleanup now.
        os.killpg(host_pgid, signal.SIGKILL)
        assert _await_dead(descendant_pid)
    finally:
        _kill_group_exact(host_pgid)


@requires_macos_tools
def test_host_stop_during_startup_launches_nothing(host_binary: Path, tmp_path: Path) -> None:
    # A stop that lands after the handlers are installed but while AppKit is
    # still starting must win: no child is launched. Launches are observed as
    # NOTE_FORK on the host pid, which posix_spawn also raises, so a child
    # killed before it could record itself still counts. The stop is swept
    # across the startup window because its timing cannot be pinned from
    # outside; samples whose fork may have raced the stop itself are discarded.
    pids = tmp_path / "launched.pids"
    child = _write_fake_child(
        tmp_path,
        f"#!/bin/sh\necho $$ >> \"{pids}\"\ntrap 'exit 0' TERM\nwhile :; do sleep 0.1; done\n",
    )
    handled = 0
    try:
        for delay in (0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05) * 3:
            host = subprocess.Popen([str(host_binary), "serve", "--python", str(child)])
            queue = select.kqueue()
            try:
                queue.control(
                    [
                        select.kevent(
                            host.pid,
                            filter=select.KQ_FILTER_PROC,
                            flags=select.KQ_EV_ADD,
                            fflags=select.KQ_NOTE_FORK | select.KQ_NOTE_EXIT,
                        )
                    ],
                    0,
                )
                time.sleep(delay)
                forked_before = _host_forked(queue.control(None, 8, 0))
                stopped_at = time.monotonic()
                host.send_signal(signal.SIGTERM)
                # A fork soon after the stop may come from a launch decided just
                # before it: the host polls its stop queue, then spawns, and a
                # contended runner can preempt it between the two for a whole
                # scheduler slice. Spawning takes about a millisecond on an idle
                # Mac, but a 5 ms window failed a loaded macOS CI runner (#2156).
                # The exact ordering, a stop pending at the decision means no
                # fork, is proved by test_host_startup_stop_precedes_child_launch;
                # this sweep only checks the shipped binary against real startup
                # timing, so it gives a racing launch one scheduler slice plus
                # margin.
                racing = queue.control(None, 8, 0.05)
                forked_racing = _host_forked(racing)
                events: list[Any] = list(racing)
                forked_after = None
                while not any(event.fflags & select.KQ_NOTE_EXIT for event in events):
                    batch = queue.control(None, 8, 45)
                    assert batch, "host did not exit after a startup stop"
                    if forked_after is None and _host_forked(batch):
                        forked_after = time.monotonic() - stopped_at
                    events.extend(batch)
                code = host.wait(timeout=5)
            finally:
                queue.close()
                _terminate_exact(host)
            if code != 0 or forked_before or forked_racing:
                # Killed before the handlers existed, or launched before the stop.
                continue
            handled += 1
            assert not _host_forked(events), (
                f"a child was launched after a stop at {delay}s "
                f"(fork seen {forked_after}s after the stop)"
            )
    finally:
        time.sleep(0.2)
        if pids.exists():
            for line in pids.read_text().split():
                pid = int(line)
                if _pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
    if handled == 0:
        pytest.skip("no stop landed inside the startup window on this machine")


# The deterministic startup-stop tests need the host's main queue held between
# the first event-loop turn and the launch, which cannot be done from outside
# the process. They compile a copy of the shipped source with a pause inserted at
# that one point; the shipped host has no such hook. The transformation anchors on
# the exact launch block and fails if it moves.
_LAUNCH_BLOCK = "DispatchQueue.main.async { self?.launch() }"
_PAUSE_BLOCK = (
    "DispatchQueue.main.async {\n"
    "                if let base = getenv(\"HOST_PAUSE\") {\n"
    "                    let prefix = String(cString: base)\n"
    "                    close(open(prefix + \".paused\", O_WRONLY | O_CREAT, 0o644))\n"
    "                    while access(prefix + \".go\", F_OK) != 0 { usleep(1000) }\n"
    "                }\n"
    "                self?.launch()\n"
    "            }"
)


@pytest.fixture(scope="module")
def paused_host_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not HAS_TOOLS:
        pytest.skip("the native host build requires macOS Command Line Tools")
    source = HOST_SOURCE.read_text(encoding="utf-8")
    assert source.count(_LAUNCH_BLOCK) == 1, "the launch block moved; update the pause anchor"
    workdir = tmp_path_factory.mktemp("server-host-paused")
    patched = workdir / "ServerHostPaused.swift"
    patched.write_text(source.replace(_LAUNCH_BLOCK, _PAUSE_BLOCK), encoding="utf-8")
    binary = workdir / "Ciaobot Server"
    completed = subprocess.run(
        ["xcrun", "swiftc", str(patched), "-target", _host_target(), "-O", "-o", str(binary)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    return binary


def _start_paused_host(binary: Path, child: Path, prefix: Path) -> subprocess.Popen[Any]:
    host = subprocess.Popen(
        [str(binary), "serve", "--python", str(child)],
        env=_child_env(HOST_PAUSE=prefix),
    )
    assert _wait_for(lambda: Path(f"{prefix}.paused").exists(), 30), "host never reached the launch"
    return host


@requires_macos_tools
def test_host_startup_stop_precedes_child_launch(
    paused_host_binary: Path, tmp_path: Path
) -> None:
    # The host's main queue is held after the launch block was queued, so the
    # signal source cannot run its handler. A stop sent now is pending, not yet
    # handled, when the launch decision is made, and must still win: no fork.
    pids = tmp_path / "launched.pids"
    child = _write_fake_child(
        tmp_path, f"#!/bin/sh\necho $$ >> \"{pids}\"\nexec sleep 30\n"
    )
    prefix = tmp_path / "pause"
    host = _start_paused_host(paused_host_binary, child, prefix)
    queue = select.kqueue()
    try:
        queue.control(
            [
                select.kevent(
                    host.pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD,
                    fflags=select.KQ_NOTE_FORK | select.KQ_NOTE_EXIT,
                )
            ],
            0,
        )
        host.send_signal(signal.SIGTERM)
        # kill() records the signal in the host's kqueue synchronously, and the
        # main queue is blocked, so releasing the launch now is deterministic.
        Path(f"{prefix}.go").touch()
        events: list[Any] = []
        while not any(event.fflags & select.KQ_NOTE_EXIT for event in events):
            batch = queue.control(None, 8, 30)
            assert batch, "host did not exit after the stop"
            events.extend(batch)
        assert host.wait(timeout=5) == 0
        assert not _host_forked(events), "a child was launched although a stop was pending"
        assert not pids.exists()
    finally:
        queue.close()
        _terminate_exact(host)


@requires_macos_tools
def test_host_startup_launch_then_stop_reaps_its_child(
    paused_host_binary: Path, tmp_path: Path
) -> None:
    # The opposite ordering: the launch decision is made with no stop pending, the
    # child starts, and the stop that follows is forwarded, the child reaped and the
    # host exits 0. Not a startup violation.
    pids = tmp_path / "launched.pids"
    got_term = tmp_path / "got_term"
    ready = tmp_path / "ready"
    child = _write_fake_child(
        tmp_path,
        f"#!/bin/sh\ntrap 'touch \"{got_term}\"; exit 0' TERM\n"
        f"echo $$ >> \"{pids}\"\ntouch \"{ready}\"\n"
        "while :; do sleep 0.1; done\n",
    )
    prefix = tmp_path / "pause"
    host = _start_paused_host(paused_host_binary, child, prefix)
    child_pid = 0
    try:
        Path(f"{prefix}.go").touch()
        # The child touches `ready` only after its trap is installed.
        assert _wait_for(ready.exists, 10), "the child never became ready"
        child_pid = int(pids.read_text().split()[0])
        host.send_signal(signal.SIGTERM)
        assert host.wait(timeout=45) == 0
        assert got_term.exists(), "the child never received the forwarded stop"
        assert _await_dead(child_pid)
    finally:
        _terminate_exact(host)
        if child_pid and _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def _host_forked(events: list[Any]) -> bool:
    return any(event.fflags & select.KQ_NOTE_FORK for event in events)


@requires_macos_tools
def test_host_launch_failure(host_binary: Path, tmp_path: Path) -> None:
    # An executable file that is not a valid program passes the parser and fails
    # at posix_spawn, which the host reports as a diagnostic and exit 1.
    broken = tmp_path / "broken"
    broken.write_bytes(b"not a mach-o executable")
    broken.chmod(0o755)
    result = subprocess.run(
        [str(host_binary), "serve", "--python", str(broken)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 1
    assert "could not launch supervisor" in result.stderr


# The self-quit trigger for the graceful-quit fixture. It is inserted just
# before the top-level entrypoint (`let operation = parseInvocation(...)`), which
# never returns, so appending it after the source would leave it unreachable. It
# sends this process its own `kAEQuitApplication` event — the same event
# loginwindow sends on logout and `quit app "Ciaobot Server"` sends — which needs
# no Automation consent because the target is this process. The struct and
# controller source above it are unchanged.
_QUIT_TRIGGER_SOURCE = r"""
// --- test-only self-quit trigger (never shipped) ---
DispatchQueue.global().asyncAfter(deadline: .now() + 1.0) {
    var psn = ProcessSerialNumber(highLongOfPSN: 0, lowLongOfPSN: UInt32(kCurrentProcess))
    let target = NSAppleEventDescriptor(
        descriptorType: typeProcessSerialNumber, bytes: &psn,
        length: MemoryLayout<ProcessSerialNumber>.size
    )
    let event = NSAppleEventDescriptor(
        eventClass: AEEventClass(kCoreEventClass), eventID: AEEventID(kAEQuitApplication),
        targetDescriptor: target, returnID: AEReturnID(kAutoGenerateReturnID),
        transactionID: AETransactionID(kAnyTransactionID)
    )
    _ = try? event.sendEvent(options: [.noReply], timeout: 2.0)
}
"""

_ENTRYPOINT_MARKER = "\nlet operation = parseInvocation(CommandLine.arguments)\n"


@pytest.fixture(scope="module")
def quit_host_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The shipped source with the self-quit trigger injected, compiled once."""
    if not HAS_TOOLS:
        pytest.skip("the native host build requires macOS Command Line Tools")
    source = HOST_SOURCE.read_text(encoding="utf-8")
    assert source.count(_ENTRYPOINT_MARKER) == 1
    fixture_source = source.replace(
        _ENTRYPOINT_MARKER, _QUIT_TRIGGER_SOURCE + _ENTRYPOINT_MARKER
    )
    workdir = tmp_path_factory.mktemp("server-host-quit")
    source_path = workdir / "QuitFixture.swift"
    source_path.write_text(fixture_source, encoding="utf-8")
    binary = workdir / "CiaobotServerHostQuitFixture"
    completed = subprocess.run(
        ["xcrun", "swiftc", str(source_path), "-target", _host_target(), "-O", "-o", str(binary)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    return binary


@requires_macos_tools
def test_host_graceful_quit_forwards_stop_and_reaps(
    quit_host_binary: Path, child_helper: Path, tmp_path: Path
) -> None:
    # A graceful macOS quit must forward a stop to the supervisor and mirror its
    # status, not call NSApplication.terminate(_:) and orphan it. The fixture
    # sends itself the Quit Apple Event the same way loginwindow does.
    record = tmp_path / "child.json"
    marker = tmp_path / "got-term"
    env = _child_env(HOST_RECORD=record, HOST_MODE="term", HOST_MARKER=marker)
    host = subprocess.Popen([str(quit_host_binary), "serve", "--python", str(child_helper)], env=env)
    try:
        rec = _read_json(record)
        # The quit arrives from the fixture about a second in; a clean mirrored
        # stop is exit 0.
        assert host.wait(timeout=20) == 0
        assert marker.exists(), "the quit did not forward a stop to the supervisor"
        assert _await_dead(rec["pid"]), "the supervisor was not reaped"
    finally:
        _terminate_exact(host)


def test_serve_branch_has_no_permission_api() -> None:
    source = HOST_SOURCE.read_text(encoding="utf-8")
    # The only AX call lives in the request branch's one named function.
    before_request, after_request = source.split("func requestAccessibilityTrust()")
    request_region, after_request_fn = after_request.split("func runRequestAccessibility()")
    assert "AXIsProcessTrustedWithOptions" in request_region

    # Everything outside that function, runServe included, is permission-free.
    run_serve = "func runServe(" + after_request_fn.split("func runServe(")[1]
    for region in (before_request, run_serve):
        for forbidden in ("AXIsProcessTrusted", "AXUIElement", "NSAppleScript", "osascript"):
            assert forbidden not in region
    # No shell or exec replacement anywhere.
    assert "/bin/sh" not in source
    assert "execv" not in source
    # The host must never signal the shared launchd job group itself.
    assert "killpg" not in _source_without_comments()


def test_the_move_branch_keeps_the_fixed_argv_discipline() -> None:
    # The host's promise is no arbitrary-command surface, and the move branch
    # is the newest one: its child is the fixed move command plus the two
    # validated values, and nothing else reaches the argv.
    source = _source_without_comments()
    # The fixed pieces are the only caller-supplied-free part of the argv.
    assert (
        'let MOVE_ARGUMENTS = ["-I", "-m", "ciao.workspace_move", "run", "--operation"]'
        in source
    )
    assert 'let MOVE_STATE_DIR_ARGUMENT = "--state-dir"' in source
    # The spawn builds the argv from the fixed pieces and the two values.
    match = re.search(
        r"func spawnMove\(.*?\n\}\n", source, re.DOTALL
    )
    assert match is not None
    spawn = match.group(0)
    assert "MOVE_ARGUMENTS + [operation, MOVE_STATE_DIR_ARGUMENT, stateDir]" in spawn
    # The move child is spawned by the same fixed-spawn helper as the
    # supervisor, so it gets the same attributes and no new process group.
    assert "spawnFixedChild(" in spawn
    # The parser validates both values before the child is built.
    parse = source.split("func parseInvocation(")[1].split("/// The outcome of preparing")[0]
    assert "isRunnableExecutable(python)" in parse
    assert "isPlainIdentifier(operation)" in parse
    assert "isNormalizedAbsolutePath(stateDir)" in parse
    # No new process group for the move child: it stays in launchd's job group.
    assert "POSIX_SPAWN_SETPGROUP" not in source


def test_host_protocol_revision_matches_builder() -> None:
    # The Swift constant and the builder's plist key must move together.
    source = HOST_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"^let HOST_PROTOCOL_REVISION = (\d+)$", source, re.MULTILINE)
    assert match is not None
    assert int(match.group(1)) == BUILD.HOST_PROTOCOL_REVISION


def test_host_replaces_appkit_quit_handler() -> None:
    # A graceful macOS Quit Apple Event must reach the controller's stop path,
    # not AppKit's default terminate/exit(0).
    source = HOST_SOURCE.read_text(encoding="utf-8")
    assert "NSApplicationDelegate" in source
    assert "NSAppleEventManager.shared().setEventHandler(" in source
    assert "kAEQuitApplication" in source
    # Installed before the deferred launch can run, not after didFinishLaunching.
    assert "applicationWillFinishLaunching" in source
    assert "applicationDidFinishLaunching" not in _source_without_comments()
    # It must route to the controller's stop path, and never set a cancel/terminate.
    assert "controller.handleStop()" in source
    assert "terminateLater" not in source
    assert "terminateCancel" not in source
    assert "NSApplication.shared.terminate" not in source


def test_host_stop_grace_outlasts_supervisor_grace() -> None:
    # The host is the outer bound: its escalation must fire after the Python
    # supervisor's own grace, or it would SIGKILL a supervisor that is still
    # stopping the engine cleanly.
    from ciao import supervise

    source = HOST_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"^let STOP_GRACE_SECONDS = ([0-9.]+)$", source, re.MULTILINE)
    assert match is not None
    assert float(match.group(1)) > supervise.STOP_GRACE_S


def test_host_registers_signal_source_before_ignoring_disposition() -> None:
    # N1: resume the dispatch source before setting SIG_IGN, so a stop signal is
    # never both ignored and unobserved during startup.
    source = HOST_SOURCE.read_text(encoding="utf-8")
    region = source.split("func begin()")[1].split("DispatchQueue.main.async")[0]
    resume = region.index("source.resume()")
    ignore = region.index("signal(sig, SIG_IGN)")
    assert resume < ignore


def _source_without_comments() -> str:
    source = HOST_SOURCE.read_text(encoding="utf-8")
    return "\n".join(line.split("//", 1)[0] for line in source.splitlines())


def test_host_uses_public_posix_spawn_not_foundation_process() -> None:
    # R1: the child must stay in launchd's job group, which Foundation.Process
    # would take away by making it a group leader.
    source = _source_without_comments()
    assert "posix_spawn(" in source
    assert "POSIX_SPAWN_CLOEXEC_DEFAULT" in source
    assert "posix_spawn_file_actions_addinherit_np" in source
    assert "POSIX_SPAWN_SETPGROUP" not in source
    assert "Process()" not in source


# --- process helpers (exact PIDs only, never broad patterns) ----------------


def _wait_from_file(path: Path, timeout: float = 5.0) -> bool:
    return _wait_for(lambda: path.exists() and path.read_text().strip() != "", timeout=timeout)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_state(pid: int) -> str:
    """The ``ps`` state character(s), or "" when the pid is gone.

    A zombie (``Z``) is not a survivor: it holds no scheduling slot and will be
    reaped by its parent, so callers treat it as dead.
    """
    try:
        completed = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def _is_running(pid: int) -> bool:
    state = _process_state(pid)
    if not state or "Z" in state:
        return False
    return True


def _await_dead(pid: int, timeout: float = 5.0) -> bool:
    return _wait_for(lambda: not _is_running(pid), timeout=timeout)


def _terminate_exact(host: subprocess.Popen[Any]) -> None:
    if host.poll() is None:
        host.terminate()
        try:
            host.wait(timeout=5)
        except subprocess.TimeoutExpired:
            host.kill()
            host.wait(timeout=5)


def _kill_exact_from_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        pid = int(path.read_text().strip())
    except (ValueError, OSError):
        return
    if pid > 0 and _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _kill_group_exact(pgid: int) -> None:
    """Kill one explicitly known fixture group; never a broad pattern."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
