"""Signed engine release manifest (#562): build, verify, and check packaged assets."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

MANIFEST_NAME = "ciaobot-engine-manifest.json"
SIGNATURE_NAME = MANIFEST_NAME + ".sig"
SCHEMA_VERSION = 1
# Same minisign key as desktop/installer-verify/src/main.rs and the Tauri updater
# pubkey in desktop/src-tauri/tauri.conf.json; its secret half is the
# TAURI_SIGNING_PRIVATE_KEY release secret.
RELEASE_PUBLIC_KEY = "RWSDUnIeQDnpmnNJiTjLmN6XOVFqgn1A0EXvTVG7AJIZXJxhyFN9osxm"
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_ASSET_REF_RE = re.compile(r'(?:src|href)="(/assets/[^"]+)"')
_CHUNK = 1 << 20


class SignatureError(ValueError):
    """The signature is malformed, made by another key, or does not match."""


def artifact_entry(
    path: Path, *, kind: str, platform: str = "any", arch: str = "any"
) -> dict[str, Any]:
    """Describe one release artifact by filename, digest, and size."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return {
        "filename": path.name,
        "kind": kind,
        "platform": platform,
        "arch": arch,
        "sha256": digest.hexdigest(),
        "size": size,
    }


def build_manifest(
    version: str, artifacts: list[dict[str, Any]], *, created: str
) -> dict[str, Any]:
    """Wrap the artifact list in the versioned manifest envelope."""
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"not a release version: {version!r}")
    if not artifacts:
        raise ValueError("manifest needs at least one artifact")
    return {
        "schema": SCHEMA_VERSION,
        "version": version,
        "tag": f"v{version}",
        "created": created,
        "artifacts": artifacts,
    }


