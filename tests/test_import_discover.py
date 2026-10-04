"""Engine-host discovery and consent-scoped selection, against synthetic sessions only.

Every session here is written by these tests: no real transcript, no real
``~/.claude``, no real ``~/.opencode`` and no real conversation is read. The
OpenCode CLI is never executed either — its listing is replaced — so nothing here
spawns a server or reaches a session database.

What is pinned is the boundary the issue states, not the plumbing:

* discovery reads **no conversation content** — a file whose first turn is a
  secret never has that secret reach a listing, and a listing is names, hints
  and sizes;
* it scans no directory outside the workspace's own configured roots, so another
  workspace's sessions cannot appear in this one's list;
* Ciaobot's own sessions are excluded **from Ciaobot's own records**, before the
  file is opened, and nothing in `available` is claimed to be `external`;
* a link and an over-cap file are refused rather than followed or half-read;
* a source that cannot be listed (an OpenCode below the enforced V2 floor) is a
  row with a reason, not an empty list that reads as "you have none";
* a listing that reached a cap says so, per source;
* the selected half reads only what was selected, and never returns a character
  of what it read.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ciao.agent_paths import claude_projects_dir
from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.import_decouple import (
    AMBIGUOUS,
    CIAOBOT_OWN,
    EXTERNAL,
    RegistrySnapshotError,
)
from ciao.import_discover import (
    BATCH_CAP,
    MAX_SESSIONS_PER_SOURCE,
    REASON_CIAOBOT_OWN,
    REASON_OVER_CAP,
    REASON_UNREADABLE,
    STATE_EXCLUDED,
    STATE_READY,
    STATE_UNREADABLE,
    UnknownWorkspace,
    classify_for_import,
    discover_sources,
    preview_selected,
)
from ciao.import_sources.contract import (
    MAX_SESSION_BYTES,
    PROVIDER_CLAUDE_CODE,
    PROVIDER_OPENCODE,
    SourceRef,
)

#: The version the enforced V2 floor accepts, and one that does not. Neither is
#: run: the adapter's own version check is replaced.
SUPPORTED_VERSION = "opencode v2.0.22"
UNSUPPORTED_VERSION = "opencode v1.9.4"

#: A binary that does not exist. Which path the adapter resolved is the
#: provider's business and has its own tests.
SYNTHETIC_BINARY = "/nonexistent/opencode"


def _entry(uuid: str, parent: str | None, kind: str, text: str, **flags: Any) -> str:
    """One JSONL entry in the shape Claude Code writes, as a line."""
    return json.dumps(
        {
            "type": kind,
            "uuid": uuid,
            "parentUuid": parent,
            "sessionId": "synthetic-session",
            "message": {"role": kind, "content": text},
            **flags,
        }
    )


def _session(first_user_turn: str = "help me with the vault layout") -> str:
    """A two-turn synthetic transcript with a readable opening turn."""
    return "\n".join(
        (
            _entry("u1", None, "user", first_user_turn),
            _entry("a1", "u1", "assistant", "sure — here is the layout"),
        )
    )


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CiaoConfig:
    """A config with one registered workspace, over a tmp runtime.

    ``reset_reroot_cache`` because ``agent_root_for`` resolves a workspace's own
    directory through the re-rooting receipt, which is process-level state a
    previous test may have set.
    """
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", lambda *a, **k: None)
    reset_reroot_cache()
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        pwa_auth_required=True,
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal")},
    )
    Path(config.workspace_vault_root("personal")).mkdir(parents=True, exist_ok=True)
    return config


def _slug_dir(config: CiaoConfig, workspace: str = "personal") -> Path:
    """The one Claude Code directory this workspace's discovery may read."""
    root = config.agent_root(workspace) if config.workspace(workspace) else config.workspace_root
    directory = claude_projects_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_session(directory: Path, session_id: str, body: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text(body if body is not None else _session(), encoding="utf-8")
    return path


def _record_own_session(config: CiaoConfig, session_id: str, workspace: str = "personal") -> None:
    """Record ``session_id`` as a live Ciaobot chat in the engine's own registry.

    Written the way ``ProjectChatManager`` writes it, because the exclusion set
    reads that file: a listing that could not see this would offer Ciaobot's own
    sessions as the user's history, which is the failure discovery exists to
    prevent.
    """
    registry = Path(config.state_path).parent / "web_projects.json"
    registry.write_text(
        json.dumps(
            {
                "projects": {"p1": {"id": "p1", "name": "General", "workspace": workspace}},
                "chats": {
                    "c1": {
                        "id": "c1",
                        "project_id": "p1",
                        "provider": "claude",
                        "session_id": session_id,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _fake_opencode(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = SUPPORTED_VERSION,
    rows: Any = None,
    refused: str | None = None,
) -> list[tuple[str, ...]]:
    """Replace the OpenCode adapter's bounded calls; return each JSON command's argv.

    Nothing is executed: the binary resolution, the version check and the JSON
    command are all replaced, so no server is started and no session database is
    reachable. A test can assert on what was asked for and what came back.
    """
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "ciao.import_sources.opencode._resolve_binary", lambda binary: SYNTHETIC_BINARY
    )
    monkeypatch.setattr(
        "ciao.import_sources.opencode._require_supported_v2",
        lambda binary, *, timeout: version,
    )

    def _run(binary: str, args: Sequence[str], timeout: float, **_: object) -> Any:
        calls.append(tuple(args))
        if refused is not None:
            from ciao.import_sources.opencode import SourceError

            raise SourceError(refused, "the fake CLI refused")
        return rows

    monkeypatch.setattr("ciao.import_sources.opencode._run_opencode_json", _run)
    return calls


# ── Metadata only, and only this workspace's own directories ───────────────


def test_discovery_lists_metadata_without_reading_a_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    directory = _slug_dir(config)
    secret = "NEVER-SURFACED first user turn"
    _write_session(directory, "sess-a", _session(secret))
    _fake_opencode(monkeypatch, rows=[])

    result = discover_sources(config, "personal")

    assert [ref.source_id for ref in result.available] == ["sess-a"]
    assert secret not in json.dumps(result.to_json())
    # Only the fields the contract's SourceRef carries: an id, a hint, a path.
    assert set(result.available[0].to_json()) == {
        "provider",
        "source_id",
        "project_hint",
        "path",
    }


def test_discovery_scans_only_this_workspace_s_own_slug_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(_slug_dir(config), "mine")
    # Another workspace's slug directory, one level above: a scan that globbed
    # `~/.claude/projects` instead of resolving this workspace's slug would find
    # this file and offer somebody else's history.
    elsewhere = claude_projects_dir(config.workspace_root / "someone-else")
    _write_session(elsewhere, "theirs")
    _fake_opencode(monkeypatch, rows=[])

    result = discover_sources(config, "personal")

    assert [ref.source_id for ref in result.available] == ["mine"]


def test_an_unknown_workspace_is_refused_rather_than_scanned_as_the_install_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(_slug_dir(config), "sess-a")
    _fake_opencode(monkeypatch, rows=[])

    for name in ("", "nope", "   "):
        with pytest.raises(UnknownWorkspace):
            discover_sources(config, name)


def test_an_unreadable_ciaobot_registry_refuses_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(_slug_dir(config), "sess-a")
    _fake_opencode(monkeypatch, rows=[])
    (Path(config.state_path).parent / "web_projects.json").write_text("{ not json", encoding="utf-8")

    # Raised, not degraded to an empty exclusion set: a listing built from one
    # would offer Ciaobot's own sessions as the user's history.
    with pytest.raises(RegistrySnapshotError):
        discover_sources(config, "personal")


# ── Ciaobot's own usage, and the "undecided, never external" rule ──────────


def test_ciaobot_s_own_sessions_are_excluded_before_their_file_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    directory = _slug_dir(config)
    _write_session(directory, "chat-deadbeef")
    _write_session(directory, "recorded-session")
    _record_own_session(config, "recorded-session")
    _fake_opencode(monkeypatch, rows=[])
    opened: list[Path] = []
    real_open = open

    def _watched_open(path: str, *args: Any, **kwargs: Any) -> Any:
        opened.append(Path(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _watched_open)
    result = discover_sources(config, "personal")

    assert result.available == ()
    assert {row.ref.source_id for row in result.excluded} == {
        "chat-deadbeef",
        "recorded-session",
    }
    assert {row.reason for row in result.excluded} == {REASON_CIAOBOT_OWN}
    assert [row.ref.source_id for row in result.excluded] == sorted(
        row.ref.source_id for row in result.excluded
    )
    # Nothing was opened: an excluded row is decided from Ciaobot's own records.
    assert not [p for p in opened if p.suffix == ".jsonl"]


def test_no_offered_row_is_claimed_external_without_reading_its_opening_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(_slug_dir(config), "sess-a")
    _fake_opencode(monkeypatch, rows=[])
    ref = SourceRef(PROVIDER_CLAUDE_CODE, "sess-a")

    # The rule, applied as discovery applies it: no readable opening turn means
    # `ambiguous`, never `external`.
    assert classify_for_import(config, "personal", ref) == AMBIGUOUS
    # ...and with the turn read, an unmarked one is external, which is the
    # decision `preview_selected` makes.
    assert classify_for_import(config, "personal", ref, first_user_turn="hi") == EXTERNAL
    assert [r.source_id for r in discover_sources(config, "personal").available] == ["sess-a"]


def test_a_ciaobot_marker_in_the_opening_turn_makes_a_session_ciaobot_s_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(
        _slug_dir(config),
        "unrecorded",
        _session("[CIAO_CONTEXT_BEGIN]\nCIAO_CONTEXT_END"),
    )
    _fake_opencode(monkeypatch, rows=[])
    ref = SourceRef(PROVIDER_CLAUDE_CODE, "unrecorded")

    # The one rule that sees a session Ciaobot drove without keeping a row for it.
    # Discovery cannot apply it (it reads no content), so the row is offered and
    # the preview is where it is refused.
    assert classify_for_import(config, "personal", ref) == AMBIGUOUS
    assert (
        classify_for_import(
            config, "personal", ref, first_user_turn="[CIAO_CONTEXT_BEGIN]x"
        )
        == CIAOBOT_OWN
    )


# ── Refusals: links, oversize files, vanished sessions ─────────────────────


def test_a_symlinked_session_is_refused_rather_than_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    directory = _slug_dir(config)
    _write_session(directory, "real-session")
    outside = tmp_path / "outside.jsonl"
    outside.write_text(_session(), encoding="utf-8")
    (directory / "linked-session.jsonl").symlink_to(outside)

    _fake_opencode(monkeypatch, rows=[])
    result = discover_sources(config, "personal")

    # The link is not offered at all: whatever it points at is not a file Claude
    # Code wrote under this slug.
    assert [ref.source_id for ref in result.available] == ["real-session"]
    assert all(
        not Path(ref.path).is_symlink() for ref in result.available
    )


def test_a_session_over_the_byte_cap_is_refused_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    directory = _slug_dir(config)
    _write_session(directory, "sess-a")
    oversize = _write_session(directory, "sess-huge", _session())
    with oversize.open("a", encoding="utf-8") as handle:
        handle.write("x" * (MAX_SESSION_BYTES + 1))
    _fake_opencode(monkeypatch, rows=[])

    result = discover_sources(config, "personal")

    assert [ref.source_id for ref in result.available] == ["sess-a"]
    refused = {row.ref.source_id: row for row in result.excluded}
    assert refused["sess-huge"].reason == REASON_OVER_CAP


def test_a_vanished_session_is_refused_rather_than_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    directory = _slug_dir(config)
    path = _write_session(directory, "sess-a")
    _fake_opencode(monkeypatch, rows=[])
    # Discovery lists what is on disk; a session that disappears between the
    # listing and the check must not be offered as though it were readable.
    listing = SourceRef(PROVIDER_CLAUDE_CODE, "sess-a", "", str(path))
    path.unlink()

    from ciao.import_discover import _refusal

    refusal = _refusal(listing)
    assert refusal is not None
    assert refusal.reason == REASON_UNREADABLE


def test_a_session_id_that_is_not_a_plain_file_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(_slug_dir(config), "sess-a")
    _fake_opencode(monkeypatch, rows=[])

    # A selection carries ids, never paths: a traversal attempt has no path to
    # resolve and is refused rather than joined. (A *blank* id cannot be built at
    # all — `SourceRef` refuses it — so it is the route boundary that turns one
    # into a 400.)
    for source_id in ("../secrets", "..", "a/b", "a\\b", "/etc/passwd"):
        preview = preview_selected(
            config, "personal", [SourceRef(PROVIDER_CLAUDE_CODE, source_id)]
        )
        assert preview.conversations[0].state == STATE_UNREADABLE
        assert preview.conversations[0].classification == AMBIGUOUS


# ── Unsupported sources and caps are rows, never empty lists ──────────────


def test_an_opencode_below_the_v2_floor_is_a_row_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(_slug_dir(config), "sess-a")
    _fake_opencode(monkeypatch, refused="unsupported_version")

    result = discover_sources(config, "personal")

    # The Claude Code listing still stands; OpenCode is reported as unusable
    # rather than as "no OpenCode conversations".
    assert [ref.source_id for ref in result.available] == ["sess-a"]
    assert [(row.provider, row.reason) for row in result.unsupported] == [
        (PROVIDER_OPENCODE, "unsupported_version")
    ]
    assert result.truncated[PROVIDER_OPENCODE] is False


def test_a_source_with_no_adapter_is_reported_and_never_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _fake_opencode(monkeypatch, rows=[])

    result = discover_sources(config, "personal", sources=["claude_account"])

    assert result.available == ()
    assert [(row.provider, row.reason) for row in result.unsupported] == [
        ("claude_account", "no_adapter")
    ]


def test_a_source_name_the_contract_does_not_know_is_a_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        discover_sources(config, "personal", sources=["clad_code"])


def test_a_full_opencode_page_is_reported_as_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.import_sources.opencode import DISCOVERY_MAX_COUNT

    config = _world(tmp_path, monkeypatch)
    page = [
        {"id": f"ses_{index:04d}", "directory": str(tmp_path)} for index in range(DISCOVERY_MAX_COUNT)
    ]
    _fake_opencode(monkeypatch, rows=page)

    result = discover_sources(config, "personal", sources=[PROVIDER_OPENCODE])

    assert len(result.available) == DISCOVERY_MAX_COUNT
    # The CLI answers one page with no cursor, so a full page is a page.
    assert result.truncated == {PROVIDER_OPENCODE: True}


def test_a_short_opencode_page_is_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _fake_opencode(monkeypatch, rows=[{"id": "ses_1", "directory": str(tmp_path)}])

    result = discover_sources(config, "personal", sources=[PROVIDER_OPENCODE])

    assert [ref.source_id for ref in result.available] == ["ses_1"]
    assert result.truncated == {PROVIDER_OPENCODE: False}


def test_a_claude_code_listing_over_the_cap_is_reported_as_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    directory = _slug_dir(config)
    for index in range(MAX_SESSIONS_PER_SOURCE + 3):
        _write_session(directory, f"sess-{index:05d}")
    _fake_opencode(monkeypatch, rows=[])

    result = discover_sources(config, "personal", sources=[PROVIDER_CLAUDE_CODE])

    assert len(result.available) == MAX_SESSIONS_PER_SOURCE
    assert result.truncated == {PROVIDER_CLAUDE_CODE: True}


# ── The selected half: counts, reasons, and no text anywhere ───────────────


def test_the_preview_summarizes_only_what_was_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    directory = _slug_dir(config)
    _write_session(directory, "sess-a")
    _write_session(directory, "sess-b")
    _fake_opencode(monkeypatch, rows=[])
    selected = [
        SourceRef(PROVIDER_CLAUDE_CODE, "sess-a", path=str(directory / "sess-a.jsonl"))
    ]

    preview = preview_selected(config, "personal", selected)

    assert [row.ref.source_id for row in preview.conversations] == ["sess-a"]
    row = preview.conversations[0]
    assert row.state == STATE_READY
    assert row.classification == EXTERNAL
    assert row.message_count == 2
    # No transcript text in the payload, at any level.
    assert "help me with the vault layout" not in json.dumps(preview.to_json())
    assert "sure — here is the layout" not in json.dumps(preview.to_json())


def test_the_preview_states_the_provider_model_volume_and_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(_slug_dir(config), "sess-a")
    _fake_opencode(monkeypatch, rows=[])
    ref = SourceRef(PROVIDER_CLAUDE_CODE, "sess-a")

    preview = preview_selected(config, "personal", [ref])

    # The same answers a new chat gets, so the screen cannot promise one
    # provider and run another.
    assert preview.provider == config.default_provider_for_workspace("personal")
    assert preview.model == config.default_model_for_workspace("personal", preview.provider)
    assert preview.batch_cap == BATCH_CAP
    assert preview.estimated_messages == 2
    assert preview.estimated_chars > 0
    assert Path(preview.destination) == config.workspace_vault_root("personal")


def test_an_opencode_id_from_another_project_is_refused_before_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(_slug_dir(config), "sess-a")
    # This workspace's own listing names one session. `opencode session export
    # <id>` resolves ids across every project on the machine, so an id a caller
    # hands the preview is not evidence that the session belongs here.
    calls = _fake_opencode(monkeypatch, rows=[{"id": "ses_mine", "directory": str(tmp_path)}])

    preview = preview_selected(
        config, "personal", [SourceRef(PROVIDER_OPENCODE, "ses_other_project")]
    )

    row = preview.conversations[0]
    assert row.state == STATE_UNREADABLE
    assert row.reason == REASON_UNREADABLE
    assert "this workspace" in row.message
    assert preview.estimated_messages == 0
    # Listed once, exported never: the refusal must not need the content.
    assert [args[:2] for args in calls] == [("session", "list")]


def test_the_preview_lists_opencode_once_for_the_whole_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(_slug_dir(config), "sess-a")
    calls = _fake_opencode(
        monkeypatch,
        rows=[{"id": "ses_one", "directory": str(tmp_path)}, {"id": "ses_two", "directory": str(tmp_path)}],
    )

    preview = preview_selected(
        config,
        "personal",
        [
            SourceRef(PROVIDER_OPENCODE, "ses_one"),
            SourceRef(PROVIDER_OPENCODE, "ses_two"),
            SourceRef(PROVIDER_CLAUDE_CODE, "sess-a"),
        ],
    )

    # One `session list` for two OpenCode rows, and one export per listed row:
    # the membership answer is a fact about the workspace, not about each ref.
    assert [args[:2] for args in calls] == [
        ("session", "list"),
        ("session", "export"),
        ("session", "export"),
    ]


def test_a_preview_without_an_opencode_ref_never_lists_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(_slug_dir(config), "sess-a")
    calls = _fake_opencode(monkeypatch, rows=[])

    preview_selected(config, "personal", [SourceRef(PROVIDER_CLAUDE_CODE, "sess-a")])

    assert calls == []


def test_an_opencode_listing_that_fails_refuses_rather_than_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(_slug_dir(config), "sess-a")
    calls = _fake_opencode(monkeypatch, rows=None, refused="unsupported_version")

    preview = preview_selected(config, "personal", [SourceRef(PROVIDER_OPENCODE, "ses_mine")])

    # A check that did not run cannot pass: the row carries the adapter's own
    # reason, and nothing was exported.
    assert preview.conversations[0].reason == "unsupported_version"
    assert [args[:2] for args in calls] == [("session", "list")]


def test_a_selection_wider_than_the_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.import_discover import MAX_SELECTION

    config = _world(tmp_path, monkeypatch)
    _fake_opencode(monkeypatch, rows=[])
    refs = [SourceRef(PROVIDER_CLAUDE_CODE, f"sess-{i}") for i in range(MAX_SELECTION + 1)]

    with pytest.raises(ValueError):
        preview_selected(config, "personal", refs)


def test_a_ciaobot_own_session_is_never_read_by_the_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    directory = _slug_dir(config)
    path = _write_session(directory, "recorded-session")
    _record_own_session(config, "recorded-session")
    _fake_opencode(monkeypatch, rows=[])

    reads: list[Path] = []
    real_open = open

    def _watched_open(file: str, *args: Any, **kwargs: Any) -> Any:
        reads.append(Path(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _watched_open)
    preview = preview_selected(
        config, "personal", [SourceRef(PROVIDER_CLAUDE_CODE, "recorded-session")]
    )

    assert preview.conversations[0].state == STATE_EXCLUDED
    assert preview.conversations[0].classification == CIAOBOT_OWN
    assert preview.estimated_messages == 0
    assert not [p for p in reads if p == path]


def test_a_session_that_opens_with_a_ciaobot_marker_is_excluded_after_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    _write_session(
        _slug_dir(config), "unrecorded", _session("[CIAO_CONTEXT_BEGIN]\nCIAO_CONTEXT_END")
    )
    _fake_opencode(monkeypatch, rows=[])
    ref = SourceRef(PROVIDER_CLAUDE_CODE, "unrecorded")

    preview = preview_selected(config, "personal", [ref])

    row = preview.conversations[0]
    assert row.state == STATE_EXCLUDED
    assert row.classification == CIAOBOT_OWN
    assert row.reason == REASON_CIAOBOT_OWN


def test_a_session_with_no_readable_opening_turn_is_ambiguous_not_external(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _world(tmp_path, monkeypatch)
    # A transcript whose only readable turn is an assistant's: there is no first
    # user turn to read, so the session is not decided — and not imported.
    body = _entry("a1", None, "assistant", "here is the layout")
    _write_session(_slug_dir(config), "no-opening-turn", body)
    _fake_opencode(monkeypatch, rows=[])

    preview = preview_selected(
        config, "personal", [SourceRef(PROVIDER_CLAUDE_CODE, "no-opening-turn")]
    )

    row = preview.conversations[0]
    assert row.state == STATE_EXCLUDED
    assert row.classification == AMBIGUOUS
    assert row.reason == AMBIGUOUS
    assert preview.estimated_messages == 0