from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ciao.release_manifest import (
    MANIFEST_NAME,
    RELEASE_PUBLIC_KEY,
    SignatureError,
    artifact_entry,
    build_manifest,
    main,
    missing_static_assets,
    parse_public_key,
    stamp_installer,
    verify_manifest,
    verify_signature,
)

ROOT = Path(__file__).parents[1]
WHEEL = "ciaobot-1.2.3-py3-none-any.whl"
SHA_A = "ab" * 32

TEST_TEAM = "TESTTEAM01"

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


def test_main_verify_checks_assets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    priv, public_key, key_id = _keypair()
    wheel = tmp_path / "ciaobot-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    document = build_manifest(
        "1.2.3", [artifact_entry(wheel, kind="wheel")], created="2026-09-25T10:00:00+00:00"
    )
    manifest = tmp_path / MANIFEST_NAME
    raw = json.dumps(document).encode()
    manifest.write_bytes(raw)
    signature = tmp_path / (MANIFEST_NAME + ".sig")
    signature.write_text(_sign(raw, priv, key_id), encoding="utf-8")
    base = ["verify", str(manifest), str(signature), "--public-key", public_key]

    assert main([*base, "--asset", str(wheel)]) == 0
    assert "asset matches: ciaobot-1.2.3-py3-none-any.whl" in capsys.readouterr().out

    # Same length, one byte changed: only the sha256 comparison can refuse it.
    wheel.write_bytes(b"wheel bytez")
    assert main([*base, "--asset", str(wheel)]) == 1
    assert "does not match the manifest" in capsys.readouterr().err

    # Without --asset the signature-and-schema verification is unchanged.
    assert main(base) == 0


def test_build_cli_refuses_server_host_option(tmp_path: Path) -> None:
    # The release lists the wheel only (#3130 R1). The host and installer options
    # are gone from the writer, so a publish step that still passes one fails
    # the job at argument parsing instead of writing a manifest the release
    # does not carry.
    wheel = tmp_path / WHEEL
    wheel.write_bytes(b"wheel bytes")
    archive = tmp_path / "ciaobot-server-host-macos-universal-v1.tar.gz"
    archive.write_bytes(b"host archive bytes")
    out = tmp_path / MANIFEST_NAME

    for option in (["--server-host", str(archive)], ["--installer", str(archive)]):
        with pytest.raises(SystemExit) as refused:
            main(["build", "--version", "1.2.3", "--out", str(out), *option, str(wheel)])
        assert refused.value.code == 2
        assert not out.exists(), "a refused build must not write a manifest"


def test_build_lists_only_the_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / WHEEL
    wheel.write_bytes(b"wheel bytes")
    out = tmp_path / MANIFEST_NAME

    assert main(["build", "--version", "1.2.3", "--out", str(out), str(wheel)]) == 0

    artifacts = json.loads(out.read_text(encoding="utf-8"))["artifacts"]
    assert artifacts == [
        {
            "filename": WHEEL,
            "kind": "wheel",
            "platform": "any",
            "arch": "any",
            "sha256": hashlib.sha256(b"wheel bytes").hexdigest(),
            "size": len(b"wheel bytes"),
        }
    ]


