"""The record of provider CLI versions a release was tested with.

``ciao/stock/provider-versions.json`` ships in the wheel. ``ciao.release``
writes it on ``--apply``; this module only owns the schema, the strict parser
and the loader. It never probes a binary.
"""

from __future__ import annotations

import importlib.resources
import json
import re
from collections.abc import Mapping
from datetime import date
from typing import Any

SCHEMA_VERSION = 1
RECORD_NAME = "provider-versions.json"
PROVIDERS = ("opencode", "claude")
UNTESTED_REASONS = frozenset(
    {"not_recorded", "not_installed", "unpublished", "unreadable"}
)
STABLE_VERSION_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")

_TOP_KEYS = frozenset({"schema", "ciaobot", "recorded", "providers"})
_TESTED_KEYS = {
    "opencode": frozenset({"status", "version", "package"}),
    "claude": frozenset({"status", "version", "source"}),
}
_UNTESTED_KEYS = frozenset({"status", "reason"})
_CLI_VERSION_RE = re.compile(r"(?<![\d.])v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)")


class ProviderRecordError(ValueError):
    """The provider-version record is missing or does not match the schema."""


def parse_cli_version(text: str) -> str | None:
    """The version in a CLI's ``--version`` output, without a leading ``v``."""
    match = _CLI_VERSION_RE.search(text or "")
    return match.group(1) if match else None


def _fail(field: str, why: str) -> ProviderRecordError:
    return ProviderRecordError(f"provider record: {field} {why}")


def validate_record(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise _fail("record", "must be an object")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise _fail("record", f"has unknown keys {sorted(unknown)}")
    missing = _TOP_KEYS - set(data)
    if missing:
        raise _fail("record", f"is missing keys {sorted(missing)}")
    if type(data["schema"]) is not int or data["schema"] != SCHEMA_VERSION:
        raise _fail("schema", f"must be {SCHEMA_VERSION}")
    if not isinstance(data["ciaobot"], str) or not STABLE_VERSION_RE.fullmatch(
        data["ciaobot"]
    ):
        raise _fail("ciaobot", "must be an X.Y.Z version")
    if not isinstance(data["recorded"], str):
        raise _fail("recorded", "must be a string")
    if data["recorded"]:
        try:
            date.fromisoformat(data["recorded"])
        except ValueError:
            raise _fail("recorded", "must be an ISO date") from None
    providers = data["providers"]
    if not isinstance(providers, dict):
        raise _fail("providers", "must be an object")
    if set(providers) != set(PROVIDERS):
        raise _fail("providers", f"must be exactly {list(PROVIDERS)}")
    any_tested = False
    for name in PROVIDERS:
        entry = providers[name]
        if not isinstance(entry, dict):
            raise _fail(f"providers.{name}", "must be an object")
        status = entry.get("status")
        if status == "tested":
            any_tested = True
            extra = set(entry) - _TESTED_KEYS[name]
            if extra:
                raise _fail(f"providers.{name}", f"has unknown keys {sorted(extra)}")
            version = entry.get("version")
            if not isinstance(version, str) or not STABLE_VERSION_RE.fullmatch(version):
                raise _fail(
                    f"providers.{name}.version", "must be a stable X.Y.Z version"
                )
            for key in _TESTED_KEYS[name] - {"status", "version"}:
                if key in entry and not isinstance(entry[key], str):
                    raise _fail(f"providers.{name}.{key}", "must be a string")
        elif status == "untested":
            extra = set(entry) - _UNTESTED_KEYS
            if extra:
                raise _fail(
                    f"providers.{name}",
                    f"is untested and must not carry {sorted(extra)}",
                )
            if entry.get("reason") not in UNTESTED_REASONS:
                raise _fail(
                    f"providers.{name}.reason",
                    f"must be one of {sorted(UNTESTED_REASONS)}",
                )
        else:
            raise _fail(f"providers.{name}.status", "must be 'tested' or 'untested'")
    if not data["recorded"] and any_tested:
        raise _fail("recorded", "may be empty only when every provider is untested")
    return data


def load_record() -> dict[str, Any]:
    try:
        text = (
            importlib.resources.files("ciao.stock")
            .joinpath(RECORD_NAME)
            .read_text(encoding="utf-8")
        )
        data = json.loads(text)
    except (OSError, ValueError) as exc:
        raise ProviderRecordError(f"provider record unreadable: {exc}") from exc
    return validate_record(data)


def tested_version(record: Mapping[str, Any], provider: str) -> str | None:
    entry = record.get("providers", {}).get(provider)
    if isinstance(entry, Mapping) and entry.get("status") == "tested":
        version = entry.get("version")
        return version if isinstance(version, str) else None
    return None


def render_record(record: Mapping[str, Any]) -> str:
    """One canonical byte form, so release diffs are stable."""
    return json.dumps(record, indent=2, sort_keys=False) + "\n"
