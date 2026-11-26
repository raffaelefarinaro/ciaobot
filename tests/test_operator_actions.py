"""Tests for the operator-action registry (P4.1) and its cheap-detection rule.

The registry lives in ``ciao/operator_actions.py``. The contract under test:
every detector reaches zero, is idempotent across identical passes, offers at
least run or chat, and never touches the vault (the ``scan_vault`` spy proves
it). The web routes are covered in ``test_web_housekeeping.py``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from ciao.operator_actions import (
    DetectionContext,
    _run_provider_recheck,
    detect_actions,
    dismiss_action,
    run_action,
)


class _FakeConfig:
    """Minimal stand-in for the workspace registry surface detectors read."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        workspaces: tuple[str, ...] = ("personal",),
    ) -> None:
        self.workspace_root = tmp_path
        self.vault_root = tmp_path / "memory-vault"
        self._names = list(workspaces)
        self._roots = {
            name: self.vault_root / name for name in self._names
        }

    def workspace_names(self) -> list[str]:
        return list(self._names)

    def agent_root(self, name: str) -> Path:
        return self.workspace_root / name

    def agent_root_targets(self) -> list[tuple[Path, str]]:
        return [(self.agent_root(name), name) for name in self._names]

    def primary_workspace(self) -> str:
        # Same preference as CiaoConfig: `personal` for continuity, else the
        # first registered. Required rather than optional — the migration must
        # never guess which root inherits the guide and the skill catalog.
        if "personal" in self._names:
            return "personal"
        return self._names[0] if self._names else ""

    def workspace_vault_root(self, name: str) -> Path:
        return self._roots.get(name, self.vault_root / name)

    def canonical_workspace_vault_root(self, name: str) -> Path:
        return self.vault_root / name


class _Entry:
    def __init__(self, **kw):
        self.frequency = kw.get("frequency", "daily")
        self.enabled = kw.get("enabled", True)
        self.last_triggered_on = kw.get("last_triggered_on", "")
        self.run_at_date = kw.get("run_at_date", "")
        self.timezone_name = kw.get("timezone_name", "UTC")
        self.schedule_id = kw.get("schedule_id", "sched-1")
        self.title = kw.get("title", "")
        self.prompt = kw.get("prompt", "")
        self.daily_time_utc = kw.get("daily_time_utc", "00:00")


class _Store:
    def __init__(self, entries=None):
        self._entries = entries or []

    def list_entries(self, **kwargs):
        return list(self._entries)


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


def _context(tmp_path: Path, **overrides) -> DetectionContext:
    config = overrides.pop("config", _FakeConfig(tmp_path))
    runtime = overrides.pop("runtime", _runtime(tmp_path))
    return DetectionContext(config=config, runtime_dir=runtime, **overrides)


