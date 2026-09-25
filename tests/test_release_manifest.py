from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ciao.release_manifest import (
    RELEASE_PUBLIC_KEY,
    SignatureError,
    artifact_entry,
    build_manifest,
    main,
    missing_static_assets,
    parse_public_key,
    verify_manifest,
    verify_signature,
)

TRUSTED = "timestamp:1\tfile:ciaobot-engine-manifest.json"


def _keypair() -> tuple[Ed25519PrivateKey, str, bytes]:
    """A throwaway minisign key: (private key, public key text, key id)."""
    private_key = Ed25519PrivateKey.generate()
    key_id = b"\x01" * 8
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_key, base64.b64encode(b"Ed" + key_id + public_raw).decode(), key_id


def _sign(
    data: bytes,
    priv: Ed25519PrivateKey,
    key_id: bytes,
    *,
    prehashed: bool = True,
    trusted: str = TRUSTED,
    wrap: bool = False,
) -> str:
    """Minisign text over ``data``, in either algorithm, optionally base64-wrapped."""
    algorithm = b"ED" if prehashed else b"Ed"
    message = hashlib.blake2b(data, digest_size=64).digest() if prehashed else data
    signature = priv.sign(message)
    global_sig = priv.sign(signature + trusted.encode())
    text = (
        "untrusted comment: test\n"
        f"{base64.b64encode(algorithm + key_id + signature).decode()}\n"
        f"trusted comment: {trusted}\n"
        f"{base64.b64encode(global_sig).decode()}\n"
    )
    return base64.b64encode(text.encode()).decode() if wrap else text


def test_artifact_entry_hashes_and_sizes_file(tmp_path: Path) -> None:
    payload = b"ciaobot engine wheel" * 1000
    wheel = tmp_path / "ciaobot-1.2.3-py3-none-any.whl"
    wheel.write_bytes(payload)

    entry = artifact_entry(wheel, kind="wheel")

    assert entry["filename"] == wheel.name
    assert entry["kind"] == "wheel"
    assert entry["platform"] == "any"
    assert entry["arch"] == "any"
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entry["size"] == len(payload)


def test_build_manifest_shape() -> None:
    manifest = build_manifest(
        "1.2.3", [{"filename": "ciaobot-1.2.3-py3-none-any.whl"}], created="2026-09-25T10:00:00+00:00"
    )

    assert set(manifest) == {"schema", "version", "tag", "created", "artifacts"}
    assert manifest["version"] == "1.2.3"
    assert manifest["tag"] == "v1.2.3"
    assert manifest["schema"] == 1
    assert manifest["created"] == "2026-09-25T10:00:00+00:00"


def test_build_manifest_rejects_bad_version_and_empty_artifacts() -> None:
    with pytest.raises(ValueError):
        build_manifest("1.2", [{"filename": "x.whl"}], created="now")
    with pytest.raises(ValueError):
        build_manifest("1.2.3", [], created="now")


def test_verify_accepts_prehashed_signature() -> None:
    priv, public_key, key_id = _keypair()
    signature = _sign(b"manifest bytes", priv, key_id, prehashed=True)

    assert verify_signature(b"manifest bytes", signature, public_key) == TRUSTED


def test_verify_accepts_pure_signature() -> None:
    priv, public_key, key_id = _keypair()
    signature = _sign(b"manifest bytes", priv, key_id, prehashed=False)

    assert verify_signature(b"manifest bytes", signature, public_key) == TRUSTED


def test_verify_accepts_tauri_base64_wrapped_signature() -> None:
    priv, public_key, key_id = _keypair()
    signature = _sign(b"manifest bytes", priv, key_id, wrap=True)

    assert verify_signature(b"manifest bytes", signature, public_key) == TRUSTED


def test_verify_rejects_tampered_data() -> None:
    priv, public_key, key_id = _keypair()
    signature = _sign(b"a", priv, key_id)

    with pytest.raises(SignatureError):
        verify_signature(b"b", signature, public_key)


def test_verify_rejects_tampered_trusted_comment() -> None:
    priv, public_key, key_id = _keypair()
    lines = _sign(b"manifest bytes", priv, key_id).splitlines()
    lines[2] = "trusted comment: timestamp:2\tfile:other.json"
    edited = "\n".join(lines) + "\n"

    with pytest.raises(SignatureError):
        verify_signature(b"manifest bytes", edited, public_key)


