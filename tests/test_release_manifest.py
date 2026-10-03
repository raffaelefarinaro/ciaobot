from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ciao.release_manifest import (
    RELEASE_PUBLIC_KEY,
    SERVER_HOST_ARCH,
    SERVER_HOST_BUNDLE_ID,
    SERVER_HOST_FILENAME,
    SERVER_HOST_KIND,
    SERVER_HOST_PLATFORM,
    SERVER_HOST_PROTOCOL,
    SERVER_HOST_REVISION,
    SignatureError,
    artifact_entry,
    build_manifest,
    main,
    missing_static_assets,
    parse_public_key,
    select_server_host_artifact,
    server_host_artifact_entry,
    verify_manifest,
    verify_signature,
)

TRUSTED = "timestamp:1\tfile:ciaobot-engine-manifest.json"


def _entry(**overrides: Any) -> dict[str, Any]:
    """A complete, valid artifact entry, the shape `artifact_entry` produces."""
    entry: dict[str, Any] = {
        "filename": "ciaobot-1.2.3-py3-none-any.whl",
        "kind": "wheel",
        "platform": "any",
        "arch": "any",
        "sha256": "ab" * 32,
        "size": 1024,
    }
    entry.update(overrides)
    return entry


def _host_entry(**overrides: Any) -> dict[str, Any]:
    """A valid ``server-host`` entry, the shape `server_host_artifact_entry` makes."""
    entry: dict[str, Any] = {
        "filename": SERVER_HOST_FILENAME,
        "kind": SERVER_HOST_KIND,
        "platform": SERVER_HOST_PLATFORM,
        "arch": SERVER_HOST_ARCH,
        "sha256": "cd" * 32,
        "size": 4096,
        "bundle_id": SERVER_HOST_BUNDLE_ID,
        "host_revision": SERVER_HOST_REVISION,
        "host_protocol": SERVER_HOST_PROTOCOL,
    }
    entry.update(overrides)
    return entry


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


def test_verify_accepts_base64_wrapped_signature() -> None:
    priv, public_key, key_id = _keypair()
    signature = _sign(b"manifest bytes", priv, key_id, wrap=True)

    assert verify_signature(b"manifest bytes", signature, public_key) == TRUSTED


# A real engine manifest, byte for byte what `release_manifest build` wrote,
# and a real signature over it, both frozen from one run of the release
# signer (`npx @tauri-apps/cli@2.11.4 signer sign`, with a throwaway key that
# lives only in this test). #653 moved signing off the app tree, because the
# release became engine-only; the replacement runs the same pinned CLI
# standalone and needs no project of its own, which is why deleting that tree
# (#656) did not break signing. If a later CLI changed the form it
# emits, this fails here rather than as a `release_manifest verify` failure
# during a release.
TCLI_SIGNED_MANIFEST = b"""{
  "artifacts": [
    {
      "arch": "any",
      "filename": "ciaobot-1.2.3-py3-none-any.whl",
      "kind": "wheel",
      "platform": "any",
      "sha256": "94f73d20d44763804bf582c495d46737981eb2a5f88c77e4c7b7456c12509f14",
      "size": 25
    }
  ],
  "created": "2026-09-27T22:20:26+00:00",
  "schema": 1,
  "tag": "v1.2.3",
  "version": "1.2.3"
}
"""
TCLI_SIGNED_KEY = (
    "untrusted comment: minisign public key: EBFBA27FB1EBCCF7\n"
    "RWT3zOuxf6L761L+DxU0Jhi262aZY/pvKP2LKyBrqpiIFPs5KPKv3wT3\n"
)
TCLI_SIGNED_SIG = (
    "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVU"
    "M3pPdXhmNkw3NjN2QW45emFNRnVXSXRnOVJXbTdtZ1BvY3owZ0Z3bzFpcTdLbFRhbHUxOSts"
    "YUFHNTA3RUpzUEJDQ1pjSVBPeW12M0hJYkg2K2RxaXdjTlpQVGxaNHdZPQp0cnVzdGVkIGNv"
    "bW1lbnQ6IHRpbWVzdGFtcDoxNzkwNTQ3NjI3CWZpbGU6Y2lhb2JvdC1lbmdpbmUtbWFuaWZl"
    "c3QuanNvbgpZNmtKWlgxNjRucmZGMklzZnVNdml5L3d1dFQwM1NWQnArWDd1MWk4cUMvVU"
    "FVVHZheStzRC8xMWhVK3RwSis4bkJVSEtHOXJsajk3MkVvTnk3S0xEdz09Cg==\n"
)