def _starred(tmp_path: Path) -> None:
    """Write a star receipt so the GitHub-star nudge stays silent.

    A configured install that has not starred (or dismissed) the ask would
    otherwise surface the nudge, which is not what these tests are about.
    """
    runtime = _runtime(tmp_path)
    (runtime / "star-receipt.json").write_text(
        json.dumps({"status": "starred", "at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )


def test_empty_on_a_healthy_install(tmp_path: Path) -> None:
    """A conformant install with no receipts is empty."""
    config = _FakeConfig(tmp_path)
    # Mark the vocabulary receipt present and resolved so that detector is silent.
    (tmp_path / ".runtime" / "migration").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".runtime" / "migration" / "vault-vocabulary.json").write_text(
        json.dumps({"renamed": [], "unresolved": {}}), encoding="utf-8"
    )
    context = DetectionContext(
        config=config, runtime_dir=_runtime(tmp_path), schedule_store=_Store()
    )
    _starred(tmp_path)
    assert detect_actions(context) == []


def test_a_full_proposal_queue_raises_no_tile(tmp_path: Path) -> None:
    """Pending proposals are Home's "What changed" rail's job, not the strip's.

    The old review-queue tile counted raw queue bullets and skill-proposal
    files while the rail counts the scoped /api/proposals rows, so the two
    showed different numbers on the same screen for the same queue.
    """
    _starred(tmp_path)
    config = _FakeConfig(tmp_path, workspaces=("personal",))
    root = config.workspace_vault_root("personal") / "Workspace"
    (root / "Skill-Proposals").mkdir(parents=True, exist_ok=True)
    (root / "Memory-Proposals.md").write_text(
        "\n".join(f"- [memory] Pending {i}." for i in range(50)), encoding="utf-8"
    )
    for i in range(50):
        (root / "Skill-Proposals" / f"p{i}.md").write_text("# p\n", encoding="utf-8")
    context = DetectionContext(
        config=config, runtime_dir=_runtime(tmp_path), schedule_store=_Store()
    )
    assert [a for a in detect_actions(context) if "proposal" in a.title.lower()] == []


def test_package_update_fires_only_on_available(tmp_path: Path) -> None:
    _starred(tmp_path)
    context = _context(tmp_path, package_status=lambda: {"update_available": False})
    assert detect_actions(context) == []
    context = _context(
        tmp_path,
        package_status=lambda: {
            "update_available": True,
            "latest_version": "9.9.9",
            "current_version": "0.1.0",
        },
    )
    ids = [a.id for a in detect_actions(context)]
    assert "package-update" in ids


def test_package_update_leads_with_the_settings_button(tmp_path: Path) -> None:
    """Updating is the forward action; release notes are supporting reading."""
    _starred(tmp_path)
    context = _context(
        tmp_path,
        package_status=lambda: {
            "update_available": True,
            "latest_version": "9.9.9",
            "current_version": "0.1.0",
        },
    )
    tile = next(a for a in detect_actions(context) if a.id == "package-update")
    assert tile.primary == "view"
    assert tile.as_dict()["primary"] == "view"
    assert tile.view_route == "/settings"
    assert not tile.chat_prompt
    assert tile.link_label == "Release notes"


def test_github_star_silent_while_bootstrapping(tmp_path: Path) -> None:
    """The first-run wizard is not the moment to ask for a star."""
    config = _FakeConfig(tmp_path)
    config.bootstrap_mode = True
    context = _context(tmp_path, config=config)
    assert [a.id for a in detect_actions(context)] == []


def test_github_star_fires_on_a_configured_install(tmp_path: Path) -> None:
    context = _context(tmp_path)
    actions = [a for a in detect_actions(context) if a.id == "github-star"]
    assert len(actions) == 1
    tile = actions[0]
    assert tile.run_label == "Star on GitHub"
    assert tile.link_label == "Open repository"
    assert tile.link_url == "https://github.com/raffaelefarinaro/ciaobot"
    assert tile.dismiss_label == "Later"
    assert tile.glyph == "★"


def test_github_star_silent_after_starred(tmp_path: Path) -> None:
    _starred(tmp_path)
    context = _context(tmp_path)
    assert "github-star" not in [a.id for a in detect_actions(context)]


def test_github_star_snoozed_by_later(tmp_path: Path) -> None:
    from ciao.operator_actions import dismiss_action

    context = _context(tmp_path)
    result, _summary = dismiss_action("github-star", context)
    assert result["status"] == "later"
    assert "github-star" not in [a.id for a in detect_actions(context)]


def test_github_star_resurfaces_after_snooze_expires(tmp_path: Path) -> None:
    from datetime import timedelta

    from ciao.operator_actions import _STAR_SNOOZE_DAYS, dismiss_action

    context = _context(tmp_path)
    dismiss_action("github-star", context)
    # Freeze "now" past the snooze window: the ask may surface once more.
    context.now = datetime.now(UTC) + timedelta(days=_STAR_SNOOZE_DAYS + 1)
    assert "github-star" in [a.id for a in detect_actions(context)]


async def test_github_star_run_records_starred(tmp_path: Path) -> None:
    from ciao.operator_actions import run_action

    context = _context(tmp_path)
    with patch("subprocess.run") as mock_run:
        result, _summary = await run_action("github-star", context)
    mock_run.assert_called_once()
    called = mock_run.call_args.args[0]
    assert called[:3] == ["gh", "api", "-X"]
    assert called[-1] == "user/starred/raffaelefarinaro/ciaobot"
    assert result["status"] == "starred"
    assert "github-star" not in [a.id for a in detect_actions(context)]


async def test_github_star_run_reports_gh_missing(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(RuntimeError, match="gh"):
            await run_action("github-star", context)
    # A failed run must not falsely record the star.
    assert "github-star" in [a.id for a in detect_actions(context)]


def test_dismiss_unknown_action_is_404(tmp_path: Path) -> None:
    from ciao.operator_actions import dismiss_action

    with pytest.raises(ValueError):
        dismiss_action("not-a-real-action", _context(tmp_path))


def test_locked_skills_orphaned_fires_when_lock_has_no_custom_copy(tmp_path: Path) -> None:
    _starred(tmp_path)
    tmp_path.joinpath("skills-lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": {
                    "brainstorming": {"source": "owner/repo", "sourceType": "github"},
                    "airtable-projects": {"source": "owner/repo", "sourceType": "github"},
                },
            }
        ),
        encoding="utf-8",
    )
    context = _context(tmp_path)
    actions = [a for a in detect_actions(context) if a.id == "locked-skills-orphaned"]
    assert len(actions) == 1
    assert "brainstorming" in actions[0].detail
    assert "airtable-projects" in actions[0].detail
    assert actions[0].chat_label == "Recover them"


def test_locked_skills_orphaned_silent_when_custom_copy_exists(tmp_path: Path) -> None:
    _starred(tmp_path)
    tmp_path.joinpath("skills-lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": {
                    "brainstorming": {"source": "owner/repo", "sourceType": "github"},
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "skills" / "brainstorming").mkdir(parents=True)
    (tmp_path / "skills" / "brainstorming" / "SKILL.md").write_text(
        "---\nname: brainstorming\ndescription: Demo\n---\n", encoding="utf-8"
    )
    context = _context(tmp_path)
    assert "locked-skills-orphaned" not in [a.id for a in detect_actions(context)]


def test_locked_skills_orphaned_silent_without_lock(tmp_path: Path) -> None:
    _starred(tmp_path)
    context = _context(tmp_path)
    assert "locked-skills-orphaned" not in [a.id for a in detect_actions(context)]


def test_missed_schedules_fires_collapsed_tile(tmp_path: Path) -> None:
    now = datetime(2026, 1, 20, 0, 0, tzinfo=UTC)
    store = _Store(
        [
            _Entry(
                frequency="once",
                run_at_date="2026-01-18",
                schedule_id="sched-a",
            ),
            _Entry(
                frequency="once",
                run_at_date="2026-01-19",
                schedule_id="sched-b",
            ),
        ]
    )
    context = DetectionContext(
        config=_FakeConfig(tmp_path, workspaces=("personal",)),
        runtime_dir=_runtime(tmp_path),
        schedule_store=store,
        now=now,
    )
    actions = detect_actions(context)
    tiles = [a for a in actions if a.kind == "missed-schedules"]
    assert len(tiles) == 1
    assert "2" in tiles[0].title

    # Already-triggered or not-yet-due one-timers are not missed.
    store = _Store(
        [
            _Entry(
                frequency="once",
                run_at_date="2026-01-18",
                last_triggered_on="done",
                schedule_id="sched-a",
            ),
            _Entry(
                frequency="once",
                run_at_date="2026-01-25",
                schedule_id="sched-c",
            ),
        ]
    )
    context = DetectionContext(
        config=_FakeConfig(tmp_path, workspaces=("personal",)),
        runtime_dir=_runtime(tmp_path),
        schedule_store=store,
        now=now,
    )
    tiles = [a for a in detect_actions(context) if a.kind == "missed-schedules"]
    assert tiles == []


def test_missed_schedules_first_line_names_the_reminders(tmp_path: Path) -> None:
    """Home shows one line per row, so line one must carry the decision data."""
    now = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    entries = [
        _Entry(frequency="once", run_at_date=f"2026-11-0{i}", title=name, schedule_id=f"s{i}")
        for i, name in ((1, "Call Anna"), (2, "Renew\nlicence"), (3, "Book flights"), (4, "Pay rent"))
    ]
    context = DetectionContext(
        config=_FakeConfig(tmp_path, workspaces=("personal",)),
        runtime_dir=_runtime(tmp_path),
        schedule_store=_Store(entries),
        now=now,
    )
    tile = next(a for a in detect_actions(context) if a.kind == "missed-schedules")
    first = tile.detail.splitlines()[0]
    assert first == "4 missed: Call Anna (due 2026-11-01), Renew licence (due 2026-11-02) and 2 more"
    # A newline in a title cannot split the first line.
    assert len(tile.detail.split("\n\n")[0].splitlines()) == 1
    # The linked list stays below for the chat and any full view.
    assert "- [Call Anna](/schedules/s1)" in tile.detail


def test_missed_schedules_honors_fire_time_of_day(tmp_path: Path) -> None:
    """A once reminder due later today is not missed, and not fired early.

    The target is ``daily_time_utc`` on ``run_at_date`` in the entry's
    timezone, not midnight of ``run_at_date``. At 09:00 a reminder due at
    23:00 today must stay out of the strip (and out of "Fire now"), or it
    would be dispatched — and deleted — hours early.
    """
    now = datetime(2026, 1, 20, 9, 0, tzinfo=UTC)
    store = _Store(
        [
            _Entry(
                frequency="once",
                run_at_date="2026-01-20",
                daily_time_utc="23:00",
                schedule_id="sched-later-today",
            ),
        ]
    )
    context = DetectionContext(
        config=_FakeConfig(tmp_path, workspaces=("personal",)),
        runtime_dir=_runtime(tmp_path),
        schedule_store=store,
        now=now,
    )
    tiles = [a for a in detect_actions(context) if a.kind == "missed-schedules"]
    assert tiles == []

    # The same reminder is missed once its fire time has passed.
    store = _Store(
        [
            _Entry(
                frequency="once",
                run_at_date="2026-01-20",
                daily_time_utc="23:00",
                schedule_id="sched-later-today",
            ),
        ]
    )
    context = DetectionContext(
        config=_FakeConfig(tmp_path, workspaces=("personal",)),
        runtime_dir=_runtime(tmp_path),
        schedule_store=store,
        now=datetime(2026, 1, 20, 23, 30, tzinfo=UTC),
    )
    tiles = [a for a in detect_actions(context) if a.kind == "missed-schedules"]
    assert len(tiles) == 1


def test_every_action_offers_run_or_chat(tmp_path: Path) -> None:
    """Contract 4: no action is a bare notice with neither a run nor a chat."""
    # Force every detector to fire so the whole registry is exercised.
    config = _FakeConfig(tmp_path, workspaces=("personal", "work"))
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-vocabulary.json").write_text(
        json.dumps({"renamed": [], "unresolved": {"log": ["x.md"]}}), encoding="utf-8"
    )
    now = datetime(2026, 1, 20, 0, 0, tzinfo=UTC)
    store = _Store([_Entry(frequency="once", run_at_date="2026-01-18", schedule_id="s1")])
    context = DetectionContext(
        config=config,
        runtime_dir=runtime,
        schedule_store=store,
        now=now,
        package_status=lambda: {
            "update_available": True,
            "latest_version": "9.9.9",
            "current_version": "0.1.0",
        },
    )
    actions = detect_actions(context)
    assert actions, "expected at least some actions to fire"
    for action in actions:
        assert action.run_label or action.chat_prompt or action.link_url, (
            f"action {action.id} offers neither run, chat, nor an external link"
        )


def test_ids_are_stable_across_calls(tmp_path: Path) -> None:
    """Contract 3: byte-identical actions across two passes."""
    config = _FakeConfig(tmp_path, workspaces=("personal", "work"))
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-vocabulary.json").write_text(
        json.dumps({"renamed": [], "unresolved": {"log": ["x.md"]}}), encoding="utf-8"
    )
    context = DetectionContext(
        config=config,
        runtime_dir=runtime,
        schedule_store=_Store([]),
        package_status=lambda: {
            "update_available": True,
            "latest_version": "9.9.9",
            "current_version": "0.1.0",
        },
    )
    first = detect_actions(context)
    second = detect_actions(context)
    assert [a.as_dict() for a in first] == [a.as_dict() for a in second]


