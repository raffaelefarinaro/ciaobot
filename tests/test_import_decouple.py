"""Tests for ``ciao.import_decouple`` — the #994 decoupling contract.

Three questions, and every test here is a way one of them could be answered
wrongly in the direction that re-ingests Ciaobot's own work as the user's
history:

* is this ``(provider, session_id)`` Ciaobot's own, judged from Ciaobot's own
  registry/state rather than from the provider's storage?
* is this discovered session Ciaobot's own, external, or undecided?
* may this provenance tag be written at all?

Every registry snapshot, state snapshot and session record below is a literal
dict written in this file. Nothing here reads ``~/.claude``, ``~/.opencode``,
an account export, or a real provider session, and no test starts an engine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao import import_decouple as dec
from ciao.config import CiaoConfig, WorkspaceConfig

WORKSPACE = "personal"
OTHER_WORKSPACE = "clientA"


# ── Fixtures ──────────────────────────────────────────────────────────────


def _config(tmp_path: Path) -> CiaoConfig:
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in (WORKSPACE, OTHER_WORKSPACE)
        },
    )


def _registry(**chats: dict) -> dict:
    """A chat-registry snapshot: the payload ``_state_payload`` writes."""
    return {
        "version": 1,
        "projects": {
            "proj-notes": {"name": "Notes", "workspace": WORKSPACE, "kind": ""},
            "proj-memory": {
                "name": "Memory",
                "workspace": WORKSPACE,
                "kind": "memory",
            },
            "proj-client": {"name": "Client", "workspace": OTHER_WORKSPACE, "kind": ""},
        },
        "chats": dict(chats),
    }


def _write(config: CiaoConfig, registry: dict | None, state: dict | None) -> None:
    """Put synthetic snapshots where the engine's two records live."""
    runtime = Path(config.state_path).parent
    runtime.mkdir(parents=True, exist_ok=True)
    if registry is not None:
        (runtime / dec.REGISTRY_FILENAME).write_text(
            json.dumps(registry), encoding="utf-8"
        )
    if state is not None:
        Path(config.state_path).write_text(json.dumps(state), encoding="utf-8")


def _state(**sessions: str) -> dict:
    """A state-store snapshot: one recorded session id per context key."""
    return {
        "version": 3,
        "contexts": {
            key: {"session": {"session_id": value, "message_count": 1}}
            for key, value in sessions.items()
        },
    }


def _ciaobot_turn() -> str:
    """A first user turn carrying the capsule Ciaobot prepends."""
    return (
        f"{dec.CIAO_CONTEXT_BEGIN}\n[Project: \"Notes\"]\n{dec.CIAO_CONTEXT_END}\n\n"
        "Summarise yesterday's standup."
    )


# ── The exclusion set ─────────────────────────────────────────────────────


def test_live_chat_session_and_rotation_lineage_are_excluded() -> None:
    """A live chat's current id AND every id it rotated through are excluded.

    The rotation lineage is the half that is easy to lose: autocompact and a
    resume-failure fork push the old id onto ``previous_session_ids`` and start
    a new session, so matching only the current ``session_id`` leaves the
    conversation's earlier file importable as the user's own history.
    """
    registry = _registry(
        **{
            "chat-live1": {
                "project_id": "proj-notes",
                "provider": "claude",
                "session_id": "11111111-1111-4111-8111-111111111111",
                "previous_session_ids": [
                    "22222222-2222-4222-8222-222222222222",
                    "33333333-3333-4333-8333-333333333333",
                ],
                "archived": False,
            }
        }
    )

    own = dec.own_session_ids_from_snapshot(registry)

    assert own == {
        ("claude", "11111111-1111-4111-8111-111111111111"),
        ("claude", "22222222-2222-4222-8222-222222222222"),
        ("claude", "33333333-3333-4333-8333-333333333333"),
    }


def test_archived_chat_whose_reclaim_failed_is_still_excluded() -> None:
    """An archived row still names its session, because reclaim is fail-open.

    ``_archive_chat_unlocked`` reclaims the provider session and keeps
    ``session_id`` on the row either way, and the reclaim logs and continues
    when the provider is unavailable. A scanner that treated "archived" as
    "gone from disk" would import a file that is still there.
    """
    registry = _registry(
        **{
            "chat-arch1": {
                "project_id": "proj-notes",
                "provider": "opencode",
                "session_id": "ses_archived1",
                "previous_session_ids": [],
                "archived": True,
                "archive_path": "Logs/Chats/2026/10/chat-arch1.md",
            }
        }
    )

    own = dec.own_session_ids_from_snapshot(registry)

    assert ("opencode", "ses_archived1") in own
    assert dec.classify_session("opencode", "ses_archived1", "my own notes", own) == (
        dec.CIAOBOT_OWN
    )


