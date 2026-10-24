"""Domain operations for the resumable onboarding receipt."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from ciao.onboarding import (
    OnboardingConflictError,
    OnboardingProgress,
    OnboardingReceipt,
    OnboardingStateError,
    Step,
    read_receipt,
    write_receipt,
)
from ciao.workspaces import resolve_workspace_name


class OnboardingTargetNotFound(LookupError):
    """The target is not a registered workspace with a receipt entry."""


class OnboardingTargetClosed(PermissionError):
    """The target is completed or exempt and cannot be reopened."""


class OnboardingFinishRequired(RuntimeError):
    """Completion was requested before the finish step."""


_STEPS = {"location", "runtime", "access_memory", "care", "finish"}


def _root(config: Any) -> Path:
    # The startup path decides where the receipt lives from ``state_path``, which
    # can point outside the workspace root (bootstrap and external runtimes).
    # Reading from ``workspace_root`` here would silently disagree with it.
    return Path(config.state_path.parent)


def get_progress(config: Any) -> OnboardingReceipt:
    receipt = read_receipt(_root(config))
    if receipt is None:
        raise FileNotFoundError("onboarding receipt is not initialized")
    return receipt


def register_new_workspace(config: Any, name: str) -> OnboardingReceipt:
    """Register onboarding for a newly-created workspace, without resetting an entry."""
    receipt = get_progress(config)
    if name in receipt.workspaces:
        return receipt
    workspaces = dict(receipt.workspaces)
    workspaces[name] = OnboardingProgress("in_progress", "runtime")
    return write_receipt(
        _root(config),
        OnboardingReceipt(receipt.revision, receipt.installation, workspaces),
        expected_revision=receipt.revision,
    )


def rename_workspace_progress(
    config: Any, *, source: str, destination: str, expected_revision: int
) -> OnboardingReceipt:
    """Move a renamed workspace's progress, dropping any stale destination entry.

    Runs even when the source has no entry: a name reused from an archive must
    not keep the old completion attached to the renamed workspace.
    """
    receipt = get_progress(config)
    if receipt.revision != expected_revision:
        raise OnboardingConflictError("onboarding receipt revision conflict")
    workspaces = dict(receipt.workspaces)
    progress = workspaces.pop(source, None)
    stale = workspaces.pop(destination, None)
    if progress is not None:
        workspaces[destination] = progress
    if progress is None and stale is None:
        # Nothing to move and nothing stale to drop: do not burn a revision.
        return receipt
    return write_receipt(
        _root(config),
        OnboardingReceipt(receipt.revision, receipt.installation, workspaces),
        expected_revision=expected_revision,
    )


def _select(
    receipt: OnboardingReceipt, target: dict[str, Any], config: Any
) -> tuple[OnboardingProgress, str | None]:
    kind = target.get("kind")
    if kind == "installation" and set(target) == {"kind"}:
        return receipt.installation, None
    if kind != "workspace" or set(target) != {"kind", "name"}:
        raise ValueError("invalid target")
    name = target["name"]
    if not isinstance(name, str) or not name:
        raise ValueError("invalid target")
    try:
        name = resolve_workspace_name(config, name)
    except ValueError as exc:
        raise OnboardingTargetNotFound(name) from exc
    if name not in receipt.workspaces:
        raise OnboardingTargetNotFound(name)
    return receipt.workspaces[name], name


def _mutate(
    config: Any,
    target: dict[str, Any],
    expected_revision: int,
    *,
    step: str | None,
    complete: bool = False,
) -> OnboardingReceipt:
    receipt = get_progress(config)
    progress, name = _select(receipt, target, config)
    # Replaying a completion is the one place a stale revision is accepted: the
    # outcome is already persisted, and a second device finishing the same
    # journey must not be turned into an error.
    if complete and progress.status == "completed":
        return receipt
    if receipt.revision != expected_revision:
        raise OnboardingConflictError("onboarding receipt revision conflict")
    if progress.status != "in_progress":
        raise OnboardingTargetClosed(name or "installation")
    if complete:
        if progress.step != "finish":
            raise OnboardingFinishRequired(name or "installation")
        updated = OnboardingProgress("completed", progress.step)
    else:
        if step not in _STEPS or (name is not None and step == "location"):
            raise ValueError("invalid step")
        if progress.step == step:
            return receipt
        updated = OnboardingProgress(progress.status, cast(Step, step))
    installation = updated if name is None else receipt.installation
    workspaces = dict(receipt.workspaces)
    if name is not None:
        workspaces[name] = updated
    return write_receipt(
        _root(config),
        OnboardingReceipt(receipt.revision, installation, workspaces),
        expected_revision=expected_revision,
    )


def change_step(
    config: Any, *, target: dict[str, Any], expected_revision: int, step: str
) -> OnboardingReceipt:
    return _mutate(config, target, expected_revision, step=step)


def complete_progress(
    config: Any, *, target: dict[str, Any], expected_revision: int
) -> OnboardingReceipt:
    return _mutate(config, target, expected_revision, step=None, complete=True)
