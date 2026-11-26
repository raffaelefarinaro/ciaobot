"""Home-screen housekeeping actions: the operator-facing strip of fixes.

Why this exists
---------------
Several things Ciaobot already detects about its own install — an update, a
missing workspace root, a stale skill — are acted on from the Settings audit or a terminal, which is not
where the operator is looking. This module turns those detected conditions
into concrete, clearable actions the PWA renders as tiles on the home screen.
A tile either runs the fix, opens a chat that talks it through, or both, and
disappears only when re-detection says the underlying condition is gone. There
is no ``done`` flag and no first-run suppression: a tile is a view of the
machine, not a record of the operator's intent.

The one hard constraint is cheapness. Detection runs on app open and on every
window focus, so no detector may walk the vault or diff a mirrored tree. The
detectors here read a receipt, compare a registry value, or list a small known
folder. Expensive work happens on the button press, never to decide whether to
draw the button.

Contract, enforced in tests
---------------------------
1. Every detector can reach zero: a condition the user may legitimately decline
   is not an action.
2. Detection is idempotent under re-detection: two passes with nothing changed
   produce byte-identical actions, ids included.
3. Every action offers at least one of ``run_label`` / ``chat_prompt``. An
   action with neither is a notice and belongs in the Settings audit.
4. No detector walks the vault (the ``scan_vault`` spy in the tests proves it).
5. An advisory provider tile is dismissible per target version, and the receipt
   never silences a newer target.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class OperatorAction:
    """One clearable condition the operator can act on.

    ``id`` is stable and scope-suffixed (``provider-update:claude``) so a
    client can accept it by id across renders. ``severity`` is a sort key only,
    never rendered. ``glyph`` is one character shown next to the amber rule.
    """

    id: str
    kind: str
    severity: int
    title: str
    detail: str
    glyph: str
    workspace: str
    run_label: str = ""
    chat_label: str = ""
    chat_prompt: str = ""
    # A view this tile can send the operator to, when a purpose-built surface
    # already exists. The proposal queue has had per-row accept/dismiss, a
    # destination picker, a leak confirm and batch operations since P5, and the
    # tiles offered only "Review in chat" — so the operator was told to discuss
    # 109 items in prose while the buttons for them sat one route away,
    # unreachable from the one place that mentions them.
    view_label: str = ""
    view_route: str = ""
    # An external page this action links to (release notes, the repository).
    # Rendered like a run button but opens a new tab; pressing it may also
    # record the run (see the GitHub star nudge).
    link_label: str = ""
    link_url: str = ""
    # A "not now" button for ask-style actions: records a suppression receipt
    # via dismiss_action() instead of running a fix.
    dismiss_label: str = ""
    # Which button leads the tile. Empty keeps the default order (run, link and
    # view buttons filled, chat as a chip below). "view" puts the view button
    # first as the filled primary and demotes the link to a chip: on the update
    # tile the forward action is updating, and release notes are supporting
    # reading.
    primary: str = ""
    # Unmissable and not dismissible: a precondition the install cannot get past
    # on its own. Deliberately NOT an app-wide lock — the one realistic cause is
    # an uncommitted vault, and locking the app would take away the assistant the
    # operator needs to fix it. Prominent, permanent, and retryable beats modal.
    blocking: bool = False
    # A host-terminal action id (``ciao.host_terminal``) the client may offer
    # when the browser is on the host, and the same command as copyable text,
    # always shown beside it. Only the provider-update tile sets them.
    terminal_action: str = ""
    command: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Serializable form for the API payload."""
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "glyph": self.glyph,
            "workspace": self.workspace,
            "run_label": self.run_label,
            "chat_label": self.chat_label,
            "chat_prompt": self.chat_prompt,
            "view_label": self.view_label,
            "view_route": self.view_route,
            "link_label": self.link_label,
            "link_url": self.link_url,
            "dismiss_label": self.dismiss_label,
            "primary": self.primary,
            "blocking": self.blocking,
            "terminal_action": self.terminal_action,
            "command": self.command,
        }


@dataclass
class DetectionContext:
    """Everything a detector may read, none of it expensive.

    ``config`` is a CiaoConfig-like object exposing the workspace registry and
    vault/workspace roots. ``runtime_dir`` holds the receipts (falls
    back to the config's ``state_path`` parent). ``schedule_store`` is any
    object exposing ``list_entries()`` (a ScheduleManager or ScheduleStore).
    ``package_status`` is a zero-arg callable returning the package-status dict
    from ``package_version.package_status``, pre-cached; when omitted the
    package detector declines to guess and stays silent. ``provider_checks`` is
    the same idea for the provider CLIs: a zero-arg callable returning
    ``{provider: release_check dict}`` (``provider_versions.make_cached_checks``)
    that may expose ``invalidate()``; ``None`` keeps the provider detector silent.
    """

    config: Any
    runtime_dir: Path | None = None
    schedule_store: Any = None
    package_status: Callable[[], dict[str, Any]] | None = None
    provider_checks: Any = None
    now: datetime | None = None

    @property
    def runtime(self) -> Path | None:
        if self.runtime_dir is not None:
            return self.runtime_dir
        state = getattr(self.config, "state_path", None)
        if state is not None:
            return Path(state).parent
        return None


