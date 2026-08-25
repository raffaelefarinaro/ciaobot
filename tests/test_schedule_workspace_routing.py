"""Schedule -> project resolution when the stored web_project_id is stale.

Project IDs regenerate per device on fresh init, so schedules.json (shared
via git) carries dangling web_project_id values. `_resolve_schedule_project`
maps a stale id to a local General project. It must honour an explicit
`workspace` field on the schedule instead of guessing from the schedule_id
prefix, otherwise work schedules whose id doesn't start with "sched-work"
(e.g. the morning action briefing, sched-ee193709) land in personal General
and lose the work-only MCPs.

The manager seeds a "General" project per workspace on init, so the tests
assert on the resolved project's workspace rather than a hand-made id.
"""

from __future__ import annotations

from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.schedules import ScheduleEntry, ScheduleStore
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal", "work")
        },
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _entry(*, schedule_id: str, workspace: str = "") -> ScheduleEntry:
    return ScheduleEntry(
        schedule_id=schedule_id,
        daily_time_utc="08:00",
        prompt="Morning action briefing.",
        chat_id=0,
        created_at="2026-06-08T00:00:00Z",
        web_project_id="proj-stale00",  # dangling id, not on this device
        workspace=workspace,
    )


def test_explicit_work_workspace_beats_non_work_schedule_id(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)

    entry = _entry(schedule_id="sched-ee193709", workspace="work")
    resolved = pcm._resolve_schedule_project("proj-stale00", entry)

    assert resolved is not None
    assert resolved.workspace == "work"
    assert resolved.name == "General"


def test_explicit_personal_workspace_beats_work_schedule_id(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)

    # id says "work" but the explicit field says personal: the field wins.
    entry = _entry(schedule_id="sched-workthing", workspace="personal")
    resolved = pcm._resolve_schedule_project("proj-stale00", entry)

    assert resolved is not None
    assert resolved.workspace == "personal"
    assert resolved.name == "General"


def test_falls_back_to_id_prefix_when_workspace_unset(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)

    work_entry = _entry(schedule_id="sched-workdaily", workspace="")
    personal_entry = _entry(schedule_id="sched-memorycur", workspace="")

    assert pcm._resolve_schedule_project("proj-stale00", work_entry).workspace == "work"
    assert (
        pcm._resolve_schedule_project("proj-stale00", personal_entry).workspace
        == "personal"
    )


def test_create_persists_workspace(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    store.create(
        daily_time_utc="08:00",
        prompt="Morning action briefing.",
        model="",
        mode="auto",
        chat_id=0,
        web_project_id="proj-abc12345",
        workspace="work",
    )
    [entry] = store.list_entries()
    assert entry.workspace == "work"


def test_system_schedule_default_inherits_first_workspace_routing(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "personal": WorkspaceConfig(
                name="personal",
                vault_root="personal",
                default_provider="opencode",
                default_model="",
            ),
            "work": WorkspaceConfig(
                name="work",
                vault_root="work",
                default_provider="claude",
                default_model="opus",
            ),
        },
    )
    pcm = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    entry = ScheduleEntry(
        schedule_id="system-memory-curation",
        daily_time_utc="00:01",
        prompt="curate",
        chat_id=0,
        created_at="1970-01-01T00:00:00Z",
        scope="system",
        workspace="default",
    )

    provider, model, workspace = pcm.schedule_effective_routing(entry)

    assert workspace == "personal"
    assert provider == "opencode"
    assert model == ""


def test_schedule_inheritance_is_resolved_again_after_workspace_change(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    workspace = WorkspaceConfig(
        name="personal",
        vault_root="personal",
        default_provider="opencode",
        default_model="",
    )
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={"personal": workspace},
    )
    pcm = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("Scheduled", workspace="personal")
    entry = ScheduleEntry(
        schedule_id="sched-dynamic",
        daily_time_utc="08:00",
        prompt="dynamic",
        chat_id=0,
        created_at="2026-07-15T00:00:00Z",
        web_project_id=project.project_id,
        workspace="personal",
    )

    assert pcm.schedule_effective_routing(entry) == ("opencode", "", "personal")

    workspace.default_provider = "claude"
    workspace.default_model = "sonnet"

    assert pcm.schedule_effective_routing(entry) == (
        "claude",
        "sonnet",
        "personal",
    )


# ── re-homing by remembered project name ────────────────────────────────
#
# Falling back to General discards the project the user picked, silently. The
# recorded name survives the id regeneration that caused the staleness, so it
# is what makes the schedule land where it was configured to.


def _named_entry(*, name: str, workspace: str = "work") -> ScheduleEntry:
    entry = _entry(schedule_id="sched-ee193709", workspace=workspace)
    entry.web_project_name = name
    return entry