def parse_public_key(text: str) -> tuple[bytes, bytes]:
    """Return the (key id, 32-byte key) of a minisign Ed25519 public key.

    The last non-empty line is used, so a key file with the usual
    ``untrusted comment:`` header works as well as a bare key.
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        raise SignatureError("public key is empty")
    try:
        raw = base64.b64decode(lines[-1], validate=True)
    except binascii.Error as exc:
        raise SignatureError("public key is not valid base64") from exc
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise SignatureError("public key is not a minisign Ed25519 key")
    return raw[2:10], raw[10:42]


def _signature_lines(signature_text: str) -> list[str]:
    """Return the four minisign lines, unwrapping Tauri's base64 form first."""
    text = signature_text.strip()
    if not text.startswith("untrusted comment:"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise SignatureError(
                "signature is neither plain nor base64-wrapped minisign text"
            ) from exc
    lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    if len(lines) != 4:
        raise SignatureError(f"expected 4 signature lines, got {len(lines)}")
    if not lines[0].startswith("untrusted comment:"):
        raise SignatureError("first signature line is not an untrusted comment")
    if not lines[2].startswith("trusted comment: "):
        raise SignatureError("third signature line is not a trusted comment")
    return lines


def verify_signature(
    data: bytes, signature_text: str, public_key: str = RELEASE_PUBLIC_KEY
) -> str:
    """Verify a minisign signature and return its trusted comment.

    Both minisign algorithms are accepted: ``ED`` (BLAKE2b-512 prehashed) and
    ``Ed`` (pure Ed25519). The cryptography import stays inside this function so
    ``build`` and ``check-static`` keep working without it.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key_id, key_bytes = parse_public_key(public_key)
    lines = _signature_lines(signature_text)
    try:
        sig_blob = base64.b64decode(lines[1], validate=True)
    except binascii.Error as exc:
        raise SignatureError("signature is not valid base64") from exc
    if len(sig_blob) != 74:
        raise SignatureError("malformed signature")
    algorithm, sig_key_id, signature = sig_blob[:2], sig_blob[2:10], sig_blob[10:]
    if sig_key_id != key_id:
        raise SignatureError("signed by a different key")
    if algorithm == b"ED":
        message = hashlib.blake2b(data, digest_size=64).digest()
    elif algorithm == b"Ed":
        message = data
    else:
        raise SignatureError(f"unsupported signature algorithm: {algorithm!r}")
    trusted_comment = lines[2][len("trusted comment: ") :]
    try:
        global_sig = base64.b64decode(lines[3], validate=True)
    except binascii.Error as exc:
        raise SignatureError("global signature is not valid base64") from exc
    pk = Ed25519PublicKey.from_public_bytes(key_bytes)
    try:
        pk.verify(signature, message)
        pk.verify(global_sig, signature + trusted_comment.encode("utf-8"))
    except InvalidSignature as exc:
        raise SignatureError("signature does not match") from exc
    return trusted_comment


def verify_manifest(
    manifest_bytes: bytes, signature_text: str, public_key: str = RELEASE_PUBLIC_KEY
) -> dict[str, Any]:
    """Verify the signature, then the schema, of a release manifest."""
    verify_signature(manifest_bytes, signature_text, public_key)
    try:
        parsed: Any = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("manifest is not a JSON object")
    manifest: dict[str, Any] = parsed
    if manifest.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"unsupported manifest schema: {manifest.get('schema')!r}")
    version = manifest.get("version")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise ValueError(f"manifest version is not a release version: {version!r}")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("manifest lists no artifacts")
    return manifest


def missing_static_assets(static_dir: Path) -> list[str]:
    """Return the packaged PWA files the built index.html needs but lacks."""
    index = static_dir / "index.html"
    if not index.is_file():
        return ["index.html"]
    try:
        html = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["index.html"]
    missing = {
        ref
        for ref in _ASSET_REF_RE.findall(html)
        if not (static_dir / ref.lstrip("/")).is_file()
    }
    return sorted(missing)


def packaged_static_dir() -> Path:
    """The PWA static directory as packaged in the installed engine."""
    return Path(str(resources.files("ciao.web").joinpath("static")))


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m ciao.release_manifest``."""
    parser = argparse.ArgumentParser(
        prog="python -m ciao.release_manifest", description=__doc__
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Write the engine release manifest")
    build.add_argument("--version", required=True, help="release version, no leading v")
    build.add_argument("--out", required=True, type=Path, help="manifest path to write")
    build.add_argument("wheels", nargs="+", type=Path, help="wheels to list")

    verify = sub.add_parser("verify", help="Verify a signed engine manifest")
    verify.add_argument("manifest", type=Path, help="signed manifest to read")
    verify.add_argument("signature", type=Path, help="manifest signature to read")
    verify.add_argument(
        "--public-key", default=RELEASE_PUBLIC_KEY, help="minisign public key"
    )

    check_static = sub.add_parser(
        "check-static", help="Check the packaged PWA assets"
    )
    check_static.add_argument(
        "--static-dir", type=Path, default=None, help="static directory to check"
    )

    args = parser.parse_args(argv)

    if args.command == "build":
        version: str = args.version
        out: Path = args.out
        wheels: list[Path] = args.wheels
        for wheel in wheels:
            if f"-{version}-" not in wheel.name:
                print(f"Error: {wheel.name} is not version {version}", file=sys.stderr)
                return 1
        artifacts = [artifact_entry(wheel, kind="wheel") for wheel in wheels]
        manifest = build_manifest(
            version,
            artifacts,
            created=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(out)
        return 0

    if args.command == "verify":
        manifest_path: Path = args.manifest
        signature_path: Path = args.signature
        public_key: str = args.public_key
        try:
            raw = manifest_path.read_bytes()
            signature_text = signature_path.read_text(encoding="utf-8")
            trusted_comment = verify_signature(raw, signature_text, public_key)
            manifest = verify_manifest(raw, signature_text, public_key)
        except (SignatureError, ValueError, OSError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"verified {manifest['version']}: {trusted_comment}")
        return 0

    static_dir: Path = args.static_dir or packaged_static_dir()
    missing = missing_static_assets(static_dir)
    if missing:
        for item in missing:
            print(f"Error: missing {item}", file=sys.stderr)
        return 1
    print(f"static assets complete: {static_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
