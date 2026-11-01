"""Shared artifact fixture for the native coordinator tests (#2404).

Both native test modules build the shell's artifact set here, so neither
imports the other. ``rehash`` recomputes the two digests after a test swaps a
file's bytes, because the coordinator checks each digest against the bytes it
copies.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
from pathlib import Path

import ciao
from ciao import native_installer as native


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_artifacts(tmp_path: Path) -> native.NativeInstallArtifacts:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    uv = root / "uv"
    uv.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(uv, 0o755)
    wheel = root / f"ciaobot-{ciao.__version__}-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    manifest = root / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    signature = root / "manifest.json.sig"
    signature.write_text("sig", encoding="utf-8")
    host_archive = root / "host.tar.gz"
    host_archive.write_bytes(b"archive")
    return native.NativeInstallArtifacts(
        version=ciao.__version__,
        uv=uv,
        wheel=wheel,
        manifest=manifest,
        signature=signature,
        host_archive=host_archive,
        uv_sha256=sha256_of(uv),
        wheel_sha256=sha256_of(wheel),
    )


def rehash(artifacts: native.NativeInstallArtifacts) -> native.NativeInstallArtifacts:
    """The same artifacts with digests recomputed from their current bytes."""
    return dataclasses.replace(
        artifacts,
        uv_sha256=sha256_of(artifacts.uv),
        wheel_sha256=sha256_of(artifacts.wheel),
    )