# -- registry ----------------------------------------------------------------


def detect_actions(context: DetectionContext) -> list[OperatorAction]:
    """Run every registered detector and return the sorted, deduplicated list.

    A detector that raises is logged and skipped so one broken detector never
    sinks the strip. Results are sorted by ``(severity, id)``. No truncation:
    a list that routinely exceeds a handful is the signal this should be a
    page, and silently dropping the tail would hide it.
    """
    actions: list[OperatorAction] = []
    for detect in _DETECTORS:
        try:
            actions.extend(detect(context))
        except Exception:  # noqa: BLE001 — one bad detector must not fail the strip
            logger.exception("operator action detector %s failed", getattr(detect, "__name__", "?"))
    actions.sort(key=lambda action: (action.severity, action.id))
    logger.info("operator actions: %d detected", len(actions))
    return actions


# -- package update ----------------------------------------------------------

_PACKAGE_SEVERITY = 30


def _detect_package_update(context: DetectionContext) -> list[OperatorAction]:
    """Surfaces an installable release when one is available.

    Reads the cached package status; the caller owns the cache so detection
    never blocks on GitHub. With no status provider the condition is unknown,
    which is not a condition to act on, so it stays silent.
    """
    if context.package_status is None:
        return []
    try:
        status = context.package_status()
    except Exception:  # noqa: BLE001 — a broken probe must not fail the strip
        logger.exception("operator actions: package status probe failed")
        return []
    if not status.get("update_available"):
        return []
    latest = status.get("latest_version") or ""
    from ciao.package_version import latest_release_redirect_url

    return [
        OperatorAction(
            id="package-update",
            kind="package-update",
            severity=_PACKAGE_SEVERITY,
            title="A new Ciaobot version is ready to install",
            detail=f"Version {latest} is available and the current install is "
            f"{status.get('current_version') or 'unknown'}.",
            glyph="▲",
            workspace="",
            link_label="Release notes",
            link_url=latest_release_redirect_url(),
            view_label="Update in Settings",
            view_route="/settings",
            primary="view",
        )
    ]


# -- provider update ---------------------------------------------------------

_PROVIDER_UPDATE_SEVERITY = 40
_PROVIDER_LABELS = {"opencode": "OpenCode", "claude": "Claude Code"}
_PROVIDER_DISMISSALS = "provider-update-dismissals.json"
_PROVIDER_ACTION_PREFIX = "provider-update:"


def _provider_dismissals_path(context: DetectionContext) -> Path | None:
    runtime = context.runtime
    if runtime is None:
        return None
    return Path(runtime) / _PROVIDER_DISMISSALS


def _read_provider_dismissals(context: DetectionContext) -> dict[str, str]:
    path = _provider_dismissals_path(context)
    if path is None:
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("operator actions: could not read %s", path, exc_info=True)
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        key: value
        for key, value in loaded.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _write_provider_dismissal(context: DetectionContext, provider: str, target: str) -> None:
    path = _provider_dismissals_path(context)
    if path is None:
        return
    receipt = _read_provider_dismissals(context)
    receipt[provider] = target
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt), encoding="utf-8", newline="")
    except OSError:
        logger.exception("operator actions: could not write provider dismissal")


def _provider_checks(context: DetectionContext) -> dict[str, dict[str, str]]:
    checks = context.provider_checks() if context.provider_checks is not None else {}
    return checks if isinstance(checks, dict) else {}


def _detect_provider_updates(context: DetectionContext) -> list[OperatorAction]:
    """One advisory tile per provider CLI older than the release-tested version.

    Reads the cached check, so a warm cache spawns nothing; with no checker wired
    the condition is unknown and the detector stays silent. "Not now" is a receipt
    keyed to the tested version it was pressed for, so a release that tests a newer
    version shows the tile again.
    """
    if context.provider_checks is None:
        return []
    try:
        checks = _provider_checks(context)
    except Exception:  # noqa: BLE001 — a broken probe must not fail the strip
        logger.exception("operator actions: provider check failed")
        return []
    dismissed = _read_provider_dismissals(context)
    actions: list[OperatorAction] = []
    for provider in sorted(_PROVIDER_LABELS):
        check = checks.get(provider)
        if not isinstance(check, dict) or check.get("state") != "below_tested":
            continue
        tested = str(check.get("tested", ""))
        if dismissed.get(provider) == tested:
            continue
        label = _PROVIDER_LABELS[provider]
        actions.append(
            OperatorAction(
                id=f"{_PROVIDER_ACTION_PREFIX}{provider}",
                kind="provider-update",
                severity=_PROVIDER_UPDATE_SEVERITY,
                title=f"Update {label}",
                detail=(
                    f"{label} {check.get('installed', '')} is older than {tested}, "
                    "the version this Ciaobot release was tested with."
                ),
                glyph="▲",
                workspace="",
                terminal_action=str(check.get("terminal_action", "")),
                command=str(check.get("command", "")),
                run_label="Check again",
                dismiss_label="Not now",
            )
        )
    return actions


