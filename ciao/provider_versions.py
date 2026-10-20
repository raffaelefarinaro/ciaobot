"""The record of provider CLI versions a release was tested with.

``ciao/stock/provider-versions.json`` ships in the wheel. ``ciao.release``
writes it on ``--apply``; this module owns the schema, the strict parser and
the loader, plus the offline runtime comparison of the installed CLIs against
the record (``release_check``, ``current_checks``). Nothing here touches the
network or installs anything.
"""

from __future__ import annotations

import importlib.resources
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

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


_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")


def _semver_key(text: str) -> tuple[tuple[int, int, int], list[str] | None] | None:
    match = _SEMVER_RE.fullmatch(text)
    if not match:
        return None
    core = (int(match[1]), int(match[2]), int(match[3]))
    return core, match[4].split(".") if match[4] else None


def _compare_identifiers(a: list[str], b: list[str]) -> int:
    for left, right in zip(a, b):
        if left == right:
            continue
        left_num, right_num = left.isdigit(), right.isdigit()
        if left_num and right_num:
            return -1 if int(left) < int(right) else 1
        if left_num != right_num:
            return -1 if left_num else 1
        return -1 if left < right else 1
    return (len(a) > len(b)) - (len(a) < len(b))


def compare_versions(a: str, b: str) -> int | None:
    """-1, 0 or 1 by SemVer precedence, or None when either side is not X.Y.Z[-pre]."""
    left, right = _semver_key(a), _semver_key(b)
    if left is None or right is None:
        return None
    if left[0] != right[0]:
        return -1 if left[0] < right[0] else 1
    pre_a, pre_b = left[1], right[1]
    if pre_a is None and pre_b is None:
        return 0
    if pre_a is None:
        return 1
    if pre_b is None:
        return -1
    return _compare_identifiers(pre_a, pre_b)


def update_command(provider: str, tested: str, binary: str) -> str:
    """The terminal text that brings ``provider`` to the tested version.

    ``tested`` is validated before it is interpolated and the binary is quoted,
    because the host terminal runs this string as shell text.
    """
    from ciao import host_terminal
    from ciao.tool_path import resolve_on_terminal_path

    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    if not STABLE_VERSION_RE.fullmatch(tested):
        raise ValueError(f"tested version {tested!r} is not a stable X.Y.Z version")
    try:
        on_path = resolve_on_terminal_path(provider)
    except Exception:
        on_path = None
    if not binary:
        raise ValueError("update_command needs the resolved binary")
    try:
        same = bool(on_path) and os.path.samefile(on_path or "", binary)
    except OSError:
        same = False
    if same:
        name = provider
    elif host_terminal.host_platform() == host_terminal.PLATFORM_WINDOWS:
        name = "& " + host_terminal._ps_quote(binary)
    else:
        name = shlex.quote(binary)
    return f"{name} upgrade {tested}" if provider == "opencode" else f"{name} update"


def release_check(
    provider: str,
    installed_text: str,
    *,
    bundled: bool = False,
    binary: str = "",
    record: Mapping[str, Any] | None = None,
    running_version: str | None = None,
) -> dict[str, str]:
    """Compare an installed provider CLI with the version the release was tested with.

    Suggestions are suppressed (``untested``) when the record belongs to another
    Ciaobot version, e.g. a release cut with ``--skip-provider-versions`` keeps
    the previous record. A develop checkout or dev install reports the last
    release's version until the next cut, so it compares against that release's
    record; that is safe because ``below_tested`` only ever points up to a
    version a release was tested with, and bundled Claude never gets a command.
    """
    if record is None:
        try:
            record = load_record()
        except ProviderRecordError as exc:
            _warn_once(str(exc))
            return {"state": "untested"}
    if running_version is None:
        import ciao

        running_version = ciao.__version__
    tested = tested_version(record, provider)
    if tested is None or record.get("ciaobot") != running_version:
        return {"state": "untested"}
    if provider == "opencode":
        from ciao.providers.opencode import _server_version_error

        if _server_version_error({"version": tested}) is not None:
            return {"state": "untested"}
    if bundled:
        return {"state": "bundled", "tested": tested}
    if provider == "opencode":
        if _server_version_error({"version": installed_text}) is not None:
            return {"state": "unsupported", "tested": tested}
    installed = parse_cli_version(installed_text)
    order = compare_versions(installed, tested) if installed else None
    if installed is None or order is None:
        return {"state": "unknown", "tested": tested}
    if order < 0:
        return {
            "state": "below_tested",
            "installed": installed,
            "tested": tested,
            "command": update_command(provider, tested, binary),
            "terminal_action": f"{provider}-update",
        }
    return {"state": "current", "installed": installed, "tested": tested}


_warned: set[str] = set()


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        logger.warning("%s", message)


def _probe_version(binary: str) -> str:
    try:
        run = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = (run.stdout or "").strip().splitlines()
    return lines[0] if lines else ""


def current_checks() -> dict[str, dict[str, str]]:
    """``release_check`` for each installed provider; absent providers have no row."""
    from ciao import setup_status
    from ciao.providers.claude import get_bundled_claude_path
    from ciao.providers.opencode import resolve_opencode_binary

    checks: dict[str, dict[str, str]] = {}
    try:
        opencode_binary = resolve_opencode_binary()
    except OSError:
        opencode_binary = None
    if opencode_binary:
        checks["opencode"] = release_check(
            "opencode", _probe_version(opencode_binary), binary=opencode_binary
        )
    claude_path = setup_status.claude_cli_path()
    if claude_path:
        bundled_path = get_bundled_claude_path()
        bundled = bool(bundled_path) and claude_path == str(bundled_path)
        checks["claude"] = release_check(
            "claude",
            "" if bundled else _probe_version(claude_path),
            bundled=bundled,
            binary=claude_path,
        )
    return checks


class CachedChecks:
    def __init__(self, ttl: float, clock: Callable[[], float]) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._value: dict[str, dict[str, str]] | None = None
        self._expires = 0.0

    def __call__(self) -> dict[str, dict[str, str]]:
        with self._lock:
            now = self._clock()
            if self._value is None or now >= self._expires:
                self._value = current_checks()
                self._expires = now + self._ttl
            return self._value

    def invalidate(self) -> None:
        with self._lock:
            self._value = None


def make_cached_checks(
    *, ttl: float = 60.0, clock: Callable[[], float] = time.monotonic
) -> CachedChecks:
    return CachedChecks(ttl, clock)