def test_verify_accepts_a_standalone_signer_signature() -> None:
    # Exactly what the release now does: the standalone CLI signs, and the
    # .sig is read back as the whole file - base64-wrapped minisign included.
    trusted = verify_signature(TCLI_SIGNED_MANIFEST, TCLI_SIGNED_SIG, TCLI_SIGNED_KEY)

    assert trusted == "timestamp:1790547627\tfile:ciaobot-engine-manifest.json"
    assert (
        verify_manifest(TCLI_SIGNED_MANIFEST, TCLI_SIGNED_SIG, TCLI_SIGNED_KEY)[
            "version"
        ]
        == "1.2.3"
    )

    with pytest.raises(SignatureError):
        verify_signature(
            TCLI_SIGNED_MANIFEST + b" ", TCLI_SIGNED_SIG, TCLI_SIGNED_KEY
        )


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


def test_embedded_release_key_is_a_well_formed_minisign_key() -> None:
    # The app and its native verifier are gone (#656), so this constant is the
    # only copy of the release signing key left in the repo. A typo here is
    # unrecoverable against a published release, so its shape is pinned even
    # though there is no second file left to cross-check it against.
    key_id, key_bytes = parse_public_key(RELEASE_PUBLIC_KEY)
    assert len(key_id) == 8
    assert len(key_bytes) == 32


def test_verify_manifest_round_trip_and_schema_checks() -> None:
    priv, public_key, key_id = _keypair()
    document = build_manifest(
        "1.2.3",
        [_entry()],
        created="2026-09-25T10:00:00+00:00",
    )
    raw = json.dumps(document).encode()
    signature = _sign(raw, priv, key_id)

    assert verify_manifest(raw, signature, public_key)["version"] == "1.2.3"

    wrong_schema = json.dumps({"schema": 2, "version": "1.2.3", "artifacts": [{}]}).encode()
    with pytest.raises(ValueError):
        verify_manifest(wrong_schema, _sign(wrong_schema, priv, key_id), public_key)


# A downloader resolves a URL from `tag` while a consumer compares against
# `version`. A signed manifest whose two disagree can therefore be served
# under one release's directory and read as another's.
def test_verify_manifest_rejects_tag_mismatch() -> None:
    priv, public_key, key_id = _keypair()
    document = build_manifest(
        "1.2.3", [_entry()], created="2026-09-25T10:00:00+00:00"
    )
    document["tag"] = "v1.2.4"
    raw = json.dumps(document).encode()

    with pytest.raises(ValueError, match="tag does not match"):
        verify_manifest(raw, _sign(raw, priv, key_id), public_key)


# Every field a consumer trusts when it downloads and checks a digest. A
# filename with a separator escapes the staging directory, a non-canonical
# sha256 never compares equal to a computed one, and a non-int size means the
# size check was coerced rather than enforced.
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("filename", "../escape.whl"),
        ("filename", "sub/dir.whl"),
        ("filename", "sub\\dir.whl"),
        ("filename", ".."),
        ("filename", ""),
        ("kind", ""),
        ("kind", None),
        ("sha256", "A" * 64),
        ("sha256", "ab"),
        ("sha256", "z" * 64),
        ("size", "1024"),
        ("size", True),
        ("size", 1.5),
        ("size", -1),
    ],
)
def test_verify_manifest_rejects_malformed_artifacts(field: str, value: object) -> None:
    priv, public_key, key_id = _keypair()
    entry = _entry()
    if value is None:
        entry.pop(field)
    else:
        entry[field] = value
    document = build_manifest("1.2.3", [entry], created="2026-09-25T10:00:00+00:00")
    raw = json.dumps(document).encode()

    with pytest.raises(ValueError, match="manifest artifact is malformed"):
        verify_manifest(raw, _sign(raw, priv, key_id), public_key)


