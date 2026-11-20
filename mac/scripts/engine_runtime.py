#!/usr/bin/env python3
"""Offline adapter for the repository-reviewed Apple Silicon Python runtime."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

MAX_PIN_BYTES = 65_536
MANIFEST = Path(__file__).resolve().parents[1] / "engine-runtime.json"
_EXPECTED_TOP = {
    "schema_version", "upstream", "release", "python_version", "target", "flavor",
    "filename", "url", "size", "sha256", "checksum_url", "release_metadata_url",
    "archive_root", "minimum_macos", "provenance", "license",
}
_EXPECTED_PROVENANCE = {"official_metadata_review", "source_revision", "source_revision_status"}
_EXPECTED_LICENSE = {"member", "size", "sha256"}


class RuntimePinError(Exception):
    """A fixed, non-descriptive refusal for invalid runtime pins."""


def _load_extractor():
    path = Path(__file__).resolve().with_name("engine_artifact.py")
    spec = importlib.util.spec_from_file_location("engine_artifact", path)
    if spec is None or spec.loader is None:
        raise ImportError("engine artifact helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine_artifact = _load_extractor()


def _read_pin_bytes() -> bytes:
    with MANIFEST.open("rb") as manifest:
        raw = manifest.read(MAX_PIN_BYTES + 1)
    if len(raw) > MAX_PIN_BYTES:
        raise RuntimePinError()
    return raw


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimePinError()
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise RuntimePinError()


def _validate(pin: Any) -> dict[str, Any]:
    if type(pin) is not dict or set(pin) != _EXPECTED_TOP:
        raise RuntimePinError()
    if type(pin["schema_version"]) is not int or pin["schema_version"] != 1:
        raise RuntimePinError()
    strings = _EXPECTED_TOP - {"schema_version", "size", "provenance", "license"}
    if any(type(pin[key]) is not str for key in strings):
        raise RuntimePinError()
    if type(pin["size"]) is not int or not 0 < pin["size"] <= engine_artifact.MAX_COMPRESSED:
        raise RuntimePinError()
    if re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]) is None:
        raise RuntimePinError()
    provenance, license_record = pin["provenance"], pin["license"]
    if type(provenance) is not dict or set(provenance) != _EXPECTED_PROVENANCE:
        raise RuntimePinError()
    if provenance != {
        "official_metadata_review": "coordinator comparison recorded in #3128 planning evidence",
        "source_revision": None,
        "source_revision_status": "not recorded in supplied review evidence",
    }:
        raise RuntimePinError()
    if type(license_record) is not dict or set(license_record) != _EXPECTED_LICENSE:
        raise RuntimePinError()
    if (license_record["member"] != "python/lib/python3.12/LICENSE.txt"
            or type(license_record["size"]) is not int or license_record["size"] != 13936
            or license_record["sha256"] != "3b2f81fe21d181c499c59a256c8e1968455d6689d269aa85373bfb6af41da3bf"):
        raise RuntimePinError()
    if (pin["upstream"] != "astral-sh/python-build-standalone"
            or re.fullmatch(r"[0-9]{8}", pin["release"]) is None
            or re.fullmatch(r"3\.12\.(?:0|[1-9][0-9]*)", pin["python_version"]) is None
            or pin["target"] != "aarch64-apple-darwin"
            or pin["flavor"] != "install_only"
            or pin["archive_root"] != "python/"
            or pin["minimum_macos"] != "13.0"):
        raise RuntimePinError()
    filename = f"cpython-{pin['python_version']}+{pin['release']}-{pin['target']}-{pin['flavor']}.tar.gz"
    base = f"https://github.com/astral-sh/python-build-standalone/releases/download/{pin['release']}/"
    expected = {
        "filename": filename,
        "url": base + filename.replace("+", "%2B"),
        "checksum_url": base + "SHA256SUMS",
        "release_metadata_url": f"https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/{pin['release']}",
    }
    if any(pin[key] != value for key, value in expected.items()):
        raise RuntimePinError()
    return pin


def load_runtime_pin() -> dict:
    try:
        raw = _read_pin_bytes()
        if len(raw) > MAX_PIN_BYTES:
            raise RuntimePinError()
        pin = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_reject_constant)
        return _validate(pin)
    except RuntimePinError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise RuntimePinError() from exc


def extract_pinned_runtime(archive: Path, destination: Path) -> None:
    pin = load_runtime_pin()
    engine_artifact.verify_and_extract(
        archive, destination, sha256=pin["sha256"], size=pin["size"]
    )


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        print("runtime verification failed", file=sys.stderr)
        raise SystemExit(2)


def main() -> int:
    parser = _Parser(allow_abbrev=False, description="Verify and extract the fixed offline runtime pin.")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    if not args.destination or any(part in (".", "..") for part in args.destination.split("/")):
        print("runtime verification failed", file=sys.stderr)
        return 1
    try:
        extract_pinned_runtime(Path(args.archive), Path(args.destination))
    except (RuntimePinError, engine_artifact.ArtifactError, OSError, ValueError):
        print("runtime verification failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