def test_detector_raising_is_logged_and_skipped(tmp_path: Path) -> None:
    """A broken detector must not sink the pass."""
    from ciao.operator_actions import _DETECTORS

    def boom(_context):
        raise RuntimeError("boom")

    # Inject a failing detector, run, and restore.
    with patch("ciao.operator_actions._DETECTORS", _DETECTORS + [boom]):
        actions = detect_actions(
            DetectionContext(config=_FakeConfig(tmp_path), runtime_dir=_runtime(tmp_path))
        )
    assert isinstance(actions, list)


def test_scan_vault_is_never_touched(tmp_path: Path) -> None:
    """Contract 4 (cheap): no detector walks the vault."""
    from ciao.operator_actions import detect_actions
    from ciao.vault_index import scan_vault

    config = _FakeConfig(tmp_path, workspaces=("personal", "work"))
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-vocabulary.json").write_text(
        json.dumps({"renamed": [], "unresolved": {"log": ["x.md"]}}), encoding="utf-8"
    )
    context = DetectionContext(
        config=config,
        runtime_dir=runtime,
        schedule_store=_Store([]),
        package_status=lambda: {
            "update_available": True,
            "latest_version": "9.9.9",
            "current_version": "0.1.0",
        },
    )
    with patch("ciao.vault_index.scan_vault", wraps=scan_vault) as spy:
        detect_actions(context)
    spy.assert_not_called()