def _provider_of(action_id: str) -> str:
    provider = action_id.removeprefix(_PROVIDER_ACTION_PREFIX)
    if provider not in _PROVIDER_LABELS:
        raise ValueError(f"unknown operator action id: {action_id}")
    return provider


def _run_provider_recheck(context: DetectionContext, provider: str) -> tuple[dict[str, Any], str]:
    """Drop the cached check and read it again, so a finished update clears the tile."""
    invalidate = getattr(context.provider_checks, "invalidate", None)
    if callable(invalidate):
        invalidate()
    check = _provider_checks(context).get(provider) or {}
    state = str(check.get("state", "unknown"))
    label = _PROVIDER_LABELS[provider]
    if state == "current":
        return {"state": state}, f"{label} is up to date."
    if state == "below_tested":
        return {"state": state}, (
            f"{label} {check.get('installed', '')} is still older than {check.get('tested', '')}."
        )
    if state == "unsupported":
        return {"state": state}, f"{label} is not compatible with this Ciaobot version."
    return {"state": state}, f"Could not verify {label}'s version."


def _dismiss_provider_update(context: DetectionContext, provider: str) -> tuple[dict[str, Any], str]:
    """Hide the tile for the target version it currently shows, and no newer one."""
    try:
        check = _provider_checks(context).get(provider) or {}
    except Exception as exc:  # noqa: BLE001 — a broken probe is not a 500
        logger.exception("operator actions: provider check failed")
        raise ValueError(f"could not check {provider}: {exc}") from exc
    tested = check.get("tested")
    if check.get("state") != "below_tested" or not isinstance(tested, str) or not tested:
        raise ValueError(f"no provider update to dismiss for {provider}")
    _write_provider_dismissal(context, provider, tested)
    return {"dismissed": tested}, "Hidden until a newer tested version ships."


# -- GitHub star nudge ------------------------------------------------------

# Promotional, so it sorts after every machine-condition tile (the strip is
# ordered by ascending severity). A star is the cheapest way a happy user can
# help the project, but it is an ask, not a condition — hence the receipt.
_STAR_NUDGE_SEVERITY = 80
# "Later" suppresses the ask for this long, then it may surface once more.
_STAR_SNOOZE_DAYS = 30


def _star_receipt_path(context: DetectionContext) -> Path | None:
    runtime = context.runtime
    if runtime is None:
        return None
    return Path(runtime) / "star-receipt.json"


def _read_star_receipt(context: DetectionContext) -> dict[str, Any]:
    path = _star_receipt_path(context)
    if path is None:
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_star_receipt(context: DetectionContext, status: str) -> None:
    path = _star_receipt_path(context)
    if path is None:
        return
    now = context.now or datetime.now(UTC)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"status": status, "at": now.isoformat()}),
            encoding="utf-8", newline="",
        )
    except OSError:
        logger.exception("operator actions: could not write star receipt")


def _detect_github_star(context: DetectionContext) -> list[OperatorAction]:
    """A one-time ask, not a machine condition: after setup has completed, a
    GitHub star is the cheapest way for a happy user to help the project.

    Receipt-gated like every one-timer: ``starred`` silences it for good, and
    "Later" snoozes it instead of nagging on every home visit. Bootstrap
    installs (the first-run wizard) stay silent — the ask only makes sense
    once there is something to be happy about.
    """
    config = context.config
    if getattr(config, "bootstrap_mode", False):
        return []
    receipt = _read_star_receipt(context)
    status = str(receipt.get("status", ""))
    if status == "starred":
        return []
    if status == "later":
        try:
            at = datetime.fromisoformat(str(receipt.get("at", "")))
        except ValueError:
            at = None
        now = context.now or datetime.now(UTC)
        if at is not None and now - at < timedelta(days=_STAR_SNOOZE_DAYS):
            return []
    from ciao.package_version import latest_release_redirect_url

    repo_url = latest_release_redirect_url().removesuffix("/releases/latest")
    return [
        OperatorAction(
            id="github-star",
            kind="github-star",
            severity=_STAR_NUDGE_SEVERITY,
            title="Enjoying Ciaobot?",
            detail=(
                "If Ciaobot is working well for you, a GitHub star helps other "
                "developers discover it."
            ),
            glyph="★",
            workspace="",
            run_label="Star on GitHub",
            link_label="Open repository",
            link_url=repo_url,
            dismiss_label="Later",
        )
    ]


