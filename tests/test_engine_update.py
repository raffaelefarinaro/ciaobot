from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import ciao.engine_update as engine_update
from ciao.engine_update import (
    OPERATION_NAME,
    PREVIOUS_ENV_NAME,
    RECOVER_LABEL,
    RECOVER_PLIST_NAME,
    SERVER_LABEL,
    UPDATER_LABEL,
    UPDATER_PLIST_NAME,
    Operation,
    UpdateError,
    UpdateInProgress,
    acquire_lock,
    apply_update,
    default_fetch,
    find_uv,
    main,
    read_operation,
    recover_apply,
    recover_interrupted_apply,
    release_lock,
    run_apply,
    stage_update,
    write_operation,
)
from ciao import install_receipt, macos_service
from ciao.install_receipt import InstallReceipt, read_receipt, write_receipt
from ciao.release_manifest import artifact_entry, build_manifest

# The minisign helpers are copied from tests/test_release_manifest.py rather
# than imported, so a change there cannot silently change what these tests
# actually sign.
TRUSTED = "timestamp:1\tfile:ciaobot-engine-manifest.json"

RELEASE_BASE = "https://example.test/releases/download"


def _keypair() -> tuple[Ed25519PrivateKey, str, bytes]:
    """A throwaway minisign key: (private key, public key text, key id)."""
    private_key = Ed25519PrivateKey.generate()
    key_id = b"\x01" * 8
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_key, base64.b64encode(b"Ed" + key_id + public_raw).decode(), key_id


def _sign(data: bytes, priv: Ed25519PrivateKey, key_id: bytes) -> str:
    """Minisign text (prehashed) over ``data``."""
    signature = priv.sign(hashlib.blake2b(data, digest_size=64).digest())
    global_sig = priv.sign(signature + TRUSTED.encode())
    return (
        "untrusted comment: test\n"
        f"{base64.b64encode(b'ED' + key_id + signature).decode()}\n"
        f"trusted comment: {TRUSTED}\n"
        f"{base64.b64encode(global_sig).decode()}\n"
    )


class FakeRelease:
    """A release directory served through a fake ``release_base`` URL."""

    def __init__(self, root: Path, version: str, wheel: Path) -> None:
        self.root = root
        self.version = version
        self.dir = root / f"v{version}"
        self.dir.mkdir(parents=True)
        # What the release actually serves; tampering with this is what a
        # digest mismatch is.
        self.wheel = self.dir / wheel.name
        shutil.copy(wheel, self.wheel)
        self.entry = artifact_entry(self.wheel, kind="wheel")
        self.manifest = self.dir / "ciaobot-engine-manifest.json"
        self.signature = self.dir / "ciaobot-engine-manifest.json.sig"

    def publish(self, key: tuple[Ed25519PrivateKey, str, bytes]) -> None:
        """Write the manifest and signature the way a real release does."""
        priv, public_key, key_id = key
        raw = json.dumps(
            build_manifest(
                self.version, [self.entry], created="2026-09-25T10:00:00+00:00"
            )
        ).encode()
        self.manifest.write_bytes(raw)
        self.signature.write_text(_sign(raw, priv, key_id), encoding="utf-8")
        self.public_key = public_key

    def serve(self, url: str, dest: Path, **kwargs: Any) -> None:
        # `**kwargs` absorbs the download bounds `engine_update` passes for
        # the wheel (`max_bytes`); the fake release serves whatever the manifest
        # is sized to, so there is nothing to enforce here.
        shutil.copy(self.root / url.split("/download/", 1)[1], dest)


@pytest.fixture
def release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRelease:
    """A signed fake release, plus the public key `engine_update` will read.

    `verify_manifest` binds its default key at definition time, so the only way
    a test key can reach it is via the module attribute `engine_update` reads
    explicitly at call time.
    """
    key = _keypair()
    wheel = tmp_path / "src" / "ciaobot-1.2.3-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"a fake engine wheel" * 64)
    rel = FakeRelease(tmp_path / "rel", "1.2.3", wheel)
    rel.publish(key)
    monkeypatch.setattr("ciao.release_manifest.RELEASE_PUBLIC_KEY", rel.public_key)
    return rel


@pytest.fixture
def fake_run() -> tuple[Any, list[list[str]]]:
    """A `run` that records argv, fakes `uv venv`, and reports 1.2.3."""
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if "venv" in argv:
            env_dir = Path(argv[-1])
            (env_dir / "bin").mkdir(parents=True, exist_ok=True)
            (env_dir / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        stdout = "1.2.3\n" if "-c" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    return run, calls


def test_stage_update_happy_path(tmp_path: Path, release: FakeRelease, fake_run) -> None:
    run, calls = fake_run

    op = stage_update(
        "1.2.3",
        current_version="1.2.2",
        state_dir=tmp_path / "state",
        release_base=RELEASE_BASE,
        fetch=release.serve,
        run=run,
        uv="/fake/uv",
        python_version="3.13",
    )

    assert op.phase == "staged"
    assert op.from_version == "1.2.2"
    assert op.to_version == "1.2.3"
    assert op.id.endswith("-1.2.3")
    assert op.wheel_sha256 == release.entry["sha256"]

    env_dir = Path(op.stage_dir) / "env"
    assert calls[0][:4] == ["/fake/uv", "venv", "--python", "3.13"]
    assert calls[0][4] == str(env_dir)
    assert calls[1][:3] == ["/fake/uv", "pip", "install"]
    assert calls[1][3:] == ["--python", str(env_dir / "bin" / "python"), op.wheel]
    assert calls[2][:3] == [str(env_dir / "bin" / "python"), "-I", "-c"]

    assert read_operation(tmp_path / "state") == op
    record = tmp_path / "state" / "operation.json"
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
    # The staged env is really on disk, not just described by the record.
    assert (env_dir / "bin" / "python").exists()


def test_stage_update_rejects_bad_signature(
    tmp_path: Path, release: FakeRelease, fake_run
) -> None:
    run, calls = fake_run
    manifest = json.loads(release.manifest.read_text(encoding="utf-8"))
    manifest["created"] = "2099-01-01T00:00:00+00:00"
    release.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(UpdateError):
        stage_update(
            "1.2.3",
            current_version="1.2.2",
            state_dir=tmp_path / "state",
            release_base=RELEASE_BASE,
            fetch=release.serve,
            run=run,
            uv="/fake/uv",
        )

    op = read_operation(tmp_path / "state")
    assert op is not None
    assert op.phase == "failed"
    assert op.error
    # Nothing is installed from a manifest that did not verify.
    assert calls == []


def test_stage_update_rejects_wheel_digest_mismatch(
    tmp_path: Path, release: FakeRelease, fake_run
) -> None:
    run, calls = fake_run
    # Swap the served bytes after signing: the manifest is valid, the download
    # is not, which is exactly the case a digest check exists for.
    release.wheel.write_bytes(b"a different wheel" * 64)

    with pytest.raises(UpdateError, match="does not match"):
        stage_update(
            "1.2.3",
            current_version="1.2.2",
            state_dir=tmp_path / "state",
            release_base=RELEASE_BASE,
            fetch=release.serve,
            run=run,
            uv="/fake/uv",
        )

    assert calls == []
    op = read_operation(tmp_path / "state")
    assert op is not None
    assert op.phase == "failed"


def test_stage_update_rejects_version_check_mismatch(
    tmp_path: Path, release: FakeRelease
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if "venv" in argv:
            env_dir = Path(argv[-1])
            (env_dir / "bin").mkdir(parents=True, exist_ok=True)
            (env_dir / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="9.9.9\n", stderr="")

    with pytest.raises(UpdateError):
        stage_update(
            "1.2.3",
            current_version="1.2.2",
            state_dir=tmp_path / "state",
            release_base=RELEASE_BASE,
            fetch=release.serve,
            run=run,
            uv="/fake/uv",
        )

    op = read_operation(tmp_path / "state")
    assert op is not None
    assert op.phase == "failed"
    assert "9.9.9" in op.error


def test_stage_update_already_current_writes_no_record(
    tmp_path: Path, release: FakeRelease
) -> None:
    with pytest.raises(UpdateError, match="already on 1.2.3"):
        stage_update("1.2.3", current_version="1.2.3", state_dir=tmp_path / "state")

    assert read_operation(tmp_path / "state") is None
    assert not (tmp_path / "state" / "1.2.3").exists()


def test_stage_update_lock_blocks_concurrent_run(
    tmp_path: Path, release: FakeRelease, fake_run
) -> None:
    run, _ = fake_run
    state = tmp_path / "state"
    state.mkdir()
    handle = acquire_lock(state)
    try:
        with pytest.raises(UpdateInProgress):
            stage_update(
                "1.2.3",
                current_version="1.2.2",
                state_dir=state,
                release_base=RELEASE_BASE,
                fetch=release.serve,
                run=run,
                uv="/fake/uv",
            )
    finally:
        release_lock(handle)

    # The lock is released with the run, so the next attempt can take it.
    assert (
        stage_update(
            "1.2.3",
            current_version="1.2.2",
            state_dir=state,
            release_base=RELEASE_BASE,
            fetch=release.serve,
            run=run,
            uv="/fake/uv",
        ).phase
        == "staged"
    )


def test_stage_update_copies_previous_receipt(
    tmp_path: Path, release: FakeRelease, fake_run
) -> None:
    run, _ = fake_run
    # conftest already isolated the receipt path to a tmp dir.
    from ciao import install_receipt

    receipt_path = install_receipt.default_receipt_path()
    write_receipt(
        InstallReceipt(
            version="1.2.2",
            executable="/u/.local/bin/ciao",
            python="/u/.local/share/uv/tools/ciaobot/bin/python",
            service_backend="launchd",
            service_label="com.ciao.server",
            installed_at="2026-09-25T16:00:00+00:00",
        ),
        receipt_path,
    )

    op = stage_update(
        "1.2.3",
        current_version="1.2.2",
        state_dir=tmp_path / "state",
        release_base=RELEASE_BASE,
        fetch=release.serve,
        run=run,
        uv="/fake/uv",
    )

    assert op.previous_receipt.endswith("previous-receipt.json")
    assert Path(op.previous_receipt).read_bytes() == receipt_path.read_bytes()


def test_find_uv_prefers_receipt_then_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _executable(path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    receipt_uv = _executable(tmp_path / "from-receipt" / "uv")
    which_uv = _executable(tmp_path / "from-path" / "uv")
    home = tmp_path / "home"
    _executable(home / ".local" / "bin" / "uv")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(shutil, "which", lambda name: which_uv if name == "uv" else None)

    # The receipt wins: it is the uv that built the running environment.
    assert find_uv(receipt_uv) == receipt_uv
    # Then PATH.
    assert find_uv() == which_uv
    # Then the installer's well-known location.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert find_uv() == str(home / ".local" / "bin" / "uv")
    # Nothing findable is a clear failure, not a guess.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty-home"))
    with pytest.raises(UpdateError, match="uv"):
        find_uv(str(tmp_path / "absent" / "uv"))


def test_default_fetch_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"downloaded bytes"
    calls: list[str] = []
    attempts = {"n": 0}

    class _Response(io.BytesIO):
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: Any) -> None:
            self.close()

    def urlopen(request: Any, timeout: float = 0.0) -> _Response:
        calls.append(request.full_url)
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.URLError("temporary failure")
        return _Response(payload)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))

    dest = tmp_path / "out" / "file.bin"
    default_fetch("https://example.test/file.bin", dest)

    assert dest.read_bytes() == payload
    assert len(calls) == 2
    assert slept == [2]
    # The interrupted download leaves no .part for a later run to trip over.
    assert list(dest.parent.glob("*.part")) == []


