"""Private, resumable onboarding lifecycle receipt.

``write_receipt`` uses a process-local keyed lock for its read/modify/write,
which only coordinates threads. Callers mutating receipt state must also hold
the process-lifetime server instance lock so concurrent processes cannot race.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from ciao.async_reads import keyed_lock
from ciao.os_support.private import mkstemp_private
from ciao.workspaces import WORKSPACE_NAME_RE

Status = Literal["in_progress", "completed", "legacy_exempt"]
Step = Literal["location", "runtime", "access_memory", "care", "finish"]
RECEIPT_FILENAME = "onboarding.json"


class OnboardingStateError(ValueError):
    """Receipt is unreadable or fails its strict schema."""


class OnboardingConflictError(OnboardingStateError):
    """Receipt revision changed since the caller read it."""


@dataclass(frozen=True)
class OnboardingProgress:
    status: Status
    step: Step


@dataclass(frozen=True)
class OnboardingReceipt:
    revision: int
    installation: OnboardingProgress
    workspaces: dict[str, OnboardingProgress]
    schema: int = 1

_STATUSES = {"in_progress", "completed", "legacy_exempt"}
_STEPS = {"location", "runtime", "access_memory", "care", "finish"}


def _path(root: Path) -> Path:
    return root / RECEIPT_FILENAME


def _progress(value: object) -> OnboardingProgress:
    if not isinstance(value, dict) or set(value) != {"status", "step"}:
        raise OnboardingStateError("invalid onboarding progress object")
    status, step = value["status"], value["step"]
    if not isinstance(status, str) or status not in _STATUSES:
        raise OnboardingStateError("invalid onboarding status")
    if not isinstance(step, str) or step not in _STEPS:
        raise OnboardingStateError("invalid onboarding status or step")
    return OnboardingProgress(cast(Status, status), cast(Step, step))


def _parse(data: bytes) -> OnboardingReceipt:
    try:
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) != {"schema", "revision", "installation", "workspaces"}:
            raise ValueError("invalid receipt shape")
        if type(value["schema"]) is not int or value["schema"] != 1:
            raise ValueError("unknown schema")
        revision = value["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("invalid revision")
        if not isinstance(value["workspaces"], dict) or any(
            not isinstance(k, str) or not WORKSPACE_NAME_RE.fullmatch(k)
            for k in value["workspaces"]
        ):
            raise ValueError("invalid workspaces")
        return OnboardingReceipt(
            revision,
            _progress(value["installation"]),
            {k: _progress(v) for k, v in value["workspaces"].items()},
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
        raise OnboardingStateError(f"invalid onboarding receipt: {exc}") from exc


def _payload(receipt: OnboardingReceipt) -> bytes:
    if type(receipt.schema) is not int or receipt.schema != 1:
        raise OnboardingStateError("unknown onboarding schema")
    if isinstance(receipt.revision, bool) or not isinstance(receipt.revision, int) or receipt.revision < 0:
        raise OnboardingStateError("invalid onboarding revision")

    def progress(item: OnboardingProgress) -> dict[str, str]:
        if not isinstance(item, OnboardingProgress):
            raise OnboardingStateError("invalid onboarding progress")
        status = _progress({"status": item.status, "step": item.step})
        return {"status": status.status, "step": status.step}

    if not isinstance(receipt.workspaces, dict):
        raise OnboardingStateError("invalid workspaces")
    workspaces: dict[str, dict[str, str]] = {}
    for name, item in receipt.workspaces.items():
        if not isinstance(name, str) or not WORKSPACE_NAME_RE.fullmatch(name):
            raise OnboardingStateError("invalid workspace key")
        workspaces[name] = progress(item)
    document = {
        "schema": receipt.schema,
        "revision": receipt.revision,
        "installation": progress(receipt.installation),
        "workspaces": workspaces,
    }
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()


def read_receipt(runtime_root: Path) -> OnboardingReceipt | None:
    try:
        data = _path(runtime_root).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OnboardingStateError(f"cannot read onboarding receipt: {exc}") from exc
    return _parse(data)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = mkstemp_private(dir=path.parent, prefix=".onboarding.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_receipt(runtime_root: Path, *, established_install: bool) -> OnboardingReceipt:
    path = _path(runtime_root)
    with keyed_lock(f"onboarding:{path.resolve()}"):
        current = read_receipt(runtime_root)
        if current is not None:
            return current
        status: Status = "legacy_exempt" if established_install else "in_progress"
        receipt = OnboardingReceipt(0, OnboardingProgress(status, "location"), {})
        _atomic_write(path, _payload(receipt))
        return OnboardingReceipt(receipt.revision, receipt.installation, dict(receipt.workspaces))


def write_receipt(runtime_root: Path, receipt: OnboardingReceipt, *, expected_revision: int) -> OnboardingReceipt:
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise OnboardingStateError("invalid expected revision")
    _payload(receipt)
    path = _path(runtime_root)
    with keyed_lock(f"onboarding:{path.resolve()}"):
        current = read_receipt(runtime_root)
        if current is None or current.revision != expected_revision:
            raise OnboardingConflictError("onboarding receipt revision conflict")
        if receipt.revision != expected_revision:
            raise OnboardingConflictError("receipt must carry expected revision")
        updated = OnboardingReceipt(
            expected_revision + 1, receipt.installation, dict(receipt.workspaces)
        )
        # Validate and serialize the exact incremented snapshot before replacement.
        serialized = _payload(updated)
        _atomic_write(path, serialized)
        return updated


def ensure_startup_receipt(runtime_root: Path, *, workspace_root: Path, bootstrap_mode: bool) -> OnboardingReceipt:
    established = False
    if not bootstrap_mode:
        from ciao.setup_marker import WORKSPACE_REGISTRY_FILENAME, is_set_up_workspace

        # A pre-registry install can be discovered through the installed
        # service's private `.env`; `is_set_up_workspace` reads that file
        # without exporting it, so ambient `CIAO_WORKSPACES` alone is not
        # enough evidence. Notes folders and unrelated `.env` files stay new.
        established = is_set_up_workspace(workspace_root)
        registry = runtime_root / WORKSPACE_REGISTRY_FILENAME
        established = established or registry.is_file() or bool(
            os.environ.get("CIAO_WORKSPACES", "").strip()
        )
        established = established or any(
            (runtime_root / name).is_file() for name in ("web_projects.json", "state.json")
        )
    return ensure_receipt(runtime_root, established_install=established)