def test_verify_manifest_rejects_a_non_object_artifact() -> None:
    priv, public_key, key_id = _keypair()
    document = build_manifest("1.2.3", ["ciaobot.whl"], created="2026-09-25T10:00:00+00:00")
    raw = json.dumps(document).encode()

    with pytest.raises(ValueError, match="manifest artifact is malformed"):
        verify_manifest(raw, _sign(raw, priv, key_id), public_key)


def test_server_host_artifact_entry(tmp_path: Path) -> None:
    payload = b"universal host archive" * 128
    archive = tmp_path / SERVER_HOST_FILENAME
    archive.write_bytes(payload)

    entry = server_host_artifact_entry(archive)

    assert entry["filename"] == SERVER_HOST_FILENAME
    assert entry["kind"] == SERVER_HOST_KIND
    assert entry["platform"] == SERVER_HOST_PLATFORM
    assert entry["arch"] == SERVER_HOST_ARCH
    assert entry["bundle_id"] == SERVER_HOST_BUNDLE_ID
    assert entry["host_revision"] == SERVER_HOST_REVISION
    assert entry["host_protocol"] == SERVER_HOST_PROTOCOL
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entry["size"] == len(payload)
    # The revision is fixed, independent of any engine version, and never bool.
    assert type(entry["host_revision"]) is int


def test_server_host_artifact_entry_rejects_other_names(tmp_path: Path) -> None:
    other = tmp_path / "ciaobot-server-host-macos-universal-v2.tar.gz"
    other.write_bytes(b"host")

    with pytest.raises(ValueError, match="must be named"):
        server_host_artifact_entry(other)


def test_manifest_with_host_signed_round_trip(tmp_path: Path) -> None:
    priv, public_key, key_id = _keypair()
    wheel = tmp_path / "ciaobot-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    archive = tmp_path / SERVER_HOST_FILENAME
    archive.write_bytes(b"host archive bytes")
    document = build_manifest(
        "1.2.3",
        [artifact_entry(wheel, kind="wheel"), server_host_artifact_entry(archive)],
        created="2026-09-25T10:00:00+00:00",
    )
    raw = json.dumps(document).encode()

    verified = verify_manifest(raw, _sign(raw, priv, key_id), public_key)
    entry = select_server_host_artifact(verified)

    assert entry["filename"] == SERVER_HOST_FILENAME
    assert entry["host_revision"] == SERVER_HOST_REVISION
    # The wheel-only generic path is untouched by the host being present.
    wheels = [a for a in verified["artifacts"] if a["kind"] == "wheel"]
    assert len(wheels) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platform", "linux"),
        ("platform", "any"),
        ("arch", "arm64"),
        ("arch", "any"),
        ("filename", "ciaobot-server-host-macos-universal-v2.tar.gz"),
        ("bundle_id", "local.ciaobot.other"),
        # bool is an int subclass; a protocol of True must not pass as 1.
        ("host_protocol", True),
        ("host_protocol", 2),
        ("host_protocol", None),
        ("host_revision", True),
        ("host_revision", 2),
        ("host_revision", None),
        ("size", 0),
        ("size", -1),
    ],
)
def test_server_host_metadata_rejected(field: str, value: object) -> None:
    priv, public_key, key_id = _keypair()
    entry = _host_entry()
    if value is None:
        entry.pop(field)
    else:
        entry[field] = value
    document = build_manifest("1.2.3", [entry], created="2026-09-25T10:00:00+00:00")
    raw = json.dumps(document).encode()

    with pytest.raises(ValueError):
        select_server_host_artifact(
            verify_manifest(raw, _sign(raw, priv, key_id), public_key)
        )