@pytest.mark.parametrize(
    "name",
    [
        "ciaobot-1.2.3-py3-none-any.tar.gz",
        "ciaobot-1.2.3.tar.gz",
        "other-1.2.3-py3-none-any.whl",
        "ciaobot-1.2.4-py3-none-any.whl",
    ],
)
def test_build_refuses_a_non_wheel_path(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(b"not the release wheel")
    out = tmp_path / MANIFEST_NAME

    assert main(["build", "--version", "1.2.3", "--out", str(out), str(path)]) == 1
    assert not out.exists()


TEMPLATE = (
    "#!/bin/sh\n"
    "CIAOBOT_VERSION=__CIAOBOT_VERSION__\n"
    "WHEEL_SHA256=__WHEEL_SHA256__\n"
)


def test_stamp_installer_replaces_each_placeholder_once() -> None:
    stamped = stamp_installer(TEMPLATE, version="1.2.3", wheel_sha256=SHA_A)

    assert stamped == (
        "#!/bin/sh\n"
        "CIAOBOT_VERSION=1.2.3\n"
        f"WHEEL_SHA256={SHA_A}\n"
    )


def test_stamp_installer_refuses_missing_or_duplicate_placeholder() -> None:
    # A second copy would be stamped only at its first match, so the one that
    # gets pinned would depend on where it sits. Refuse instead.
    with pytest.raises(ValueError, match="0 __CIAOBOT_VERSION__"):
        stamp_installer("WHEEL_SHA256=__WHEEL_SHA256__\n", version="1.2.3", wheel_sha256=SHA_A)
    with pytest.raises(ValueError, match="2 __WHEEL_SHA256__"):
        stamp_installer(
            TEMPLATE + "# __WHEEL_SHA256__\n", version="1.2.3", wheel_sha256=SHA_A
        )


@pytest.mark.parametrize(
    ("version", "digest"),
    [
        ("1.2", SHA_A),
        ("v1.2.3", SHA_A),
        ("1.2.3\nevil", SHA_A),
        ("1.2.3", "AB" * 32),
        ("1.2.3", "ab"),
        ("1.2.3", SHA_A + "\n"),
    ],
)
def test_stamp_installer_rejects_bad_version_or_digest(version: str, digest: str) -> None:
    with pytest.raises(ValueError):
        stamp_installer(TEMPLATE, version=version, wheel_sha256=digest)


def test_stamp_cli_digest_is_the_wheels_and_mode_is_755(tmp_path: Path) -> None:
    wheel = tmp_path / WHEEL
    wheel.write_bytes(b"release wheel bytes")
    template = tmp_path / "install.sh"
    template.write_text(TEMPLATE, encoding="utf-8")
    out = tmp_path / "install-out.sh"

    assert main(
        ["stamp-installer", "--template", str(template), "--version", "1.2.3",
         "--wheel", str(wheel), "--out", str(out)]
    ) == 0

    digest = hashlib.sha256(b"release wheel bytes").hexdigest()
    assert out.read_text(encoding="utf-8") == stamp_installer(
        TEMPLATE, version="1.2.3", wheel_sha256=digest
    )
    assert out.stat().st_mode & 0o777 == 0o755

    # The wheel name carries the version, so a wheel for another release cannot
    # be pinned into this one.
    other = tmp_path / "ciaobot-1.2.4-py3-none-any.whl"
    other.write_bytes(b"release wheel bytes")
    refused = tmp_path / "refused.sh"
    assert main(
        ["stamp-installer", "--template", str(template), "--version", "1.2.3",
         "--wheel", str(other), "--out", str(refused)]
    ) == 1
    assert not refused.exists()


def test_stamped_repo_installer_has_both_placeholders() -> None:
    # The stamp step runs against this file. Exactly one of each placeholder, or
    # the release step refuses to stamp.
    installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert installer.count("__CIAOBOT_VERSION__") == 1
    assert installer.count("__WHEEL_SHA256__") == 1


def _run_gate(*args: str) -> subprocess.CompletedProcess[str]:
    # Stdlib only, so a fresh interpreter with -I is the honest way to run it.
    return subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "legacy-update-gate.py"), *args],
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if not k.startswith("CIAO_")},
    )


def test_legacy_gate_refuses_a_non_release_target(tmp_path: Path) -> None:
    result = _run_gate("--target", "1.2", "--release-root", str(tmp_path), "--uv", "/bin/true")

    assert result.returncode == 2
    assert "not a release version" in result.stderr


def test_legacy_gate_needs_exactly_one_release_source(tmp_path: Path) -> None:
    neither = _run_gate("--target", "2.0.0", "--uv", "/bin/true")
    both = _run_gate(
        "--target", "2.0.0", "--release-root", str(tmp_path),
        "--release-base", "https://example.invalid/download", "--uv", "/bin/true",
    )

    assert neither.returncode == 2
    assert both.returncode == 2


