"""``ciao.import_run`` — a filed batch, run into the ordinary review queue.

Everything below is synthetic: no model, no provider, no engine, no chat
transcript of Ciaobot's own making, and no real history. ``run_oneshot`` is
patched, the conversations are literal ``NormalizedSession`` objects built in
this file, and the vault is a scratch tree whose every file is compared byte for
byte before and after — so "nothing durable is written outside the queue" is an
enforcement result and not an absence nobody checked.

What is pinned here is the wiring C7 owns, not C4's contract (that is
``tests/test_import_extract.py``) and not the store's rules (that is
``tests/test_import_store.py``):

* a selected external conversation lands in the queue, and the batch records the
  progress, the per-source digest and one provenance row per filed bullet;
* a Ciaobot-own session is refused before any turn, and nothing is filed;
* an unchanged re-import appends nothing, and a changed one appends beside the
  old bullets rather than over them;
* a model-written date is dropped and the **source** message's date survives;
* two conflicting dated claims both stay queued for a person to decide;
* a cancel mid-batch keeps what was filed and stops the rest;
* forgetting the batch leaves an accepted memory intact.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.import_decouple import CIAO_CONTEXT_BEGIN
from ciao.import_run import batch_store, run_import_batch
from ciao.import_sources.contract import (
    PROVIDER_CLAUDE_CODE,
    PROVIDER_OPENCODE,
    ROLE_ASSISTANT,
    ROLE_USER,
    NormalizedMessage,
    NormalizedSession,
    SourceRef,
)
from ciao.import_store import (
    DONE,
    FAILED,
    PARTIAL,
    SOURCE_EXTRACTED,
    SOURCE_REFUSED,
    SOURCE_SKIPPED,
    ImportStoreError,
    engine_store_path,
)
from ciao.memory_proposals import list_proposals

QUEUE = "Workspace/Memory-Proposals.md"
SESSION_ID = "ses_synthetic0001"
CLAUDE_SESSION_ID = "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"


# ── A world: two workspaces, a real config, a real batch store ─────────────


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CiaoConfig:
    """A config over a scratch tree with `personal` and `work` registered."""
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", lambda *a, **k: None)
    reset_reroot_cache()
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return CiaoConfig(
        pwa_auth_token="test-token",
        pwa_auth_required=True,
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            ),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )


def _seed_vault(config: CiaoConfig) -> tuple[Path, Path]:
    """The destination vault and its guide, both seeded with real content.

    Returns ``(vault_root, guide)``. The guide is outside the vault (it is the
    workspace's ``AGENTS.md``) and is seeded so "the region did not change"
    compares against content rather than against a file the test itself wrote.
    """
    from ciao import memory_tool as mt

    vault = config.workspace_vault_root("personal")
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    (vault / "Workspace" / "Learnings.md").write_text(
        "# Learnings\n\n## Active\n\n- Existing lesson.\n", encoding="utf-8"
    )
    (vault / "projects" / "active").mkdir(parents=True, exist_ok=True)
    (vault / "projects" / "active" / "Checkout.md").write_text(
        "---\ntype: project\n---\n# Checkout\n\nStaging detail lives here.\n",
        encoding="utf-8",
    )
    guide = config.workspace_root / "AGENTS.md"
    guide.write_text("# Guide\n", encoding="utf-8")
    mt.ensure_regions(guide)
    mt.write_region(guide, "memory", ["Durable rule: ship small."])
    mt.write_region(guide, "profile", ["Prefers short release notes."])
    return vault, guide


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file under ``root``, by ``as_posix()`` relative path, as bytes.

    Keys are ``as_posix()`` and never ``str()``: ``str`` spells the separators
    with backslashes on Windows, so a ``/``-spelled expectation would match
    nothing there. ``queue-locks/`` is skipped — a lock file is not workspace
    content.
    """
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "queue-locks" not in path.parts
    }


def _memory_bytes(config: CiaoConfig) -> dict[str, bytes]:
    """Everything a durable memory write would touch, by name, as bytes.

    The destination vault plus the guide that holds the bounded regions — which
    is where a region, doc, entity or learning write would land. The private
    batch store is deliberately *not* here: recording progress and provenance
    on it is what this child is for, and it lives under the runtime directory
    rather than in anybody's vault.
    """
    vault = config.workspace_vault_root("personal")
    files = _snapshot(vault)
    guide = config.workspace_root / "AGENTS.md"
    files["AGENTS.md"] = guide.read_bytes()
    return files