def test_verify_manifest_accepts_an_unsupported_host_beside_the_wheel() -> None:
    # The engine updater verifies every manifest with verify_manifest and only
    # picks the wheel. A later host revision, or two hosts during a transition,
    # must not refuse the wheel update on engines that predate them; only the
    # selector, which a host consumer calls, pins the supported identity.
    priv, public_key, key_id = _keypair()
    future_host = _host_entry(
        filename="ciaobot-server-host-macos-universal-v2.tar.gz",
        host_revision=2,
        host_protocol=2,
    )
    document = build_manifest(
        "1.2.3",
        [_entry(), _host_entry(), future_host],
        created="2026-09-25T10:00:00+00:00",
    )
    raw = json.dumps(document).encode()

    verified = verify_manifest(raw, _sign(raw, priv, key_id), public_key)

    assert [a for a in verified["artifacts"] if a["kind"] == "wheel"] == [_entry()]
    with pytest.raises(ValueError, match="more than one server host"):
        select_server_host_artifact(verified)


def test_server_host_selection_requires_unique_supported_asset() -> None:
    priv, public_key, key_id = _keypair()

    # Absence: the selector raises rather than falling back to the wheel.
    wheel_only = build_manifest(
        "1.2.3", [_entry()], created="2026-09-25T10:00:00+00:00"
    )
    raw = json.dumps(wheel_only).encode()
    verified = verify_manifest(raw, _sign(raw, priv, key_id), public_key)
    with pytest.raises(ValueError, match="no server host artifact"):
        select_server_host_artifact(verified)

    # Duplicate host entries (same bytes, listed twice) are ambiguous.
    duplicate = build_manifest(
        "1.2.3",
        [_host_entry(), _host_entry()],
        created="2026-09-25T10:00:00+00:00",
    )
    raw = json.dumps(duplicate).encode()
    verified = verify_manifest(raw, _sign(raw, priv, key_id), public_key)
    with pytest.raises(ValueError, match="more than one server host"):
        select_server_host_artifact(verified)

    # The host filename reused under another kind is ambiguous.
    cross_kind = build_manifest(
        "1.2.3",
        [_host_entry(), _entry(filename=SERVER_HOST_FILENAME, kind="wheel")],
        created="2026-09-25T10:00:00+00:00",
    )
    raw = json.dumps(cross_kind).encode()
    verified = verify_manifest(raw, _sign(raw, priv, key_id), public_key)
    with pytest.raises(ValueError, match="reuses the server host filename"):
        select_server_host_artifact(verified)


def test_server_host_selection_rejects_unsupported_entry() -> None:
    # A selector caller that has verified the manifest still refuses an entry
    # whose identity is not the supported one, so shape validation is not
    # bypassed by calling the selector directly.
    manifest = {
        "schema": 1,
        "version": "1.2.3",
        "tag": "v1.2.3",
        "artifacts": [_host_entry(host_protocol=2)],
    }

    with pytest.raises(ValueError):
        select_server_host_artifact(manifest)


def test_server_host_selection_refuses_malformed_or_ambiguous_entries() -> None:
    # The selector is called on a dict, so it repeats the per-entry and
    # ambiguity checks rather than calling .get on whatever the list holds.
    def manifest(*artifacts: Any) -> dict[str, Any]:
        return {"schema": 1, "version": "1.2.3", "tag": "v1.2.3", "artifacts": list(artifacts)}

    with pytest.raises(ValueError, match="malformed"):
        select_server_host_artifact(manifest(_host_entry(), "not an object"))
    with pytest.raises(ValueError, match="no artifacts"):
        select_server_host_artifact(manifest())
    with pytest.raises(ValueError, match="more than one server host"):
        select_server_host_artifact(manifest(_host_entry(), _host_entry()))
    with pytest.raises(ValueError, match="reuses the server host filename"):
        select_server_host_artifact(
            manifest(_host_entry(), _entry(filename=SERVER_HOST_FILENAME, kind="wheel"))
        )
    # Order does not matter: the host filename listed first under another kind
    # is just as ambiguous.
    with pytest.raises(ValueError, match="reuses the server host filename"):
        select_server_host_artifact(
            manifest(_entry(filename=SERVER_HOST_FILENAME, kind="wheel"), _host_entry())
        )