def test_legacy_gate_refuses_a_release_root_without_the_manifest(tmp_path: Path) -> None:
    # The gate must not stage anything from a release directory that is missing
    # the signed manifest; it names the missing file and stops.
    result = _run_gate("--target", "2.0.0", "--release-root", str(tmp_path), "--uv", "/bin/true")

    assert result.returncode == 1
    assert MANIFEST_NAME in result.stderr



def test_embedded_release_key_is_the_published_release_key() -> None:
    # Literal pin of the key that signs every published release manifest. Changing
    # it means no release verifies against this module, so the edit has to be made
    # here as well, on purpose.
    assert RELEASE_PUBLIC_KEY == "RWSDUnIeQDnpmnNJiTjLmN6XOVFqgn1A0EXvTVG7AJIZXJxhyFN9osxm"


def _load_legacy_gate():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "legacy_update_gate", ROOT / "scripts" / "legacy-update-gate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = _load_legacy_gate()
_GATE_ENVIRON = {
    "PATH": "/usr/bin:/bin",
    "LANG": "en_US.UTF-8",
    "TMPDIR": "/private/tmp",
    "SSL_CERT_FILE": "/etc/ssl/cert.pem",
    "UV_CACHE_DIR": "/var/cache/uv",
    "UV_PYTHON_INSTALL_DIR": "/var/uv-python",
    "UV_INDEX_URL": "https://attacker.example/simple",
    "UV_NO_CONFIG": "1",
    "HOME": "/Users/operator",
    "GH_TOKEN": "gh-secret",
    "CIAO_PWA_AUTH_TOKEN": "ciao-secret",
    "PYTHONPATH": "/evil",
    "SOMETHING_ELSE": "x",
}


def test_legacy_gate_pins_the_1_10_0_wheel_digest() -> None:
    # Trust on first use, cross-checked against the signed v1.10.0 manifest: the
    # digest below is the wheel sha256 that manifest lists.
    assert GATE.LEGACY_VERSIONS == ("1.10.0",)
    assert GATE.LEGACY_WHEEL_SHA256 == {
        "1.10.0": "7d9fd66814e57b32b464bd1f44d6fa31716c4088bb271a2474f331afb1e2133b"
    }


def test_legacy_gate_commands_are_argv_lists_with_python_isolated(tmp_path: Path) -> None:
    download = GATE.gh_download_argv("1.10.0", tmp_path)
    assert download == [
        "gh", "release", "download", "v1.10.0",
        "--repo", "raffaelefarinaro/ciaobot",
        "--pattern", "ciaobot-1.10.0-py3-none-any.whl",
        "--dir", str(tmp_path),
    ]
    py = tmp_path / "venv" / "bin" / "python"
    runner = GATE.runner_argv(py, "2.0.0", "file:///releases", "/bin/uv", tmp_path / "state")
    assert runner[:3] == [str(py), "-I", "-c"]
    assert runner[3] == GATE.RUNNER
    assert runner[4:] == ["2.0.0", "file:///releases", "/bin/uv", str(tmp_path / "state")]
    assert all(isinstance(arg, str) for arg in runner + download)


def test_legacy_gate_child_env_is_an_allowlist_with_a_temp_home(tmp_path: Path) -> None:
    env = GATE.child_env(_GATE_ENVIRON, tmp_path)

    # Only the two uv paths the gate needs cross over; every other UV_* (an
    # index URL, a config switch) stays with the caller.
    assert env == {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "TMPDIR": "/private/tmp",
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
        "UV_CACHE_DIR": "/var/cache/uv",
        "UV_PYTHON_INSTALL_DIR": "/var/uv-python",
        "HOME": str(tmp_path),
    }