def _changed_memory(config: CiaoConfig, before: dict[str, bytes]) -> set[str]:
    after = _memory_bytes(config)
    return {
        name for name in set(before) | set(after) if before.get(name) != after.get(name)
    }


# ── A synthetic conversation ───────────────────────────────────────────────


def _session(
    *,
    provider: str = PROVIDER_OPENCODE,
    source_id: str = SESSION_ID,
    messages: tuple[NormalizedMessage, ...] | None = None,
) -> NormalizedSession:
    """One normalized conversation, built from literals.

    ``msg_0003`` carries a real date so "the fact is dated by its source, not by
    the import" is testable; the others carry none, which is the honest state of
    a source with no per-message date.
    """
    return NormalizedSession(
        source=SourceRef(provider=provider, source_id=source_id),
        messages=(
            messages
            if messages is not None
            else (
                NormalizedMessage(
                    role=ROLE_USER,
                    text="How do deploys work here?",
                    anchor="msg_0001",
                ),
                NormalizedMessage(
                    role=ROLE_ASSISTANT,
                    text="Only on Fridays, and the release owner signs off first.",
                    anchor="msg_0002",
                ),
                NormalizedMessage(
                    role=ROLE_USER,
                    text="The staging database is refreshed every Sunday at 02:00 UTC.",
                    anchor="msg_0003",
                    timestamp="2024-05-01T02:00:00Z",
                ),
                NormalizedMessage(
                    role=ROLE_ASSISTANT,
                    text="Noted for release notes: the user writes them short.",
                    anchor="msg_0004",
                ),
            )
        ),
    )


def _own_session(*, source_id: str = SESSION_ID) -> NormalizedSession:
    """A session Ciaobot drove itself, decided by the marker in its own turn."""
    return NormalizedSession(
        source=SourceRef(provider=PROVIDER_OPENCODE, source_id=source_id),
        messages=(
            NormalizedMessage(
                role=ROLE_USER,
                text=f"{CIAO_CONTEXT_BEGIN} vault context follows",
                anchor="msg_0001",
            ),
            NormalizedMessage(role=ROLE_ASSISTANT, text="Noted.", anchor="msg_0002"),
        ),
    )