async def test_run_action_unknown_id_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await run_action("no-such-action", DetectionContext(config=_FakeConfig(tmp_path)))


# -- the queue tile the operator saw on screen -------------------------------


# -- post-migration drift detectors (§11.2) -----------------------------------


class _RerootedConfig(_FakeConfig):
    """A config whose workspaces have their own agent roots, as after re-rooting."""

    def __init__(self, tmp_path: Path, *, workspaces: tuple[str, ...] = ("personal", "work")) -> None:
        super().__init__(tmp_path, workspaces=workspaces)
        self.env_source: dict[str, str] = {}

    def agent_root(self, name: str) -> Path:
        return self.workspace_root / name

    def agent_root_targets(self) -> list[tuple[Path, str]]:
        return [(self.agent_root(n), n) for n in self.workspace_names()]


def _kinds(context) -> set[str]:
    return {a.kind for a in detect_actions(context)}


def test_env_vars_the_engine_no_longer_reads_are_surfaced(tmp_path: Path) -> None:
    """A setting that is set and silently ignored is worse than one that never
    existed: the operator believes it is in effect."""
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    config.env_source = {"PWA_AUTH_REQUIRED": "false", "CIAO_VAULT_ROOT": "x"}

    actions = [a for a in detect_actions(_context(tmp_path, config=config))
               if a.kind == "legacy-env-ignored"]

    assert len(actions) == 1
    assert "PWA_AUTH_REQUIRED" in actions[0].detail
    # A variable that IS still read must not be dragged in.
    assert "CIAO_VAULT_ROOT" not in actions[0].detail
    # The engine owns `.env`, so the fix is a run button; the chat is an
    # explainer, not the only way to clear the tile (#1134).
    assert actions[0].run_label
    assert actions[0].chat_prompt
    # The agent must not print `.env` values, so the prompt must not ask for
    # them (#1457).
    prompt = actions[0].chat_prompt.lower()
    assert "tell me" not in prompt
    assert "do not edit `.env` yourself" in prompt
    # The fixture sets PWA_AUTH_REQUIRED=false; that value must never appear.
    assert "false" not in prompt
    assert "where its setting lives now" in prompt