# -- Mac app switch ----------------------------------------------------------

# Between a package update (30) and provider updates (40): it is a machine
# condition of the install, but moving to the app is bigger than applying a
# release, so it yields to the update tile and its own restart flow.
_MAC_APP_SWITCH_SEVERITY = 35
# "Not now" suppresses the ask for this long, then it may surface once more.
MAC_APP_SWITCH_SNOOZE_DAYS = 30


def _mac_switch_receipt_path(context: DetectionContext) -> Path | None:
    runtime = context.runtime
    if runtime is None:
        return None
    return Path(runtime) / "mac-app-switch-receipt.json"


def _read_mac_switch_receipt(context: DetectionContext) -> dict[str, Any]:
    path = _mac_switch_receipt_path(context)
    if path is None:
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_mac_switch_receipt(context: DetectionContext, status: str) -> None:
    path = _mac_switch_receipt_path(context)
    if path is None:
        return
    now = context.now or datetime.now(UTC)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"status": status, "at": now.isoformat()}),
            encoding="utf-8", newline="",
        )
    except OSError:
        logger.exception("operator actions: could not write mac app switch receipt")


def _detect_mac_app_switch(context: DetectionContext) -> list[OperatorAction]:
    """A Mac install the 1.x installer set up should move into Ciaobot for Mac.

    The condition lives on the engine, decided by
    ``package_version.needs_mac_app_switch`` from where the engine is
    installed: only an installer-managed engine on an Apple-silicon Mac emits
    it, so a Linux engine, the app's bundled engine and a checkout never do.
    Apple silicon only, because the 2.0 app runs a local engine there alone
    (#3129) — an Intel Mac would download an app that cannot take over.

    It is ask-shaped, not a fix, so it works like the star nudge: link-only
    (the engine downloads and installs nothing), a shared "Not now" receipt
    that snoozes every device for 30 days, and silence while the first-run
    wizard is up. Remote viewers of the same engine see the row too, which the
    copy says plainly: the install happens on the computer running Ciaobot.
    """
    if getattr(context.config, "bootstrap_mode", False):
        return []
    from ciao import package_version

    # Called through the module attribute so tests can monkeypatch it.
    if not package_version.needs_mac_app_switch():
        return []
    receipt = _read_mac_switch_receipt(context)
    status = str(receipt.get("status", ""))
    if status == "later":
        try:
            at = datetime.fromisoformat(str(receipt.get("at", "")))
        except ValueError:
            at = None
        now = context.now or datetime.now(UTC)
        if at is not None and now - at < timedelta(days=MAC_APP_SWITCH_SNOOZE_DAYS):
            return []
    return [
        OperatorAction(
            id="mac-app-switch",
            kind="mac-app-switch",
            severity=_MAC_APP_SWITCH_SEVERITY,
            title="Switch to the new Ciaobot app",
            # The row renders only the first line and it arrives ellipsised, so
            # the reassurance leads: it survives the cut.
            detail=(
                "Your workspaces, chats and settings are kept. Install Ciaobot "
                "for Mac on the computer running Ciaobot and it takes over from "
                "this install."
            ),
            glyph="▲",
            workspace="",
            link_label="Download for Mac",
            link_url=package_version.MAC_APP_DOWNLOAD_URL,
            dismiss_label="Not now",
        )
    ]


def _dismiss_mac_app_switch(context: DetectionContext) -> tuple[dict[str, Any], str]:
    _write_mac_switch_receipt(context, "later")
    return {"status": "later"}, "Hidden for 30 days."


# -- missed one-time schedules ----------------------------------------------


def _schedule_display_name(entry: Any) -> str:
    """Human label for a schedule: title > prompt snippet."""
    title = str(getattr(entry, "title", "") or "").strip()
    if title:
        return title
    raw = str(getattr(entry, "prompt", "") or "").strip()
    if raw:
        first = raw.splitlines()[0].strip() if raw.splitlines() else raw
        if len(first) > 60:
            return first[:57] + "…"
        return first
    return str(getattr(entry, "schedule_id", ""))