def _own_registry(config: CiaoConfig, session_id: str) -> None:
    """Record ``session_id`` as a Ciaobot chat row for the workspace."""
    Path(config.state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(config.state_path).parent.joinpath("web_projects.json").write_text(
        json.dumps(
            {
                "projects": {},
                "chats": {
                    "chat-1a2b3c4d": {
                        "project_id": "missing",
                        "session_id": session_id,
                        "provider": "opencode",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _read_session(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[str, NormalizedSession]
) -> dict[str, NormalizedSession]:
    """Answer the runner's adapter read from a literal map, keyed by source id.

    Patches the two C5 entry points the runner calls, so the resolution rules
    under them (the slug-directory rebuild, the OpenCode membership check) stay
    the real ones while no file and no CLI are involved. The map is returned so
    a caller can wrap the patched reader.
    """
    monkeypatch.setattr(
        "ciao.import_run.read_claude_code_session",
        lambda path, **_kw: mapping[Path(path).stem],
    )
    monkeypatch.setattr(
        "ciao.import_run.read_opencode_session",
        lambda session_id: mapping[session_id],
    )
    monkeypatch.setattr(
        "ciao.import_run._resolve_claude_code_ref",
        lambda root, ref: root / f"{ref.source_id}.jsonl",
    )
    monkeypatch.setattr(
        "ciao.import_run._opencode_membership",
        lambda selected, root: frozenset(ref.source_id for ref in selected),
    )
    return mapping


def _reply(monkeypatch: pytest.MonkeyPatch, reply: str) -> list[dict[str, Any]]:
    """Replace ``run_oneshot`` with one returning ``reply``; record what it saw."""
    calls: list[dict[str, Any]] = []

    async def fake_run_oneshot(prompt: str, **kwargs: object) -> str:
        calls.append({"prompt": prompt, **kwargs})
        return reply

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_run_oneshot)
    return calls


def _as_of(text: str) -> str:
    """The ``[as-of: YYYY-MM-DD]`` date on a queued bullet, or ``""``."""
    match = re.search(r"\[as-of:\s*(\d{4}-\d{2}-\d{2})\]", text)
    return match.group(1) if match else ""


def _row_reply(*, anchor: str = "msg_0003", **overrides: Any) -> str:
    """A one-row model reply, as JSON.

    The default row is the fixture fact cited to the session's dated message, so
    the date it picks up in the queue is the source message's own.
    """
    row = {
        "text": "The staging database is refreshed every Sunday at 02:00 UTC.",
        "destination": "memory",
        "payload": "",
        "source_anchor": anchor,
        "as_of": None,
    }
    row.update(overrides)
    return json.dumps([row])


# ── The happy path: proposals, progress, provenance, and nothing else ──────


@pytest.mark.asyncio
async def test_a_selected_conversation_lands_in_the_queue_and_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole wiring, asserted on both of its outputs.

    The queue gains exactly the fixture facts, the batch records progress, the
    source's content digest and one provenance row per filed bullet, and every
    other file in the scratch tree is byte-identical afterwards — which is what
    "nothing durable is written outside ``append_proposals``" means.
    """
    config = _world(tmp_path, monkeypatch)
    vault, _guide = _seed_vault(config)
    before = _memory_bytes(config)
    assert before, "the scratch tree is seeded, so this compares against content"
    _read_session(monkeypatch, {SESSION_ID: _session()})
    _reply(
        monkeypatch,
        (
            Path(__file__).resolve().parent
            / "fixtures/import/extraction_reply_valid.json"
        ).read_text(encoding="utf-8"),
    )

    store = batch_store(config)
    batch = store.create(
        workspace="personal",
        sources=[{"provider": PROVIDER_OPENCODE, "source_id": SESSION_ID}],
    )
    result = await run_import_batch(
        config, batch.batch_id, model="haiku", provider="claude", store=store
    )

    # The queue: the three facts, dated by their source message and not by the
    # import.
    rows = list_proposals(vault / QUEUE)
    assert [row["text"] for row in rows] == [
        "Deploys happen only on Fridays and need the release owner's sign-off.",
        "The staging database is refreshed every Sunday at 02:00 UTC. "
        "[as-of: 2024-05-01]",
        "Prefers short sentences over long prose in release notes.",
    ]
    assert all(
        row["source"].startswith(f"opencode:{SESSION_ID}:") for row in rows
    ), "every row keeps the external tag the review UI shows"

    assert result.status == DONE
    assert result.sources_extracted == 1
    assert result.proposals_filed == 3
    assert result.cancelled is False
    assert result.error == ""

    settled = store.get(batch.batch_id)
    assert settled.status == DONE
    assert settled.progress.total_sources == 1
    assert settled.progress.completed_sources == 1
    assert settled.progress.proposals_filed == 3
    assert settled.progress.current_source_id == ""
    source = settled.sources[0]
    assert source.status == SOURCE_EXTRACTED
    assert len(source.content_digest) == 64, "the digest of the session just read"
    assert sorted(
        (row.provider, row.source_id, row.anchor, row.destination)
        for row in settled.provenance
    ) == [(PROVIDER_OPENCODE, SESSION_ID, f"msg_000{n}", "personal") for n in (2, 3, 4)]
    assert all(
        row.accepted is False for row in settled.provenance
    ), "a filed proposal is not an accepted fact"

    changed = _changed_memory(config, before)
    assert changed == {QUEUE}, f"a run changed more than the queue: {sorted(changed)}"


@pytest.mark.asyncio
async def test_the_run_calls_exactly_one_turn_per_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One no-tools turn, and never with a caller-named model.

    The model and provider the runner uses come from the configuration (the same
    answer the preview showed), so the assertion is on what the call was given
    and on the fact that the body could not have chosen it.
    """
    config = _world(tmp_path, monkeypatch)
    _seed_vault(config)
    _read_session(monkeypatch, {SESSION_ID: _session()})
    calls = _reply(monkeypatch, _row_reply())

    store = batch_store(config)
    batch = store.create(
        workspace="personal",
        sources=[{"provider": PROVIDER_OPENCODE, "source_id": SESSION_ID}],
    )
    await run_import_batch(config, batch.batch_id, store=store)

    assert len(calls) == 1, "one conversation is one turn, not one per fact"
    assert calls[0]["model"] == config.default_model_for_workspace(
        "personal", "opencode"
    )
    assert calls[0]["provider"] == "opencode"
    assert calls[0]["timeout_s"] > 0


# ── Ciaobot's own sessions never reach a turn ──────────────────────────────


@pytest.mark.asyncio
async def test_a_ciaobot_own_session_is_refused_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session Ciaobot drove is refused by its own marker, at zero cost.

    The exclusion set is read from Ciaobot's own records, so a second refusal —
    a recorded session id, whose shape alone would not look like a chat id — is
    exercised too. Neither costs a turn, and neither files anything.
    """
    config = _world(tmp_path, monkeypatch)
    vault, _guide = _seed_vault(config)
    calls = _reply(monkeypatch, _row_reply())
    _read_session(
        monkeypatch,
        {
            SESSION_ID: _own_session(),
            "ses_ciaobot_run_0001": _own_session(source_id="ses_ciaobot_run_0001"),
        },
    )
    _own_registry(config, "ses_ciaobot_run_0001")

    store = batch_store(config)
    first = store.create(
        workspace="personal",
        sources=[{"provider": PROVIDER_OPENCODE, "source_id": SESSION_ID}],
    )
    by_marker = await run_import_batch(
        config, first.batch_id, model="haiku", provider="claude", store=store
    )
    assert by_marker.status == FAILED
    assert by_marker.sources_refused == 1
    assert by_marker.sources_extracted == 0
    assert store.get(first.batch_id).sources[0].status == SOURCE_REFUSED
    store.forget(first.batch_id)

    second = store.create(
        workspace="personal",
        sources=[{"provider": PROVIDER_OPENCODE, "source_id": "ses_ciaobot_run_0001"}],
    )
    by_record = await run_import_batch(
        config, second.batch_id, model="haiku", provider="claude", store=store
    )

    assert by_record.sources_refused == 1
    assert calls == [], "no turn ran for either refusal"
    assert list_proposals(vault / QUEUE) == []
    assert not (vault / QUEUE).exists()


# ── Dedupe: an unchanged re-import appends nothing ─────────────────────────


@pytest.mark.asyncio
async def test_an_unchanged_re_import_appends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same conversation again is the same fact, so it is not queued twice.

    The per-fact dedupe is ``append_proposals``' exact-text one; the batch-level
    key is C6's, released only by ``forget``. Together they make a re-import of
    unchanged content a no-op rather than a second copy of the same fact.
    """
    config = _world(tmp_path, monkeypatch)
    vault, _guide = _seed_vault(config)
    _read_session(monkeypatch, {SESSION_ID: _session()})
    _reply(monkeypatch, _row_reply())

    store = batch_store(config)
    first = store.create(
        workspace="personal",
        sources=[{"provider": PROVIDER_OPENCODE, "source_id": SESSION_ID}],
    )
    before = await run_import_batch(config, first.batch_id, store=store)
    assert before.proposals_filed == 1
    digest = store.get(first.batch_id).sources[0].content_digest
    store.forget(first.batch_id)

    second = store.create(
        workspace="personal",
        sources=[
            {
                "provider": PROVIDER_OPENCODE,
                "source_id": SESSION_ID,
                "content_digest": digest,
            }
        ],
    )
    again = await run_import_batch(config, second.batch_id, store=store)

    assert again.proposals_filed == 0, "the queue's exact-text dedupe held the row"
    assert again.sources_extracted == 1, "the conversation was still read"
    assert len(list_proposals(vault / QUEUE)) == 1
    assert (
        store.get(second.batch_id).provenance == ()
    ), "no evidence is recorded for a bullet this run did not file"
    assert store.get(second.batch_id).sources[0].content_digest == digest, (
        "the same conversation has the same digest, which is what makes the "
        "batch-level key match"
    )


@pytest.mark.asyncio
async def test_a_changed_conversation_is_a_new_attempt_not_an_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changed content appends beside the old bullet; it never replaces it.

    A conversation that grew since a previous import produces a new fact, and
    the queue is the record of both attempts. Nothing is silently overwritten:
    removing a fact is the review surface's own decision.
    """
    config = _world(tmp_path, monkeypatch)
    vault, _guide = _seed_vault(config)
    grown = NormalizedSession(
        source=_session().source,
        messages=(
            *_session().messages,
            NormalizedMessage(
                role=ROLE_ASSISTANT,
                text="Since then the refresh also rotates the credentials.",
                anchor="msg_0005",
            ),
        ),
    )
    _read_session(monkeypatch, {SESSION_ID: _session()})
    _reply(monkeypatch, _row_reply())
    store = batch_store(config)
    first = store.create(
        workspace="personal",
        sources=[{"provider": PROVIDER_OPENCODE, "source_id": SESSION_ID}],
    )
    await run_import_batch(config, first.batch_id, store=store)
    first_digest = store.get(first.batch_id).sources[0].content_digest
    store.forget(first.batch_id)

    _read_session(monkeypatch, {SESSION_ID: grown})
    _reply(
        monkeypatch,
        _row_reply(
            text="Since then the refresh also rotates the credentials.",
            source_anchor="msg_0005",
        ),
    )
    second = store.create(
        workspace="personal",
        sources=[
            {
                "provider": PROVIDER_OPENCODE,
                "source_id": SESSION_ID,
                "content_digest": first_digest,
            }
        ],
    )
    await run_import_batch(config, second.batch_id, store=store)

    texts = [row["text"] for row in list_proposals(vault / QUEUE)]
    assert texts == [
        "The staging database is refreshed every Sunday at 02:00 UTC. "
        "[as-of: 2024-05-01]",
        "Since then the refresh also rotates the credentials.",
    ], "the older bullet is still queued; the new one is appended beside it"


# ── Dates: the source's, never the model's and never the import's ──────────


@pytest.mark.asyncio
async def test_a_model_written_date_is_dropped_and_the_source_date_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forged ``[as-of:]`` is not a fact, and the real date is the source's.

    Two rows: one whose text carries a date the model wrote (dropped), and one
    cited to the dated message (kept, with *that* message's date). The import
    date never appears on either.
    """
    config = _world(tmp_path, monkeypatch)
    vault, _guide = _seed_vault(config)
    _read_session(monkeypatch, {SESSION_ID: _session()})
    calls = _reply(
        monkeypatch,
        json.dumps(
            [
                {
                    "text": "The release is on 2026-01-01 [as-of: 2026-01-01].",
                    "destination": "memory",
                    "payload": "",
                    "source_anchor": "msg_0002",
                    "as_of": "2026-01-01",
                },
                {
                    "text": "Deploys happen only on Fridays.",
                    "destination": "memory",
                    "payload": "",
                    "source_anchor": "msg_0003",
                    "as_of": "2026-01-01",
                },
            ]
        ),
    )

    store = batch_store(config)
    batch = store.create(
        workspace="personal",
        sources=[{"provider": PROVIDER_OPENCODE, "source_id": SESSION_ID}],
    )
    result = await run_import_batch(config, batch.batch_id, store=store)

    rows = list_proposals(vault / QUEUE)
    assert [row["text"] for row in rows] == [
        "Deploys happen only on Fridays. [as-of: 2024-05-01]"
    ]
    assert result.skipped == 1, "the forged row was dropped, not dated"
    assert result.proposals_filed == 1
    prompt = calls[0]["prompt"]
    assert "2026-01-01" not in prompt, "the model's own date was never evidence"


@pytest.mark.asyncio
async def test_two_conflicting_dated_claims_both_stay_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflict is two rows a person decides between, not one auto-selected.

    Same durable fact, two conversations, two source dates. Nothing in the
    review path compares them, and the acceptance criterion is precisely that
    neither is dropped or re-dated: both remain, each carrying its own source
    date and anchor.
    """
    config = _world(tmp_path, monkeypatch)
    vault, _guide = _seed_vault(config)
    _read_session(
        monkeypatch,
        {
            SESSION_ID: _session(),
            "ses_synthetic0002": _session(source_id="ses_synthetic0002"),
        },
    )
    _reply(monkeypatch, _row_reply())

    store = batch_store(config)
    first = store.create(
        workspace="personal",
        sources=[{"provider": PROVIDER_OPENCODE, "source_id": SESSION_ID}],
    )
    await run_import_batch(config, first.batch_id, store=store)
    store.forget(first.batch_id)

    later = NormalizedSession(
        source=SourceRef(provider=PROVIDER_OPENCODE, source_id="ses_synthetic0002"),
        messages=(
            NormalizedMessage(
                role=ROLE_USER,
                text="Refreshes happen on Tuesdays now.",
                anchor="msg_0001",
                timestamp="2024-09-09T09:00:00Z",
            ),
        ),
    )
    _read_session(
        monkeypatch,
        {SESSION_ID: _session(), "ses_synthetic0002": later},
    )
    _reply(
        monkeypatch,
        _row_reply(
            text="The staging database is refreshed every Sunday at 02:00 UTC.",
            source_anchor="msg_0001",
        ),
    )
    second = store.create(
        workspace="personal",
        sources=[{"provider": PROVIDER_OPENCODE, "source_id": "ses_synthetic0002"}],
    )
    await run_import_batch(config, second.batch_id, store=store)

    rows = list_proposals(vault / QUEUE)
    assert len(rows) == 2, "neither claim was resolved away"
    assert {row["source"] for row in rows} == {
        f"opencode:{SESSION_ID}:msg_0003",
        "opencode:ses_synthetic0002:msg_0001",
    }
    assert sorted(_as_of(row["text"]) for row in rows) == [
        "2024-05-01",
        "2024-09-09",
    ], "each row keeps its own source date, and neither was re-stamped"


# ── Cancellation ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_cancel_mid_batch_keeps_what_was_filed_and_stops_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation stops future extraction and unsends nothing.

    The cancel is placed by the test's own ``run_oneshot`` — mid-batch, between
    the first conversation's turn and the second's — which is the only place a
    cancel can land that this module could not have handled at the boundaries.
    """
    config = _world(tmp_path, monkeypatch)
    vault, _guide = _seed_vault(config)
    _read_session(
        monkeypatch,
        {
            SESSION_ID: _session(),
            "ses_synthetic0002": _session(source_id="ses_synthetic0002"),
        },
    )
    store = batch_store(config)

    def _reply_cancel_after_first(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        prompts: list[str] = []

        async def fake_run_oneshot(prompt: str, **_kwargs: object) -> str:
            prompts.append(prompt)
            store.cancel(batch.batch_id, reason="stopped by a test")
            return _row_reply()

        monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_run_oneshot)
        return prompts

    batch = store.create(
        workspace="personal",
        sources=[
            {"provider": PROVIDER_OPENCODE, "source_id": SESSION_ID},
            {"provider": PROVIDER_OPENCODE, "source_id": "ses_synthetic0002"},
        ],
    )
    prompts = _reply_cancel_after_first(monkeypatch)

    result = await run_import_batch(config, batch.batch_id, store=store)

    assert len(prompts) == 1, "the second conversation's turn never ran"
    assert result.cancelled is True
    assert result.status == "cancelled"
    assert result.proposals_filed == 1, "the proposal filed before the cancel stays"
    assert len(list_proposals(vault / QUEUE)) == 1
    settled = store.get(batch.batch_id)
    assert settled.status == "cancelled", "cancellation owns the final state"
    assert settled.progress.completed_sources == 0, (
        "the turn that finished after the cancel did not rewrite what the "
        "cancellation retained"
    )


# ── Forgetting a batch never deletes an accepted memory ────────────────────


@pytest.mark.asyncio
async def test_forgetting_the_batch_leaves_an_accepted_memory_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept a proposal, forget the import, and the vault entry survives.

    Removing an import is a record-keeping act. The facts a person accepted are
    theirs, with the source date and the anchor that admitted them; the batch's
    selection, digests and unaccepted provenance are what go away.
    """
    config = _world(tmp_path, monkeypatch)
    vault, _guide = _seed_vault(config)
    _read_session(monkeypatch, {SESSION_ID: _session()})
    _reply(monkeypatch, _row_reply())
    store = batch_store(config)
    batch = store.create(
        workspace="personal",
        sources=[{"provider": PROVIDER_OPENCODE, "source_id": SESSION_ID}],
    )
    await run_import_batch(config, batch.batch_id, store=store)

    from ciao import memory_tool as mt
    from ciao.memory_proposals import accept_region_fact

    rows = list_proposals(vault / QUEUE)
    guide = config.workspace_root / "AGENTS.md"
    outcome, _promotable = accept_region_fact(
        guide_path=guide,
        target=rows[0]["kind"],
        text=rows[0]["text"],
        vault_root=vault,
        actor="operator",
        source="pwa",
        workspace="personal",
    )
    assert outcome == "written", outcome
    region = mt.read_region(guide, "memory")[0]
    assert any(
        "Sunday" in entry for entry in region
    ), "the accepted fact is in the region before anything is forgotten"
    assert any(
        "2024-05-01" in entry for entry in region
    ), "and it is dated by its source message, not by the import"

    store.forget(batch.batch_id)

    assert (
        mt.read_region(guide, "memory")[0] == region
    ), "forgetting an import never deletes an accepted memory"
    assert (
        list_proposals(vault / QUEUE) == rows
    ), "and it does not unfile the proposals that are still queued"


# ── Store refusals are the caller's, not swallowed ─────────────────────────


@pytest.mark.asyncio
async def test_an_unknown_or_settled_batch_is_refused_by_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A route answers 404/409 from these, so they propagate rather than return."""
    config = _world(tmp_path, monkeypatch)
    _seed_vault(config)
    store = batch_store(config)

    with pytest.raises(ImportStoreError) as missing:
        await run_import_batch(config, "0123456789abcdef0123456789abcdef", store=store)
    assert missing.value.code == "not_found"

    batch = store.create(
        workspace="personal",
        sources=[{"provider": PROVIDER_OPENCODE, "source_id": SESSION_ID}],
    )
    store.cancel(batch.batch_id)
    with pytest.raises(ImportStoreError) as settled:
        await run_import_batch(config, batch.batch_id, store=store)
    assert settled.value.code == "conflict"


@pytest.mark.asyncio
async def test_a_claude_code_selection_is_resolved_through_the_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Claude Code row runs on Claude's own insights model.

    The provider a per-provider model override is keyed by is the *source's*,
    canonicalised — the same rule the memory pass applies to a chat's provider.
    """
    config = _world(tmp_path, monkeypatch)
    config.provider_insights_models = {"claude": "claude-sonnet-test"}
    _seed_vault(config)
    _read_session(
        monkeypatch,
        {
            CLAUDE_SESSION_ID: _session(
                provider=PROVIDER_CLAUDE_CODE, source_id=CLAUDE_SESSION_ID
            )
        },
    )
    calls = _reply(monkeypatch, _row_reply())
    store = batch_store(config)
    batch = store.create(
        workspace="personal",
        sources=[{"provider": PROVIDER_CLAUDE_CODE, "source_id": CLAUDE_SESSION_ID}],
    )

    await run_import_batch(config, batch.batch_id, store=store)

    assert len(calls) == 1
    assert calls[0]["model"] == "claude-sonnet-test"
    assert calls[0]["provider"] == "claude"


def test_an_unreadable_source_is_one_skipped_row_not_a_failed_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished conversation costs its own source, not the batch."""
    from ciao.import_sources.opencode import SourceError

    config = _world(tmp_path, monkeypatch)
    _seed_vault(config)

    def _refuse(session_id: str) -> NormalizedSession:
        if session_id in _readable:
            return _readable[session_id]
        raise SourceError("unreadable_output", f"no such session {session_id}")

    _readable = _read_session(monkeypatch, {SESSION_ID: _session()})
    monkeypatch.setattr("ciao.import_run.read_opencode_session", _refuse)
    calls = _reply(monkeypatch, _row_reply())
    store = batch_store(config)
    batch = store.create(
        workspace="personal",
        sources=[
            {"provider": PROVIDER_OPENCODE, "source_id": SESSION_ID},
            {"provider": PROVIDER_OPENCODE, "source_id": "ses_gone"},
        ],
    )

    result = asyncio.run(run_import_batch(config, batch.batch_id, store=store))

    assert result.status == PARTIAL
    assert result.sources_extracted == 1
    assert result.sources_skipped == 1
    statuses = {row.source_id: row.status for row in store.get(batch.batch_id).sources}
    assert statuses == {SESSION_ID: SOURCE_EXTRACTED, "ses_gone": SOURCE_SKIPPED}
    assert len(calls) == 1, "the readable conversation still ran its turn"


def test_the_batch_store_is_the_one_the_routes_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One path for the file, so a run and a route cannot disagree."""
    from ciao.web.routes_import import import_batch_store

    config = _world(tmp_path, monkeypatch)

    assert batch_store(config).path == import_batch_store(config).path
    assert batch_store(config).path == engine_store_path(config)