def test_retired_ciao_workspaces_variable_is_surfaced(tmp_path: Path) -> None:
    """The workspace list is Settings-owned runtime state; a CIAO_WORKSPACES
    still in .env is inert after its one-time import and should be removed."""
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    config.env_source = {"CIAO_WORKSPACES": '[{"name":"personal"}]'}

    actions = [a for a in detect_actions(_context(tmp_path, config=config))
               if a.kind == "legacy-env-ignored"]

    assert len(actions) == 1
    assert "CIAO_WORKSPACES" in actions[0].detail
    assert "Settings" in actions[0].chat_prompt


def test_retired_auth_switch_is_surfaced(tmp_path: Path) -> None:
    """Password protection is always on, so PWA_AUTH_REQUIRED=false no longer
    does what the .env says."""
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    config.env_source = {"PWA_AUTH_REQUIRED": "false"}

    actions = [a for a in detect_actions(_context(tmp_path, config=config))
               if a.kind == "legacy-env-ignored"]

    assert len(actions) == 1
    assert "PWA_AUTH_REQUIRED" in actions[0].detail


def test_server_variables_that_moved_to_settings_are_surfaced(tmp_path: Path) -> None:
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    config.env_source = {
        "PWA_HOST": "127.0.0.1",
        "CIAO_LOG_LEVEL": "debug",
        "CIAO_DEV_MODE": "true",
        "CIAO_APP_REPO": "/src",
    }

    actions = [a for a in detect_actions(_context(tmp_path, config=config))
               if a.kind == "legacy-env-ignored"]

    assert len(actions) == 1
    for name in config.env_source:
        assert name in actions[0].detail
    assert "Settings" in actions[0].chat_prompt


def test_lines_setup_wrote_itself_do_not_raise_the_tile(tmp_path: Path) -> None:
    """Every pre-upgrade `.env` carries PWA_AUTH_REQUIRED=true and
    CIAO_VAULT_MODE from setup; both still describe reality, so no tile."""
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    config.env_source = {"PWA_AUTH_REQUIRED": "true", "CIAO_VAULT_MODE": "scratch"}

    assert "legacy-env-ignored" not in _kinds(_context(tmp_path, config=config))


def test_no_legacy_env_vars_means_no_tile(tmp_path: Path) -> None:
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    config.env_source = {"CIAO_VAULT_ROOT": "x"}

    assert "legacy-env-ignored" not in _kinds(_context(tmp_path, config=config))


def test_a_moved_runtime_root_in_dotenv_raises_the_tile(tmp_path: Path) -> None:
    """`.env` no longer sets the runtime root; a non-default value names a
    runtime the engine does not use, so the operator has to hear about it."""
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    config.env_source = {}
    (tmp_path / ".env").write_text(
        f"CIAO_RUNTIME_ROOT={tmp_path / 'elsewhere'}\n", encoding="utf-8"
    )

    actions = [a for a in detect_actions(_context(tmp_path, config=config))
               if a.kind == "legacy-env-ignored"]

    assert len(actions) == 1
    assert "CIAO_RUNTIME_ROOT" in actions[0].detail