def _once_target_datetime(entry: Any) -> datetime | None:
    """The aware fire time of a ``once`` schedule, or ``None`` if malformed.

    A one-time reminder fires at ``daily_time_utc`` on ``run_at_date`` in
    ``timezone_name`` — the same target ``schedules.compute_next_run`` builds
    for ``once`` entries. Treating midnight of ``run_at_date`` as the target
    would mark a reminder due later today as already missed and let "Fire now"
    dispatch (and delete) it early.
    """
    run_at_date = str(getattr(entry, "run_at_date", "") or "").strip()
    if not run_at_date:
        return None
    try:
        tz = ZoneInfo(entry.timezone_name or "UTC")
        target_date = datetime.fromisoformat(run_at_date).date()
        hh, mm = str(getattr(entry, "daily_time_utc", "") or "00:00").split(":", 1)
        target_h, target_m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    return datetime(
        target_date.year, target_date.month, target_date.day,
        target_h, target_m, tzinfo=tz,
    )


def _detect_missed_schedules(context: DetectionContext) -> list[OperatorAction]:
    """One collapsed tile for every missed one-time schedule.

    A ``once`` schedule whose target time has passed and which never fired is a
    stale reminder that would otherwise sit in the schedule list forever, or
    auto-fire un-backdated on the next server start. The detector counts them
    by reading the schedule store, never by dispatching. One tile for the whole
    pile, so a burst of missed one-timers does not crowd the strip.
    """
    store = context.schedule_store
    if store is None:
        return []
    try:
        entries = store.list_entries()
    except Exception:  # noqa: BLE001 — a broken store must not fail the strip
        logger.exception("operator actions: schedule store read failed")
        return []
    now = context.now or datetime.now(UTC)
    missed: list[Any] = []
    for entry in entries:
        if entry.frequency != "once" or not entry.enabled:
            continue
        if entry.last_triggered_on:
            continue
        if not entry.run_at_date:
            continue
        target = _once_target_datetime(entry)
        if target is None:
            continue
        if now >= target:
            missed.append(entry)
    if not missed:
        return []
    # Build a detail that names each missed reminder with a link to its
    # schedule detail (rendered as markdown in the strip). Cap the list so a
    # burst does not blow up the tile.
    preview = missed[:5]
    bullet_lines: list[str] = []
    for entry in preview:
        label = _schedule_display_name(entry).replace("[", "\\[").replace("]", "\\]")
        due = str(getattr(entry, "run_at_date", "") or "")
        due_suffix = f" — due {due}" if due else ""
        # Markdown link: [label](/schedules/schedule_id)
        sid = str(getattr(entry, "schedule_id", ""))
        bullet_lines.append(f"- [{label}](/schedules/{sid}){due_suffix}")
    if len(missed) > len(preview):
        bullet_lines.append(f"- …and {len(missed) - len(preview)} more")
    # The first line is what Home shows (one line per row), so it carries the
    # decision data itself: which reminders, and when each was due.
    summary_names = [
        " ".join(_schedule_display_name(e).split())
        + (f" (due {e.run_at_date})" if getattr(e, "run_at_date", "") else "")
        for e in preview[:2]
    ]
    summary = f"{len(missed)} missed: " + ", ".join(summary_names)
    if len(missed) > len(summary_names):
        summary += f" and {len(missed) - len(summary_names)} more"
    detail_base = (
        f"{summary}\n\n"
        f"The server was down past {len(missed)} one-time reminder(s). "
        "They were not auto-fired because they are stale by now."
    )
    detail = detail_base + "\n\nMissed:\n" + "\n".join(bullet_lines) if bullet_lines else detail_base
    # Chat prompt also enumerates names so the operator can decide per item.
    chat_lines = [
        f"- { _schedule_display_name(e)} (/schedules/{getattr(e, 'schedule_id', '')}) — due {getattr(e, 'run_at_date', '')}"
        for e in preview
    ]
    chat_prompt = (
        f"{len(missed)} one-time reminder(s) were missed while the server "
        "was off and were not auto-fired because they are stale. "
        + "Missed:\n"
        + "\n".join(chat_lines)
        + ("\n…and more" if len(missed) > len(preview) else "")
        + "\n\nFire the ones that are still relevant, or dismiss the rest."
    )
    return [
        OperatorAction(
            id="missed-schedules",
            kind="missed-schedules",
            severity=10,
            title=f"{len(missed)} one-time reminder(s) were missed",
            detail=detail,
            glyph="⏰",
            workspace="",
            run_label="Fire now",
            chat_label="Review in chat",
            chat_prompt=chat_prompt,
        )
    ]


# -- orphaned GitHub skills ----------------------------------------------------

_DRIFT_SEVERITY = 15


