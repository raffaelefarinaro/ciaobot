"""The native coordinator's production wiring (#2340, #2404).

These tests drive the real CLI, the scrubbed child environment, the private
copies of uv and the wheel, the engine install and receipt, the wizard argv and
the production dependency closures. Every external effect is a fake (a shell
stub standing in for uv, a recorder in place of a real step) or an isolated tmp
path. Nothing reaches the real launchd, the real home directory, the network or
a real install. ``pinned_home`` pins the account home to the isolated ``HOME``
the conftest sets, so the production home check passes under test.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import install_receipt, native_handoff, native_lock, native_readiness
from ciao import native_installer as native
from ciao import server_host, server_host_install
from ciao.install_discovery import NativeInstallState
from ciao.native_installer import NativeCoordinatorError
from ciao.native_lock import NativeLockError, NativeRunLock
from ciao.native_readiness import NativeReadinessProof
from tests.native_artifacts import make_artifacts, rehash
from tests.test_native_installer import (
    RUN_ID,
    _available,
    _coordinator_deps,
    _coordinator_request,
    _drain,
    _fresh_host_value,
    _handoff,
    _lock_path,
    _public_event_pipe,
    _record_bytes,
    _record_path,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native coordinator is macOS-only; its POSIX helpers are not on Windows",
)

ROOT = Path(__file__).resolve().parents[1]

#: Host variables a child must never see: each one redirects tools, indexes,
#: interpreters, loader paths or the TLS trust store.
DROPPED_HOST_ENV = {
    "UV_TOOL_DIR": "/evil/tools",
    "XDG_DATA_HOME": "/evil/xdg",
    "UV_INDEX_URL": "https://evil.example/simple",
    "PYTHONPATH": "/evil/py",
    "DYLD_INSERT_LIBRARIES": "/evil/lib.dylib",
    "PIP_INDEX_URL": "https://evil.example/pip",
    "HTTPS_PROXY": "http://evil.example:3128",
    "SSL_CERT_FILE": "/evil/ca.pem",
    "VIRTUAL_ENV": "/evil/venv",
}


@pytest.fixture
def pinned_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """The isolated HOME the conftest set, used as the account home too."""
    home = Path(os.environ["HOME"])
    monkeypatch.setattr(native, "_passwd_home", lambda: home)
    return home


def _tool_dirs_under(home: Path) -> tuple[Path, Path]:
    return home / ".local" / "share" / "uv" / "tools", home / ".local" / "bin"


def _request(events_fd: int = 3) -> native.NativeInstallRequest:
    return native.NativeInstallRequest(
        run_id=RUN_ID, events_fd=events_fd, first_seq=1, prior_events=0
    )


def _outcome(terminal: str | None) -> native.NativeInstallOutcome:
    return native.NativeInstallOutcome(
        run_id=RUN_ID,
        terminal=terminal,
        failure_code=None if terminal == "service_ready" else "internal_error",
        host_step_attempted=False,
        host_step_completed=False,
        activation_attempted=False,
        activation_completed=False,
        selected_workspace=None,
        lock_retained=False,
    )


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def _events_of(raw: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in raw.splitlines()]


def _env_names(dump: Path) -> set[str]:
    return {line.split("=", 1)[0] for line in dump.read_text().splitlines()}


def _place(target: Path, source: Path) -> list[str]:
    """Shell lines that copy ``source`` to ``target``, executable."""
    return [
        f'mkdir -p "{target.parent}"',
        f'cp "{source}" "{target}"',
        f'chmod 755 "{target}"',
    ]


def _link(link: Path, target: Path) -> list[str]:
    """Shell lines that make ``link`` a symlink to ``target``, as uv does."""
    return [f'mkdir -p "{link.parent}"', f'ln -sf "{target}" "{link}"']


def _fake_uv(
    root: Path,
    home: Path,
    *,
    rc: int = 0,
    shim: str | None = "link",
    python: str | None = "copy",
    python_version: str | None = None,
    probe: tuple[Path, int] | None = None,
    console: Path | None = None,
) -> Path:
    """A uv stub for ``tool install`` that records its argv, env and cwd.

    ``shim`` is how the ``ciao`` shim lands: ``"link"`` (what real uv does: a
    symlink in ``bin`` to a real file in ``tools/ciaobot/bin``), ``"file"`` (a
    regular file in ``bin``), ``"outside"`` (a link to a file outside the tool
    environment) or ``None``. ``python`` is ``"copy"`` (a regular interpreter in
    the tool environment), ``"managed"`` (a link into uv's managed Python root),
    ``"elsewhere"`` (a link to a file outside both) or ``None``. The stub logs its
    argv to ``root/uv.log`` and its environment to ``root/uv.env``, and the
    interpreter stub records its own environment to ``root/stub.env``. With
    ``probe``, both record whether the given descriptor is open.
    """
    tools, bin_dir = _tool_dirs_under(home)
    stub = root / "stub"
    probe_line = ""
    if probe is not None:
        probe_path, fd = probe
        probe_line = (
            f'[ -e "/dev/fd/{fd}" ] && echo open >> "{probe_path}" '
            f'|| echo closed >> "{probe_path}"\n'
        )
    _script(
        stub,
        probe_line
        + f'env > "{root / "stub.env"}"\n'
        + f'printf "%s" "{python_version or _version()}"\n',
    )
    lines = [
        probe_line.rstrip("\n") if probe is not None else "",
        f'for a in "$@"; do printf "%s\\n" "$a"; done >> "{root / "uv.log"}"',
        f'env > "{root / "uv.env"}"',
        f'pwd -P > "{root / "uv.cwd"}"',
        f"[ {rc} -eq 0 ] || exit {rc}",
    ]
    if python == "copy":
        lines += _place(tools / "ciaobot" / "bin" / "python", stub)
    elif python == "wrapper":
        # A regular interpreter that answers the probe and runs its arguments
        # under the test's own Python, so the wizard can run a Python console.
        wrapper = root / "python-wrapper"
        wrapper.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            f'  -I) printf "%s" "{python_version or _version()}"; exit 0 ;;\n'
            "esac\n"
            f'exec "{sys.executable}" "$@"\n',
            encoding="utf-8",
        )
        os.chmod(wrapper, 0o755)
        lines += _place(tools / "ciaobot" / "bin" / "python", wrapper)
    elif python in {"managed", "elsewhere"}:
        if python == "managed":
            interpreter = home / ".local" / "share" / "uv" / "python" / "cpython-3.13-test" / "bin" / "python3.13"
        else:
            interpreter = root / "elsewhere" / "python3.13"
        lines += _place(interpreter, stub)
        lines += _link(tools / "ciaobot" / "bin" / "python", interpreter)
    if shim == "link":
        lines += _place(tools / "ciaobot" / "bin" / "ciao", console or stub)
        lines += _link(bin_dir / "ciao", tools / "ciaobot" / "bin" / "ciao")
    elif shim == "file":
        lines += _place(bin_dir / "ciao", stub)
    elif shim == "outside":
        outside = root / "outside" / "ciao"
        lines += _place(outside, stub)
        lines += _link(bin_dir / "ciao", outside)
    return _script(root / "uv", "\n".join(line for line in lines if line) + "\n")


def _version() -> str:
    import ciao

    return ciao.__version__


def _install_env() -> dict[str, str]:
    return native._scrubbed_env(keep_user_path=False)


# ── CLI contract ─────────────────────────────────────────────────────────────


def _cli_argv(artifacts: native.NativeInstallArtifacts, *, fd: str = "7") -> list[str]:
    return [
        "run",
        "--run-id",
        RUN_ID,
        "--events-fd",
        fd,
        "--first-seq",
        "4",
        "--prior-events",
        "3",
        "--version",
        artifacts.version,
        "--uv",
        str(artifacts.uv),
        "--uv-sha256",
        artifacts.uv_sha256,
        "--wheel",
        str(artifacts.wheel),
        "--wheel-sha256",
        artifacts.wheel_sha256,
        "--manifest",
        str(artifacts.manifest),
        "--signature",
        str(artifacts.signature),
        "--host-archive",
        str(artifacts.host_archive),
    ]


def test_main_parses_the_shell_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(request, artifacts, *, cancellation=None):  # type: ignore[no-untyped-def]
        captured["request"] = request
        captured["artifacts"] = artifacts
        return _outcome("service_ready")

    monkeypatch.setattr(native, "run_native_install", fake_run)
    artifacts = make_artifacts(tmp_path)
    code = native.main([*_cli_argv(artifacts), "--workspace", "/tmp/w"])
    assert code == 0
    assert captured["request"] == native.NativeInstallRequest(
        run_id=RUN_ID,
        events_fd=7,
        first_seq=4,
        prior_events=3,
        workspace=Path("/tmp/w"),
    )
    assert captured["artifacts"] == artifacts


@pytest.mark.parametrize(
    "case", ["no-sub", "missing-uv", "bad-fd", "bad-run-id", "bad-digest"]
)
def test_main_rejects_bad_handover_arguments_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    artifacts = make_artifacts(tmp_path)
    with _public_event_pipe() as (read_fd, write_fd):
        argv = _cli_argv(artifacts, fd=str(write_fd))
        if case == "no-sub":
            argv = argv[1:]
            with pytest.raises(SystemExit) as caught:
                native.main(argv)
            assert caught.value.code == 2
        elif case == "missing-uv":
            index = argv.index("--uv")
            del argv[index : index + 2]
            with pytest.raises(SystemExit) as caught:
                native.main(argv)
            assert caught.value.code == 2
        elif case == "bad-fd":
            argv = _cli_argv(artifacts, fd=str(native.MAX_EVENT_FD + 1))
            assert native.main(argv) == 2
        elif case == "bad-run-id":
            argv[argv.index("--run-id") + 1] = "NOT-A-UUID"
            assert native.main(argv) == 2
        else:
            argv[argv.index("--uv-sha256") + 1] = "ABC"
            assert native.main(argv) == 2
        assert _available(read_fd) == b""
    assert capsys.readouterr().err == "invalid handover arguments\n"


def test_main_rejects_abbreviations_and_echoes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts = make_artifacts(tmp_path)
    argv = _cli_argv(artifacts)
    argv[argv.index("--prior-events")] = "--prior-e"
    with pytest.raises(SystemExit) as caught:
        native.main(argv)
    assert caught.value.code == 2
    assert capsys.readouterr().err == "invalid handover arguments\n"


def test_main_never_echoes_a_rejected_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _cli_argv(make_artifacts(tmp_path))
    argv[argv.index("--events-fd") + 1] = "SECRET-VALUE-1234"
    with pytest.raises(SystemExit) as caught:
        native.main(argv)
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert err == "invalid handover arguments\n"
    assert "SECRET" not in err


def test_main_signal_handlers_only_cancel_and_are_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    original = signal.getsignal(signal.SIGTERM)
    seen: dict[str, bool] = {}

    def fake_run(request, artifacts, *, cancellation=None):  # type: ignore[no-untyped-def]
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        assert handler is not original
        handler(signal.SIGTERM, None)
        seen["cancelled"] = cancellation.cancelled
        return _outcome("failed")

    monkeypatch.setattr(native, "run_native_install", fake_run)
    code = native.main(_cli_argv(make_artifacts(tmp_path)))
    assert code == 1
    assert seen == {"cancelled": True}
    assert signal.getsignal(signal.SIGTERM) is original


# ── artifacts: digests, copies and the private run directory ────────────────


def _must_not_classify(workspace: object) -> NativeInstallState:
    raise AssertionError("classification ran before the artifacts were validated")


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("relative", "invalid_arguments"),
        ("missing", "invalid_arguments"),
        ("symlink", "invalid_arguments"),
        ("not-exec", "invalid_arguments"),
        ("bad-version", "invalid_arguments"),
        ("other-version", "verification_failed"),
    ],
    ids=["relative", "missing", "symlink", "not-exec", "bad-version", "other-version"],
)
def test_invalid_artifacts_fail_with_a_fixed_event_before_any_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, code: str
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    artifacts = make_artifacts(tmp_path)
    if case == "relative":
        artifacts = dataclasses.replace(artifacts, wheel=Path("wheel.whl"))
    elif case == "missing":
        artifacts = dataclasses.replace(artifacts, manifest=tmp_path / "nope.json")
    elif case == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(artifacts.manifest)
        artifacts = dataclasses.replace(artifacts, manifest=link)
    elif case == "not-exec":
        os.chmod(artifacts.uv, 0o644)
    elif case == "bad-version":
        artifacts = dataclasses.replace(artifacts, version="bad version!")
    else:
        artifacts = dataclasses.replace(artifacts, version="0.0.0")
    production = native._production_deps(_request(), artifacts, native.NativeCancellation())
    deps = dataclasses.replace(production.deps, classify=_must_not_classify)
    try:
        with _public_event_pipe() as (read_fd, write_fd):
            outcome = native._run_native_install(
                dataclasses.replace(_request(), events_fd=write_fd), deps=deps
            )
            events = _events_of(_drain(read_fd))
    finally:
        production.close()
    assert outcome.failure_code == code
    assert [(event["type"], event.get("code")) for event in events] == [
        ("failed", code)
    ]


def test_a_wrong_digest_is_verification_failed(tmp_path: Path) -> None:
    artifacts = dataclasses.replace(make_artifacts(tmp_path), uv_sha256="0" * 64)
    production = native._production_deps(_request(), artifacts, native.NativeCancellation())
    try:
        with pytest.raises(NativeCoordinatorError) as caught:
            production.deps.validate_inputs()
        assert caught.value.failure_code == "verification_failed"
    finally:
        production.close()


def test_a_symlinked_artifact_is_refused(tmp_path: Path) -> None:
    artifacts = make_artifacts(tmp_path)
    link = tmp_path / "uv-link"
    link.symlink_to(artifacts.uv)
    production = native._production_deps(
        _request(), dataclasses.replace(artifacts, uv=link), native.NativeCancellation()
    )
    try:
        with pytest.raises(NativeCoordinatorError) as caught:
            production.deps.validate_inputs()
        assert caught.value.failure_code == "invalid_arguments"
    finally:
        production.close()


def test_a_group_writable_artifact_is_refused(tmp_path: Path) -> None:
    artifacts = make_artifacts(tmp_path)
    os.chmod(artifacts.wheel, 0o664)
    production = native._production_deps(_request(), artifacts, native.NativeCancellation())
    try:
        with pytest.raises(NativeCoordinatorError) as caught:
            production.deps.validate_inputs()
        assert caught.value.failure_code == "invalid_arguments"
    finally:
        production.close()


def test_a_foreign_owner_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fstat = os.fstat
    foreign = os.getuid() + 1

    def foreign_owner(fd: int) -> SimpleNamespace:
        real = real_fstat(fd)
        return SimpleNamespace(st_mode=real.st_mode, st_uid=foreign, st_size=real.st_size)

    monkeypatch.setattr(native, "_fstat", foreign_owner)
    production = native._production_deps(
        _request(), make_artifacts(tmp_path), native.NativeCancellation()
    )
    try:
        with pytest.raises(NativeCoordinatorError) as caught:
            production.deps.validate_inputs()
        assert caught.value.failure_code == "invalid_arguments"
    finally:
        production.close()


def test_later_steps_receive_the_private_copies(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = make_artifacts(tmp_path)
    tools, bin_dir = _tool_dirs_under(pinned_home)
    seen: dict[str, object] = {}

    def record_tool_dirs(uv: Path, **kwargs: object) -> tuple[Path, Path]:
        seen["tool_dirs_uv"] = uv
        return tools, bin_dir

    def record_engine(art, tool_dir, bin_arg, remaining, **kwargs):  # type: ignore[no-untyped-def]
        seen["engine"] = art
        return None

    monkeypatch.setattr(native, "_tool_dirs", record_tool_dirs)
    monkeypatch.setattr(native, "_install_engine", record_engine)
    monkeypatch.setattr(
        native, "classify_native_install", lambda **kwargs: NativeInstallState.fresh
    )
    production = native._production_deps(_request(), artifacts, native.NativeCancellation())
    try:
        deps = production.deps
        deps.validate_inputs()
        deps.classify(None)
        deps.install_engine(30.0)
        uv_copy = seen["tool_dirs_uv"]
        engine = seen["engine"]
        assert isinstance(uv_copy, Path) and isinstance(engine, native.NativeInstallArtifacts)
        root = uv_copy.parent
        assert uv_copy != artifacts.uv
        assert engine.uv == uv_copy
        assert engine.wheel != artifacts.wheel
        assert engine.wheel.parent.parent == root
        assert uv_copy.read_bytes() == artifacts.uv.read_bytes()
        assert engine.wheel.read_bytes() == artifacts.wheel.read_bytes()
        assert os.stat(uv_copy).st_mode & 0o777 == 0o700
        assert os.stat(engine.wheel).st_mode & 0o777 == 0o600
    finally:
        production.close()
    assert not root.exists()


# ── the scrubbed environment ─────────────────────────────────────────────────


def test_uv_and_probe_env_drops_host_variables_and_pins_home(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in DROPPED_HOST_ENV.items():
        monkeypatch.setenv(name, value)
    cwd = tmp_path / "private-cwd"
    cwd.mkdir()
    artifacts = rehash(
        dataclasses.replace(
            make_artifacts(tmp_path), uv=_fake_uv(tmp_path, pinned_home)
        )
    )
    tool_dir, bin_dir = _tool_dirs_under(pinned_home)
    native._install_engine(
        artifacts,
        tool_dir,
        bin_dir,
        30.0,
        cancellation=native.NativeCancellation(),
        env=_install_env(),
        cwd=cwd,
    )
    for dump in (tmp_path / "uv.env", tmp_path / "stub.env"):
        names = _env_names(dump)
        assert names.isdisjoint(DROPPED_HOST_ENV)
        assert not {n for n in names if n.startswith(("UV_", "XDG_", "PYTHON", "DYLD_", "PIP_"))}
        assert f"PATH={native.FIXED_PATH}" in dump.read_text().splitlines()
        assert f"HOME={pinned_home}" in dump.read_text().splitlines()
    assert Path((tmp_path / "uv.cwd").read_text().strip()) == cwd.resolve()
    argv = (tmp_path / "uv.log").read_text().splitlines()
    assert "--no-config" in argv
    assert argv[argv.index("--default-index") + 1] == native.DEFAULT_INDEX_URL


def test_tool_dir_calls_use_the_scrubbed_env(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in DROPPED_HOST_ENV.items():
        monkeypatch.setenv(name, value)
    tools, bin_dir = _tool_dirs_under(pinned_home)
    uv = _script(
        tmp_path / "uv",
        f'env > "{tmp_path / "dir.env"}"\n'
        f'printf "%s\\n" "$*" >> "{tmp_path / "dir.log"}"\n'
        f'case "$*" in *--bin*) printf "%s\\n" "{bin_dir}";; '
        f'*) printf "%s\\n" "{tools}";; esac\n',
    )
    assert native._tool_dirs(
        uv,
        env=_install_env(),
        cwd=tmp_path,
        cancellation=native.NativeCancellation(),
    ) == (tools, bin_dir)
    assert _env_names(tmp_path / "dir.env").isdisjoint(DROPPED_HOST_ENV)
    calls = (tmp_path / "dir.log").read_text().splitlines()
    assert calls == ["tool dir --no-config", "tool dir --no-config --bin"]


def test_a_home_mismatch_refuses_before_any_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = tmp_path / "account"
    account.mkdir()
    monkeypatch.setattr(native, "_passwd_home", lambda: account)
    marker = tmp_path / "ran"
    uv = _script(tmp_path / "uv", f'touch "{marker}"\n')
    production = native._production_deps(
        _request(), rehash(dataclasses.replace(make_artifacts(tmp_path), uv=uv)),
        native.NativeCancellation(),
    )
    try:
        production.deps.validate_inputs()
        with pytest.raises(NativeCoordinatorError) as caught:
            production.deps.classify(None)
        assert caught.value.failure_code == "unknown_state"
    finally:
        production.close()
    assert not marker.exists()


@pytest.mark.parametrize(
    "case",
    ["rc", "relative", "two-lines", "dotdot", "cr", "elsewhere", "hang"],
)
def test_unusable_tool_dirs_are_unknown_state(
    tmp_path: Path,
    pinned_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    bodies = {
        "rc": "exit 3\n",
        "relative": 'printf "%s\\n" "relative/tools"\n',
        "two-lines": 'printf "%s\\n%s\\n" "$HOME/.local/share/uv/tools" "$HOME/x"\n',
        "dotdot": 'printf "%s\\n" "$HOME/.local/../share/uv/tools"\n',
        "cr": 'printf "%s\\r\\n" "$HOME/.local/share/uv/tools"\n',
        "elsewhere": 'printf "%s\\n" "$HOME/elsewhere"\n',
        "hang": "exec sleep 60\n",
    }
    uv = _script(tmp_path / "uv", bodies[case])
    if case == "hang":
        monkeypatch.setattr(native, "TOOL_DIR_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(NativeCoordinatorError) as caught:
        native._tool_dirs(
            uv,
            env=_install_env(),
            cwd=tmp_path,
            cancellation=native.NativeCancellation(),
        )
    assert caught.value.failure_code == "unknown_state"
    assert caught.value.reason == "tool_dir_unknown"


def test_the_classifier_receives_the_scrubbed_environment(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_TOOL_DIR", "/evil/tools")
    tools, bin_dir = _tool_dirs_under(pinned_home)
    seen: dict[str, str] = {}

    def fake_classify(**kwargs: object) -> NativeInstallState:
        seen.update(kwargs["environ"])  # type: ignore[index, arg-type]
        return NativeInstallState.fresh

    monkeypatch.setattr(native, "_tool_dirs", lambda *a, **k: (tools, bin_dir))
    monkeypatch.setattr(native, "classify_native_install", fake_classify)
    production = native._production_deps(
        _request(), make_artifacts(tmp_path), native.NativeCancellation()
    )
    try:
        production.deps.validate_inputs()
        production.deps.classify(None)
    finally:
        production.close()
    assert "UV_TOOL_DIR" not in seen
    assert seen["PATH"] == native.FIXED_PATH


def test_the_wizard_child_keeps_the_user_path_and_drops_host_variables(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in DROPPED_HOST_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/opt/user/bin:/usr/bin:/bin")
    production = native._production_deps(
        _request(), make_artifacts(tmp_path), native.NativeCancellation()
    )
    try:
        production.deps.validate_inputs()
        env, cwd = production.deps.wizard_environment()
        dump = tmp_path / "wizard.json"
        script = (
            "import json, os\n"
            f"open({str(dump)!r}, 'w').write(json.dumps("
            "{'env': dict(os.environ), 'cwd': os.getcwd()}))\n"
        )
        read_fd, write_fd = native._bind_pipe()
        public_fd = os.open(os.devnull, os.O_RDONLY)
        try:
            child = native._spawn_owned_child(
                (sys.executable, "-c", script),
                notification_fd=write_fd,
                public_fd=public_fd,
                policy=native._CoordinatorPolicy(),
                env=dict(env),
                cwd=cwd,
            )
            native._close_fd(write_fd)
            assert child.wait_for_exit(
                native._Deadline.after(20), native.NativeCancellation()
            ) == 0
        finally:
            native._close_fd(read_fd)
            native._close_fd(public_fd)
        seen = json.loads(dump.read_text())
    finally:
        production.close()
    assert seen["env"]["PATH"] == "/opt/user/bin:/usr/bin:/bin"
    assert seen["env"]["HOME"] == str(pinned_home)
    assert not set(DROPPED_HOST_ENV) & set(seen["env"])
    assert Path(seen["cwd"]).resolve() == cwd.resolve()


# ── cancellation during uv ───────────────────────────────────────────────────


def test_cancel_during_uv_kills_the_session_promptly(
    tmp_path: Path, pinned_home: Path
) -> None:
    tools, bin_dir = _tool_dirs_under(pinned_home)
    sentinel = tmp_path / "release"
    pidfile = tmp_path / "uv.pid"
    uv = _script(
        tmp_path / "uv",
        f'echo $$ > "{pidfile}"\nwhile [ ! -f "{sentinel}" ]; do sleep 0.05; done\n',
    )
    cancellation = native.NativeCancellation()
    timer = threading.Timer(0.3, cancellation.cancel)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(native._Cancelled):
            native._install_engine(
                dataclasses.replace(make_artifacts(tmp_path), uv=uv),
                tools,
                bin_dir,
                60.0,
                cancellation=cancellation,
                env=_install_env(),
                cwd=tmp_path,
            )
    finally:
        timer.cancel()
    assert time.monotonic() - started < 2.0
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_an_unreaped_engine_child_keeps_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A uv child that cannot be proven reaped keeps the run lock, as the wizard does."""
    import signal

    monkeypatch.setattr(sys, "platform", "darwin")
    pidfile = tmp_path / "uv.pid"
    uv = _script(tmp_path / "uv", f'echo $$ > "{pidfile}"\nexec sleep 60\n')
    artifacts = dataclasses.replace(make_artifacts(tmp_path), uv=uv)
    tools, bin_dir = _tool_dirs_under(tmp_path / "home")
    cancellation = native.NativeCancellation()

    def cannot_reap(self: object) -> None:
        raise native._ChildCleanupFailed

    monkeypatch.setattr(native._OwnedChild, "terminate_and_reap", cannot_reap)

    def engine(remaining: float) -> None:
        # A short budget forces the timeout path, which terminates and reaps.
        native._install_engine(
            artifacts,
            tools,
            bin_dir,
            0.3,
            cancellation=cancellation,
            env={"PATH": native.FIXED_PATH},
            cwd=tmp_path,
        )

    deps = _coordinator_deps(tmp_path, install_engine=engine)
    try:
        with _public_event_pipe() as (read_fd, write_fd):
            outcome = native._run_native_install(
                _coordinator_request(write_fd), deps=deps, cancellation=cancellation
            )
            events = _events_of(_drain(read_fd))
        state = tmp_path / "state"
        assert outcome.terminal == "failed"
        assert outcome.failure_code == "install_failed"
        assert outcome.lock_retained is True
        assert [(e["type"], e.get("phase"), e.get("code")) for e in events] == [
            ("phase", "install", None),
            ("failed", None, "install_failed"),
        ]
        assert all(e["type"] != "service_ready" for e in events)
        assert _lock_path(state).is_dir()
        assert _record_path(state).is_file()
    finally:
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)


