"""Checks for the production Ciaobot Server native host and its builder (#1009).

The pure checks import the builder and the real ``ciao`` constants and run on
every platform. The macOS-only checks compile the actual ``ServerHost.swift``
once per module and drive it with a scratch fake child that records its argv,
cwd, environment and parent. Nothing here launches the ``.app`` through
LaunchServices, boots the engine, installs a bundle, or touches permissions or
TCC: the ``request-accessibility`` branch is deliberately never executed.
"""

from __future__ import annotations

import hashlib
import importlib.util
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


def _write_fake_child(directory: Path, script: str, name: str = "fake_child.sh") -> Path:
    path = directory / name
    path.write_text(script)
    path.chmod(0o755)
    return path


def _wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


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
def test_host_fixed_supervisor_argv_and_lifetime(host_binary: Path, tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    child = _write_fake_child(
        tmp_path,
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$@" > "{record}.args"\n'
        f'echo "$PWD" > "{record}.cwd"\n'
        f'echo "$PPID" > "{record}.ppid"\n'
        f'env > "{record}.env"\n'
        "echo child-stdout\n"
        "echo child-stderr 1>&2\n"
        "exit 0 \n",
    )

    env = dict(os.environ)
    env["CIAO_SERVER_HOST_TEST_MARKER"] = "inherited-value"
    host = subprocess.Popen(
        [str(host_binary), "serve", "--python", str(child)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert _wait_for(lambda: Path(f"{record}.args").exists())
    stdout, stderr = host.communicate(timeout=30)

    # The child's io is inherited, not captured by the host.
    assert stdout == "child-stdout\n"
    assert stderr == "child-stderr\n"
    # The host stays alive until the child exits and mirrors a clean status.
    assert host.returncode == 0

    assert Path(f"{record}.args").read_text().splitlines() == ["-I", "-m", "ciao.cli", "supervise"]
    assert Path(f"{record}.cwd").read_text().strip() == str(tmp_path)
    child_ppid = int(Path(f"{record}.ppid").read_text().strip())
    # Process re-parenting: the recorded ppid is the host that spawned it.
    assert child_ppid == host.pid

    child_env = dict(
        line.split("=", 1) for line in Path(f"{record}.env").read_text().splitlines() if "=" in line
    )
    # Environment is inherited; no custom DYLD/PYTHONPATH knob is injected.
    assert child_env.get("CIAO_SERVER_HOST_TEST_MARKER") == "inherited-value"
    for key in ("DYLD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME"):
        assert child_env.get(key) == env.get(key)


@requires_macos_tools
def test_host_mirrors_child_exit_and_signal(host_binary: Path, tmp_path: Path) -> None:
    exiting = _write_fake_child(tmp_path, "#!/bin/sh\nexit 7\n", name="exit7.sh")
    result = subprocess.run(
        [str(host_binary), "serve", "--python", str(exiting)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 7

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
def test_host_forwards_stop_and_reaps(host_binary: Path, tmp_path: Path) -> None:
    marker = tmp_path / "got-term"
    child = _write_fake_child(
        tmp_path,
        "#!/bin/sh\n"
        "echo $$ > \"" + str(tmp_path / "child.pid") + "\"\n"
        f"trap 'echo term > \"{marker}\"; exit 0' TERM\n"
        "while :; do sleep 0.2; done\n",
    )
    pid_file = tmp_path / "child.pid"
    host = subprocess.Popen([str(host_binary), "serve", "--python", str(child)])
    try:
        assert _wait_from_file(pid_file)
        host.send_signal(signal.SIGTERM)
        assert host.wait(timeout=30) == 0
        assert marker.exists()
        child_pid = int(pid_file.read_text().strip())
        # The child was reaped: its pid is gone.
        assert _wait_for(lambda: not _pid_alive(child_pid))
    finally:
        _terminate_exact(host)
        _kill_exact_from_file(pid_file)


@requires_macos_tools
def test_host_stop_escalates_stalled_child(host_binary: Path, tmp_path: Path) -> None:
    # The stalled child keeps a descendant in its own process group, the way the
    # supervisor keeps the engine; the escalation must not orphan it.
    descendant_file = tmp_path / "descendant.pid"
    child = _write_fake_child(
        tmp_path,
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "sleep 600 &\n"
        "echo $! > \"" + str(descendant_file) + "\"\n"
        "echo $$ > \"" + str(tmp_path / "child.pid") + "\"\n"
        "while :; do sleep 0.5; done\n",
    )
    pid_file = tmp_path / "child.pid"
    host = subprocess.Popen([str(host_binary), "serve", "--python", str(child)])
    child_pid: int | None = None
    try:
        assert _wait_from_file(pid_file)
        child_pid = int(pid_file.read_text().strip())
        host.send_signal(signal.SIGTERM)
        started = time.time()
        # A child that ignores TERM is SIGKILLed after the 35 s stop grace.
        assert host.wait(timeout=45) == 0
        assert time.time() - started < 40
        assert _wait_for(lambda: not _pid_alive(child_pid), timeout=5)
        descendant_pid = int(descendant_file.read_text().strip())
        assert _wait_for(lambda: not _pid_alive(descendant_pid), timeout=5)
    finally:
        _terminate_exact(host)
        _kill_exact_from_file(pid_file)
        _kill_exact_from_file(descendant_file)


@requires_macos_tools
def test_host_stop_during_startup_launches_nothing(host_binary: Path, tmp_path: Path) -> None:
    # A stop that lands after the handlers are installed but while AppKit is
    # still starting must win: no child is launched. Launches are observed as
    # NOTE_FORK on the host pid, so a child killed before it could record
    # itself still counts. The stop is swept across the startup window because
    # its timing cannot be pinned from outside; samples whose fork may have
    # raced the stop itself are discarded.
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
    # at Process.run, which the host reports as a diagnostic and exit 1.
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


def test_host_protocol_revision_matches_builder() -> None:
    # The Swift constant and the builder's plist key must move together.
    source = HOST_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"^let HOST_PROTOCOL_REVISION = (\d+)$", source, re.MULTILINE)
    assert match is not None
    assert int(match.group(1)) == BUILD.HOST_PROTOCOL_REVISION


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