def _detect_locked_skills_orphaned(context: DetectionContext) -> list[OperatorAction]:
    """GitHub skills that were only represented by ``skills-lock.json``.

    The GitHub install surface was removed: ``sync_workspace_skills`` no longer
    restores missing cached copies, and the inventory no longer lists them. A
    workspace that still carries a ``skills-lock.json`` therefore has skills
    that are silently invisible — the operator has no recovery path unless the
    cached copy is copied into ``skills/<name>/``. This detector surfaces that
    so the operator can recover before the cache is gone.
    """
    config = context.config
    install = getattr(config, "workspace_root", None)
    if install is None:
        return []
    roots = [Path(install)]
    for root, _name in config.agent_root_targets():
        if Path(root) not in roots:
            roots.append(Path(root))
    locked: list[str] = []
    for root in roots:
        lockfile = root / "skills-lock.json"
        if not lockfile.is_file():
            continue
        try:
            data = json.loads(lockfile.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        skills = data.get("skills")
        if not isinstance(skills, dict):
            continue
        for name in skills:
            if name and not (root / "skills" / name / "SKILL.md").is_file():
                locked.append(name)
    if not locked:
        return []
    shown = ", ".join(sorted(locked)[:5])
    more = f" and {len(locked) - 5} more" if len(locked) > 5 else ""
    return [
        OperatorAction(
            id="locked-skills-orphaned",
            kind="locked-skills-orphaned",
            severity=_DRIFT_SEVERITY,
            title=f"{len(locked)} GitHub skill(s) are no longer installed",
            detail=(
                f"These skills were installed from GitHub and are only recorded "
                f"in skills-lock.json, which is no longer restored: {shown}{more}. "
                "Copy each cached copy into skills/<name>/ to keep it."
            ),
            glyph="⚿",
            workspace="",
            chat_label="Recover them",
            chat_prompt=(
                f"These skills were installed from GitHub and are only recorded "
                f"in skills-lock.json, which is no longer restored: {shown}{more}. "
                "For each one, look for a cached copy under `.claude/skills/<name>/` "
                "or `.agents/skills/<name>/` and copy it into `skills/<name>/` so it "
                "stays installed, then run `ciao sync-skills` for that root. If no "
                "cached copy exists, the skill is gone and I can help you re-add it "
                "from its source or a zip."
            ),
        )
    ]


_IGNORED_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("PWA_AUTH_REQUIRED", "remove it: password protection is always on"),
    # Server settings that moved to Settings. Server startup imports each one
    # into `.runtime/app_settings.json` once; after that they are inert.
    ("PWA_HOST", "remove it: it was imported once into Settings → Access & devices → Network access"),
    ("CIAO_LOG_LEVEL", "remove it: it was imported once into Settings → Advanced → Developer"),
    ("CIAO_DEV_MODE", "remove it: it was imported once into Settings → Advanced → Developer"),
    ("CIAO_APP_REPO", "remove it: it was imported once into Settings → Advanced → Developer"),
    # The workspace list is runtime state owned by Settings. Server startup
    # imports the variable into workspaces.json once; after that it is inert.
    (
        "CIAO_WORKSPACES",
        "remove it: its workspaces were imported into workspaces.json once, "
        "and workspaces are managed in Settings now",
    ),
)


# Values `ciao setup` itself wrote into every `.env`. They say what is still
# true, so flagging them would put the tile on every upgraded install for a line
# the operator never chose. `CIAO_VAULT_MODE` is never listed at all: setup
# wrote it and nothing the operator sets there changes behaviour now.
_SETUP_WRITTEN_DEFAULTS: dict[str, str] = {"PWA_AUTH_REQUIRED": "true"}


def _is_setup_written_default(name: str, value: str) -> bool:
    default = _SETUP_WRITTEN_DEFAULTS.get(name)
    return default is not None and value.strip().lower() == default


# Ignored in `.env` only: the process environment (a service definition, test
# isolation) still sets it, so it is read from the file, never from the merged
# source. Setup wrote `.runtime`; any value naming `<workspace>/.runtime` is that
# default and stays silent.
_RUNTIME_ROOT_HINT = (
    "remove it: the runtime folder is always <workspace>/.runtime now; move "
    "its contents there before deleting the line"
)


def _dotenv_runtime_root_moved(config: Any) -> bool:
    """Whether the workspace `.env` sets a non-default ``CIAO_RUNTIME_ROOT``."""
    workspace = getattr(config, "workspace_root", None)
    if not workspace:
        return False
    from ciao.macos_service import read_dotenv

    root = Path(workspace).expanduser()
    raw = read_dotenv(root / ".env").get("CIAO_RUNTIME_ROOT", "").strip()
    if not raw:
        return False
    named = Path(raw).expanduser()
    if not named.is_absolute():
        named = root / named
    return named.resolve() != (root / ".runtime").resolve()