def test_server_host_constants_match_the_builder() -> None:
    # The manifest pins what scripts/build-server-host.py produces; if the two
    # drift, the release would sign an entry the archive does not match.
    script = Path(__file__).parents[1] / "scripts" / "build-server-host.py"
    spec = importlib.util.spec_from_file_location("build_server_host_contract", script)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    assert builder.ARCHIVE_NAME == SERVER_HOST_FILENAME
    assert builder.BUNDLE_ID == SERVER_HOST_BUNDLE_ID
    assert builder.HOST_REVISION == SERVER_HOST_REVISION
    assert builder.HOST_PROTOCOL_REVISION == SERVER_HOST_PROTOCOL
    assert set(builder.ARCHITECTURES) == {"arm64", "x86_64"}


def test_main_build_with_server_host(tmp_path: Path) -> None:
    wheel = tmp_path / "ciaobot-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    archive = tmp_path / SERVER_HOST_FILENAME
    archive.write_bytes(b"host archive bytes")
    out = tmp_path / "ciaobot-engine-manifest.json"

    assert main(
        [
            "build",
            "--version",
            "1.2.3",
            "--out",
            str(out),
            "--server-host",
            str(archive),
            str(wheel),
        ]
    ) == 0

    written = json.loads(out.read_text(encoding="utf-8"))
    kinds = {a["kind"] for a in written["artifacts"]}
    assert kinds == {"wheel", SERVER_HOST_KIND}
    host = next(a for a in written["artifacts"] if a["kind"] == SERVER_HOST_KIND)
    assert host["filename"] == SERVER_HOST_FILENAME
    assert host["bundle_id"] == SERVER_HOST_BUNDLE_ID


def test_main_build_rejects_bad_server_host_without_partial_output(tmp_path: Path) -> None:
    wheel = tmp_path / "ciaobot-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    out = tmp_path / "ciaobot-engine-manifest.json"

    missing = tmp_path / "missing.tar.gz"
    assert main(
        [
            "build", "--version", "1.2.3", "--out", str(out),
            "--server-host", str(missing), str(wheel),
        ]
    ) == 1
    assert not out.exists(), "a refused build must not write a partial manifest"

    empty = tmp_path / SERVER_HOST_FILENAME
    empty.write_bytes(b"")
    assert main(
        [
            "build", "--version", "1.2.3", "--out", str(out),
            "--server-host", str(empty), str(wheel),
        ]
    ) == 1
    assert not out.exists()

    misnamed = tmp_path / "ciaobot-server-host-macos-universal-v2.tar.gz"
    misnamed.write_bytes(b"host")
    assert main(
        [
            "build", "--version", "1.2.3", "--out", str(out),
            "--server-host", str(misnamed), str(wheel),
        ]
    ) == 1
    assert not out.exists()


def test_server_host_signature_tampering_fails_before_schema() -> None:
    # The signed metadata is what authenticates the host; editing a fixed field
    # without resigning must fail signature verification, not merely schema.
    priv, public_key, key_id = _keypair()
    document = build_manifest(
        "1.2.3",
        [_host_entry(host_protocol=2)],
        created="2026-09-25T10:00:00+00:00",
    )
    raw = json.dumps(document).encode()
    honest = json.dumps(
        build_manifest(
            "1.2.3",
            [_host_entry()],
            created="2026-09-25T10:00:00+00:00",
        )
    ).encode()

    with pytest.raises(SignatureError):
        verify_manifest(raw, _sign(honest, priv, key_id), public_key)


def test_server_host_digest_tampering_fails_signature() -> None:
    # Swapping the host bytes means swapping the signed digest; a manifest
    # whose host sha256 was edited after signing never reaches the selector.
    priv, public_key, key_id = _keypair()
    honest = json.dumps(
        build_manifest("1.2.3", [_entry(), _host_entry()], created="2026-09-25T10:00:00+00:00")
    ).encode()
    signature = _sign(honest, priv, key_id)
    tampered = honest.replace(("cd" * 32).encode(), ("ef" * 32).encode())
    assert tampered != honest

    with pytest.raises(SignatureError):
        verify_manifest(tampered, signature, public_key)


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


def test_missing_static_assets_rejects_placeholder_index(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>", encoding="utf-8")

    assert missing_static_assets(static) == ["/assets/* (index.html references none)"]


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
        "1.2.3", [_entry()], created="2026-09-25T10:00:00+00:00"
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