def test_deleted_chat_recorded_only_in_the_state_store_is_still_excluded(
    tmp_path: Path,
) -> None:
    """A chat whose registry row is gone but whose state context survives.

    ``delete_chat`` pops the row from ``web_projects.json`` and drops the
    context from ``state.json`` — two different files, and the second write is
    the one that can be missed. The surviving context is the only remaining
    record that this session id was Ciaobot's, so it is read.
    """
    config = _config(tmp_path)
    _write(config, _registry(), _state(**{"chat-gone01": "ses_deleted01"}))

    own = dec.ciaobot_own_session_ids(config)

    # The state store records no provider, so the id is excluded against every
    # supported one rather than guessed at.
    assert ("opencode", "ses_deleted01") in own
    assert ("claude", "ses_deleted01") in own


def test_helper_chat_sessions_are_excluded_like_any_other_row(
    tmp_path: Path,
) -> None:
    """A memory-pass chat is an ordinary row, so there is no second list.

    The memory pass runs as a normal chat in the workspace's hidden Memory
    project; it keeps its own ``session_id`` and its own ``helper`` record. A
    separate helper exclusion list would be a second thing to keep in step with
    the registry, and it would be wrong the first time a helper kind was added.
    """
    registry = _registry(
        **{
            "chat-pass01": {
                "project_id": "proj-memory",
                "provider": "claude",
                "session_id": "44444444-4444-4444-8444-444444444444",
                "previous_session_ids": [],
                "archived": False,
                "helper": {"kind": "memory_pass", "source_chat_id": "chat-live1"},
            },
            "chat-help01": {
                "project_id": "proj-notes",
                "provider": "opencode",
                "session_id": "ses_helper01",
                "previous_session_ids": [],
                "archived": False,
                "helper": {
                    "kind": "proposal",
                    "intent": "review",
                    "proposal_ids": ["abc123"],
                    "archive_policy": "manual",
                },
            },
        }
    )

    own = dec.own_session_ids_from_snapshot(registry)

    assert ("claude", "44444444-4444-4444-8444-444444444444") in own
    assert ("opencode", "ses_helper01") in own


def test_memory_pass_session_classifies_as_ciaobot_own() -> None:
    """The classification a memory-pass / subagent session gets.

    A subagent turn never gets its own chat row: on Claude it is a sidechain
    entry in the parent's session file, so it is covered by the parent's id. The
    row that covers the memory pass is the pass chat's own, and a session it
    drove is therefore ``ciaobot_own`` rather than external.
    """
    pass_session = "55555555-5555-4555-8555-555555555555"
    known = dec.own_session_ids_from_snapshot(
        _registry(
            **{
                "chat-pass02": {
                    "project_id": "proj-memory",
                    "provider": "claude",
                    "session_id": pass_session,
                    "previous_session_ids": [],
                    "archived": False,
                    "helper": {"kind": "memory_pass", "source_chat_id": "chat-x"},
                }
            }
        )
    )

    assert dec.classify_session("claude", pass_session, _ciaobot_turn(), known) == (
        dec.CIAOBOT_OWN
    )
    # Same answer from the marker alone, for the pass row a failed reclaim left
    # with no recorded id anywhere.
    assert dec.classify_session("claude", "ses-unrecorded", _ciaobot_turn(), ()) == (
        dec.CIAOBOT_OWN
    )