# ── tool directories, engine install and receipt ────────────────────────────


def test_engine_install_runs_uv_without_force_and_writes_the_one_receipt(
    tmp_path: Path, pinned_home: Path
) -> None:
    artifacts = rehash(
        dataclasses.replace(make_artifacts(tmp_path), uv=_fake_uv(tmp_path, pinned_home))
    )
    tools, bin_dir = _tool_dirs_under(pinned_home)
    native._install_engine(
        artifacts,
        tools,
        bin_dir,
        30.0,
        cancellation=native.NativeCancellation(),
        env=_install_env(),
        cwd=tmp_path,
    )
    argv = (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines()
    assert argv == [
        "tool",
        "install",
        "--no-config",
        "--default-index",
        native.DEFAULT_INDEX_URL,
        "--python",
        "3.13",
        str(artifacts.wheel),
    ]
    assert "--force" not in argv
    receipt = install_receipt.read_receipt()
    assert receipt is not None
    assert receipt.version == artifacts.version
    assert receipt.executable == str(bin_dir / "ciao")
    assert receipt.python == str(tools / "ciaobot" / "bin" / "python")
    assert receipt.service_backend == "launchd"
    assert receipt.service_label == "com.ciao.server"
    assert receipt.previous_version == ""
    assert receipt.previous_executable == ""
    assert receipt.uv == ""


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rc": 1},
        {"shim": None},
        {"python": None},
        {"python_version": "0.0.1"},
    ],
    ids=["uv-rc", "no-shim", "no-python", "wrong-version"],
)
def test_engine_install_failures_are_install_failed_and_leave_no_receipt(
    tmp_path: Path, pinned_home: Path, kwargs: dict[str, object]
) -> None:
    artifacts = rehash(
        dataclasses.replace(
            make_artifacts(tmp_path),
            uv=_fake_uv(tmp_path, pinned_home, **kwargs),  # type: ignore[arg-type]
        )
    )
    tools, bin_dir = _tool_dirs_under(pinned_home)
    with pytest.raises(NativeCoordinatorError) as caught:
        native._install_engine(
            artifacts,
            tools,
            bin_dir,
            30.0,
            cancellation=native.NativeCancellation(),
            env=_install_env(),
            cwd=tmp_path,
        )
    assert caught.value.failure_code == "install_failed"
    assert not install_receipt.default_receipt_path().exists()


