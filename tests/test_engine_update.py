from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ciao.engine_update import (
    UpdateError,
    UpdateInProgress,
    acquire_lock,
    default_fetch,
    find_uv,
    main,
    read_operation,
    release_lock,
    stage_update,
)
from ciao.install_receipt import InstallReceipt, write_receipt
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

    def serve(self, url: str, dest: Path) -> None:
        """A `fetch` that resolves a release URL into the fixture directory."""
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