def test_workspace_narrowing_keeps_unattributable_rows() -> None:
    """Another workspace's chat is left out; an unresolvable one is not.

    A row whose project names no workspace is the case
    ``_workspace_for_chat`` sends to ``primary_workspace()``, so it cannot be
    claimed for the workspace being scanned — and dropping it would be the one
    way this filter could under-exclude.
    """
    registry = _registry(
        **{
            "chat-mine01": {
                "project_id": "proj-notes",
                "provider": "claude",
                "session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "previous_session_ids": [],
            },
            "chat-theirs": {
                "project_id": "proj-client",
                "provider": "claude",
                "session_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "previous_session_ids": [],
            },
            "chat-orphan": {
                "project_id": "proj-gone",
                "provider": "claude",
                "session_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "previous_session_ids": [],
            },
        }
    )

    mine = dec.own_session_ids_from_snapshot(registry, workspace=WORKSPACE)

    assert ("claude", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") in mine
    assert ("claude", "cccccccc-cccc-4ccc-8ccc-cccccccccccc") in mine
    assert ("claude", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb") not in mine


def test_legacy_row_without_a_provider_is_excluded_as_claude() -> None:
    """A row predating the ``provider`` key is read the way ``_load`` reads it."""
    registry = _registry(
        **{
            "chat-legacy": {
                "project_id": "proj-notes",
                "session_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "previous_session_ids": [],
            }
        }
    )

    assert dec.own_session_ids_from_snapshot(registry) == {
        ("claude", "dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    }


def test_unreadable_registry_is_refused_rather_than_read_as_empty(
    tmp_path: Path,
) -> None:
    """A corrupt record stops the scan instead of excluding nothing.

    An empty exclusion set looks exactly like "this install has never run a
    chat", and a scan that trusted it would classify every Ciaobot session as
    the user's own history.
    """
    config = _config(tmp_path)
    runtime = Path(config.state_path).parent
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / dec.REGISTRY_FILENAME).write_text("{not json", encoding="utf-8")

    with pytest.raises(dec.RegistrySnapshotError):
        dec.ciaobot_own_session_ids(config)


def test_install_that_never_ran_a_chat_excludes_nothing() -> None:
    """Absent records are an empty set, not a refusal."""
    assert dec.own_session_ids_from_snapshot({}) == set()
    assert dec.own_session_ids_from_state({}) == set()


# ── Classification ────────────────────────────────────────────────────────


def test_unmarked_external_session_is_external() -> None:
    """A session with a readable opening turn and no Ciaobot marker is external."""
    assert (
        dec.classify_session(
            "claude",
            "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "why did the deploy fail last night?",
            {("claude", "ffffffff-ffff-4fff-8fff-ffffffffffff")},
        )
        == dec.EXTERNAL
    )


def test_session_with_a_ciaobot_marker_and_no_known_id_is_ciaobot_own() -> None:
    """The marker is the only rule that sees a session with no row behind it.

    A memory pass whose row is gone, an OpenCode child session Ciaobot drove:
    nothing in Ciaobot's state names the id, and the only evidence left is the
    capsule in the session's own first turn.
    """
    assert dec.classify_session("opencode", "ses_child001", _ciaobot_turn(), ()) == (
        dec.CIAOBOT_OWN
    )


def test_undecidable_session_is_ambiguous_and_never_external() -> None:
    """Nothing readable means ``ambiguous`` — absence of evidence is not evidence.

    A session whose opening turn could not be read (a truncated export, an
    adapter that found no first user turn) proves nothing in either direction,
    and reporting it ``external`` is how an undecided session gets imported.
    """
    assert dec.classify_session("claude", "", "some text", ()) == dec.AMBIGUOUS
    assert (
        dec.classify_session(
            "claude", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "   ", ()
        )
        == dec.AMBIGUOUS
    )


def test_a_ciaobot_chat_id_is_never_an_external_session_id() -> None:
    """A session id that is a chat id is Ciaobot's own bookkeeping.

    No provider mints ``chat-<8 hex>``, so such an id is the ``source_chat_id``
    overload the feasibility report rules out — it names a Ciaobot chat, which
    is not a conversation anyone typed.
    """
    assert dec.classify_session("claude", "chat-abcd1234", "hello", ()) == (
        dec.CIAOBOT_OWN
    )


def test_mixed_session_policy_excludes_the_whole_session() -> None:
    """A session Ciaobot has also used is excluded whole, never in part.

    After a Ciaobot turn the file holds prompt scaffolding, injected context,
    memory-pass output and compaction summaries, so no turn-level rule can
    separate the user's from Ciaobot's — the one signal that could
    (``user_turn_unattended``) lives in Ciaobot's registry, not in the file.
    """
    assert dec.MIXED_SESSION_POLICY == "exclude_whole_session"


# ── Provenance ────────────────────────────────────────────────────────────


def test_provenance_assertion_rejects_a_ciaobot_chat_id() -> None:
    """A fact may cite an external session, never a Ciaobot chat."""
    with pytest.raises(dec.ProvenanceNotExternal):
        dec.assert_external_provenance("claude:chat-abcd1234:msg_1")

    # Even when the caller hands over its own chat-id set, and even when the id
    # is one this install happens not to have minted.
    with pytest.raises(dec.ProvenanceNotExternal):
        dec.assert_external_provenance(
            "opencode:ses_not_a_chat_id:msg_9", known_chat_ids=["ses_not_a_chat_id"]
        )


def test_provenance_assertion_rejects_a_recorded_ciaobot_session() -> None:
    """A recorded Ciaobot-own session is refused even when its id looks external."""
    known = {("claude", "44444444-4444-4444-8444-444444444444")}

    with pytest.raises(dec.ProvenanceNotExternal):
        dec.assert_external_provenance(
            "claude:44444444-4444-4444-8444-444444444444:uuid-7",
            known_own_ids=known,
        )

    # The same tag passes once the session is external, so the refusal is the
    # contract and not a blanket ban on the shape.
    dec.assert_external_provenance("claude:ses_user_owned:uuid-7", known_own_ids=known)


def test_provenance_assertion_requires_the_documented_shape() -> None:
    """A bare id, or an empty tag, names no source and is refused."""
    for anchor in ("", "   ", "chat-abcd1234", "claude", ":ses_x:uuid-1"):
        with pytest.raises(dec.ProvenanceNotExternal):
            dec.assert_external_provenance(anchor)


def test_external_anchor_is_accepted() -> None:
    """The shape an imported fact is expected to carry."""
    dec.assert_external_provenance("claude_code:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:uuid-7")
    dec.assert_external_provenance("opencode:ses_user_owned:msg_01H")
