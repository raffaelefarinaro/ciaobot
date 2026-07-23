"""Schedule -> project resolution when the stored web_project_id is stale.

Project IDs regenerate per device on fresh init, so schedules.json (shared
via git) carries dangling web_project_id values. `_resolve_schedule_project`
maps a stale id to a local General project. It must honour an explicit
`workspace` field on the schedule instead of guessing from the schedule_id
prefix, otherwise work schedules whose id doesn't start with "sched-work"
(e.g. the morning action briefing, sched-ee193709) land in personal General
and lose the work-only connector MCPs.

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
                default_provider="codex",
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
    assert provider == "codex"
    assert model == ""


def test_schedule_inheritance_is_resolved_again_after_workspace_change(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    workspace = WorkspaceConfig(
        name="personal",
        vault_root="personal",
        default_provider="codex",
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

    assert pcm.schedule_effective_routing(entry) == ("codex", "", "personal")

    workspace.default_provider = "claude"
    workspace.default_model = "sonnet"

    assert pcm.schedule_effective_routing(entry) == (
        "claude",
        "sonnet",
        "personal",
    )
