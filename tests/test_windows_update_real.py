"""ciao.windows_update against the real Task Scheduler and the real uv (#857).

Windows-only, and nothing here touches the machine's own Ciaobot: the tasks
are registered under a unique throwaway name and deleted, and uv installs a
throwaway wheel into a tool dir and bin dir under ``tmp_path``. What these
prove that the fakes in ``test_windows_update.py`` cannot:

* Task Scheduler accepts the updater and recovery task XML as rendered, and
  ``task_command`` reads the registered command back through schtasks' OEM
  output;
* ``uv tool install --offline --force --link-mode copy --constraint`` rebuilds
  an env from the cache a normal install warmed, with relative paths from a
  working directory that may contain a space, and the bin-dir launcher it
  leaves starts;
* after a rollback renames the old env back, copying its own
  ``Scripts\\<name>.exe`` over the bin dir's makes the launcher start the old
  env again (``after_env_restored``).

The offline reinstall time is logged, as the maintainer asked (#857, decision 2).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

import pytest

from ciao import engine_update
from ciao import windows_service as ws
from ciao.engine_update import Operation
from ciao.windows_update import WindowsUpdateHost

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Task Scheduler and Windows uv launchers")

logger = logging.getLogger(__name__)


def _unique(name: str) -> str:
    return f"\\Ciaobot\\Test-{os.getpid()}-{uuid.uuid4().hex[:8]}-{name}"


@pytest.mark.parametrize("kind", ["updater", "recover"])
def test_task_scheduler_accepts_the_update_task_xml(kind: str, tmp_path: Path) -> None:
    name = _unique(kind)
    pythonw = ws.windowless_python(sys.executable)
    arguments = ["-I", "-m", "ciao.engine_update", f"run-{'apply' if kind == 'updater' else 'recover'}", "--operation", "op"]
    if kind == "updater":
        xml = ws.render_oneshot_task_xml(python=pythonw, arguments=arguments, workdir=str(tmp_path), user=ws.current_user())
        xml = xml.replace(ws.UPDATER_TASK_NAME, name)
    else:
        xml = ws.render_recover_task_xml(
            python=pythonw, arguments=arguments, workdir=str(tmp_path), user=ws.current_user(),
            start=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        xml = xml.replace(ws.RECOVER_TASK_NAME, name)
    definition = tmp_path / "task.xml"
    definition.write_bytes(xml.encode("utf-16"))
    try:
        ws.register_task(definition, name)
        assert ws.task_exists(name)
        assert ws.task_command(name) == pythonw
    finally:
        ws.unregister_task(name)
    assert not ws.task_exists(name)


def _wheel(directory: Path, version: str) -> Path:
    """A dependency-free ``ciaobot`` wheel whose ``ciao`` script prints its version."""
    dist_info = f"ciaobot-{version}.dist-info"
    path = directory / f"ciaobot-{version}-py3-none-any.whl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ciaobot_probe/__init__.py", f"def main():\n    print({version!r})\n")
        archive.writestr(f"{dist_info}/METADATA", f"Metadata-Version: 2.1\nName: ciaobot\nVersion: {version}\n")
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{dist_info}/entry_points.txt", "[console_scripts]\nciao = ciaobot_probe:main\n")
        archive.writestr(f"{dist_info}/RECORD", "")
    return path


def _launcher_says(bin_dir: Path) -> str:
    completed = subprocess.run([str(bin_dir / "ciao.exe")], capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv on PATH")
def test_offline_copy_mode_reinstall_and_launcher_restore_with_real_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    # A space in the stage path, as in every "First Last" Windows profile: the
    # reinstall must not hand uv an absolute --constraint path.
    stage = tmp_path / "stage dir" / "0.0.2"
    tools = tmp_path / "tools"
    bin_dir = tmp_path / "bin"
    env = {**os.environ, "UV_TOOL_DIR": str(tools), "UV_TOOL_BIN_DIR": str(bin_dir)}
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    old_wheel = _wheel(tmp_path / "old", "0.0.1")
    new_wheel = _wheel(stage, "0.0.2")

    # What the installer leaves: a copy-mode install of the old release.
    subprocess.run(
        [uv, "tool", "install", "--link-mode", "copy", "--python", python, str(old_wheel)],
        env=env, check=True, capture_output=True, text=True, timeout=300,
    )
    live = tools / "ciaobot"
    assert _launcher_says(bin_dir) == "0.0.1"
    # What staging leaves: the new wheel in uv's cache, and its resolved pins.
    staged_tools = stage / "tool"
    subprocess.run(
        [uv, "tool", "install", "--python", python, str(new_wheel)],
        env={**os.environ, "UV_TOOL_DIR": str(staged_tools), "UV_TOOL_BIN_DIR": str(stage / "bin")},
        check=True, capture_output=True, text=True, timeout=300,
    )
    staged_python = staged_tools / "ciaobot" / "Scripts" / "python.exe"
    freeze = subprocess.run(
        [uv, "pip", "freeze", "--python", str(staged_python)],
        check=True, capture_output=True, text=True, timeout=120,
    ).stdout
    op = Operation(
        id="op", phase="swapping", from_version="0.0.1", to_version="0.0.2",
        started_at="", updated_at="", stage_dir=str(stage), wheel=str(new_wheel),
        wheel_sha256=engine_update._sha256(new_wheel)[0],
        env_python=str(staged_python), env_freeze=freeze or "ciaobot==0.0.2\n",
    )
    host = WindowsUpdateHost(state_dir=tmp_path / "state")
    monkeypatch.setattr(host, "_uv", lambda: uv)

    # The swap: the live env renamed aside, then rebuilt offline in its place.
    previous = stage / "previous-env"
    os.replace(live, previous)
    with caplog.at_level(logging.INFO, logger="ciao.windows_update"):
        started = time.monotonic()
        host.install_env(op, staged_python.parent.parent, live, bin_dir=bin_dir, wheel=new_wheel)
        elapsed = time.monotonic() - started
    logger.warning("offline copy-mode reinstall took %.2fs", elapsed)
    print(f"offline copy-mode reinstall took {elapsed:.2f}s")
    assert any("offline reinstall took" in record.message for record in caplog.records)
    assert _launcher_says(bin_dir) == "0.0.2"
    # Copy mode: the rebuilt env's files are its own, not links into uv's cache.
    assert all(path.stat().st_nlink == 1 for path in live.rglob("*.pyc") if path.is_file())
    # The staged env is untouched: it is the updater's interpreter.
    assert staged_python.is_file()

    # The rollback: the new env out of the way, the old one renamed back, and
    # the old env's launchers copied over the bin dir's.
    discarded = tmp_path / "discarded"
    os.replace(live, discarded)
    os.replace(previous, live)
    host.after_env_restored(live, bin_dir=bin_dir, wheel=new_wheel)
    assert _launcher_says(bin_dir) == "0.0.1"