def _stale_legacy_env(config: Any) -> list[tuple[str, str]]:
    """The retired `.env` variables that are still set, with their hints.

    One list, read by both the detector and the run handler, so the button
    removes exactly what the tile reported. ``config.env_source`` is the test
    seam; a real ``CiaoConfig`` has none, so the process environment is read —
    which is where server startup's one-time ``load_dotenv`` put the file's
    values.
    """
    source = getattr(config, "env_source", None) or os.environ
    stale = [
        (name, hint)
        for name, hint in _IGNORED_ENV_VARS
        if str(source.get(name, "")).strip()
        and not _is_setup_written_default(name, str(source.get(name, "")))
    ]
    if _dotenv_runtime_root_moved(config):
        stale.append(("CIAO_RUNTIME_ROOT", _RUNTIME_ROOT_HINT))
    return stale


def _detect_legacy_env_ignored(context: DetectionContext) -> list[OperatorAction]:
    """Environment variables the engine no longer reads.

    `PWA_AUTH_REQUIRED` went when password protection became unconditional;
    `CIAO_RUNTIME_ROOT` is no longer read from `.env`;
    the server variables moved to Settings after a one-time import;
    `CIAO_WORKSPACES` gave way to the Settings-owned runtime registry. A variable that is set and silently
    ignored is worse than one that never existed: the operator believes a setting
    is in effect. The fix comments the lines out of `.env`, so it carries a run
    button — the engine owns `.env`; the tile's Remove button edits it.
    """
    stale = _stale_legacy_env(context.config)
    if not stale:
        return []
    names = ", ".join(name for name, _hint in stale)
    return [
        OperatorAction(
            id="legacy-env-ignored",
            kind="legacy-env-ignored",
            severity=_DRIFT_SEVERITY,
            title=f"{len(stale)} setting(s) in .env are no longer read",
            detail=(
                f"{names} are no longer read by the engine, so whatever they "
                "say is not in effect."
            ),
            glyph="⚑",
            workspace="",
            run_label="Remove them",
            chat_label="What changed?",
            chat_prompt=(
                f"These variables in my `.env` are no longer read by the engine: "
                f"{names}. For each one: "
                + "; ".join(f"{name}: {hint}" for name, hint in stale)
                + ". Do not edit `.env` yourself: the tile's Remove button "
                "comments these lines out. Explain what each variable used to control and where its "
                "setting lives now, and how I can check the current value (in "
                "Settings, where it has one)."
            ),
        )
    ]


_DETECTORS: list[Callable[[DetectionContext], list[OperatorAction]]] = [
    _detect_package_update,
    _detect_mac_app_switch,
    _detect_provider_updates,
    _detect_missed_schedules,
    _detect_locked_skills_orphaned,
    _detect_legacy_env_ignored,
    _detect_github_star,
]


# -- run dispatch -----------------------------------------------------------


async def run_action(action_id: str, context: DetectionContext) -> tuple[dict[str, Any], str]:
    """Perform the mechanical work for one action id.

    Returns ``(result, summary_text)``. Raises :class:`ValueError` for an
    unknown action id (the caller maps that to 404) and lets any operational
    error propagate so the route can report the failure. ``summary_text`` is a
    short human line the client can show while the strip re-renders.
    """
    if action_id == "package-update":
        return _run_package_update(context)
    if action_id.startswith(_PROVIDER_ACTION_PREFIX):
        return _run_provider_recheck(context, _provider_of(action_id))
    if action_id == "github-star":
        return await _run_github_star(context)
    if action_id == "missed-schedules":
        return await _run_missed_schedules(context)
    if action_id == "legacy-env-ignored":
        return _run_legacy_env_cleanup(context)
    raise ValueError(f"unknown operator action id: {action_id}")


def dismiss_action(action_id: str, context: DetectionContext) -> tuple[dict[str, Any], str]:
    """Record a "not now" for one action id.

    Same contract as :func:`run_action`: returns ``(result, summary_text)`` and
    raises :class:`ValueError` for an unknown id. Only ask-style and advisory
    actions accept a dismissal; a machine condition is never dismissed, only fixed.
    """
    if action_id.startswith(_PROVIDER_ACTION_PREFIX):
        return _dismiss_provider_update(context, _provider_of(action_id))
    if action_id == "github-star":
        return _dismiss_github_star(context)
    if action_id == "mac-app-switch":
        return _dismiss_mac_app_switch(context)
    raise ValueError(f"unknown operator action id: {action_id}")