def test_verify_rejects_other_key_id() -> None:
    priv, _, key_id = _keypair()
    signature = _sign(b"manifest bytes", priv, key_id)
    other_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    other_key = base64.b64encode(b"Ed" + b"\x02" * 8 + other_raw).decode()

    with pytest.raises(SignatureError):
        verify_signature(b"manifest bytes", signature, other_key)


def test_verify_rejects_garbage() -> None:
    with pytest.raises(SignatureError):
        verify_signature(b"manifest bytes", "not a signature", RELEASE_PUBLIC_KEY)


def test_embedded_release_key_matches_installer_verifier() -> None:
    key_id, key_bytes = parse_public_key(RELEASE_PUBLIC_KEY)
    assert len(key_id) == 8
    assert len(key_bytes) == 32

    source = (
        Path(__file__).parents[1] / "desktop" / "installer-verify" / "src" / "main.rs"
    ).read_text(encoding="utf-8")
    literal = re.search(r'const PUBLIC_KEY: &str = "(.*?)";', source)
    assert literal, "desktop/installer-verify/src/main.rs no longer defines PUBLIC_KEY"
    assert literal.group(1).rsplit("\\n", 1)[-1] == RELEASE_PUBLIC_KEY


def test_verify_manifest_round_trip_and_schema_checks() -> None:
    priv, public_key, key_id = _keypair()
    document = build_manifest(
        "1.2.3",
        [{"filename": "ciaobot-1.2.3-py3-none-any.whl", "kind": "wheel"}],
        created="2026-09-25T10:00:00+00:00",
    )
    raw = json.dumps(document).encode()
    signature = _sign(raw, priv, key_id)

    assert verify_manifest(raw, signature, public_key)["version"] == "1.2.3"

    wrong_schema = json.dumps({"schema": 2, "version": "1.2.3", "artifacts": [{}]}).encode()
    with pytest.raises(ValueError):
        verify_manifest(wrong_schema, _sign(wrong_schema, priv, key_id), public_key)


def test_missing_static_assets(tmp_path: Path) -> None:
    static = tmp_path / "static"

    assert missing_static_assets(static) == ["index.html"]

    assets = static / "assets"
    assets.mkdir(parents=True)
    (assets / "a.js").write_text("a", encoding="utf-8")
    (static / "index.html").write_text(
        '<html><head><link href="/assets/b.css"></head><body>'
        '<script src="/assets/a.js"></script></body></html>',
        encoding="utf-8",
    )

    assert missing_static_assets(static) == ["/assets/b.css"]

    (assets / "b.css").write_text("b", encoding="utf-8")

    assert missing_static_assets(static) == []


def test_main_build_writes_manifest_and_rejects_wrong_version(tmp_path: Path) -> None:
    wheel = tmp_path / "ciaobot-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    out = tmp_path / "ciaobot-engine-manifest.json"

    assert main(["build", "--version", "1.2.3", "--out", str(out), str(wheel)]) == 0

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["version"] == "1.2.3"
    assert written["artifacts"][0]["kind"] == "wheel"
    assert written["artifacts"][0]["filename"] == wheel.name

    assert main(["build", "--version", "1.2.4", "--out", str(out), str(wheel)]) == 1


def test_main_verify_cli(tmp_path: Path) -> None:
    priv, public_key, key_id = _keypair()
    manifest = tmp_path / "ciaobot-engine-manifest.json"
    raw = build_manifest(
        "1.2.3", [{"filename": "ciaobot-1.2.3-py3-none-any.whl"}], created="2026-09-25T10:00:00+00:00"
    )
    manifest.write_bytes(json.dumps(raw).encode())
    signature = tmp_path / "ciaobot-engine-manifest.json.sig"
    signature.write_text(_sign(manifest.read_bytes(), priv, key_id), encoding="utf-8")

    assert main(
        ["verify", str(manifest), str(signature), "--public-key", public_key]
    ) == 0

    manifest.write_bytes(b'{"schema": 1}')
    assert main(
        ["verify", str(manifest), str(signature), "--public-key", public_key]
    ) == 1


def test_main_check_static_cli(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text(
        '<html><script src="/assets/a.js"></script></html>', encoding="utf-8"
    )

    assert main(["check-static", "--static-dir", str(static)]) == 1

    (static / "assets").mkdir()
    (static / "assets" / "a.js").write_text("a", encoding="utf-8")

    assert main(["check-static", "--static-dir", str(static)]) == 0