def test_legacy_gate_gh_env_carries_the_token_only_to_gh(tmp_path: Path) -> None:
    env = GATE.gh_env(_GATE_ENVIRON)

    assert env["GH_TOKEN"] == "gh-secret"
    assert env["HOME"] == "/Users/operator"
    assert "CIAO_PWA_AUTH_TOKEN" not in env
    assert "PYTHONPATH" not in env
    assert "GH_TOKEN" not in GATE.child_env(_GATE_ENVIRON, tmp_path)


def test_legacy_gate_refuses_a_1x_wheel_whose_digest_differs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_gate_run(monkeypatch, wheel=b"not the 1.10.0 wheel")

    with pytest.raises(GATE.GateError, match="sha256 is"):
        GATE._gate_one("1.10.0", "2.0.0", "file:///r", "/bin/uv", _GATE_ENVIRON, tmp_path)

    # Refused before uv or the 1.x updater runs at all.
    assert [cmd for cmd, _ in calls if cmd[0] == "/bin/uv" or cmd[1] == "-I"] == []


def test_legacy_gate_runs_every_child_in_the_scratch_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # gh, uv venv, uv pip and the 1.x runner all start in the per-release scratch
    # dir, never in the caller's working directory.
    wheel = b"a stand-in 1.x wheel"
    monkeypatch.setitem(GATE.LEGACY_WHEEL_SHA256, "1.10.0", hashlib.sha256(wheel).hexdigest())
    calls = _fake_gate_run(monkeypatch, wheel=wheel)

    GATE._gate_one("1.10.0", "2.0.0", "file:///r", "/bin/uv", _GATE_ENVIRON, tmp_path)

    assert [cmd[0] for cmd, _ in calls] == ["gh", "/bin/uv", "/bin/uv", str(tmp_path / "venv" / "bin" / "python")]
    assert {cwd for _, cwd in calls} == {tmp_path}


def test_legacy_gate_prints_the_1x_stderr_before_refusing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel = b"a stand-in 1.x wheel"
    monkeypatch.setitem(GATE.LEGACY_WHEEL_SHA256, "1.10.0", hashlib.sha256(wheel).hexdigest())
    _fake_gate_run(monkeypatch, wheel=wheel, stage_rc=1, stage_stderr="Traceback: no manifest\n")

    with pytest.raises(GATE.GateError, match="did not stage"):
        GATE._gate_one("1.10.0", "2.0.0", "file:///r", "/bin/uv", _GATE_ENVIRON, tmp_path)

    assert "Traceback: no manifest" in capsys.readouterr().err


def test_legacy_gate_run_starts_the_child_in_the_scratch_dir(tmp_path: Path) -> None:
    # The real subprocess call, not a stand-in: the child reports its own cwd.
    probe = [sys.executable, "-c", "import os, sys; sys.stdout.write(os.getcwd())"]
    result = GATE._run(probe, {"PATH": os.environ["PATH"]}, tmp_path)

    assert Path(result.stdout).resolve() == tmp_path.resolve()


def test_legacy_gate_turns_a_missing_binary_into_a_gate_error(tmp_path: Path) -> None:
    with pytest.raises(GATE.GateError, match="cannot run ciaobot-no-such-binary"):
        GATE._run(["ciaobot-no-such-binary", "venv"], {"PATH": ""}, tmp_path)


def _fake_gate_run(
    monkeypatch: pytest.MonkeyPatch, *, wheel: bytes, stage_rc: int = 0, stage_stderr: str = ""
) -> list[tuple[list[str], Path]]:
    # Stands in for every child process: gh writes the wheel into --dir, uv
    # succeeds, and the 1.x runner (python -I) returns the requested outcome.
    # Each call is recorded with the cwd it was asked to run in.
    calls: list[tuple[list[str], Path]] = []

    def fake(cmd: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, cwd))
        if cmd[:3] == ["gh", "release", "download"]:
            dest = Path(cmd[cmd.index("--dir") + 1])
            (dest / "ciaobot-1.10.0-py3-none-any.whl").write_bytes(wheel)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1] == "-I":
            return subprocess.CompletedProcess(cmd, stage_rc, "{}\n", stage_stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(GATE, "_run", fake)
    return calls