async def _run_github_star(context: DetectionContext) -> tuple[dict[str, Any], str]:
    """Star the repository directly through the GitHub CLI.

    A real API call, not a receipt on trust: the button used to only open the
    repository page, which confirms nothing (the visitor may not even be
    signed in). ``gh`` already carries the operator's own GitHub auth, so this
    can star on their behalf without the app ever handling a token.

    The CLI has no ``gh repo star`` subcommand (verified against gh 2.98.0 —
    `gh repo --help` lists archive/clone/create/... and no "star"), so this
    goes through the CLI's own thin API wrapper instead: a plain authenticated
    ``PUT`` on the starring endpoint, which is what `gh repo star` would have
    to do internally anyway.
    """
    from ciao.package_version import latest_release_redirect_url

    repo_url = latest_release_redirect_url().removesuffix("/releases/latest")
    slug = repo_url.removeprefix("https://github.com/").removeprefix("http://github.com/")
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["gh", "api", "-X", "PUT", f"user/starred/{slug}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "The GitHub CLI (`gh`) isn't installed — star it yourself at "
            f"{repo_url}, or install `gh` and run `gh auth login` first."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out talking to GitHub — try again.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or "unknown error"
        raise RuntimeError(
            f"Starring via the GitHub CLI failed ({detail}) — sign in with "
            f"`gh auth login`, or star it yourself at {repo_url}."
        ) from exc
    _write_star_receipt(context, "starred")
    return {"status": "starred"}, "Thank you!"


def _dismiss_github_star(context: DetectionContext) -> tuple[dict[str, Any], str]:
    """Snooze the ask for a while instead of nagging on every home visit."""
    _write_star_receipt(context, "later")
    return {"status": "later"}, "Maybe later."


def _comment_out_env_lines(path: Path, keys: set[str]) -> list[str]:
    """Comment out every ``KEY=...`` line in ``path`` for ``key`` in ``keys``.

    Returns the keys actually commented. Only a line whose own key is targeted
    is touched, and a line already commented is left alone, so the rewrite is
    idempotent and preserves every other line's text, order and value — the
    file also holds the password and provider keys, which is why it is written
    back owner-only. A missing file is not an error: there is nothing to
    comment, and the tile is already satisfied.
    """
    from ciao.jsonio import write_private_text

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc

    commented: list[str] = []
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in keys:
            out.append(f"# {line}")
            commented.append(key)
        else:
            out.append(line)
    if commented:
        write_private_text(path, "\n".join(out).rstrip() + "\n")
    return commented


def _run_legacy_env_cleanup(context: DetectionContext) -> tuple[dict[str, Any], str]:
    """Comment the retired variables out of the workspace ``.env``.

    The engine owns `.env`, and the lines are inert — commenting them out
    changes no behaviour.

    Also drops the keys from the running process, because server startup
    ``load_dotenv``ed them into ``os.environ`` and detection reads that; without
    it the re-detection in the run response would still see them and the tile
    would survive the very press that removed the lines.
    """
    stale = _stale_legacy_env(context.config)
    if not stale:
        return {"keys": [], "commented": []}, "Nothing to remove."
    keys = {name for name, _hint in stale}

    workspace = getattr(context.config, "workspace_root", None)
    commented: list[str] = []
    if workspace:
        commented = _comment_out_env_lines(Path(workspace) / ".env", keys)

    for name in sorted(keys):
        os.environ.pop(name, None)
        source = getattr(context.config, "env_source", None)
        if isinstance(source, dict):
            source.pop(name, None)

    if not commented:
        return (
            {"keys": sorted(keys), "commented": []},
            f"Removed {len(keys)} variable(s) from the environment; no line was "
            "left in .env.",
        )
    return (
        {"keys": sorted(keys), "commented": commented},
        f"Commented {len(commented)} line(s) out of .env.",
    )


def _run_package_update(context: DetectionContext) -> tuple[dict[str, Any], str]:
    from ciao.package_version import update_package

    result = update_package()
    if result.get("ok"):
        return result, "Update started."
    reason = result.get("error") or "This checkout cannot self-update."
    raise RuntimeError(reason)


async def _run_missed_schedules(context: DetectionContext) -> tuple[dict[str, Any], str]:
    manager = context.schedule_store
    if manager is None or not hasattr(manager, "dispatch_now"):
        raise ValueError("no schedule manager to fire missed one-timers")
    now = context.now or datetime.now(UTC)
    fired: list[str] = []
    errors: list[str] = []
    for entry in manager.list_entries():
        if entry.frequency != "once" or not entry.enabled:
            continue
        if entry.last_triggered_on:
            continue
        if not entry.run_at_date:
            continue
        target = _once_target_datetime(entry)
        if target is None:
            continue
        if now < target:
            continue
        try:
            res = manager.dispatch_now(entry.schedule_id)
            if inspect.iscoroutine(res) or inspect.isawaitable(res):
                await res
            fired.append(entry.schedule_id)
        except Exception as exc:  # noqa: BLE001 — one failure must not stop the rest
            errors.append(str(exc))
    result = {"fired": fired, "errors": errors}
    text = f"{len(fired)} missed reminder(s) fired."
    if errors:
        text += f" {len(errors)} failed."
    return result, text
