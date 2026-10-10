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
# The prebuilt universal macOS server host (#1022, child D of #1008). Its
# identity metadata is fixed and independent of the engine version; only a
# deliberate host upgrade changes it. The fields are exact because a consumer
# that trusts the manifest also trusts them to pick and stage the one host it
# will run.
SERVER_HOST_KIND = "server-host"
SERVER_HOST_PLATFORM = "macos"
SERVER_HOST_ARCH = "universal"
SERVER_HOST_REVISION = 2
SERVER_HOST_PROTOCOL = 1
SERVER_HOST_BUNDLE_ID = "local.ciaobot.server"
SERVER_HOST_FILENAME = "ciaobot-server-host-macos-universal-v1.tar.gz"
# The release signing key, generated with the app updater back when the release
# also shipped Ciaobot.app and every consumer of a signature trusted this one
# key. The app and its verifier are gone (#656), so this is now the only copy
# left in the repo; its secret half is the TAURI_SIGNING_PRIVATE_KEY release
# secret, and no other key can sign a manifest this module will trust.
RELEASE_PUBLIC_KEY = "RWSDUnIeQDnpmnNJiTjLmN6XOVFqgn1A0EXvTVG7AJIZXJxhyFN9osxm"
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
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


def server_host_artifact_entry(path: Path) -> dict[str, Any]:
    """Describe the fixed universal macOS server host archive.

    The entry carries the digest and size of ``path`` plus the host's fixed
    identity and its revision/protocol, which are independent of the engine
    version. ``path`` must already be named ``SERVER_HOST_FILENAME``: the
    archive name is part of the contract, so a mismatch is refused here rather
    than discovered by a manifest consumer.
    """
    if path.name != SERVER_HOST_FILENAME:
        raise ValueError(
            f"server host archive must be named {SERVER_HOST_FILENAME}, not {path.name!r}"
        )
    entry = artifact_entry(
        path,
        kind=SERVER_HOST_KIND,
        platform=SERVER_HOST_PLATFORM,
        arch=SERVER_HOST_ARCH,
    )
    entry["bundle_id"] = SERVER_HOST_BUNDLE_ID
    entry["host_revision"] = SERVER_HOST_REVISION
    entry["host_protocol"] = SERVER_HOST_PROTOCOL
    return entry


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
    """Return the four minisign lines, unwrapping the base64 form the release
    signer emits first."""
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


def _check_artifact(entry: Any) -> None:
    """Reject an artifact a consumer could not safely act on.

    The signature already proves the manifest came from the release key, so this
    is not about authenticity: it is that a consumer which trusts the manifest
    also trusts these fields. A ``filename`` with a path separator would send
    the download outside the staging directory; a ``sha256`` that is uppercase
    or truncated will not compare equal to a computed digest; and a ``size``
    that is a string, a float or a boolean means the consumer's size check is
    the one that has been coerced rather than enforced.
    """
    if not isinstance(entry, dict):
        raise ValueError("manifest artifact is malformed")
    filename = entry.get("filename")
    if (
        not isinstance(filename, str)
        or not filename
        or "/" in filename
        or "\\" in filename
        or filename in (".", "..")
    ):
        raise ValueError("manifest artifact is malformed")
    kind = entry.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("manifest artifact is malformed")
    sha256 = entry.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise ValueError("manifest artifact is malformed")
    # `type(...) is int`, not isinstance: bool is a subclass of int, so
    # `True` would pass as a size of 1 and a float would pass as a size.
    if type(entry.get("size")) is not int or entry["size"] < 0:
        raise ValueError("manifest artifact is malformed")