def test_engine_install_children_never_see_the_public_descriptor(
    tmp_path: Path, pinned_home: Path
) -> None:
    probe = tmp_path / "probe.log"
    tools, bin_dir = _tool_dirs_under(pinned_home)
    with _public_event_pipe() as (read_fd, write_fd):
        # Make the descriptor inheritable, so only close_fds=True protects it.
        os.set_inheritable(write_fd, True)
        artifacts = rehash(
            dataclasses.replace(
                make_artifacts(tmp_path),
                uv=_fake_uv(tmp_path, pinned_home, probe=(probe, write_fd)),
            )
        )
        native._install_engine(
            artifacts,
            tools,
            bin_dir,
            30.0,
            cancellation=native.NativeCancellation(),
            env=_install_env(),
            cwd=tmp_path,
        )
        assert _available(read_fd) == b""
    records = probe.read_text(encoding="utf-8").split()
    assert records  # the uv stub and the tool interpreter both ran
    assert "open" not in records


def test_the_seam_makes_the_event_descriptor_non_inheritable_before_any_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    seen: dict[str, bool] = {}

    def install_engine(remaining: float) -> None:
        seen["inheritable"] = os.get_inheritable(write_fd)

    def no_wizard(fd: int) -> tuple[str, ...]:
        raise RuntimeError("stop before the wizard child")

    with _public_event_pipe() as (read_fd, write_fd):
        os.set_inheritable(write_fd, True)
        deps = _coordinator_deps(
            tmp_path, install_engine=install_engine, wizard_command=no_wizard
        )
        native._run_native_install(_request(write_fd), deps=deps)
        _available(read_fd)
    assert seen == {"inheritable": False}