def test_cli_stage_refuses_non_installer_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "editable")

    assert main(["stage"]) == 2

    assert "only for engines installed with" in capsys.readouterr().err


def test_cli_status_without_record(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status"]) == 0

    assert "no update staged" in capsys.readouterr().out


# A `run(..., check=True)` failure carries uv's reason in the captured stderr,
# not in `str(exc)`, which is only "returned non-zero exit status 1". The record
# must keep the real explanation, or `ciao update status` and the UI show an
# operator an unexplained failure. (Round 1 review finding.)
def test_stage_update_keeps_uv_stderr_in_failed_record(
    tmp_path: Path, release: FakeRelease, fake_run
) -> None:
    good_run, _ = fake_run

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "pip" in argv:
            raise subprocess.CalledProcessError(
                1, argv, output="", stderr="no matching distribution"
            )
        return good_run(argv, **kwargs)

    with pytest.raises(UpdateError, match="no matching distribution"):
        stage_update(
            "1.2.3",
            current_version="1.2.2",
            state_dir=tmp_path / "state",
            release_base=RELEASE_BASE,
            fetch=release.serve,
            run=run,
            uv="/fake/uv",
        )

    op = read_operation(tmp_path / "state")
    assert op is not None
    assert op.phase == "failed"
    assert "no matching distribution" in op.error


# The record's phase is what an operator reads. Printing every record as
# "staged" told a reader the update was ready right after a failure.
def test_cli_status_reports_failed_phase(capsys: pytest.CaptureFixture[str]) -> None:
    write_operation(
        Operation(
            id="20260925T100000-1.2.3",
            phase="failed",
            from_version="1.2.2",
            to_version="1.2.3",
            started_at="2026-09-25T10:00:00+00:00",
            updated_at="2026-09-25T10:00:05+00:00",
            error="boom",
            stage_dir="/home/u/.local/state/ciaobot/updates/1.2.3",
        )
    )

    assert main(["status"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("failed")
    assert "boom" in out


# ── the apply half (#570) ────────────────────────────────────────────────
#
# launchd, uv, the engine's HTTP surface, the clock and sleep are all fakes
# here: nothing below may start a service, move a real environment, or open a
# socket. The engine fake is the interesting one — it is driven by the same
# two events the real engine is (a launchd bootout, a service start), so a
# transaction that stops the engine and swaps it is observable end to end
# without a machine.

PORT = 8443
BASE = f"http://localhost:{PORT}"
FROM_VERSION = "1.2.2"
TO_VERSION = "1.2.3"


def _write_env(env: Path, version: str) -> Path:
    """A stand-in env whose ``bin/python`` reports ``version``, and its path."""
    (env / "bin").mkdir(parents=True, exist_ok=True)
    (env / "bin" / "python").write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
    return env / "bin" / "python"


def _env_version(env: Path) -> str:
    try:
        return (env / "bin" / "python").read_text(encoding="utf-8").splitlines()[1][5:]
    except (OSError, IndexError):
        return ""


class _FakeClock:
    """A monotonic clock that advances one step per reading, and never really waits."""

    def __init__(self, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


class _FakeEngine:
    """The engine the apply half drains, stops, swaps and starts again.

    `/api/startup-status` answers from two facts a real engine has: whether it
    is running (flipped by a launchd bootout and by a service start) and which
    version the env on disk carries. Deriving the version from the env is what
    keeps a test honest — a readiness the install did not produce cannot be
    asserted by accident. ``readiness`` overrides the answer for an engine that
    comes up wrong, and is told how many times the service has been started so
    a test can tell the broken new engine from the restored old one.
    """

    def __init__(self, live_env: Path, *, target: str = TO_VERSION) -> None:
        self.live_env = live_env
        self.target = target
        self.up = True
        # A bootout the engine refuses to honour, to model a stop that never
        # completes.
        self.stubborn = False
        self.readiness: Any = None
        self.starts = 0
        self.launchctl_calls: list[list[str]] = []
        self.posts: list[str] = []
        self.active_script: list[list[str]] = []
        self.polls = 0
        # True while the engine never runs out of work, so a drain cannot
        # finish.
        self.busy = False
        self.run_calls: list[list[str]] = []
        self.run_envs: list[dict[str, str] | None] = []
        # How many more readiness probes answer after *each* bootout before
        # the engine stops answering, modelling launchd finishing with a job
        # after `bootout` has already returned. 0 is the well-behaved case
        # where a bootout takes effect at once.
        self.lingering = 0
        self.lingering_after_bootout = 0
        # Set by a test that wants the engine to answer a probe with a
        # failure — a rolled-back-up opener that cannot reach it.
        self.post_error: Exception | None = None

    # -- the engine's HTTP surface ----------------------------------------
    def version(self) -> str:
        return _env_version(self.live_env)

    def post(self, url: str) -> dict[str, Any]:
        self.posts.append(url)
        if self.post_error is not None:
            raise self.post_error
        if url.endswith("/api/admin/drain"):
            # The drain is polled by asking again, so this is where the active
            # chat list is read from.
            self.polls += 1
            if self.active_script:
                return {"draining": True, "active_chat_ids": self.active_script.pop(0)}
            return {"draining": True, "active_chat_ids": ["still-busy"] if self.busy else []}
        return {}

    def get(self, url: str) -> dict[str, Any] | None:
        if not self.up:
            if self.lingering > 0:
                # Booted out, but launchd has not finished with the process
                # yet: it keeps answering for a while after `bootout` returns.
                self.lingering -= 1
                return {"version": self.version(), "overall_ready": True}
            return None
        if self.readiness is not None:
            return self.readiness(self.starts)
        return {"version": self.version(), "overall_ready": True}

    # -- launchd, uv and the service ------------------------------------
    def launchctl(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.launchctl_calls.append(list(args))
        if args[0] == "bootout" and args[-1].endswith(SERVER_LABEL) and not self.stubborn:
            self.up = False
            # Every bootout relights the lingering window, so each one has to
            # be waited out on its own.
            self.lingering = self.lingering_after_bootout
        return subprocess.CompletedProcess(args, 0, "", "")

    def start_service(self) -> SimpleNamespace:
        self.starts += 1
        self.up = True
        return SimpleNamespace(ok=True)

    def uv_run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """``uv`` as the swap needs it: install the wheel, answer both probes.

        The probes answer from the env on disk, not from what the running
        engine claims: a subprocess running the installed interpreter and a
        server reporting `/api/startup-status` are different facts, and the
        swap checks the first while the readiness check looks at the second.
        The environment each call was given is kept, because where uv is told
        to install is a real answer and not an implementation detail.
        """
        self.run_calls.append(list(argv))
        self.run_envs.append(kwargs.get("env"))
        if "tool" in argv and "install" in argv:
            # "Installing" means writing the env uv would have created.
            _write_env(self.live_env, self.target)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "version_info" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, "3.13\n", "")
        if "-c" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{_env_version(self.live_env)}\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def booted_out(self, label: str) -> bool:
        return any(
            call[0] == "bootout" and call[-1].endswith(label)
            for call in self.launchctl_calls
        )

    def changed_jobs(self) -> list[list[str]]:
        """The launchctl calls that load or unload a job.

        `launchctl print` is a read-only question about what launchd already
        has — the pre-flight asks it what `com.ciao.server` is really running,
        and recovery asks it whether a swap is still in flight — so an assertion
        about "nothing was launched" is about the calls that change something.
        """
        return [call for call in self.launchctl_calls if call[0] != "print"]


def _staged(
    root: Path, *, phase: str = "staged", from_version: str = FROM_VERSION
) -> tuple[Operation, Path, Path, _FakeEngine]:
    """A staged update with its wheel, env, live env, receipt and record.

    Built the way the staging half leaves it, so `apply_update` and `run_apply`
    see a real transaction rather than a convenient one.
    """
    state = root / "updates"
    stage_dir = state / TO_VERSION
    (stage_dir / "env").mkdir(parents=True)
    staged_python = _write_env(stage_dir / "env", TO_VERSION)
    wheel = root / f"ciaobot-{TO_VERSION}-py3-none-any.whl"
    wheel.write_bytes(b"a fake engine wheel")
    previous_receipt = stage_dir / "previous-receipt.json"

    live_env = root / "tools" / "ciaobot"
    live_python = _write_env(live_env, from_version)
    receipt_path = root / "install-receipt.json"
    write_receipt(
        InstallReceipt(
            version=from_version,
            executable=str(root / "bin" / "ciao"),
            python=str(live_python),
            service_backend="launchd",
            service_label=SERVER_LABEL,
            installed_at="2026-09-25T16:00:00+00:00",
            uv="/fake/uv",
        ),
        receipt_path,
    )
    shutil.copy2(receipt_path, previous_receipt)

    op = Operation(
        id=f"20260925T100000-{TO_VERSION}",
        phase=phase,
        from_version=from_version,
        to_version=TO_VERSION,
        started_at="2026-09-25T10:00:00+00:00",
        updated_at="2026-09-25T10:00:00+00:00",
        stage_dir=str(stage_dir),
        wheel=str(wheel),
        wheel_sha256="deadbeef",
        env_python=str(staged_python),
        previous_receipt=str(previous_receipt),
    )
    write_operation(op, state)
    return op, state, receipt_path, _FakeEngine(live_env)


def _apply(engine: _FakeEngine, state: Path, **kwargs: Any) -> Operation:
    return apply_update(
        state_dir=state,
        port=PORT,
        http_post=engine.post,
        launchctl=engine.launchctl,
        uid=501,
        sleep=kwargs.pop("sleep", lambda seconds: None),
        clock=kwargs.pop("clock", _FakeClock()),
        **kwargs,
    )


def _run(engine: _FakeEngine, op: Operation, state: Path, receipt_path: Path, **kwargs: Any) -> Operation:
    return run_apply(
        op.id,
        state_dir=state,
        port=PORT,
        http_post=engine.post,
        http_get=engine.get,
        launchctl=kwargs.pop("launchctl", engine.launchctl),
        uv=kwargs.pop("uv", "/fake/uv"),
        run=kwargs.pop("run", engine.uv_run),
        start_service=kwargs.pop("start_service", engine.start_service),
        uid=501,
        sleep=lambda seconds: None,
        clock=kwargs.pop("clock", _FakeClock()),
        receipt_path=receipt_path,
        **kwargs,
    )


def _handoff(
    engine: _FakeEngine, state: Path, **kwargs: Any
) -> Operation | None:
    """The in-process half of recovery: recognise the record, load the job.

    `updater_loaded` is injected rather than left to the real `launchctl print`,
    so nothing here can reach a real launchd.
    """
    return recover_interrupted_apply(
        state_dir=state,
        launchctl=kwargs.pop("launchctl", engine.launchctl),
        uid=501,
        updater_loaded=kwargs.pop("updater_loaded", lambda: False),
        **kwargs,
    )


def _recover_apply(
    engine: _FakeEngine, op: Operation, state: Path, receipt_path: Path, **kwargs: Any
) -> Operation | None:
    """The durable job's own entry, running the way launchd runs it."""
    return recover_apply(
        op.id,
        state_dir=state,
        port=PORT,
        http_post=kwargs.pop("http_post", engine.post),
        http_get=engine.get,
        launchctl=kwargs.pop("launchctl", engine.launchctl),
        start_service=kwargs.pop("start_service", engine.start_service),
        uid=501,
        sleep=lambda seconds: None,
        clock=kwargs.pop("clock", _FakeClock()),
        receipt_path=receipt_path,
        **kwargs,
    )


def _recorder(calls: list[str]) -> Any:
    """An injected `http_post` that records every URL it is handed."""

    def post(url: str) -> dict[str, Any]:
        calls.append(url)
        return {}

    return post


def _install_recovery_agent(state: Path, op: Operation) -> Path:
    """The durable agent's plist, as `apply_update` writes it before the stop."""
    return engine_update._write_recover_plist(
        op, op.env_python or str(Path(op.stage_dir) / "env" / "bin" / "python"), state
    )


def _login_recover_plist() -> Path:
    """The agent's login-time copy, in the directory launchd itself scans.

    `conftest.py` repoints `CIAO_LAUNCH_AGENTS_DIR` at the test's `tmp_path`, so
    this is a temp file and never the operator's own LaunchAgents directory.
    """
    return macos_service.default_launch_agents_dir() / RECOVER_PLIST_NAME


def _loaded_job(
    engine: _FakeEngine, program: str | Path | None, *, running: bool = True
) -> Any:
    """A launchctl that reports a *loaded* `com.ciao.server` running `program`.

    The rendering is the one `launchctl print` produces, because the pre-flight
    asks launchd what the job really is rather than reading the plist on disk:
    launchd loads a job once, so the file and the running job are two different
    facts. `program=None` is a job that is not loaded at all, which is the only
    case where the on-disk plist is allowed to answer instead.
    """

    def launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "print" and args[-1].endswith(SERVER_LABEL):
            if program is None:
                return subprocess.CompletedProcess(
                    args, 113, "", f'Could not find service "{SERVER_LABEL}" in domain for uid: 501'
                )
            state = "running" if running else "not running"
            return subprocess.CompletedProcess(
                args,
                0,
                f"{SERVER_LABEL} = {{\n"
                "\tactive count = 1\n"
                f"\tpath = {_server_plist_path()}\n"
                "\ttype = LaunchAgent\n"
                f"\tstate = {state}\n"
                f"\tprogram = {program}\n"
                "\targuments = {\n\t\t-m\n\t\tciao.main\n\t}\n"
                f"\tlast exit code = {0 if running else 1}\n"
                "}\n",
                "",
            )
        return engine.launchctl(args)

    return launchctl


def _running_updater(engine: _FakeEngine, *, running: bool) -> Any:
    """A launchctl that reports `com.ciao.updater` loaded, and maybe running.

    A one-shot LaunchAgent stays loaded in launchd after its process exits, so
    "loaded" alone cannot tell a live swap from a dead one — which is why the
    recovery job asks for the pid as well.
    """

    def launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "print" and args[-1].endswith(UPDATER_LABEL):
            body = [
                f"{UPDATER_LABEL} = {{",
                "\tactive count = 0",
                "\ttype = LaunchAgent",
                f"\tstate = {'running' if running else 'not running'}",
                f"\tlast exit code = {0 if running else 1}",
            ]
            if running:
                body.append("\tpid = 4242")
            body.append("}")
            return subprocess.CompletedProcess(args, 0, "\n".join(body) + "\n", "")
        return engine.launchctl(args)

    return launchctl


def _server_plist_path() -> Path:
    """The server LaunchAgent's path, from the helper that owns it.

    `conftest.py` redirects `CIAO_LAUNCH_AGENTS_DIR` to the test's `tmp_path`,
    so this is a temp plist and never the operator's live one.
    """
    return macos_service.default_launch_agents_dir() / f"{SERVER_LABEL}.plist"


def _write_server_plist(program: str | Path) -> Path:
    """Write that plist with ``program`` as its `ProgramArguments[0]`."""
    path = _server_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(
            {
                "Label": SERVER_LABEL,
                "ProgramArguments": [str(program), "-m", "ciao.main"],
                "RunAtLoad": True,
                "KeepAlive": True,
            },
            handle,
        )
    return path


def _interrupted(
    root: Path, phase: str
) -> tuple[Operation, Path, Path, _FakeEngine]:
    """A staged operation a killed swap left in ``phase``.

    The files are as that transaction left them: the live env renamed aside to
    `previous-env` (so it is gone, which is what `swapping` means), the receipt
    still the pre-update one — it is only rewritten at `starting` — and the
    staged env still in place.
    """
    op, state, receipt_path, engine = _staged(root, phase=phase)
    engine_update._move_env(engine.live_env, Path(op.stage_dir) / PREVIOUS_ENV_NAME)
    return op, state, receipt_path, engine


@pytest.fixture
def phases(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every phase the transaction persisted, in order.

    The record is the only thing that survives an update, so the sequence an
    operator would read off it is the outcome under test — not just the last
    value of it.
    """
    seen: list[str] = []
    real = engine_update.write_operation

    def spy(op: Operation, state_dir: Path | None = None) -> None:
        seen.append(op.phase)
        real(op, state_dir)

    monkeypatch.setattr(engine_update, "write_operation", spy)
    return seen


def test_apply_drains_then_bootstraps_updater(tmp_path: Path) -> None:
    op, state, _, engine = _staged(tmp_path)
    # Two readings with work, then a settled engine: one empty reading is not
    # a drained engine.
    engine.active_script = [["c1"], ["c1"], [], [], []]

    result = _apply(engine, state)

    # The drain is polled by POSTing it again rather than by reading
    # `/api/active-chats`, which a client-mode node would forward to the host.
    # Every request the engine saw is the drain itself.
    assert engine.posts == [f"{BASE}/api/admin/drain"] * 5
    assert result.phase == "applying"
    assert read_operation(state) == result
    # Two readings with work in flight, then exactly three settled ones: a
    # single empty reading is not a drained engine.
    assert engine.polls == 5

    # Bootout before bootstrap for each of the two jobs it loads here, and the
    # plist handed to launchctl is the one on disk. The updater performs the
    # swap; the recovery agent behind it is the net for a swap that never
    # finishes (`test_apply_installs_a_durable_recovery_agent`).
    assert [call[0] for call in engine.launchctl_calls] == [
        "bootout",
        "bootstrap",
        "bootout",
        "bootstrap",
    ]
    assert engine.booted_out(UPDATER_LABEL)
    plist_path = Path(
        next(
            call[-1]
            for call in engine.launchctl_calls
            if call[-1] == str(state / UPDATER_PLIST_NAME)
        )
    )
    assert plist_path.is_file()
    assert stat.S_IMODE(plist_path.stat().st_mode) == 0o600

    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["Label"] == UPDATER_LABEL
    # A relaunching updater would re-run the swap every few seconds.
    assert plist["KeepAlive"] is False
    assert plist["RunAtLoad"] is True
    # Its own process group, so booting the engine out cannot take it with it.
    assert plist["AbandonProcessGroup"] is True
    # From the staged env: the job must not be running the env it replaces.
    assert plist["ProgramArguments"] == [
        op.env_python,
        "-I",
        "-m",
        "ciao.engine_update",
        "run-apply",
        "--operation",
        op.id,
    ]
    assert plist["StandardOutPath"] == str(Path(op.stage_dir) / "updater.log")
    assert plist["StandardErrorPath"] == str(Path(op.stage_dir) / "updater.log")


def test_apply_drain_timeout_cancels_and_leaves_engine(tmp_path: Path) -> None:
    _, state, _, engine = _staged(tmp_path)
    engine.busy = True  # a turn that never settles

    with pytest.raises(UpdateError, match="left untouched"):
        _apply(engine, state, drain_timeout=5.0)

    record = read_operation(state)
    assert record is not None
    assert record.phase == "failed"
    assert "drain timed out after 5s" in record.error
    assert "left untouched" in record.error
    # Admission is reopened: an engine left refusing turns is worse than the
    # update it was waiting for. The cancel is the last thing asked of the
    # engine — everything before it is the drain itself, polled by re-POSTing.
    assert engine.posts[-1] == f"{BASE}/api/admin/drain/cancel"
    assert set(engine.posts) == {f"{BASE}/api/admin/drain", f"{BASE}/api/admin/drain/cancel"}
    # Nothing was stopped, and no updater job was started.
    assert engine.launchctl_calls == []
    assert not (state / UPDATER_PLIST_NAME).exists()


def test_apply_refuses_without_staged_update(tmp_path: Path) -> None:
    state = tmp_path / "empty"
    state.mkdir()

    with pytest.raises(UpdateError, match="nothing staged"):
        _apply(_FakeEngine(tmp_path / "tools" / "ciaobot"), state)

    assert read_operation(state) is None


def test_run_apply_happy_path(tmp_path: Path, phases: list[str]) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")

    result = _run(engine, op, state, receipt_path)

    assert result.phase == "applied"
    assert result.error == ""
    assert phases == ["stopping", "swapping", "starting", "verifying_start", "applied"]
    # The engine was booted out before any file was touched, and started again.
    assert engine.booted_out(SERVER_LABEL)
    assert engine.starts == 1

    receipt = read_receipt(receipt_path)
    assert receipt is not None
    assert receipt.version == TO_VERSION
    assert receipt.previous_version == FROM_VERSION
    # The entry point does not move with the env, so the receipt records the
    # release it replaced rather than pretending the executable changed.
    installed = read_receipt(Path(op.previous_receipt))
    assert installed is not None
    assert receipt.executable == installed.executable
    assert receipt.previous_executable == installed.executable
    assert receipt.uv == "/fake/uv"

    # One rollback generation is retained: the old env is renamed, not deleted.
    previous_env = Path(op.stage_dir) / PREVIOUS_ENV_NAME
    assert (previous_env / "bin" / "python").exists()
    assert _env_version(previous_env) == FROM_VERSION
    assert _env_version(engine.live_env) == TO_VERSION

    install = next(c for c in engine.run_calls if "tool" in c)
    assert install[:4] == ["/fake/uv", "tool", "install", "--force"]
    assert install[4] == "--python"
    assert install[5] == "3.13"  # the staged env's interpreter, not this process's
    assert install[-1] == op.wheel

    # Where uv installs is pinned to the install this receipt describes. The
    # updater runs under launchd's own environment, and an inherited
    # `UV_TOOL_DIR`/`XDG_DATA_HOME` would send the install somewhere else and
    # re-point the `ciao` shim at it — leaving the new env unused, the old one
    # gone, and a rollback that reports failure over an install that was never
    # replaced.
    env = engine.run_envs[engine.run_calls.index(install)]
    assert env is not None
    assert env["UV_TOOL_DIR"] == str(engine.live_env.parent)
    assert env["UV_TOOL_BIN_DIR"] == str(Path(installed.executable).parent)


def test_run_apply_rolls_back_when_uv_install_fails(
    tmp_path: Path, phases: list[str]
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    original_receipt = receipt_path.read_bytes()

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "tool" in argv:
            raise subprocess.CalledProcessError(
                1, argv, output="", stderr="no matching distribution"
            )
        return engine.uv_run(argv, **kwargs)

    result = _run(engine, op, state, receipt_path, run=run)

    assert result.phase == "rolled_back"
    assert "rolled back" in result.error
    # uv's own reason survives, and the record says which version came back.
    assert "no matching distribution" in result.error
    assert f"rolled back to {FROM_VERSION}" in result.error
    assert "rollback_failed" not in phases
    # The old env is back, byte for byte in the places that matter.
    assert _env_version(engine.live_env) == FROM_VERSION
    assert receipt_path.read_bytes() == original_receipt
    # The operator gets a running engine back, not a stopped one.
    assert engine.starts == 1
    assert engine.up is True


def test_run_apply_rolls_back_when_new_engine_never_ready(
    tmp_path: Path, phases: list[str]
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    # The engine answers, but the first one back never reports itself ready.
    # The second start is the restored env, which does — so a `rolled_back`
    # phase means the rollback's own readiness check really passed.
    engine.readiness = lambda starts: (
        {"version": TO_VERSION, "overall_ready": False}
        if starts == 1
        else {"version": FROM_VERSION, "overall_ready": True}
    )

    result = _run(engine, op, state, receipt_path)

    assert result.phase == "rolled_back"
    assert "did not report" in result.error
    assert engine.starts == 2
    # The version the restored engine answers with is the one on disk now.
    assert engine.version() == FROM_VERSION
    assert _env_version(engine.live_env) == FROM_VERSION
    receipt = read_receipt(receipt_path)
    assert receipt is not None
    assert receipt.version == FROM_VERSION


def test_run_apply_rollback_failed_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "tool" in argv:
            raise subprocess.CalledProcessError(1, argv, output="", stderr="uv: broken")
        return engine.uv_run(argv, **kwargs)

    # The restore is the one move the swap cannot survive losing, so simulate
    # exactly that: the move back into place fails, while the forward move and
    # every unrelated file operation still work.
    real_replace = os.replace
    real_move = shutil.move

    def guarded(original: Any) -> Any:
        def call(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
            if Path(dst) == engine.live_env:
                raise OSError(28, "no space left on device")
            return original(src, dst, *args, **kwargs)

        return call

    monkeypatch.setattr(engine_update.os, "replace", guarded(real_replace))
    monkeypatch.setattr(engine_update.shutil, "move", guarded(real_move))

    result = _run(engine, op, state, receipt_path, run=run)

    assert result.phase == "rollback_failed"
    # Both reasons, or the record cannot answer the only question it exists for.
    assert "uv: broken" in result.error
    assert "restore the previous env" in result.error
    # The env really is gone, which is why this is not a quiet success.
    assert not engine.live_env.exists()
    # Attempted anyway: a stopped engine is a worse outcome than a broken one.
    assert engine.starts == 1


def test_run_apply_move_failure_keeps_the_live_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    original_receipt = receipt_path.read_bytes()

    # Only the move of the live env is refused; the record and the receipt are
    # still written, so the outcome is readable.
    real_replace = os.replace
    real_move = shutil.move

    def refuse_live_env(original: Any) -> Any:
        def call(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
            if Path(src) == engine.live_env:
                raise OSError(13, "permission denied")
            return original(src, dst, *args, **kwargs)

        return call

    monkeypatch.setattr(engine_update.os, "replace", refuse_live_env(real_replace))
    monkeypatch.setattr(engine_update.shutil, "move", refuse_live_env(real_move))

    result = _run(engine, op, state, receipt_path)

    assert result.phase == "rolled_back"
    # The move never happened, so the live env is still the install the
    # operator is running: a rollback that "restored" it would delete the only
    # working copy of the engine.
    assert _env_version(engine.live_env) == FROM_VERSION
    assert receipt_path.read_bytes() == original_receipt
    assert engine.starts == 1
    # Nothing was installed either, because the swap stopped before uv ran.
    assert [c for c in engine.run_calls if "tool" in c] == []


def test_run_apply_engine_never_stops_touches_nothing(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    engine.stubborn = True  # the bootout does not take

    result = _run(engine, op, state, receipt_path)

    assert result.phase == "failed"
    assert "did not stop" in result.error
    # Nothing moved: the live env is the same env, and the receipt is the
    # receipt the running engine was installed with.
    assert _env_version(engine.live_env) == FROM_VERSION
    assert read_receipt(receipt_path) == read_receipt(
        Path(op.previous_receipt)
    )
    assert not (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()
    assert [c for c in engine.run_calls if "tool" in c] == []
    # Starting a service that was never really stopped is idempotent, and it
    # is attempted so an engine the bootout did touch comes back.
    assert engine.starts == 1


def test_run_apply_prunes_older_previous_env_only_after_success(tmp_path: Path) -> None:
    applied_op, applied_state, applied_receipt, applied_engine = _staged(
        tmp_path / "applied", phase="applying"
    )
    (applied_state / "1.0.0" / PREVIOUS_ENV_NAME).mkdir(parents=True)
    older = applied_state / "1.0.0" / PREVIOUS_ENV_NAME

    assert _run(applied_engine, applied_op, applied_state, applied_receipt).phase == "applied"

    # One generation is enough: the superseded one goes, this update's own
    # copy stays, because it is what a rollback would use.
    assert not older.exists()
    assert (Path(applied_op.stage_dir) / PREVIOUS_ENV_NAME).exists()

    rolled_op, rolled_state, rolled_receipt, rolled_engine = _staged(
        tmp_path / "rolled-back", phase="applying"
    )
    kept = rolled_state / "1.0.0" / PREVIOUS_ENV_NAME
    kept.mkdir(parents=True)

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "tool" in argv:
            raise subprocess.CalledProcessError(1, argv, output="", stderr="uv: broken")
        return rolled_engine.uv_run(argv, **kwargs)

    assert _run(rolled_engine, rolled_op, rolled_state, rolled_receipt, run=run).phase == "rolled_back"

    # A failed update has not superseded anything, so the older env stays.
    assert kept.exists()


# ── review round 1 ───────────────────────────────────────────────────────
#
# Each of these is a liveness or robustness hole the round-1 review found in
# this PR's own code. The comments say why the wrong behaviour is worse, not
# just what the assertion is.


def test_apply_interrupt_reopens_admission(tmp_path: Path) -> None:
    _, state, _, engine = _staged(tmp_path)
    engine.busy = True  # a turn that never settles

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _apply(engine, state, drain_timeout=600.0, sleep=interrupt)

    record = read_operation(state)
    assert record is not None
    # Ctrl-C, SIGHUP and SIGTERM are the ordinary ways a wait of up to ten
    # minutes ends. Skipping the cancel on any of them leaves the engine
    # refusing every turn with nothing in the record to say why.
    assert record.phase == "failed"
    assert engine.posts[-1] == f"{BASE}/api/admin/drain/cancel"
    # The engine was never stopped, so the staged env is intact and the apply
    # can simply be run again.
    assert engine.launchctl_calls == []
    assert _env_version(engine.live_env) == FROM_VERSION


def test_apply_after_failed_apply_names_the_failure(tmp_path: Path) -> None:
    op, state, _, engine = _staged(tmp_path)
    op.phase = "failed"
    op.error = "drain timed out after 5s; the running engine was left untouched"
    write_operation(op, state)

    with pytest.raises(UpdateError, match="last apply failed: drain timed out after 5s"):
        _apply(engine, state)

    # Nothing was drained this time: the refusal is before the drain.
    assert engine.posts == []


def test_run_apply_preflight_refusal_reopens_admission(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    # The receipt the swap replaces is gone. The pre-flight refuses here,
    # which is the whole answer — but the foreground half has already closed
    # admission on the engine that is still running, and nothing else would
    # reopen it.
    receipt_path.unlink()

    result = _run(engine, op, state, receipt_path)

    assert result.phase == "failed"
    assert "no install receipt" in result.error
    assert engine.posts == [f"{BASE}/api/admin/drain/cancel"]
    # Nothing was touched: the engine is the install it was, still running.
    assert engine.starts == 0
    assert engine.launchctl_calls == []
    assert _env_version(engine.live_env) == FROM_VERSION


def test_run_apply_refuses_when_the_install_changed(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    # The user reinstalled between stage and apply. Applying would silently
    # downgrade the new install; rolling back would restore a receipt that no
    # longer describes what is on disk.
    write_receipt(
        InstallReceipt(
            version="1.3.0",
            executable=str(tmp_path / "bin" / "ciao"),
            python=str(_write_env(tmp_path / "tools" / "ciaobot", "1.3.0")),
            service_backend="launchd",
            service_label=SERVER_LABEL,
            installed_at="2026-09-26T09:00:00+00:00",
            uv="/fake/uv",
        ),
        receipt_path,
    )

    result = _run(engine, op, state, receipt_path)

    assert result.phase == "failed"
    assert "the installed engine is 1.3.0" in result.error
    assert f"staged from {FROM_VERSION}" in result.error
    assert "ciao update stage" in result.error
    assert engine.posts == [f"{BASE}/api/admin/drain/cancel"]
    # The install the user actually has is untouched.
    assert _env_version(engine.live_env) == "1.3.0"
    assert [c for c in engine.run_calls if "tool" in c] == []


def test_run_apply_rolls_back_when_the_record_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    real = engine_update.write_operation

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "tool" in argv:
            raise subprocess.CalledProcessError(1, argv, output="", stderr="uv: broken")
        return engine.uv_run(argv, **kwargs)

    def flaky(op_written: Operation, state_dir: Path | None = None) -> None:
        # A full disk — the usual cause of a failed record write, and often
        # the same one that broke the swap. It must not be able to stop the
        # rollback, because that is what leaves the env moved aside and the
        # engine down.
        if op_written.phase in {"rolling_back", "rolled_back", "rollback_failed"}:
            raise OSError(28, "no space left on device")
        real(op_written, state_dir)

    monkeypatch.setattr(engine_update, "write_operation", flaky)

    _run(engine, op, state, receipt_path, run=run)

    # Every step of the rollback ran, and the operator gets their engine back.
    assert _env_version(engine.live_env) == FROM_VERSION
    assert engine.starts == 1
    assert engine.up is True
    # The persisted record is the last phase that could be written, which is
    # what an operator reading `ciao update status` sees.
    persisted = read_operation(state)
    assert persisted is not None
    assert persisted.phase == "swapping"


def test_run_apply_applied_survives_a_failed_record_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    real = engine_update.write_operation

    def flaky(op_written: Operation, state_dir: Path | None = None) -> None:
        if op_written.phase == "applied":
            raise OSError(28, "no space left on device")
        real(op_written, state_dir)

    monkeypatch.setattr(engine_update, "write_operation", flaky)

    result = _run(engine, op, state, receipt_path)

    # The new engine passed readiness and is serving. A record write that
    # fails afterwards is bookkeeping, and must not undo an install that is
    # already up: the env is the new one, the receipt names it, and nothing
    # was restarted or rolled back.
    assert result.phase == "applied"
    assert result.error == ""
    assert _env_version(engine.live_env) == TO_VERSION
    written = read_receipt(receipt_path)
    assert written is not None
    assert written.version == TO_VERSION
    assert engine.starts == 1
    assert engine.up is True
    # One bootout of the engine, the one that starts the swap: a record write
    # that fails afterwards is bookkeeping and must not undo an install that is
    # already serving, and a second stop would be exactly that.
    assert (
        len(
            [
                call
                for call in engine.launchctl_calls
                if call[0] == "bootout" and call[-1].endswith(SERVER_LABEL)
            ]
        )
        == 1
    )


def test_rollback_waits_for_the_engine_to_stop_before_restarting(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    # The forward path stops the engine by waiting for it to stop answering.
    # The rollback used to boot it out and start it again about half a second
    # later, which is the same race `scripts/install.sh` works around: launchd
    # removes a job asynchronously, and a start landing in that window comes
    # back against a stale environment. The engine below keeps answering for
    # two probes after its bootout, so a rollback that does not wait starts it
    # while the old process is still there.
    engine.lingering_after_bootout = 2

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "tool" in argv:
            raise subprocess.CalledProcessError(1, argv, output="", stderr="uv: broken")
        return engine.uv_run(argv, **kwargs)

    # Whether the old process was still answering at the moment the start was
    # issued: that, not the flag the bootout set, is the race.
    started_while_up: list[bool] = []

    def start_service() -> SimpleNamespace:
        started_while_up.append(engine.get(f"{BASE}/api/startup-status") is not None)
        return engine.start_service()

    result = _run(engine, op, state, receipt_path, run=run, start_service=start_service)

    assert result.phase == "rolled_back"
    assert started_while_up == [False]
    assert _env_version(engine.live_env) == FROM_VERSION


def test_local_opener_bypasses_the_system_proxy() -> None:
    # urllib's default opener honours the macOS system proxy settings, so on a
    # Mac configured with one every loopback probe would go through a proxy
    # that may not resolve `localhost` — and from a rollback that reads as
    # "the restored engine never came back". An empty `ProxyHandler` registers
    # no proxy at all, so the opener only knows how to open a URL directly.
    opener = engine_update._LOCAL_OPENER
    assert not [
        h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)
    ]
    for protocol in ("http", "https"):
        assert not [
            h
            for h in opener.handle_open.get(protocol, [])
            if isinstance(h, urllib.request.ProxyHandler)
        ]
    # Both helpers default to it, so the proxy cannot be reintroduced for one
    # and forgotten for the other.
    for helper in (engine_update._post_json, engine_update._get_json):
        assert helper.__kwdefaults__["opener"] == opener.open  # type: ignore[index]


def test_default_fetch_caps_the_download_and_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"far more than the manifest described" * 1024

    class _Response(io.BytesIO):
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: Any) -> None:
            self.close()

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=0.0: _Response(payload)
    )
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))

    dest = tmp_path / "out" / "big.bin"
    with pytest.raises(UpdateError, match="larger than"):
        default_fetch("https://example.test/big.bin", dest, max_bytes=1024)

    # Not retried: the server is answering fine, it is just serving something
    # this release does not describe.
    assert slept == []
    assert not dest.exists()
    # The oversized bytes are dropped rather than left for a retry to find.
    assert list(dest.parent.glob("*.part")) == []


def test_cli_apply_rejects_an_unusable_drain_timeout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A timeout below the floor is a typo, and `inf` is worse than one: it
    # removes the deadline, so a drain that never completes keeps the engine
    # refusing turns with no upper bound on when that ends. Both are refused
    # as arguments, before anything is drained.
    for value in ("0", "-5", "0.5", "inf", "nan", "soon"):
        with pytest.raises(SystemExit):
            main(["apply", "--drain-timeout", value])
        assert "--drain-timeout" in capsys.readouterr().err, value

    # The default is still the long one, and the floor is accepted.
    assert engine_update._drain_timeout_arg("600") == 600.0
    assert engine_update._drain_timeout_arg("1") == 1.0


def test_cli_apply_reports_an_interrupted_apply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "ciao.package_version.detect_install_mode", lambda: "installer"
    )

    def interrupt(**kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(engine_update, "apply_update", interrupt)

    assert main(["apply"]) == 130

    assert "cancelled" in capsys.readouterr().err


def test_status_prints_new_phases(capsys: pytest.CaptureFixture[str]) -> None:
    write_operation(
        Operation(
            id=f"20260925T100000-{TO_VERSION}",
            phase="rolled_back",
            from_version=FROM_VERSION,
            to_version=TO_VERSION,
            started_at="2026-09-25T10:00:00+00:00",
            updated_at="2026-09-25T10:01:00+00:00",
            error="boom",
            stage_dir="/home/u/.local/state/ciaobot/updates/1.2.3",
        )
    )

    assert main(["status"]) == 0

    out = capsys.readouterr().out
    assert out.strip() == (
        f"rolled_back {TO_VERSION} in /home/u/.local/state/ciaobot/updates/{TO_VERSION}: boom"
    )


# ── startup recovery and the plist↔receipt pre-flight (#609) ───────────
#
# A reboot, a logout or a killed updater job can leave the record in a post-move
# phase with the env moved aside and nothing resuming it. Recovery is two halves
# of the same shape as the apply: the in-process entry (from `ciao/main.py`)
# recognises the record and bootstraps the detached job, and the job does the
# rollback. That split is the point — the engine cannot boot itself out and
# restore the env it is running out of — so the first test asserts the entry
# does exactly two launchctl calls and touches no file.
#
# launchd, the service starter, the engine's HTTP surface, the clock and sleep
# are all doubles, and the updater-loaded check is injected, so nothing here can
# reach a real launchd or move a real environment.


def test_recover_interrupted_apply_bootstraps_a_detached_recovery_job(
    tmp_path: Path,
) -> None:
    op, state, _, engine = _interrupted(tmp_path, "swapping")

    result = _handoff(engine, state)

    assert result is not None
    # The record is left exactly as it was: the job that owns it now, not this
    # process, and the operator sees a real outcome rather than a guess.
    assert result.phase == "swapping"
    assert read_operation(state) == result

    plist_path = state / UPDATER_PLIST_NAME
    assert [call[0] for call in engine.launchctl_calls] == ["bootout", "bootstrap"]
    assert engine.booted_out(UPDATER_LABEL)
    assert Path(engine.launchctl_calls[-1][-1]) == plist_path
    plist = plistlib.loads(plist_path.read_bytes())
    # The same one-shot, abandon-process-group job `apply_update` uses, in its
    # recovery role — the one that survives the bootout of the engine.
    assert plist["Label"] == UPDATER_LABEL
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is False
    assert plist["AbandonProcessGroup"] is True
    assert plist["ProgramArguments"] == [
        op.env_python,
        "-I",
        "-m",
        "ciao.engine_update",
        "run-recover",
        "--operation",
        op.id,
    ]

    # Nothing was stopped and nothing was moved in the engine's own process: it
    # is the job that boots `com.ciao.server` out and puts the env back.
    assert not engine.booted_out(SERVER_LABEL)
    assert engine.starts == 0
    assert engine.up is True
    assert not engine.live_env.exists()
    assert (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()


@pytest.mark.parametrize(
    "phase", ["staged", "applying", "stopping", "applied", "failed", "rolled_back"]
)
def test_recover_interrupted_apply_is_a_noop_for_terminal_or_pre_move_phases(
    tmp_path: Path, phase: str
) -> None:
    _, state, _, engine = _staged(tmp_path, phase=phase)

    assert _handoff(engine, state) is None

    # `stopping` is in this list on purpose: the env has not moved yet, and a
    # rollback that "restored" it would delete the only working copy of the
    # engine. The terminal phases are outcomes, not interrupted transactions.
    assert engine.launchctl_calls == []
    assert not (state / UPDATER_PLIST_NAME).exists()
    assert _env_version(engine.live_env) == FROM_VERSION
    record = read_operation(state)
    assert record is not None
    assert record.phase == phase


def test_recover_interrupted_apply_defers_to_a_loaded_updater(tmp_path: Path) -> None:
    op, state, _, engine = _interrupted(tmp_path, "swapping")

    # The injected double answers "a swap is running" — the check reads the
    # loaded job's pid, not merely that launchd still knows the job, because a
    # one-shot job stays loaded after it dies
    # (`test_run_recover_recovers_after_a_killed_updater_job` is the other half).
    result = _handoff(engine, state, updater_loaded=lambda: True)

    # The engine being up says nothing about whether the apply is still running:
    # the swap belongs to a sibling job that may well be mid-swap right now, and
    # two rollbacks over one env is the race recovery exists to prevent.
    assert result is not None
    assert result.phase == "swapping"
    assert read_operation(state) == result
    assert engine.launchctl_calls == []
    assert not (state / UPDATER_PLIST_NAME).exists()
    assert engine.up is True
    assert (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()
    assert not engine.live_env.exists()


def test_recover_interrupted_apply_records_a_job_that_will_not_load(
    tmp_path: Path,
) -> None:
    op, state, _, engine = _interrupted(tmp_path, "swapping")

    def launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
        engine.launchctl_calls.append(list(args))
        if args[0] == "bootstrap":
            return subprocess.CompletedProcess(args, 113, "", "Load failed: 5: Input/output error")
        return subprocess.CompletedProcess(args, 0, "", "")

    result = _handoff(engine, state, launchctl=launchctl)

    # The phase stays an interrupted swap, so the next boot tries again, and the
    # reason is on the record because a record that says nothing is what leaves
    # an operator guessing why nothing happened.
    assert result is not None
    assert result.phase == "swapping"
    assert "recovery job did not start" in result.error
    assert "Input/output error" in result.error
    assert read_operation(state) == result
    assert not engine.booted_out(SERVER_LABEL)
    assert not engine.live_env.exists()


def test_recover_apply_rolls_back_an_interrupted_swap(
    tmp_path: Path, phases: list[str]
) -> None:
    op, state, receipt_path, engine = _interrupted(tmp_path, "swapping")
    previous_receipt = Path(op.previous_receipt).read_bytes()

    result = _recover_apply(engine, op, state, receipt_path)

    assert result.phase == "rolled_back"
    # The record says this was a recovery, so an operator reading
    # `ciao update status` is not told an update was applied.
    assert "interrupted during recovery" in result.error
    assert f"rolled back to {FROM_VERSION}" in result.error
    assert read_operation(state) == result
    assert phases[-2:] == ["rolling_back", "rolled_back"]

    # The engine was stopped first, the way the forward path stops it: its
    # install watcher would otherwise relaunch into a half-swapped env.
    assert engine.booted_out(SERVER_LABEL)
    # The env on disk is the install the operator was running before the swap.
    assert _env_version(engine.live_env) == FROM_VERSION
    assert not (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()
    # The receipt names that install again, and the operator gets a running
    # engine back rather than a stopped one.
    assert receipt_path.read_bytes() == previous_receipt
    assert engine.starts == 1
    assert engine.up is True
    # Recovery restores; it never re-runs the swap (that is what `uv` is for).
    assert [c for c in engine.run_calls if "tool" in c] == []


def test_recover_apply_reports_a_rollback_failure(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _interrupted(tmp_path, "swapping")
    # The restore is the one move a recovery cannot survive losing, so simulate
    # exactly that: the move back into place fails while everything unrelated
    # still works. Recovery runs unattended, so the outcome has to be a record
    # the operator can act on, not an exception nobody is left to read.
    (tmp_path / "gone").write_text("not a directory", encoding="utf-8")
    write_receipt(
        InstallReceipt(
            version=FROM_VERSION,
            executable=str(tmp_path / "bin" / "ciao"),
            python=str(tmp_path / "gone" / "ciaobot" / "bin" / "python"),
            service_backend="launchd",
            service_label=SERVER_LABEL,
            installed_at="2026-09-25T16:00:00+00:00",
            uv="/fake/uv",
        ),
        receipt_path,
    )

    result = _recover_apply(engine, op, state, receipt_path)

    assert result.phase == "rollback_failed"
    assert "restore the previous env" in result.error
    assert read_operation(state) == result
    # Still a returned record rather than an exception, and a running engine
    # attempted: a stopped engine is a worse outcome than a broken one.
    assert engine.starts == 1


def test_recover_apply_reports_missing_receipt(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _interrupted(tmp_path, "swapping")
    # The receipt the rollback is built from is gone, so there is no live env to
    # put back. The reason belongs on the record, and the files stay as they
    # are: nothing is touched on the way to that answer.
    receipt_path.unlink()

    result = _recover_apply(engine, op, state, receipt_path)

    assert result.phase == "rollback_failed"
    assert "no install receipt" in result.error
    assert read_operation(state) == result
    # Nothing was launched, stopped or started: this branch only reports. The
    # record it left is terminal, so the agent's next tick stands down over it
    # and retires itself.
    assert engine.changed_jobs() == []
    assert engine.starts == 0
    assert (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()
    assert not engine.live_env.exists()


def test_run_apply_refuses_plist_that_runs_another_env(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    # A Ciaobot.app installed over a terminal install, or a hand-edited plist:
    # the receipt names one env and the loaded job runs another, so the swap
    # would replace an env the service is not using.
    program = tmp_path / "elsewhere" / "ciaobot" / "bin" / "python"
    _write_server_plist(program)

    result = _run(engine, op, state, receipt_path)

    assert result.phase == "failed"
    assert "not the receipt's environment" in result.error
    # Both sides of the disagreement are named, so the operator knows which
    # interpreter is loaded and which env the receipt is about.
    assert str(program) in result.error
    assert str(engine.live_env) in result.error
    assert read_operation(state) == result
    # Admission is reopened: the foreground half already closed it, and nothing
    # else would.
    assert engine.posts == [f"{BASE}/api/admin/drain/cancel"]
    # Nothing was stopped and nothing was moved — the refusal is the pre-flight's
    # whole answer, so there is nothing to roll back. The only launchctl call is
    # the pre-flight's own read-only question about the loaded job.
    assert engine.changed_jobs() == []
    assert not engine.booted_out(SERVER_LABEL)
    assert engine.starts == 0
    assert _env_version(engine.live_env) == FROM_VERSION
    assert [c for c in engine.run_calls if "tool" in c] == []


def test_run_apply_allows_a_missing_server_plist(
    tmp_path: Path, phases: list[str]
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    # A service that has not been loaded yet is not a disagreement: the
    # rollback's own start step is what registers it again.
    assert not _server_plist_path().exists()

    result = _run(engine, op, state, receipt_path)

    assert result.phase == "applied"
    assert read_operation(state) == result
    # The check ran before the stop, so the apply got past it: the first phase
    # `run_apply` itself writes is `stopping`, and the bootout that goes with it
    # happened.
    assert phases[0] == "stopping"
    assert engine.booted_out(SERVER_LABEL)
    assert engine.starts == 1


# ── the durable recovery agent and the loaded-job pre-flight (review round 1) ──
#
# Two things the first round of this issue got wrong, both about *where* a
# recovery runs rather than *what* it does. The window the recovery exists for
# begins when the live env is renamed aside, and at that point
# `com.ciao.server`'s program is a file that does not exist: launchd cannot
# start the engine, so the startup hook that was supposed to finish the swap
# cannot run at all. The net under a swap therefore has to be launchd's own
# job, installed before the engine is ever stopped and re-checked on an
# interval — a `com.ciao.recover` LaunchAgent that runs the same `run-recover`
# from the staged interpreter. And once it is that job, the answers it gives
# while a swap is in flight are its own: standing down, posting nothing, and not
# retiring itself over the one window it exists for.
#
# launchd, the service starter, the engine's HTTP surface, the clock and sleep
# are doubles throughout, and `acquire_lock` in these tests is the real lock —
# no real launchd, no real env and no socket is touched.


def test_apply_installs_a_durable_recovery_agent(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _staged(tmp_path)

    _apply(engine, state)

    # Installed behind the updater, before `run_apply` stops anything, and from
    # the staged env: the swap replaces the live one, so a recovery that ran out
    # of it would be fixing the fire with the fuel.
    recover_plist = state / RECOVER_PLIST_NAME
    assert recover_plist.is_file()
    assert stat.S_IMODE(recover_plist.stat().st_mode) == 0o600
    plist = plistlib.loads(recover_plist.read_bytes())
    assert plist["Label"] == RECOVER_LABEL
    assert plist["ProgramArguments"] == [
        op.env_python,
        "-I",
        "-m",
        "ciao.engine_update",
        "run-recover",
        "--operation",
        op.id,
    ]
    # Loaded and then *re-checked*: unlike the one-shot updater, this job has to
    # still be there after the reboot, the logout and the death of the job that
    # installed it. A one-shot `RunAtLoad` would not be.
    assert plist["RunAtLoad"] is True
    assert plist["StartInterval"] == 30
    # A failed tick must not become a relaunch loop, and the engine's bootout
    # must not take the net down with it.
    assert plist["KeepAlive"] is False
    assert plist["AbandonProcessGroup"] is True
    # Its own log beside the updater's: two jobs writing into one file would
    # interleave their output into something nobody can read.
    assert plist["StandardOutPath"] == str(Path(op.stage_dir) / "recover.log")
    assert plist["StandardErrorPath"] == str(Path(op.stage_dir) / "recover.log")

    # A job registered with `bootstrap` lives in launchd's database for this
    # login only; the login-time scan — the one thing that re-registers agents
    # after a reboot or a logout — reads the LaunchAgents directory and nothing
    # else. Without this second copy the plist above describes a net that
    # disappears with the crash it was installed for.
    login_plist = _login_recover_plist()
    assert login_plist.is_file()
    assert login_plist.read_bytes() == recover_plist.read_bytes()
    assert stat.S_IMODE(login_plist.stat().st_mode) == 0o600


def test_apply_installs_the_recovery_agent_before_the_engine_is_stopped(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path)

    _apply(engine, state)
    # On disk from the moment the apply hands off, so the swap below runs with the
    # net in place and takes it away again itself.
    assert (state / RECOVER_PLIST_NAME).is_file()
    result = _run(engine, op, state, receipt_path)

    assert result.phase == "applied"
    calls = engine.launchctl_calls

    def where(action: str, endswith: str) -> int:
        return next(
            i
            for i, call in enumerate(calls)
            if call[0] == action and call[-1].endswith(endswith)
        )

    # The whole point of the ordering: the net is in place before the engine is
    # stopped, because the crash window it covers is one where nothing inside the
    # engine can run to notice.
    assert where("bootstrap", RECOVER_PLIST_NAME) < where("bootout", SERVER_LABEL)


def test_a_successful_swap_retires_the_recovery_agent(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _staged(tmp_path)

    _apply(engine, state)
    assert (state / RECOVER_PLIST_NAME).is_file()
    result = _run(engine, op, state, receipt_path)

    assert result.phase == "applied"
    # The update is a fact about the machine, not a transaction to be recovered,
    # so the job is booted out *and* both of its plists deleted: leaving either
    # behind means the next 30 seconds — or the next login — re-reads a settled
    # record.
    assert engine.booted_out(RECOVER_LABEL)
    assert not (state / RECOVER_PLIST_NAME).exists()
    assert not _login_recover_plist().exists()
    retired = max(
        i
        for i, call in enumerate(engine.launchctl_calls)
        if call[0] == "bootout" and call[-1].endswith(RECOVER_LABEL)
    )
    stopped = next(
        i
        for i, call in enumerate(engine.launchctl_calls)
        if call[0] == "bootout" and call[-1].endswith(SERVER_LABEL)
    )
    assert stopped < retired
    # The agent's own retirement is not a rollback, and must not look like one.
    assert engine.starts == 1
    assert _env_version(engine.live_env) == TO_VERSION


def test_a_rolled_back_swap_retires_the_recovery_agent(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    _install_recovery_agent(state, op)

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "tool" in argv:
            raise subprocess.CalledProcessError(1, argv, output="", stderr="uv: broken")
        return engine.uv_run(argv, **kwargs)

    result = _run(engine, op, state, receipt_path, run=run)

    assert result.phase == "rolled_back"
    # The env is whole again and the record says so, so the net has nothing left
    # to guard.
    assert engine.booted_out(RECOVER_LABEL)
    assert not (state / RECOVER_PLIST_NAME).exists()
    assert not _login_recover_plist().exists()


def test_run_recover_rolls_back_a_swap_with_no_live_env(
    tmp_path: Path, phases: list[str]
) -> None:
    # The crash window this whole job exists for: `live_env` is renamed to
    # `previous-env` and `uv` has not recreated it, so the engine's own program
    # is missing and nothing inside the engine could start. Asserted from the
    # recovery command's own entry, as the durable agent runs it.
    op, state, receipt_path, engine = _interrupted(tmp_path, "swapping")
    _install_recovery_agent(state, op)
    assert not engine.live_env.exists()
    posts: list[str] = []

    result = _recover_apply(
        engine, op, state, receipt_path, http_post=_recorder(posts)
    )

    assert result is not None
    assert result.phase == "rolled_back"
    assert "interrupted during recovery" in result.error
    assert read_operation(state) == result
    assert phases == ["rolling_back", "rolled_back"]
    # The install the operator was running is back, and they get a running engine.
    assert _env_version(engine.live_env) == FROM_VERSION
    assert not (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()
    assert engine.starts == 1
    assert engine.up is True
    # It never drained anything, so it cancels nothing: a cancel here would
    # reopen an admission it never closed, while a swap may be draining.
    assert posts == []
    # And it retires itself, so the next login does not load a net for a
    # transaction that is over.
    assert engine.booted_out(RECOVER_LABEL)
    assert not (state / RECOVER_PLIST_NAME).exists()
    assert not _login_recover_plist().exists()


def test_run_recover_stands_down_while_a_swap_is_in_flight(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _interrupted(tmp_path, "swapping")
    _install_recovery_agent(state, op)
    posts: list[str] = []
    # A live swap holds the lock, which is the ordinary state of the world for
    # the first tick after the agent is installed.
    handle = acquire_lock(state)
    try:
        result = _recover_apply(
            engine,
            op,
            state,
            receipt_path,
            launchctl=_running_updater(engine, running=True),
            http_post=_recorder(posts),
        )
    finally:
        release_lock(handle)

    assert result is None
    # Nothing is rolled back over a swap that is still running: that is the race
    # the durable agent exists beside, not inside.
    assert not engine.booted_out(SERVER_LABEL)
    assert engine.starts == 0
    assert engine.up is True
    assert not engine.live_env.exists()
    assert (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()
    # The record belongs to the swap in flight: not read, not written.
    assert read_operation(state) == op
    # No cancel, even though a drain may be open: it is not this job's to close.
    assert posts == []
    # And the net stays loaded, because this tick is the *normal* case and the
    # next one is 30 seconds away. Retiring here would leave no net at all for
    # the crash the agent was installed to catch.
    assert (state / RECOVER_PLIST_NAME).exists()
    assert not engine.booted_out(RECOVER_LABEL)


def test_run_recover_recovers_after_a_killed_updater_job(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _interrupted(tmp_path, "swapping")
    _install_recovery_agent(state, op)
    # The crash the durable agent is installed for: the updater job died, and a
    # one-shot LaunchAgent stays *loaded* in launchd after its process exits. A
    # check that read "loaded" as "running" would stand down here for ever, and
    # the machine would stay exactly as broken as it is now. Its lock went with
    # the process, so recovery owns the record.
    result = _recover_apply(
        engine,
        op,
        state,
        receipt_path,
        launchctl=_running_updater(engine, running=False),
    )

    assert result is not None
    assert result.phase == "rolled_back"
    assert _env_version(engine.live_env) == FROM_VERSION
    assert engine.starts == 1


def test_run_recover_stands_down_for_a_running_updater(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _interrupted(tmp_path, "swapping")
    _install_recovery_agent(state, op)
    # The belt to the lock: `run_apply` holds the lock for its whole swap, so an
    # updater that is *running* while this job holds it means the two are not the
    # process one would assume. Rolling back over a live swap is the one thing
    # this job must never do, so it stands down and tries again in 30 seconds.
    result = _recover_apply(
        engine,
        op,
        state,
        receipt_path,
        launchctl=_running_updater(engine, running=True),
    )

    assert result is None
    assert read_operation(state) == op
    assert not engine.booted_out(SERVER_LABEL)
    assert engine.starts == 0
    assert not engine.live_env.exists()
    assert (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()
    # Standing down is not standing down for good: the net is still there.
    assert (state / RECOVER_PLIST_NAME).exists()
    assert not engine.booted_out(RECOVER_LABEL)


@pytest.mark.parametrize("settled", ["applied", "rollback_failed"])
def test_run_recover_stands_down_for_a_settled_record(
    tmp_path: Path, settled: str
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    op.phase = settled
    op.error = "already over"
    write_operation(op, state)
    _install_recovery_agent(state, op)
    posts: list[str] = []

    result = _recover_apply(
        engine, op, state, receipt_path, http_post=_recorder(posts)
    )

    # The transaction settled itself while the agent was starting. A rollback now
    # would undo an outcome the operator already has.
    assert result is None
    assert read_operation(state) == op
    assert posts == []
    assert not engine.booted_out(SERVER_LABEL)
    assert engine.starts == 0
    assert _env_version(engine.live_env) == FROM_VERSION
    # Nothing left to recover, so the net comes down: bootout *and* the plists, or
    # the next login loads a job for a transaction that is over.
    assert engine.changed_jobs() == [["bootout", f"gui/501/{RECOVER_LABEL}"]]
    assert not (state / RECOVER_PLIST_NAME).exists()
    assert not _login_recover_plist().exists()


@pytest.mark.parametrize("wrong_id", [True, False], ids=["wrong-id", "no-record"])
def test_recover_apply_ignores_a_missing_or_wrong_operation(
    tmp_path: Path, wrong_id: bool
) -> None:
    op, state, receipt_path, engine = _interrupted(tmp_path, "swapping")
    _install_recovery_agent(state, op)
    if wrong_id:
        # A newer update has taken the record over, so the record describes a
        # different operation than the plist names. `op` itself is left alone:
        # it is what the recovery command is asked for.
        write_operation(replace(op, id="20260101T000000-9.9.9"), state)
    else:
        (state / OPERATION_NAME).unlink()
    posts: list[str] = []

    result = _recover_apply(
        engine, op, state, receipt_path, http_post=_recorder(posts)
    )

    # A record this job has no business rewriting, and no record at all, are the
    # same answer: there is nothing here to undo. No phase of its own, no
    # exception, and — because it never drained anything — no cancel POST that
    # would reopen an admission belonging to whatever update is in flight.
    assert result is None
    assert posts == []
    assert not engine.booted_out(SERVER_LABEL)
    assert engine.starts == 0
    assert engine.up is True
    assert not engine.live_env.exists()
    assert (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()
    if wrong_id:
        record = read_operation(state)
        assert record is not None
        assert record.id == "20260101T000000-9.9.9"
    else:
        assert read_operation(state) is None
    # The plist named an operation that is not there, so it is stale. Both
    # copies go: a login-time copy left behind would load a job for a
    # transaction that is over.
    assert engine.changed_jobs() == [["bootout", f"gui/501/{RECOVER_LABEL}"]]
    assert not (state / RECOVER_PLIST_NAME).exists()
    assert not _login_recover_plist().exists()


@pytest.mark.parametrize("phase", ["swapping", "starting", "verifying_start"])
@pytest.mark.parametrize(
    "new_receipt", [False, True], ids=["old-receipt", "new-receipt"]
)
def test_run_recover_replaces_a_recreated_live_env(
    tmp_path: Path, phase: str, new_receipt: bool
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase=phase)
    previous_receipt = Path(op.previous_receipt).read_bytes()
    # `uv` got as far as recreating the env, which is the crash this is about:
    # the old install is retained under `previous-env` *and* a new live env is on
    # disk, so "is the live env gone?" cannot tell whether the move happened.
    engine_update._move_env(engine.live_env, Path(op.stage_dir) / PREVIOUS_ENV_NAME)
    _write_env(engine.live_env, TO_VERSION)
    installed = read_receipt(receipt_path)
    assert installed is not None
    if new_receipt:
        # `run_apply` rewrites the receipt at `starting`, so a crash after that
        # leaves the new version named by both the receipt and the env on disk.
        write_receipt(
            replace(installed, version=TO_VERSION, previous_version=FROM_VERSION),
            receipt_path,
        )

    result = _recover_apply(engine, op, state, receipt_path)

    # The retained previous env is the evidence the move completed, so the
    # half-installed env is replaced rather than left in place beside a receipt
    # that no longer describes it.
    assert result is not None
    assert result.phase == "rolled_back"
    assert _env_version(engine.live_env) == FROM_VERSION
    assert not (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()
    assert receipt_path.read_bytes() == previous_receipt
    assert engine.starts == 1
    assert engine.up is True


def test_recover_interrupted_apply_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, state, _, engine = _staged(tmp_path, phase="swapping")

    def exploding_reader(state_dir: Path | None = None) -> Operation:
        raise OSError("the state dir is unreadable in a way nobody predicted")

    monkeypatch.setattr(engine_update, "read_operation", exploding_reader)

    # This runs inside the engine's startup, before the server binds. A
    # bookkeeping failure there must not be a reason the engine does not come
    # up, so the answer is "nothing was recovered" rather than an exception.
    assert _handoff(engine, state) is None
    assert engine.launchctl_calls == []
    assert not (state / UPDATER_PLIST_NAME).exists()
    assert not (state / RECOVER_PLIST_NAME).exists()
    assert _env_version(engine.live_env) == FROM_VERSION
    assert engine.starts == 0
    assert engine.up is True


def test_run_apply_refuses_when_the_loaded_job_runs_another_env(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    # The plist on disk agrees with the receipt: a reinstall, or an operator who
    # re-pointed the file. launchd loaded the job before that edit, so the
    # service actually running is the stale one, and reading the file would let
    # the swap replace an env nothing is using.
    _write_server_plist(engine.live_env / "bin" / "python")
    program = tmp_path / "elsewhere" / "ciaobot" / "bin" / "python"

    result = _run(
        engine, op, state, receipt_path, launchctl=_loaded_job(engine, program)
    )

    assert result.phase == "failed"
    assert "not the receipt's environment" in result.error
    assert str(program) in result.error
    assert str(engine.live_env) in result.error
    assert read_operation(state) == result
    assert engine.posts == [f"{BASE}/api/admin/drain/cancel"]
    assert engine.changed_jobs() == []
    assert not engine.booted_out(SERVER_LABEL)
    assert engine.starts == 0
    assert _env_version(engine.live_env) == FROM_VERSION
    assert [c for c in engine.run_calls if "tool" in c] == []


def test_run_apply_allows_a_loaded_job_that_runs_the_receipt_env(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    # The reverse disagreement: the plist on disk is the stale one, and the job
    # launchd is running uses the env the receipt names. That is the service the
    # swap exists for, so refusing would break an install the operator has
    # already put right.
    _write_server_plist(tmp_path / "elsewhere" / "ciaobot" / "bin" / "python")

    result = _run(
        engine,
        op,
        state,
        receipt_path,
        launchctl=_loaded_job(engine, engine.live_env / "bin" / "python"),
    )

    assert result.phase == "applied"
    assert read_operation(state) == result
    assert _env_version(engine.live_env) == TO_VERSION
    assert engine.booted_out(SERVER_LABEL)
    assert engine.starts == 1


@pytest.mark.parametrize(
    "printed,expected",
    [
        (
            "com.ciao.server = {\n\tprogram = /a/b/bin/python\n\targuments = {\n\t\t-m\n\t}\n}",
            "/a/b/bin/python",
        ),
        (
            "com.ciao.server = {\n\tprogram = ( /a/b/bin/python -m ciao.main )\n}",
            "/a/b/bin/python",
        ),
        ("com.ciao.server = {\n\tprogram = {\n\t\t/a/b/bin/python\n\t}\n}", "/a/b/bin/python"),
        ("com.ciao.server = {\n\tstate = running\n\tpid = 7\n}", None),
        ("", None),
    ],
)
def test_loaded_program_argument_reads_launchctl_output(
    printed: str, expected: str | None
) -> None:
    # The formats launchd has used for a loaded job's program, and the two answers
    # that must never refuse an update: no program, and no output at all.
    assert engine_update._loaded_program_argument(printed) == expected
