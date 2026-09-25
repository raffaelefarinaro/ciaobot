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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import ciao.engine_update as engine_update
from ciao.engine_update import (
    PREVIOUS_ENV_NAME,
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
    release_lock,
    run_apply,
    stage_update,
    write_operation,
)
from ciao import install_receipt
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

    # -- the engine's HTTP surface ----------------------------------------
    def version(self) -> str:
        return _env_version(self.live_env)

    def post(self, url: str) -> dict[str, Any]:
        self.posts.append(url)
        return {}

    def get(self, url: str) -> dict[str, Any] | None:
        if url.endswith("/api/active-chats"):
            self.polls += 1
            if self.active_script:
                return {"active_chat_ids": self.active_script.pop(0)}
            return {"active_chat_ids": ["still-busy"] if self.busy else []}
        if not self.up:
            return None
        if self.readiness is not None:
            return self.readiness(self.starts)
        return {"version": self.version(), "overall_ready": True}

    # -- launchd, uv and the service ------------------------------------
    def launchctl(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.launchctl_calls.append(list(args))
        if args[0] == "bootout" and args[-1].endswith(SERVER_LABEL) and not self.stubborn:
            self.up = False
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
        """
        self.run_calls.append(list(argv))
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
        http_get=engine.get,
        launchctl=engine.launchctl,
        uid=501,
        sleep=lambda seconds: None,
        clock=kwargs.pop("clock", _FakeClock()),
        **kwargs,
    )


def _run(engine: _FakeEngine, op: Operation, state: Path, receipt_path: Path, **kwargs: Any) -> Operation:
    return run_apply(
        op.id,
        state_dir=state,
        port=PORT,
        http_get=engine.get,
        launchctl=engine.launchctl,
        uv="/fake/uv",
        run=kwargs.pop("run", engine.uv_run),
        start_service=engine.start_service,
        uid=501,
        sleep=lambda seconds: None,
        clock=kwargs.pop("clock", _FakeClock()),
        receipt_path=receipt_path,
        **kwargs,
    )


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

    assert engine.posts == [f"{BASE}/api/admin/drain"]
    assert result.phase == "applying"
    assert read_operation(state) == result
    # Two readings with work in flight, then exactly three settled ones: a
    # single empty reading is not a drained engine.
    assert engine.polls == 5

    # Bootout before bootstrap, both against the updater label, and the plist
    # handed to launchctl is the one on disk.
    assert [call[0] for call in engine.launchctl_calls] == ["bootout", "bootstrap"]
    assert engine.booted_out(UPDATER_LABEL)
    plist_path = Path(engine.launchctl_calls[-1][-1])
    assert plist_path == state / UPDATER_PLIST_NAME
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
    # update it was waiting for.
    assert engine.posts == [f"{BASE}/api/admin/drain", f"{BASE}/api/admin/drain/cancel"]
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
