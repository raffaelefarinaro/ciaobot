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

static void write_record(int argc, char **argv) {
    const char *path = getenv("HOST_RECORD");
    if (!path) return;
    FILE *f = fopen(path, "w");
    if (!f) return;
    int record_fd = fileno(f);
    sigset_t mask;
    sigemptyset(&mask);
    sigprocmask(SIG_BLOCK, NULL, &mask);
    unsigned int bits = 0;
    memcpy(&bits, &mask, sizeof(bits));
    struct sigaction term, intr;
    sigaction(SIGTERM, NULL, &term);
    sigaction(SIGINT, NULL, &intr);
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
    write_record(argc, argv);
    spawn_descendant();
    const char *mode = env_or("HOST_MODE", "report");
    if (strcmp(mode, "report") == 0) {
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
    assert info["CFBundleExecutable"] == "CiaobotServerHost"
    assert info["LSUIElement"] is True

    from ciao import cli, macos_service

    reserved = {macos_service.DESKTOP_BUNDLE_ID, "local.ciaobot.menubar", *cli._OUR_BUNDLE_IDS}
    assert BUILD.BUNDLE_ID not in reserved
    # Not the experiment identity either.
    assert BUILD.BUNDLE_ID != "local.ciaobot.server-host-experiment"


def test_revision_is_independent_of_engine_version() -> None:
    info = BUILD.bundle_info()
    # The bundle version expresses the host revision, never the engine release.
    assert BUILD.HOST_REVISION == 1
    assert info["CFBundleVersion"] == "1"
    assert info["CFBundleShortVersionString"] == "1"
    assert info[BUILD.HOST_PROTOCOL_KEY] == 1
    assert info["LSMinimumSystemVersion"] == "13.0"

    from ciao import __version__

    # The engine's own version must not leak into the host identity fields.
    for value in (info["CFBundleVersion"], info["CFBundleShortVersionString"]):
        assert value != __version__
        assert __version__ not in str(value)


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
        str(p.relative_to(app)) for p in app.rglob("*") if p.is_file()
    )
    assert shipped == [
        "Contents/Info.plist",
        "Contents/MacOS/CiaobotServerHost",
        "Contents/Resources/CiaobotServer.icns",
    ]
    assert not any(name.endswith(".py") for name in shipped)


# --- pure builder API -----------------------------------------------------


def test_compile_commands_are_exact_and_both_arch() -> None:
    source = Path("/src/ServerHost.swift")
    arm = Path("/stage/CiaobotServerHost.arm64")
    intel = Path("/stage/CiaobotServerHost.x86_64")
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
    assert metadata["host_revision"] == 1
    assert metadata["host_protocol"] == 1
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
    assert "Ciaobot Server.app/Contents/MacOS/CiaobotServerHost" in names
    # No thin staging files and no absolute paths leak into the archive.
    assert not any(n.endswith((".arm64", ".x86_64")) for n in names)
    # Only the app ships: the staging and verify scratch dirs are gone.
    assert sorted(p.name for p in (tmp_path / "host").iterdir()) == sorted(
        [BUILD.APP_NAME, BUILD.ARCHIVE_NAME]
    )


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
        ["lipo", "-archs", str(app / "Contents" / "MacOS" / "CiaobotServerHost")],
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
    assert info[BUILD.HOST_PROTOCOL_KEY] == 1

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
    binary = workdir / "CiaobotServerHost"
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
        ("abort", 0, 128 + signal.SIGABRT),
    ],
)
def test_host_preserves_nonzero_and_crash_after_stop(
    host_binary: Path,
    child_helper: Path,
    tmp_path: Path,
    action: str,
    exit_code: int,
    expected: int,
) -> None:
    # A stop is a clean 0 only for exit 0 or the forwarded SIGTERM. A non-zero
    # exit or a crash after the stop is preserved, so launchd and logs see a
    # failed shutdown instead of a false success.
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
        assert time.time() - started < 40
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
                host.send_signal(signal.SIGTERM)
                # A fork within a few milliseconds of the stop was already under
                # way (spawning takes about a millisecond). A launch that ignored
                # the stop waits for the event loop, tens of milliseconds later.
                racing = queue.control(None, 8, 0.005)
                forked_racing = _host_forked(racing)
                events: list[Any] = list(racing)
                while not any(event.fflags & select.KQ_NOTE_EXIT for event in events):
                    batch = queue.control(None, 8, 45)
                    assert batch, "host did not exit after a startup stop"
                    events.extend(batch)
                code = host.wait(timeout=5)
            finally:
                queue.close()
                _terminate_exact(host)
            if code != 0 or forked_before or forked_racing:
                # Killed before the handlers existed, or launched before the stop.
                continue
            handled += 1
            assert not _host_forked(events), f"a child was launched after a stop at {delay}s"
    finally:
        time.sleep(0.2)
        if pids.exists():
            for line in pids.read_text().split():
                pid = int(line)
                if _pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
    if handled == 0:
        pytest.skip("no stop landed inside the startup window on this machine")


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


def test_host_protocol_revision_matches_builder() -> None:
    # The Swift constant and the builder's plist key must move together.
    source = HOST_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"^let HOST_PROTOCOL_REVISION = (\d+)$", source, re.MULTILINE)
    assert match is not None
    assert int(match.group(1)) == BUILD.HOST_PROTOCOL_REVISION


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