async def test_removing_legacy_env_comments_the_lines_and_clears_the_tile(
    tmp_path: Path,
) -> None:
    """The run handler is what makes the tile clearable (#1134).

    The line must be commented, not deleted: the file also holds the password
    and provider keys, whose lines must survive untouched.
    """
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    config.env_source = {"CIAO_DEV_MODE": "true", "CIAO_APP_REPO": "/src"}
    (tmp_path / ".env").write_text(
        "PWA_AUTH_TOKEN=secret\n"
        "CIAO_DEV_MODE=true\n"
        "PWA_PORT=8443\n"
        "CIAO_APP_REPO=/src\n",
        encoding="utf-8",
    )
    context = _context(tmp_path, config=config)
    assert [a.kind for a in detect_actions(context) if a.kind == "legacy-env-ignored"]

    result, summary = await run_action("legacy-env-ignored", context)

    assert set(result["keys"]) == {"CIAO_DEV_MODE", "CIAO_APP_REPO"}
    assert "2 line(s)" in summary
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "# CIAO_DEV_MODE=true" in text
    assert "# CIAO_APP_REPO=/src" in text
    # Untouched lines and their order are preserved.
    assert "PWA_AUTH_TOKEN=secret" in text
    assert "PWA_PORT=8443" in text
    # Re-detection in the same pass sees the condition gone.
    assert "legacy-env-ignored" not in _kinds(_context(tmp_path, config=config))


async def test_removing_legacy_env_pops_the_process_environment(tmp_path: Path) -> None:
    """Server startup loads `.env` into `os.environ`; detection reads that.

    Without the pop, the re-detection in the run response would still see the
    keys and the tile would survive the press that removed the lines.
    """
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    (tmp_path / ".env").write_text("CIAO_LOG_LEVEL=debug\n", encoding="utf-8")
    with patch.dict(os.environ, {"CIAO_LOG_LEVEL": "debug"}):
        await run_action("legacy-env-ignored", _context(tmp_path, config=config))
        assert "CIAO_LOG_LEVEL" not in os.environ


async def test_removing_legacy_env_is_idempotent(tmp_path: Path) -> None:
    """A second press has nothing to do and reports so, never a crash."""
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    config.env_source = {"CIAO_DEV_MODE": "true"}
    (tmp_path / ".env").write_text("CIAO_DEV_MODE=true\n", encoding="utf-8")
    context = _context(tmp_path, config=config)

    first = await run_action("legacy-env-ignored", context)
    assert first[0]["commented"] == ["CIAO_DEV_MODE"]
    # The variable is gone from the source, so there is no condition left.
    assert "legacy-env-ignored" not in _kinds(_context(tmp_path, config=config))


async def test_removing_legacy_env_without_a_dotenv_file_still_clears(tmp_path: Path) -> None:
    """A key set only in the process environment has no line to comment."""
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    # No `.env` at all: the key exists only in the process environment.
    with patch.dict(os.environ, {"CIAO_DEV_MODE": "true"}):
        result, summary = await run_action("legacy-env-ignored", _context(tmp_path, config=config))
        assert result["commented"] == []
        assert "no line" in summary
        assert "CIAO_DEV_MODE" not in os.environ