def _check_server_host_artifact(entry: dict[str, Any]) -> None:
    """Reject a ``server-host`` entry whose fixed host identity is wrong.

    A wheel is validated by the generic fields alone, but the host archive is
    picked by its exact identity, so every field a consumer acts on — the fixed
    filename, platform, arch, bundle id and the revision/protocol the host
    binary answers with — is pinned here. ``host_revision`` and
    ``host_protocol`` use ``type(...) is int`` so a boolean, which is an ``int``
    subclass, cannot stand in for revision ``1``; the size must also be
    positive, since a host archive is never empty.
    """
    if entry.get("platform") != SERVER_HOST_PLATFORM:
        raise ValueError("manifest server host platform is not supported")
    if entry.get("arch") != SERVER_HOST_ARCH:
        raise ValueError("manifest server host architecture is not supported")
    if entry.get("filename") != SERVER_HOST_FILENAME:
        raise ValueError("manifest server host filename is not supported")
    if entry.get("bundle_id") != SERVER_HOST_BUNDLE_ID:
        raise ValueError("manifest server host bundle id is not supported")
    if (
        type(entry.get("host_revision")) is not int
        or entry["host_revision"] != SERVER_HOST_REVISION
    ):
        raise ValueError("manifest server host revision is not supported")
    if (
        type(entry.get("host_protocol")) is not int
        or entry["host_protocol"] != SERVER_HOST_PROTOCOL
    ):
        raise ValueError("manifest server host protocol is not supported")
    if entry.get("size", 0) <= 0:
        raise ValueError("manifest server host archive is empty")


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
    # The tag is what a downloader resolves a URL from, and the version is
    # what the consumer compares against. If they can disagree, a signed
    # manifest for one release can be served under another's directory.
    if manifest.get("tag") != f"v{version}":
        raise ValueError(
            f"manifest tag does not match its version: "
            f"{manifest.get('tag')!r} != 'v{version}'"
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("manifest lists no artifacts")
    # The host's identity and uniqueness are checked by the selector, not here:
    # the engine updater verifies every manifest through this function, so a
    # future host revision pinned here would refuse the wheel update on every
    # installed engine, including platforms that never run the host.
    for entry in artifacts:
        _check_artifact(entry)
    return manifest


def _check_artifact_ambiguity(artifacts: list[Any]) -> None:
    """Reject two artifacts a consumer could not tell apart.

    A consumer selects the host by kind or filename, so a second ``server-host``
    entry, or the host filename reused under another kind, makes the choice
    ambiguous: which bytes it stages would depend on list order rather than on
    the signed identity. Only the selector runs this: it is about the host it
    resolves, and a manifest the wheel updater reads is never refused for it.
    """
    host_entries = [e for e in artifacts if e.get("kind") == SERVER_HOST_KIND]
    if len(host_entries) > 1:
        raise ValueError("manifest lists more than one server host artifact")
    if host_entries:
        host_filename = host_entries[0]["filename"]
        for entry in artifacts:
            if (
                entry.get("kind") != SERVER_HOST_KIND
                and entry["filename"] == host_filename
            ):
                raise ValueError(
                    "manifest reuses the server host filename under another kind"
                )


def select_server_host_artifact(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the one supported ``server-host`` entry in a verified manifest.

    Absence raises a clear ``ValueError`` rather than falling back to the wheel:
    a caller that needs the host must fail rather than run a release that never
    shipped one. This checks shape and the supported identity only — it does
    **not** prove the manifest authentic. The caller must have verified the
    signature with :func:`verify_manifest` (or :func:`verify_signature`) first;
    the selector trusts those bytes, so it must never be the only check.
    """
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("manifest lists no artifacts")
    # The same per-entry checks verify_manifest runs, so a non-object entry is
    # a ValueError rather than an AttributeError; the ambiguity and host
    # identity checks live only here, where a host is actually wanted.
    for artifact in artifacts:
        _check_artifact(artifact)
    _check_artifact_ambiguity(artifacts)
    hosts = [e for e in artifacts if e["kind"] == SERVER_HOST_KIND]
    if not hosts:
        raise ValueError("manifest has no server host artifact")
    entry: dict[str, Any] = hosts[0]
    _check_server_host_artifact(entry)
    return entry


def missing_static_assets(static_dir: Path) -> list[str]:
    """Return the packaged PWA files the built index.html needs but lacks."""
    index = static_dir / "index.html"
    if not index.is_file():
        return ["index.html"]
    try:
        html = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["index.html"]
    refs = _ASSET_REF_RE.findall(html)
    if not refs:
        # A placeholder index would otherwise pass without checking anything.
        return ["/assets/* (index.html references none)"]
    missing = {
        ref for ref in refs if not (static_dir / ref.lstrip("/")).is_file()
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
    build.add_argument(
        "--server-host",
        type=Path,
        default=None,
        help=f"optional {SERVER_HOST_FILENAME} to authenticate",
    )
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
        server_host: Path | None = args.server_host
        try:
            for wheel in wheels:
                if f"-{version}-" not in wheel.name:
                    raise ValueError(f"{wheel.name} is not version {version}")
                if not wheel.is_file():
                    raise ValueError(f"wheel is not a regular file: {wheel}")
            artifacts = [artifact_entry(wheel, kind="wheel") for wheel in wheels]
            if server_host is not None:
                if not server_host.is_file():
                    raise ValueError(
                        f"server host archive is not a regular file: {server_host}"
                    )
                if server_host.stat().st_size == 0:
                    raise ValueError(f"server host archive is empty: {server_host}")
                artifacts.append(server_host_artifact_entry(server_host))
            manifest = build_manifest(
                version,
                artifacts,
                created=datetime.now(UTC).isoformat(timespec="seconds"),
            )
            serialized = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            out.write_text(serialized, encoding="utf-8", newline="")
        except (OSError, ValueError) as exc:
            # Every wheel and the host are validated before the manifest is
            # built, so a refusal writes nothing: an existing manifest is left
            # untouched rather than replaced by a partial or inconsistent one.
            print(f"Error: {exc}", file=sys.stderr)
            return 1
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