def test_a_stale_id_re_homes_to_the_project_of_the_same_name(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    target = pcm.create_project(name="docs-improvement", workspace="work")
    entry = _named_entry(name="docs-improvement")

    resolved = pcm._resolve_schedule_project("proj-stale00", entry)

    assert resolved is not None
    assert resolved.project_id == target.project_id
    assert resolved.name == "docs-improvement"
    assert entry.web_project_id == target.project_id


def test_re_homing_stays_inside_the_schedule_workspace(tmp_path: Path) -> None:
    """A same-named project in another workspace must not capture the run."""
    pcm = _make_manager(tmp_path)
    pcm.create_project(name="shared-name", workspace="personal")
    work_target = pcm.create_project(name="shared-name", workspace="work")

    resolved = pcm._resolve_schedule_project("proj-stale00", _named_entry(name="shared-name"))

    assert resolved is not None
    assert resolved.project_id == work_target.project_id
    assert resolved.workspace == "work"


def test_an_unmatched_name_still_falls_back_to_general(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)

    resolved = pcm._resolve_schedule_project("proj-stale00", _named_entry(name="deleted-project"))

    assert resolved is not None
    assert resolved.name == "General"
    assert resolved.workspace == "work"


def test_entries_without_a_recorded_name_keep_the_old_behaviour(tmp_path: Path) -> None:
    """Schedules written before the name was stored must not regress."""
    pcm = _make_manager(tmp_path)
    pcm.create_project(name="docs-improvement", workspace="work")

    resolved = pcm._resolve_schedule_project(
        "proj-stale00", _entry(schedule_id="sched-ee193709", workspace="work")
    )

    assert resolved is not None
    assert resolved.name == "General"


def test_the_name_round_trips_through_the_schedule_store(tmp_path: Path) -> None:
    """It has to survive serialization, or it is useless on the next run."""
    store = ScheduleStore(tmp_path)
    created = store.create(
        daily_time_utc="08:00", prompt="p", chat_id=0, model="", mode="normal",
        web_project_id="proj-24db8110", web_project_name="docs-improvement",
        workspace="work",
    )
    reloaded = ScheduleStore(tmp_path).get(created.schedule_id)
    assert reloaded is not None
    assert reloaded.web_project_name == "docs-improvement"


# ── backfilling the name onto id-only entries ───────────────────────────


def test_backfill_records_the_name_while_the_id_still_resolves(tmp_path: Path) -> None:
    """An entry carrying only an id is one fresh init away from running in
    General. While the id resolves here, the intended project is knowable —
    afterwards it is not, so the stamp has to happen in that window."""
    from ciao.schedules import ScheduleManager

    store = ScheduleStore(tmp_path)
    entry = store.create(
        daily_time_utc="08:00", prompt="p", chat_id=0, model="", mode="normal",
        web_project_id="proj-live01", workspace="work",
    )
    assert not entry.web_project_name

    manager = ScheduleManager(
        store=store, resolve_target=lambda *a, **k: None,
        dispatch_to_web=None, prepare_chat=None,
    )
    stamped = manager.backfill_project_names(
        lambda pid: "docs-improvement" if pid == "proj-live01" else None
    )

    assert stamped == 1
    assert ScheduleStore(tmp_path).get(entry.schedule_id).web_project_name == "docs-improvement"


def test_backfill_leaves_unresolvable_ids_alone(tmp_path: Path) -> None:
    """Inventing a name for an id that no longer resolves would defeat the point."""
    from ciao.schedules import ScheduleManager

    store = ScheduleStore(tmp_path)
    entry = store.create(
        daily_time_utc="08:00", prompt="p", chat_id=0, model="", mode="normal",
        web_project_id="proj-gone01", workspace="work",
    )
    manager = ScheduleManager(
        store=store, resolve_target=lambda *a, **k: None,
        dispatch_to_web=None, prepare_chat=None,
    )

    assert manager.backfill_project_names(lambda _pid: None) == 0
    assert not ScheduleStore(tmp_path).get(entry.schedule_id).web_project_name


def test_backfill_does_not_overwrite_an_existing_name(tmp_path: Path) -> None:
    from ciao.schedules import ScheduleManager

    store = ScheduleStore(tmp_path)
    entry = store.create(
        daily_time_utc="08:00", prompt="p", chat_id=0, model="", mode="normal",
        web_project_id="proj-live01", web_project_name="chosen", workspace="work",
    )
    manager = ScheduleManager(
        store=store, resolve_target=lambda *a, **k: None,
        dispatch_to_web=None, prepare_chat=None,
    )

    assert manager.backfill_project_names(lambda _pid: "something-else") == 0
    assert ScheduleStore(tmp_path).get(entry.schedule_id).web_project_name == "chosen"