@pytest.mark.parametrize(
    "value", [".runtime", "./.runtime", "{root}/.runtime"]
)
def test_the_runtime_root_setup_wrote_stays_silent(tmp_path: Path, value: str) -> None:
    config = _RerootedConfig(tmp_path)
    for name in ("personal", "work"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CLAUDE.md").write_text("# G\n", encoding="utf-8")
    # The service definition sets it in the process environment; that is not
    # a `.env` line and must not raise the tile either.
    config.env_source = {"CIAO_RUNTIME_ROOT": str(tmp_path / "isolated")}
    (tmp_path / ".env").write_text(
        f"CIAO_RUNTIME_ROOT={value.format(root=tmp_path)}\n", encoding="utf-8"
    )

    assert "legacy-env-ignored" not in _kinds(_context(tmp_path, config=config))


# -- provider update tiles (#1592) --------------------------------------------


class _FakeProviderChecks:
    """A provider-check callable with the ``invalidate`` hook the real cache has."""

    def __init__(self, checks: dict[str, dict[str, str]]) -> None:
        self.checks = checks
        self.after_invalidate: dict[str, dict[str, str]] | None = None
        self.invalidations = 0

    def __call__(self) -> dict[str, dict[str, str]]:
        return self.checks

    def invalidate(self) -> None:
        self.invalidations += 1
        if self.after_invalidate is not None:
            self.checks = self.after_invalidate


def _below_tested(tested: str = "2.1.0", installed: str = "2.0.32") -> dict[str, str]:
    return {
        "state": "below_tested",
        "installed": installed,
        "tested": tested,
        "command": "opencode upgrade " + tested,
        "terminal_action": "opencode-update",
    }


def _provider_tiles(context: DetectionContext) -> list:
    return [a for a in detect_actions(context) if a.kind == "provider-update"]


def test_provider_update_silent_without_checks(tmp_path: Path) -> None:
    _starred(tmp_path)
    assert _provider_tiles(_context(tmp_path)) == []


def test_provider_update_tile_for_below_tested_opencode(tmp_path: Path) -> None:
    _starred(tmp_path)
    checks = _FakeProviderChecks({"opencode": _below_tested()})
    tiles = _provider_tiles(_context(tmp_path, provider_checks=checks))
    assert len(tiles) == 1
    tile = tiles[0]
    assert tile.id == "provider-update:opencode"
    assert "OpenCode 2.0.32 is older than 2.1.0, the version this Ciaobot release was tested with." in tile.detail
    assert tile.terminal_action == "opencode-update"
    assert tile.command == "opencode upgrade 2.1.0"
    assert tile.run_label == "Check again"
    assert tile.dismiss_label == "Not now"
    assert tile.blocking is False
    payload = tile.as_dict()
    assert payload["terminal_action"] == "opencode-update"
    assert payload["command"] == "opencode upgrade 2.1.0"


@pytest.mark.parametrize(
    "check",
    [
        {"state": "current", "installed": "2.1.0", "tested": "2.1.0"},
        {"state": "unsupported", "tested": "2.1.0"},
        {"state": "untested"},
    ],
)
def test_provider_update_silent_for_current_bundled_unsupported_untested(
    tmp_path: Path, check: dict[str, str]
) -> None:
    _starred(tmp_path)
    context = _context(tmp_path, provider_checks=_FakeProviderChecks({"opencode": check}))
    assert _provider_tiles(context) == []


def test_provider_update_dismiss_hides_for_that_target_only(tmp_path: Path) -> None:
    _starred(tmp_path)
    checks = _FakeProviderChecks({"opencode": _below_tested()})
    context = _context(tmp_path, provider_checks=checks)
    result, summary = dismiss_action("provider-update:opencode", context)
    assert result == {"dismissed": "2.1.0"}
    assert summary == "Hidden until a newer tested version ships."
    assert _provider_tiles(context) == []
    receipt = json.loads((_runtime(tmp_path) / "provider-update-dismissals.json").read_text(encoding="utf-8"))
    assert receipt == {"opencode": "2.1.0"}
    checks.checks = {"opencode": _below_tested(tested="2.2.0")}
    assert [t.id for t in _provider_tiles(context)] == ["provider-update:opencode"]


def test_provider_update_dismissals_keep_other_providers_and_ignore_junk(tmp_path: Path) -> None:
    _starred(tmp_path)
    path = _runtime(tmp_path) / "provider-update-dismissals.json"
    path.write_text(json.dumps({"claude": "2.0.0", "opencode": 5, "x": ["y"]}), encoding="utf-8")
    checks = _FakeProviderChecks({"opencode": _below_tested()})
    context = _context(tmp_path, provider_checks=checks)
    assert len(_provider_tiles(context)) == 1
    dismiss_action("provider-update:opencode", context)
    assert json.loads(path.read_text(encoding="utf-8")) == {"claude": "2.0.0", "opencode": "2.1.0"}
    path.write_text("not json", encoding="utf-8")
    assert len(_provider_tiles(context)) == 1


@pytest.mark.parametrize(
    ("check", "state", "summary"),
    [
        ({"state": "current"}, "current", "OpenCode is up to date."),
        (_below_tested(), "below_tested", "OpenCode 2.0.32 is still older than 2.1.0."),
        ({"state": "unsupported"}, "unsupported", "OpenCode is not compatible with this Ciaobot version."),
        ({"state": "unknown"}, "unknown", "Could not verify OpenCode's version."),
        ({"state": "untested"}, "untested", "Could not verify OpenCode's version."),
        (None, "unknown", "Could not verify OpenCode's version."),
        ({"state": "unexpected"}, "unexpected", "Could not verify OpenCode's version."),
    ],
)
def test_provider_recheck_reports_actual_state(
    tmp_path: Path, check: dict[str, str] | None, state: str, summary: str
) -> None:
    checks = _FakeProviderChecks({"opencode": check} if check is not None else {})
    result, actual_summary = _run_provider_recheck(_context(tmp_path, provider_checks=checks), "opencode")
    assert result == {"state": state}
    assert actual_summary == summary


@pytest.mark.asyncio
async def test_provider_update_check_again_invalidates_and_clears(tmp_path: Path) -> None:
    _starred(tmp_path)
    checks = _FakeProviderChecks({"opencode": _below_tested()})
    checks.after_invalidate = {"opencode": {"state": "current", "installed": "2.1.0", "tested": "2.1.0"}}
    context = _context(tmp_path, provider_checks=checks)
    assert len(_provider_tiles(context)) == 1
    result, summary = await run_action("provider-update:opencode", context)
    assert checks.invalidations == 1
    assert result == {"state": "current"}
    assert "up to date" in summary
    assert _provider_tiles(context) == []


@pytest.mark.asyncio
async def test_provider_update_check_again_reports_still_older(tmp_path: Path) -> None:
    checks = _FakeProviderChecks({"opencode": _below_tested()})
    result, summary = await run_action("provider-update:opencode", _context(tmp_path, provider_checks=checks))
    assert result == {"state": "below_tested"}
    assert summary == "OpenCode 2.0.32 is still older than 2.1.0."


def test_provider_update_detection_is_idempotent(tmp_path: Path) -> None:
    _starred(tmp_path)
    context = _context(tmp_path, provider_checks=_FakeProviderChecks({"opencode": _below_tested()}))
    first = [a.as_dict() for a in detect_actions(context)]
    second = [a.as_dict() for a in detect_actions(context)]
    assert first == second
    assert first


@pytest.mark.asyncio
async def test_provider_update_unknown_provider_is_value_error(tmp_path: Path) -> None:
    checks = _FakeProviderChecks({"opencode": _below_tested()})
    context = _context(tmp_path, provider_checks=checks)
    with pytest.raises(ValueError):
        await run_action("provider-update:nope", context)
    with pytest.raises(ValueError):
        dismiss_action("provider-update:nope", context)
    assert not (_runtime(tmp_path) / "provider-update-dismissals.json").exists()


def test_provider_update_dismiss_without_a_current_check_is_value_error(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        provider_checks=_FakeProviderChecks({"opencode": {"state": "current", "installed": "2.1.0", "tested": "2.1.0"}}),
    )
    with pytest.raises(ValueError):
        dismiss_action("provider-update:opencode", context)
    with pytest.raises(ValueError):
        dismiss_action("provider-update:claude", _context(tmp_path))


def test_provider_update_a_broken_probe_is_silent(tmp_path: Path) -> None:
    _starred(tmp_path)

    def boom() -> dict:
        raise RuntimeError("probe failed")

    assert _provider_tiles(_context(tmp_path, provider_checks=boom)) == []


def test_provider_update_dismiss_with_a_raising_probe_is_value_error(tmp_path: Path) -> None:
    def boom() -> dict:
        raise RuntimeError("probe failed")

    with pytest.raises(ValueError):
        dismiss_action("provider-update:opencode", _context(tmp_path, provider_checks=boom))
    assert not (_runtime(tmp_path) / "provider-update-dismissals.json").exists()


def test_provider_update_unreadable_receipt_is_logged_and_tile_still_shows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _starred(tmp_path)
    path = _runtime(tmp_path) / "provider-update-dismissals.json"
    path.write_text("{}", encoding="utf-8")
    real = Path.read_text

    def deny(self: Path, *args, **kwargs):
        if self.name == "provider-update-dismissals.json":
            raise PermissionError("denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny)
    context = _context(tmp_path, provider_checks=_FakeProviderChecks({"opencode": _below_tested()}))
    with caplog.at_level("WARNING"):
        assert len(_provider_tiles(context)) == 1
    assert "provider-update-dismissals.json" in caplog.text


def test_provider_update_tile_for_system_claude_below_tested(tmp_path: Path) -> None:
    _starred(tmp_path)
    claude = {
        "state": "below_tested",
        "installed": "2.0.0",
        "tested": "2.1.0",
        "command": "claude update",
        "terminal_action": "claude-update",
    }
    tiles = _provider_tiles(_context(tmp_path, provider_checks=_FakeProviderChecks({"claude": claude})))
    assert [t.id for t in tiles] == ["provider-update:claude"]
    assert tiles[0].title == "Update Claude Code"
    assert tiles[0].command == "claude update"
    assert tiles[0].terminal_action == "claude-update"