@pytest.mark.parametrize(
    ("workspace", "expected", "tail"),
    [
        (None, None, ()),
        (Path("/tmp/ws"), None, ("--installer-suggest-workspace=/tmp/ws",)),
        (None, Path("/tmp/ws"), ("--installer-native-workspace=/tmp/ws",)),
    ],
    ids=["plain", "suggest", "expected"],
)
def test_wizard_argv_is_the_hidden_native_bootstrap_command(
    workspace: Path | None, expected: Path | None, tail: tuple[str, ...]
) -> None:
    argv = native._wizard_argv(
        Path("/opt/tool/python"), Path("/opt/tool/ciao"), RUN_ID, 5, workspace, expected
    )
    assert argv == (
        "/opt/tool/python",
        "/opt/tool/ciao",
        "run",
        "--installer-bootstrap",
        "--installer-run-id",
        RUN_ID,
        "--installer-native-fd=5",
        *tail,
    )


# ── the shim and the probed interpreter ─────────────────────────────────────


def _installed(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[native._ProductionDeps, Path, Path]:
    tools, bin_dir = _tool_dirs_under(pinned_home)
    monkeypatch.setattr(native, "_tool_dirs", lambda *a, **k: (tools, bin_dir))
    uv = _fake_uv(tmp_path, pinned_home)
    artifacts = rehash(dataclasses.replace(make_artifacts(tmp_path), uv=uv))
    production = native._production_deps(_request(), artifacts, native.NativeCancellation())
    production.deps.validate_inputs()
    production.deps.install_engine(30.0)
    return production, tools, bin_dir


def test_a_swapped_shim_before_the_wizard_spawn_is_install_failed(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, tools, bin_dir = _installed(tmp_path, pinned_home, monkeypatch)
    try:
        assert production.deps.wizard_command(5)[0] == str(
            tools / "ciaobot" / "bin" / "python"
        )
        (bin_dir / "ciao").write_bytes(b"#!/bin/sh\necho swapped\n")
        with pytest.raises(NativeCoordinatorError) as caught:
            production.deps.wizard_command(5)
        assert caught.value.failure_code == "install_failed"
    finally:
        production.close()


@pytest.mark.parametrize("case", ["other", "probed"])
def test_the_handoff_engine_python_must_be_the_probed_interpreter(
    tmp_path: Path,
    pinned_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    production, tools, _bin = _installed(tmp_path, pinned_home, monkeypatch)
    probed = tools / "ciaobot" / "bin" / "python"
    other = tmp_path / "other-python"
    other.write_bytes(b"#!/bin/sh\n")
    os.chmod(other, 0o755)
    engine_python = probed if case == "probed" else other
    monkeypatch.setattr(
        native_handoff,
        "read_result",
        lambda *, expected_run_id: dataclasses.replace(
            _handoff(RUN_ID), engine_python=str(engine_python)
        ),
    )
    try:
        if case == "other":
            with pytest.raises(NativeCoordinatorError) as caught:
                production.deps.read_handoff(RUN_ID)
            assert caught.value.failure_code == "wizard_failed"
        else:
            assert production.deps.read_handoff(RUN_ID).engine_python == str(engine_python)
    finally:
        production.close()


def test_the_symlinked_shim_install_succeeds(
    tmp_path: Path, pinned_home: Path
) -> None:
    """Real uv links bin/ciao into the tool env; the install must accept that layout."""
    tools, bin_dir = _tool_dirs_under(pinned_home)
    artifacts = rehash(
        dataclasses.replace(make_artifacts(tmp_path), uv=_fake_uv(tmp_path, pinned_home))
    )
    installed = native._install_engine(
        artifacts,
        tools,
        bin_dir,
        30.0,
        cancellation=native.NativeCancellation(),
        env=_install_env(),
        cwd=tmp_path,
    )
    assert isinstance(installed.shim, native._ShimIdentity)
    assert installed.shim.link_target == str(tools / "ciaobot" / "bin" / "ciao")


def _installed_with_shim(
    tmp_path: Path,
    pinned_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    **uv_options: object,
) -> tuple[native._ProductionDeps, Path, Path]:
    tools, bin_dir = _tool_dirs_under(pinned_home)
    monkeypatch.setattr(native, "_tool_dirs", lambda *a, **k: (tools, bin_dir))
    uv = _fake_uv(tmp_path, pinned_home, **uv_options)  # type: ignore[arg-type]
    production = native._production_deps(
        _request(), rehash(dataclasses.replace(make_artifacts(tmp_path), uv=uv)),
        native.NativeCancellation(),
    )
    production.deps.validate_inputs()
    production.deps.install_engine(30.0)
    return production, tools, bin_dir


def test_a_retargeted_link_before_the_spawn_is_install_failed(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, tools, bin_dir = _installed_with_shim(
        tmp_path, pinned_home, monkeypatch
    )
    try:
        other = tools / "ciaobot" / "bin" / "other"
        other.write_bytes(b"#!/bin/sh\n")
        os.chmod(other, 0o755)
        (bin_dir / "ciao").unlink()
        (bin_dir / "ciao").symlink_to(other)
        with pytest.raises(NativeCoordinatorError) as caught:
            production.deps.wizard_command(5)
        assert caught.value.failure_code == "install_failed"
        assert caught.value.reason == "shim_changed"
    finally:
        production.close()


def test_a_replaced_target_before_the_spawn_is_install_failed(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, tools, _bin = _installed_with_shim(
        tmp_path, pinned_home, monkeypatch
    )
    try:
        target = tools / "ciaobot" / "bin" / "ciao"
        target.write_bytes(b"#!/bin/sh\necho replaced\n")
        with pytest.raises(NativeCoordinatorError) as caught:
            production.deps.wizard_command(5)
        assert caught.value.failure_code == "install_failed"
        assert caught.value.reason == "shim_changed"
    finally:
        production.close()


def test_a_regular_file_shim_is_install_failed(
    tmp_path: Path, pinned_home: Path
) -> None:
    tools, bin_dir = _tool_dirs_under(pinned_home)
    artifacts = rehash(
        dataclasses.replace(
            make_artifacts(tmp_path), uv=_fake_uv(tmp_path, pinned_home, shim="file")
        )
    )
    with pytest.raises(NativeCoordinatorError) as caught:
        native._install_engine(
            artifacts,
            tools,
            bin_dir,
            30.0,
            cancellation=native.NativeCancellation(),
            env=_install_env(),
            cwd=tmp_path,
        )
    assert caught.value.failure_code == "install_failed"
    assert caught.value.reason == "shim_invalid"


def test_a_link_outside_the_tool_dir_is_install_failed(
    tmp_path: Path, pinned_home: Path
) -> None:
    tools, bin_dir = _tool_dirs_under(pinned_home)
    artifacts = rehash(
        dataclasses.replace(
            make_artifacts(tmp_path), uv=_fake_uv(tmp_path, pinned_home, shim="outside")
        )
    )
    with pytest.raises(NativeCoordinatorError) as caught:
        native._install_engine(
            artifacts,
            tools,
            bin_dir,
            30.0,
            cancellation=native.NativeCancellation(),
            env=_install_env(),
            cwd=tmp_path,
        )
    assert caught.value.failure_code == "install_failed"
    assert caught.value.reason == "shim_invalid"


def test_a_managed_python_link_is_accepted(
    tmp_path: Path, pinned_home: Path
) -> None:
    """Real uv links tool python into uv's managed Python root; that layout is valid."""
    tools, bin_dir = _tool_dirs_under(pinned_home)
    artifacts = rehash(
        dataclasses.replace(
            make_artifacts(tmp_path),
            uv=_fake_uv(tmp_path, pinned_home, python="managed"),
        )
    )
    installed = native._install_engine(
        artifacts,
        tools,
        bin_dir,
        30.0,
        cancellation=native.NativeCancellation(),
        env=_install_env(),
        cwd=tmp_path,
    )
    assert installed.tool_python == tools / "ciaobot" / "bin" / "python"


def test_a_tool_python_outside_the_tool_and_managed_roots_is_install_failed(
    tmp_path: Path, pinned_home: Path
) -> None:
    tools, bin_dir = _tool_dirs_under(pinned_home)
    artifacts = rehash(
        dataclasses.replace(
            make_artifacts(tmp_path),
            uv=_fake_uv(tmp_path, pinned_home, python="elsewhere"),
        )
    )
    with pytest.raises(NativeCoordinatorError) as caught:
        native._install_engine(
            artifacts,
            tools,
            bin_dir,
            30.0,
            cancellation=native.NativeCancellation(),
            env=_install_env(),
            cwd=tmp_path,
        )
    assert caught.value.failure_code == "install_failed"
    assert caught.value.reason == "interpreter_outside"


def test_the_classifier_runs_in_the_private_cwd(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, bin_dir = _tool_dirs_under(pinned_home)
    seen: dict[str, Path] = {}

    def fake_classify(**kwargs: object) -> NativeInstallState:
        seen["cwd"] = kwargs["cwd"]  # type: ignore[assignment]
        return NativeInstallState.fresh

    monkeypatch.setattr(native, "_tool_dirs", lambda *a, **k: (tools, bin_dir))
    monkeypatch.setattr(native, "classify_native_install", fake_classify)
    production = native._production_deps(
        _request(), make_artifacts(tmp_path), native.NativeCancellation()
    )
    try:
        production.deps.validate_inputs()
        production.deps.classify(None)
        assert production.private.cwd is not None
        assert seen["cwd"] == production.private.cwd
        assert seen["cwd"] != Path.cwd()
    finally:
        production.close()


#: A console script that is a Python file, as uv writes one: it records a bind
#: report on the descriptor named by ``--installer-native-fd``, then holds until
#: the release file exists (bounded) and exits 0. Holding keeps the child alive
#: through the coordinator's listener-ownership check, which a child that exits
#: at once can lose on a slow runner.
def _bind_console(release: Path) -> str:
    return (
        "import os, sys, time\n"
        f"release = {str(release)!r}\n"
        "for argument in sys.argv:\n"
        "    if argument.startswith('--installer-native-fd='):\n"
        "        fd = int(argument.split('=', 1)[1])\n"
        "        os.write(fd, b'{\"schema\":1,\"port\":54321}\\n')\n"
        "        os.close(fd)\n"
        "        give_up = time.monotonic() + 30\n"
        "        while not os.path.exists(release) and time.monotonic() < give_up:\n"
        "            time.sleep(0.01)\n"
        "        sys.exit(0)\n"
        "sys.exit(3)\n"
    )


def _e2e(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[native._ProductionDeps, native._CoordinatorDeps, Path]:
    """Production install and wizard over a real spawn; only the readiness steps are faked."""
    monkeypatch.setattr(sys, "platform", "darwin")
    tools, bin_dir = _tool_dirs_under(pinned_home)
    monkeypatch.setattr(native, "_tool_dirs", lambda *a, **k: (tools, bin_dir))
    release = tmp_path / "wizard-release"
    console = tmp_path / "ciao-console.py"
    console.write_text(_bind_console(release), encoding="utf-8")
    real_owns_process = native._OwnedChild.owns_process

    def owns_then_release(self: native._OwnedChild, pid: int) -> bool:
        result = real_owns_process(self, pid)
        release.touch()
        return result

    monkeypatch.setattr(native._OwnedChild, "owns_process", owns_then_release)
    uv = _fake_uv(tmp_path, pinned_home, python="wrapper", console=console)
    artifacts = rehash(dataclasses.replace(make_artifacts(tmp_path), uv=uv))
    production = native._production_deps(_request(), artifacts, native.NativeCancellation())
    tool_python = tools / "ciaobot" / "bin" / "python"
    handoff = dataclasses.replace(
        _handoff(RUN_ID), engine_python=str(tool_python), port=54321
    )
    monkeypatch.setattr(
        native_handoff, "read_result", lambda *, expected_run_id: handoff
    )
    monkeypatch.setattr(
        native_handoff, "activate", lambda *, run_id, is_set_up=None: handoff
    )
    deps = dataclasses.replace(
        production.deps,
        prepare_state_dir=lambda: native_lock.prepare_native_state_dir(
            tmp_path / "state", trusted_root=tmp_path
        ),
        classify=lambda workspace: NativeInstallState.fresh,
        create_host=lambda lock, run_id, remaining: _fresh_host_value(tmp_path),
        recheck_host=lambda host: None,
        prepare_scratch=lambda: tmp_path / "scratch",
        wizard_listener=lambda port, child_pid: frozenset({child_pid}),
        prove_ready=lambda host, handoff_, remaining: NativeReadinessProof(
            ready=True, failure=None, port=54321, version=artifacts.version
        ),
    )
    return production, deps, tool_python


def test_the_symlinked_shim_path_reaches_service_ready(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a symlinked shim, a Python console script, a real spawn."""
    production, deps, _tool_python = _e2e(tmp_path, pinned_home, monkeypatch)
    try:
        with _public_event_pipe() as (read_fd, write_fd):
            outcome = native._run_native_install(
                _coordinator_request(write_fd), deps=deps
            )
            events = _events_of(_drain(read_fd))
    finally:
        production.close()
    assert outcome.terminal == "service_ready"
    assert events[-1]["type"] == "service_ready"


def test_the_wizard_spawns_the_verified_target_not_the_link(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link retargeted after the identity check must not change what runs."""
    production, deps, tool_python = _e2e(tmp_path, pinned_home, monkeypatch)
    tools, bin_dir = _tool_dirs_under(pinned_home)
    verified_target = (tools / "ciaobot" / "bin" / "ciao").resolve()
    marker = tmp_path / "evil-ran"
    evil = tmp_path / "evil-ciao"
    evil.write_text(
        f"open({str(marker)!r}, 'w').close()\nimport sys; sys.exit(0)\n",
        encoding="utf-8",
    )
    recorded: dict[str, tuple[str, ...]] = {}
    real_spawn = native._spawn_owned_child

    def spawn_after_retarget(argv: tuple[str, ...], **kwargs: object) -> object:
        recorded["argv"] = argv
        link = bin_dir / "ciao"
        link.unlink()
        link.symlink_to(evil)  # after the identity check, before the spawn
        return real_spawn(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(native, "_spawn_owned_child", spawn_after_retarget)
    try:
        with _public_event_pipe() as (read_fd, write_fd):
            outcome = native._run_native_install(
                _coordinator_request(write_fd), deps=deps
            )
            _drain(read_fd)
    finally:
        production.close()
    argv = recorded["argv"]
    assert argv[0] == str(tool_python)
    assert Path(argv[1]) == verified_target
    assert not marker.exists()
    assert outcome.terminal == "service_ready"


def test_the_interpreter_replaced_after_the_probe_is_install_failed(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production, tools, _bin = _installed_with_shim(tmp_path, pinned_home, monkeypatch)
    try:
        python = tools / "ciaobot" / "bin" / "python"
        replacement = python.with_name("python.new")
        replacement.write_bytes(python.read_bytes())
        os.chmod(replacement, 0o755)
        os.replace(replacement, python)  # a new inode at the same path
        with pytest.raises(NativeCoordinatorError) as caught:
            production.deps.wizard_command(5)
        assert caught.value.failure_code == "install_failed"
        assert caught.value.reason == "python_changed"
    finally:
        production.close()


# ── production dependency closures ───────────────────────────────────────────


def test_production_deps_delegate_to_the_real_steps(
    tmp_path: Path, pinned_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = make_artifacts(tmp_path)
    calls: dict[str, object] = {}
    sentinel = object()
    readiness_deps = object()
    host = _fresh_host_value(tmp_path)
    handoff = _handoff(RUN_ID)
    scratch = tmp_path / "scratch"
    tools, bin_dir = _tool_dirs_under(pinned_home)

    def record(name: str, value: object):  # type: ignore[no-untyped-def]
        def fake(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls[name] = (args, kwargs)
            return value

        return fake

    monkeypatch.setattr(native, "_tool_dirs", lambda *a, **k: (tools, bin_dir))
    monkeypatch.setattr(
        native, "classify_native_install", record("classify", NativeInstallState.fresh)
    )
    monkeypatch.setattr(native_lock, "prepare_native_state_dir", record("prepare", sentinel))
    monkeypatch.setattr(NativeRunLock, "acquire", staticmethod(record("acquire", sentinel)))
    monkeypatch.setattr(
        native_handoff, "prepare_bootstrap_scratch", record("scratch", scratch)
    )
    monkeypatch.setattr(native, "create_fresh_host", record("create", host))
    monkeypatch.setattr(native, "verify_owned_host", record("verify", host.ownership))
    tool_python = tools / "ciaobot" / "bin" / "python"
    tool_python.parent.mkdir(parents=True, exist_ok=True)
    tool_python.write_bytes(b"#!/bin/sh\n")
    os.chmod(tool_python, 0o755)
    shim_target = tools / "ciaobot" / "bin" / "ciao"
    shim_target.parent.mkdir(parents=True, exist_ok=True)
    shim_target.write_bytes(b"#!/bin/sh\n")
    os.chmod(shim_target, 0o755)
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "ciao").symlink_to(shim_target)
    shim_identity, _target = native._shim_identity(bin_dir, tools)
    python_stat = os.stat(tool_python)
    engine_value = native._EngineInstall(
        tool_python=tool_python,
        shim=shim_identity,
        python_identity=(python_stat.st_dev, python_stat.st_ino),
    )
    monkeypatch.setattr(native, "_install_engine", record("engine", engine_value))
    handoff = dataclasses.replace(_handoff(RUN_ID), engine_python=str(tool_python))
    monkeypatch.setattr(native_handoff, "read_result", record("read", handoff))
    monkeypatch.setattr(native_handoff, "activate", record("activate", handoff))
    monkeypatch.setattr(
        native,
        "prove_native_readiness",
        record(
            "prove",
            NativeReadinessProof(
                ready=True, failure=None, port=handoff.port, version=artifacts.version
            ),
        ),
    )
    monkeypatch.setattr(native, "make_native_readiness_deps", record("deps", readiness_deps))
    monkeypatch.setattr(
        native_readiness, "wizard_listener_pids", record("listeners", frozenset({4242}))
    )

    cancellation = native.NativeCancellation()
    production = native._production_deps(_request(), artifacts, cancellation)
    try:
        deps = production.deps
        deps.validate_inputs()
        assert deps.classify(None) is NativeInstallState.fresh
        state = deps.prepare_state_dir()
        assert state is sentinel
        (prepare_args, _prepare_kwargs) = calls["prepare"]  # type: ignore[misc]
        assert prepare_args == (install_receipt.default_receipt_path().parent,)
        lock = deps.acquire_lock(state, RUN_ID)  # type: ignore[arg-type]
        assert lock is sentinel
        (acquire_args, acquire_kwargs) = calls["acquire"]  # type: ignore[misc]
        assert acquire_args == (state,)
        assert acquire_kwargs == {"run_id": RUN_ID}
        assert deps.prepare_scratch() == scratch
        assert deps.create_host(lock, RUN_ID, 30.0) is host  # type: ignore[arg-type]
        _, create_kwargs = calls["create"]  # type: ignore[misc]
        assert create_kwargs["run_id"] == RUN_ID
        assert create_kwargs["manifest_bytes"] == artifacts.manifest.read_bytes()
        assert create_kwargs["archive_path"] == artifacts.host_archive
        deps.recheck_host(host)
        (verify_args, verify_kwargs) = calls["verify"]  # type: ignore[misc]
        assert verify_args == (native.default_bundle_path(),)
        assert verify_kwargs == {"ownership_path": host.ownership_path}
        deps.install_engine(30.0)
        engine_args = calls["engine"][0]  # type: ignore[index]
        assert engine_args[2] == bin_dir and engine_args[1] == tools
        argv = deps.wizard_command(5)
        assert argv[0] == str(tool_python)
        assert Path(argv[1]).resolve() == shim_target.resolve()
        assert deps.read_handoff(RUN_ID) is handoff
        assert calls["read"] == ((), {"expected_run_id": RUN_ID})
        assert deps.activate(RUN_ID, 30.0) is handoff
        assert calls["activate"] == ((), {"run_id": RUN_ID})
        assert deps.wizard_listener(54321, 1234) == frozenset({4242})
        assert calls["listeners"] == ((54321,), {})
        proof = deps.prove_ready(host, handoff, 30.0)
        assert proof.ready is True
        (request,), prove_kwargs = calls["prove"]  # type: ignore[misc]
        assert request.version == artifacts.version
        assert request.port == handoff.port
        assert request.workspace == handoff.workspace
        assert request.host_bundle == host.bundle_path
        assert request.ownership_path == host.ownership_path
        assert request.engine_python == Path(handoff.engine_python)
        assert request.fresh_install_service_absent is True
        assert prove_kwargs["deps"] is readiness_deps
        assert prove_kwargs["policy"].timeout <= native_readiness.MAX_TIMEOUT_SECONDS
        assert prove_kwargs["cancelled"]() is False
        cancellation.cancel()
        assert prove_kwargs["cancelled"]() is True
    finally:
        production.close()


def test_recheck_refuses_a_host_that_changed_before_engine_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _fresh_host_value(tmp_path)
    changed = dataclasses.replace(host.ownership, executable_sha256="1" * 64)
    monkeypatch.setattr(native, "verify_owned_host", lambda *a, **k: changed)
    production = native._production_deps(
        _request(), make_artifacts(tmp_path), native.NativeCancellation()
    )
    try:
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            production.deps.recheck_host(host)
        assert caught.value.code == server_host.NOT_OWNED
    finally:
        production.close()


def test_the_private_copies_are_removed_when_the_run_ends(tmp_path: Path) -> None:
    production = native._production_deps(
        _request(), make_artifacts(tmp_path), native.NativeCancellation()
    )
    production.deps.validate_inputs()
    root = production.private.root
    assert root is not None and root.is_dir()
    assert os.stat(root).st_mode & 0o777 == 0o700
    production.close()
    assert not root.exists()


# ── the wizard listener ──────────────────────────────────────────────────────


def _completed(stdout: str, returncode: int = 0):  # type: ignore[no-untyped-def]
    import subprocess

    return subprocess.CompletedProcess(["lsof"], returncode, stdout, "")


def test_wizard_listener_pids_names_every_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        native_readiness, "_bounded_run", lambda argv, timeout: _completed("111\n222\n111\n")
    )
    assert native_readiness.wizard_listener_pids(54321) == frozenset({111, 222})


def test_wizard_listener_pids_is_empty_when_lsof_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        native_readiness, "_bounded_run", lambda argv, timeout: _completed("", 1)
    )
    assert native_readiness.wizard_listener_pids(54321) == frozenset()


@pytest.mark.parametrize("stdout", ["x\n", "111\nabc\n", "\n"], ids=["junk", "mixed", "blank"])
def test_wizard_listener_pids_refuses_garbage(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    monkeypatch.setattr(
        native_readiness, "_bounded_run", lambda argv, timeout: _completed(stdout)
    )
    assert native_readiness.wizard_listener_pids(54321) == frozenset()


def test_wizard_listener_pids_never_queries_a_bad_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        native_readiness,
        "_bounded_run",
        lambda argv, timeout: calls.append(argv) or _completed("1\n"),
    )
    assert native_readiness.wizard_listener_pids(0) == frozenset()
    assert calls == []


# ── shell and readiness wiring facts ─────────────────────────────────────────


def test_python_version_matches_the_shell_installer() -> None:
    text = (ROOT / "scripts" / "install-engine.sh").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.startswith("PYTHON_VERSION=")]
    assert lines == [f"PYTHON_VERSION={native.ENGINE_PYTHON_VERSION}"]


def test_a_held_lock_reports_pid_liveness_on_stderr_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    lock_dir = state / native_lock.LOCK_DIR_NAME
    lock_dir.mkdir(mode=0o700)
    record = lock_dir / native_lock.LOCK_RECORD_NAME
    record.write_bytes(_record_bytes(pid=dead.pid))
    os.chmod(record, 0o600)
    before = record.read_bytes()

    production = native._production_deps(
        _request(), make_artifacts(tmp_path), native.NativeCancellation()
    )
    deps = dataclasses.replace(
        production.deps,
        prepare_state_dir=lambda: native_lock.prepare_native_state_dir(
            state, trusted_root=tmp_path
        ),
    )
    handle = deps.prepare_state_dir()
    try:
        with pytest.raises(NativeLockError) as caught:
            deps.acquire_lock(handle, RUN_ID)
    finally:
        handle.close()
        production.close()
    assert caught.value.reason == "lock_busy"
    stderr = capsys.readouterr().err
    assert f"pid {dead.pid}, not running" in stderr
    assert str(tmp_path) not in stderr
    assert record.read_bytes() == before


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/sbin/lsof").exists(),
    reason="needs macOS lsof",
)
def test_real_lsof_adapter_names_the_owned_wizard_listener(tmp_path: Path) -> None:
    code = (
        "import socket, time\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
        "print(s.getsockname()[1], flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    assert child.stdout is not None
    try:
        port = int(child.stdout.readline())
        owned = native._OwnedChild(
            child, term_grace=1, reap_timeout=1, poll_interval=0.05
        )
        production = native._production_deps(
            _request(), make_artifacts(tmp_path), native.NativeCancellation()
        )
        try:
            assert production.deps.wizard_listener(port, child.pid) == frozenset(
                {child.pid}
            )
        finally:
            production.close()
        assert owned.owns_process(child.pid) is True

        def only_lsof(argv: tuple[str, ...], timeout: float):  # type: ignore[no-untyped-def]
            if argv[0] != "/usr/sbin/lsof":
                return None
            return native_readiness._bounded_run(argv, timeout)

        readiness = native_readiness.make_native_readiness_deps(runner=only_lsof)
        assert readiness.listener_pid(port) == child.pid
    finally:
        child.kill()
        child.wait()
        child.stdout.close()


def test_the_module_command_reaches_the_coordinator(tmp_path: Path) -> None:
    # `python -m ciao.native_installer run` must reach the coordinator: a
    # `__main__` guard placed above the functions it calls raises NameError
    # before any event is written. Run the real module in a child, refused
    # safely (HOME does not match the account's home, so nothing is touched).
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "not-the-real-home"),
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
    }
    missing = str(tmp_path / "missing")
    with _public_event_pipe() as (read_fd, event_fd):
        os.set_inheritable(event_fd, True)
        argv = [
            sys.executable, "-m", "ciao.native_installer", "run",
            "--run-id", "0f8fad5b-d9cb-469f-a165-70867728950e",
            "--events-fd", str(event_fd), "--first-seq", "4", "--prior-events", "3",
            "--version", "1.7.0",
            "--uv", missing, "--uv-sha256", "0" * 64,
            "--wheel", missing, "--wheel-sha256", "0" * 64,
            "--manifest", missing, "--signature", missing, "--host-archive", missing,
        ]
        child = subprocess.run(
            argv,
            env=env,
            cwd=tmp_path,
            pass_fds=(event_fd,),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        os.close(event_fd)
        raw = _drain(read_fd)
    assert "NameError" not in child.stderr
    assert "Traceback" not in child.stderr
    events = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    assert events, child.stderr
    assert events[-1]["type"] == "failed"
    assert child.returncode == 1
