"""Tests for ``ciao.memory_proposals``.

The archive-time producer is gone (#627): the memory pass, a chat of the
app's own, writes memory directly now. What is left is the half that outlives
it — filing a proposal by hand, the review surface, the accept path into the
bounded regions, learnings, and the receipts all of those leave.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import unittest.mock
from pathlib import Path

from ciao import memory_proposals as mp
from ciao import memory_tool as mt
from ciao import proposal_tracking


def write_guide(
    path: Path,
    memory_entries: list[str] | None = None,
    profile_entries: list[str] | None = None,
    body: str = "# Guide\n\n",
) -> Path:
    """Seed a workspace guide with bounded-memory regions for a test."""
    path.write_text(body, encoding="utf-8")
    mt.ensure_regions(path)
    if memory_entries:
        mt.write_region(path, "memory", memory_entries)
    if profile_entries:
        mt.write_region(path, "profile", profile_entries)
    return path


_SAMPLE_INSIGHTS = """
## Errors
- Bash failed: command not found -> installed via brew. [idx=4]

## User corrections
- User said: "no em dashes" -> assistant rewrote with commas. Durable rule: Avoid em dashes; use commas instead. [idx=12]

## New entities
- person: Manager Example - the user's direct manager. [idx=2]
- person: User Example - the user, product lead. [idx=5]
- project: Smart Label Capture - OCR product the user owns. [idx=7]

## Decisions
- Chose OpenRouter over Anthropic for one-shot insights because cheaper. [idx=18]

## Dead ends
- Tried `gws auth login --profile work`; blocked by missing scopes. [idx=22]
"""


def test_append_proposals_skips_empty(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert mp.append_proposals([], vault) is None
    assert not (vault / "personal" / "Workspace" / "Memory-Proposals.md").exists()


def test_pending_duplicate_proposal_keeps_base_identity_after_first_is_removed(tmp_path: Path) -> None:
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir()
    queue.write_text(
        "- [memory] Same fact — sources: one\n"
        "- [memory] Same fact — sources: one\n",
        encoding="utf-8",
    )

    class Config:
        def workspace_names(self):
            return ["personal"]

        def workspace_vault_root(self, _workspace):
            return tmp_path

    ids = proposal_tracking.pending_proposal_ids(Config())
    base = next(item for item in ids if ":" not in item)
    assert base in ids


def test_queue_bullet_round_trips_payload() -> None:
    proposal = mp.MemoryProposal(
        target="people", text="Alba - a collaborator.",
        source_section="New entities", payload="Alba",
    )
    line = proposal.as_bullet()
    assert line.startswith("- [people Alba] Alba - a collaborator.")
    from ciao.proposal_kinds import parse_bullet
    bullet = parse_bullet(line)
    assert bullet is not None
    assert bullet.kind == "people"
    assert bullet.target == "Alba"


# --- CLI: memory-proposal-add -----------------------------------------------


def _add_args(tmp_path: Path, **overrides):
    import argparse

    defaults = {
        "workspace": tmp_path,
        "vault_root": tmp_path / "memory-vault",
        "text": "A workstream invisible to the vault scores zero everywhere.",
        "kind": "memory",
        "payload": "",
        "source": "chat-824cd4ec",
        "json": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_add_command_queues_a_reviewable_fact(
    tmp_path: Path,
    capsys,
) -> None:
    """A curator-discovered fact lands in the machine queue, not just prose.

    Archive-time routing only sees chats that grew a session-insights
    section; facts the nightly curation run finds by reading a transcript in
    full must get the same review path (list, promote, dismiss).
    """
    from ciao.cli import _memory_proposal_add_command

    exit_code = _memory_proposal_add_command(_add_args(tmp_path))

    assert exit_code == 0
    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    rows = mp.list_proposals(queue)
    assert len(rows) == 1
    assert rows[0]["kind"] == "memory"
    assert "scores zero everywhere" in rows[0]["text"]
    assert rows[0]["source"] == "chat-824cd4ec"
    out = capsys.readouterr().out
    assert "Queued [memory] proposal" in out


def test_add_command_dedupes_identical_text(tmp_path: Path) -> None:
    """Re-filing what an earlier run queued is a no-op, not a duplicate.

    The nightly run re-reads the same transcripts until the user promotes or
    dismisses; a queue that grows one copy per night stops being readable.
    """
    from ciao.cli import _memory_proposal_add_command

    first = _memory_proposal_add_command(_add_args(tmp_path))
    second = _memory_proposal_add_command(_add_args(tmp_path))

    assert first == second == 0
    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    assert len(mp.list_proposals(queue)) == 1


def test_add_command_rejects_an_unknown_kind(tmp_path: Path) -> None:
    """A kind outside DESTINATIONS is refused, not written through.

    ``MemoryProposal.as_bullet`` is deliberately total for archive batches —
    one odd proposal must not fail a whole archive — but the CLI is an agent
    command where a typo'd kind would queue an unroutable bullet.
    """
    from ciao.cli import _memory_proposal_add_command

    exit_code = _memory_proposal_add_command(_add_args(tmp_path, kind="memories"))

    assert exit_code == 2
    assert not (
        tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    ).exists()


def test_add_command_requires_payload_for_addressed_kinds(tmp_path: Path) -> None:
    """`people`/`project` without a payload would queue an unroutable bullet.

    The PWA accept handlers refuse such rows ("the bullet names no person" /
    "the bullet names no project doc"), so a successful CLI invocation must
    not be able to create one.
    """
    from ciao.cli import _memory_proposal_add_command

    for kind in ("people", "project"):
        exit_code = _memory_proposal_add_command(
            _add_args(tmp_path, kind=kind, payload="")
        )

        assert exit_code == 2, kind
        assert not (
            tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
        ).exists()

    # Naming the target unblocks both kinds.
    assert (
        _memory_proposal_add_command(
            _add_args(tmp_path, kind="people", payload="Mo Salah")
        )
        == 0
    )


def test_add_command_json_serializes_an_explicit_workspace(
    tmp_path: Path,
    capsys,
) -> None:
    """--json with an explicit --workspace emits parseable JSON.

    argparse supplies the workspace as a Path, which json.dump refuses to
    serialize; the command reports the resolved root instead so a caller
    never sees a TypeError after the queue was already modified.
    """
    import json as json_module

    from ciao.cli import _memory_proposal_add_command

    exit_code = _memory_proposal_add_command(
        _add_args(tmp_path, json=True, workspace=tmp_path)
    )

    assert exit_code == 0
    payload = json_module.loads(capsys.readouterr().out)
    assert payload["queued"] is True
    assert payload["workspace"] == str(tmp_path)


def test_add_command_queues_verbatim_text_from_file(tmp_path: Path) -> None:
    """A fact filed via --text-file lands byte-for-byte, shell hazards and all.

    The curator reads transcripts full of `$(...)`, backticks, `$VARS`, and
    quotes. Passing such text as a shell argument executes or mangles it;
    the file path is the non-interpolated input route, so what reaches the
    queue must equal what the agent wrote.
    """
    from ciao.cli import _memory_proposal_add_command

    hazardous = (
        'Deploy tag is "$(cat VERSION)" on `runner-2`; $CI_ENV says "staging"'
    )
    fact_file = tmp_path / "fact.txt"
    fact_file.write_text(hazardous + "\n", encoding="utf-8")

    exit_code = _memory_proposal_add_command(
        _add_args(tmp_path, text="", text_file=str(fact_file))
    )

    assert exit_code == 0
    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    rows = mp.list_proposals(queue)
    assert len(rows) == 1
    assert rows[0]["text"] == hazardous


def test_add_command_rejects_ambiguous_fact_inputs(tmp_path: Path) -> None:
    """Both or neither of text/--text-file is a usage error, not a guess."""
    from ciao.cli import _memory_proposal_add_command

    fact_file = tmp_path / "fact.txt"
    fact_file.write_text("A fact.", encoding="utf-8")

    both = _memory_proposal_add_command(
        _add_args(tmp_path, text="A fact.", text_file=str(fact_file))
    )
    neither = _memory_proposal_add_command(
        _add_args(tmp_path, text="", text_file="")
    )
    missing = _memory_proposal_add_command(
        _add_args(tmp_path, text="", text_file=str(tmp_path / "absent.txt"))
    )

    assert both == neither == missing == 2
    assert not (tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md").exists()


def test_add_command_flattens_a_multiline_fact_file(tmp_path: Path) -> None:
    """Embedded newlines become one single-line bullet that dedupes cleanly.

    The queue is line-oriented Markdown: a raw multiline fact would parse as
    a truncated first line, strand the continuation lines (or spawn phantom
    bullets), and never match itself on re-filing because no parsed bullet
    carries the full original text.
    """
    from ciao.cli import _memory_proposal_add_command

    multiline = "Deploy froze on Tuesday.\n- [memory] injected-looking line\n"
    fact_file = tmp_path / "fact.txt"
    fact_file.write_text(multiline, encoding="utf-8")

    exit_code = _memory_proposal_add_command(
        _add_args(tmp_path, text="", text_file=str(fact_file))
    )

    assert exit_code == 0
    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    rows = mp.list_proposals(queue)
    assert len(rows) == 1
    assert rows[0]["text"] == "Deploy froze on Tuesday. - [memory] injected-looking line"

    # The flattened form is what dedupe sees, so a re-file of the same file
    # is the documented no-op rather than a second copy.
    again = _memory_proposal_add_command(
        _add_args(tmp_path, text="", text_file=str(fact_file))
    )
    assert again == 0
    assert len(mp.list_proposals(queue)) == 1


def test_add_command_flattens_source_and_payload(tmp_path: Path) -> None:
    """Provenance fields cannot split a bullet or spawn a phantom proposal.

    ``--source`` carries a chat identifier and ``--payload`` a person name, but
    both are free text on the way in. A newline in either splits the written
    line, so the queue parses a truncated first bullet plus whatever the
    continuation looks like — and the real text never appears as one parsed
    bullet, so re-filing it dodges dedupe.
    """
    from ciao.cli import _memory_proposal_add_command

    exit_code = _memory_proposal_add_command(
        _add_args(
            tmp_path,
            text="Release trains freeze on Tuesdays.",
            kind="people",
            payload="Mo Salah\n- [memory] phantom payload row",
            source="chat-1\n- [memory] phantom source row",
        )
    )

    assert exit_code == 0
    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    rows = mp.list_proposals(queue)
    # One bullet in, one bullet out: no truncation, no injected extra rows.
    assert len(rows) == 1
    assert rows[0]["text"] == "Release trains freeze on Tuesdays."
    assert rows[0]["kind"] == "people"

    # The single written line still parses, so dedupe recognises a re-file.
    again = _memory_proposal_add_command(
        _add_args(
            tmp_path,
            text="Release trains freeze on Tuesdays.",
            kind="people",
            payload="Mo Salah",
            source="chat-1",
        )
    )
    assert again == 0
    assert len(mp.list_proposals(queue)) == 1


def test_bullet_keeps_its_delimiters_out_of_free_text_fields() -> None:
    """A `]` in the payload or a `)` in the source must not end its own slot.

    ``as_bullet`` owns the queue's line grammar: the destination head closes on
    the first `]` and the provenance tail on the first `)`, so an unescaped one
    inside either field makes the whole bullet unparseable — the row then
    exists in the file but is invisible to the review UI and to dedupe.
    """
    proposal = mp.MemoryProposal(
        target="people",
        text="Prefers async standups.",
        source_section="chat-1 (imported)",
        payload="Alex] Rivera",
    )

    bullet = proposal.as_bullet()

    assert bullet.count("]") == 1
    assert bullet.count(")") == 1
    assert mp._existing_proposal_texts(bullet) == {"Prefers async standups."}


def test_dismissed_facts_are_not_refiled_by_the_next_run(tmp_path: Path) -> None:
    """A dismissal outlives its row: dedupe consults the decision history.

    Removing the bullet is not enough — the nightly run re-reads the same
    transcript while it counts as recent and would re-file identical text,
    resurrecting a decision the user already made.
    """
    from ciao.cli import _memory_proposal_add_command

    text = "The release train freezes every second Tuesday."
    assert _memory_proposal_add_command(_add_args(tmp_path, text=text)) == 0

    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    removed = mp.remove_proposal_by_substring(queue, "release train")
    assert removed is not None
    kind, removed_text = removed
    mp.record_dismissal(queue, text=removed_text, kind=kind)

    # The next nightly pass re-files what the transcript still supports.
    exit_code = _memory_proposal_add_command(_add_args(tmp_path, text=text))

    assert exit_code == 0
    assert mp.list_proposals(queue) == []
    log = queue.with_suffix(".dismissed.jsonl")
    assert "release train" in log.read_text(encoding="utf-8")


def test_add_command_targets_the_active_workspace_vault(tmp_path: Path) -> None:
    """A scheduled run files into the logical workspace's queue, not the shared vault.

    Scheduled chats export ``CIAO_VAULT_ROOT`` at the install-wide shared
    vault while re-rooting is still pending; appending to that raw value
    strands the fact in a stray file the review UI never reads. The active
    workspace name must win and resolve through the registry — the same
    authority the PWA's ``workspace_vault_root`` reads with.
    """
    import argparse
    import json as json_module

    from ciao.cli import _memory_proposal_add_command

    root = tmp_path / "install"
    runtime = root / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "workspaces.json").write_text(
        json_module.dumps([
            {"name": "personal", "vault_root": "memory-vault/personal"},
            {"name": "client", "vault_root": "memory-vault/client"},
        ]),
        encoding="utf-8",
    )
    shared = tmp_path / "shared-vault"
    shared.mkdir()
    monkeypatch_env = {
        "CIAO_WORKSPACE": str(root),
        "CIAO_ACTIVE_WORKSPACE": "client",
        "CIAO_VAULT_ROOT": str(shared),
    }

    args = argparse.Namespace(
        workspace=None,
        vault_root=None,
        text="A client-scoped fact.",
        kind="memory",
        payload="",
        source="chat-abc",
        json=False,
    )
    with unittest.mock.patch.dict(os.environ, monkeypatch_env):
        exit_code = _memory_proposal_add_command(args)

    assert exit_code == 0
    queue = root / "memory-vault" / "client" / "Workspace" / "Memory-Proposals.md"
    assert queue.is_file()
    assert not (shared / "Workspace" / "Memory-Proposals.md").exists()

    # An explicit --workspace is a manual invocation: it keeps winning over
    # the ambient active-workspace name. With no explicit vault root and no
    # exported one either, the queue lands under that folder's own default.
    explicit_dir = tmp_path / "manual"
    manual_env = {k: v for k, v in monkeypatch_env.items() if k != "CIAO_VAULT_ROOT"}
    explicit_args = argparse.Namespace(
        workspace=explicit_dir,
        vault_root=None,
        text="A manual fact.",
        kind="memory",
        payload="",
        source="chat-abc",
        json=False,
    )
    with unittest.mock.patch.dict(os.environ, manual_env):
        assert _memory_proposal_add_command(explicit_args) == 0
    assert (explicit_dir / "memory-vault" / "Workspace" / "Memory-Proposals.md").is_file()


def test_removing_last_bullet_sweeps_its_batch_header(tmp_path: Path) -> None:
    """Dismissing a batch's only bullet removes the batch header too.

    Before the sweep, every fully-dismissed batch left its ``## <timestamp>``
    header behind; a real queue accumulated 29 empty batches out of 30.
    """
    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    proposals = [
        mp.MemoryProposal(target="review", text="lone fact one", source_section="Decisions"),
    ]
    out = mp.append_proposals(proposals, vault, source_path=None)
    assert out is not None
    out2 = mp.append_proposals(
        [mp.MemoryProposal(target="review", text="second batch fact", source_section="Decisions")],
        vault,
        source_path=None,
    )
    assert out2 is not None

    removed = mp.remove_proposal_by_substring(out, "lone fact one")
    assert removed is not None

    text = out.read_text(encoding="utf-8")
    # One batch header remains (the second batch still has its bullet).
    assert len(re.findall(r"^## \d{4}-", text, re.MULTILINE)) == 1
    assert "second batch fact" in text


def test_sweep_keeps_batches_with_bullets_or_prose(tmp_path: Path) -> None:
    """The sweep only removes headers over all-blank sections.

    A timestamped section that carries prose (agents have appended notes into
    the queue) was written by someone else and is not the sweep's to delete.
    """
    lines = [
        "# Memory Proposals",
        "",
        "## 2026-08-01T00:00:00+00:00 — from `a.md`",
        "",
        "- [review] pending fact  _(from: Decisions)_",
        "",
        "## 2026-08-02T00:00:00+00:00 — from `b.md`",
        "",
        "",
        "## 2026-08-03T00:00:00+00:00 — from `c.md`",
        "",
        "Some hand-written note under a timestamp.",
        "",
    ]
    swept = mp._sweep_empty_batches(lines)
    text = "\n".join(swept)
    assert "2026-08-01" in text  # has a bullet
    assert "2026-08-02" not in text  # empty: swept
    assert "2026-08-03" in text  # has prose: kept
    assert "Some hand-written note" in text


def test_record_dismissal_ignores_empty_and_junk_lines(tmp_path: Path) -> None:
    """Blank decisions record nothing; unreadable sidecar lines never crash."""
    from ciao.cli import _memory_proposal_add_command
    from ciao.memory_proposals import dismissed_log_path

    assert _memory_proposal_add_command(_add_args(tmp_path)) == 0
    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"

    assert mp.record_dismissal(queue, text="   ") is False

    log = dismissed_log_path(queue)
    log.write_text("{not json}\n\n", encoding="utf-8")
    assert mp._dismissed_texts(queue) == set()
    # A well-formed entry alongside junk still contributes its text.
    log.write_text(
        '{not json}\n{"text": "kept fact", "kind": "memory"}\n',
        encoding="utf-8",
    )
    assert mp._dismissed_texts(queue) == {"kept fact"}
    # The pre-rename .log sidecar is read too, so history written before the
    # extension change keeps protecting across the upgrade.
    legacy = queue.with_suffix(".dismissed.log")
    legacy.write_text('{"text": "legacy fact", "kind": "memory"}\n', encoding="utf-8")
    assert mp._dismissed_texts(queue) == {"kept fact", "legacy fact"}


# ---- Write-time reconcile (ADD / UPDATE / COVERED) -----------------------


def test_parse_reconcile_reply_shapes() -> None:
    good = '[{"action": "add"}, {"action": "update", "index": 2, "text": "merged"}, {"action": "covered"}]'
    rows = mp._parse_reconcile_reply(good, 3)
    assert rows == [
        {"action": "add"},
        {"action": "update", "index": 2, "text": "merged"},
        {"action": "covered"},
    ]
    # Fenced replies are unwrapped.
    assert mp._parse_reconcile_reply(f"```json\n{good}\n```", 3) is not None
    # Wrong length or non-array: discarded whole.
    assert mp._parse_reconcile_reply('[{"action": "add"}]', 2) is None
    assert mp._parse_reconcile_reply('{"action": "add"}', 1) is None
    assert mp._parse_reconcile_reply("not json", 1) is None
    # Per-row junk defers: the candidate reached a model call only because the
    # region already holds entries, so a row we cannot read is no licence to
    # append beside whatever this fact might supersede.
    rows = mp._parse_reconcile_reply(
        '[{"action": "update"}, {"action": "delete"}, 42]', 3
    )
    assert rows is not None
    assert [row["action"] for row in rows] == ["defer", "defer", "defer"]
    assert all(row.get("reason") for row in rows)


# ---- Typed decision statuses ----------------------------------------------


def test_decision_and_outcome_vocabularies_are_closed() -> None:
    """Every state the reconcile path can be in is named, not implied.

    The defect this replaced was a bare ``None`` meaning both "no reconcile was
    needed" and "the reconcile could not decide" — opposite instructions that
    read identically downstream. Pinning the vocabularies here makes adding a
    state without handling it a visible change rather than a silent one.
    """
    from typing import get_args

    assert set(get_args(mp.ReconcileAction)) == {"add", "covered", "update", "defer"}
    assert set(get_args(mp.PromotionOutcome)) == {
        "written",
        "duplicate",
        "conflict",
        "failed",
        "unshaped",
        "deferred",
    }


# ---- Competing facts: rate, location, preference, negation, expiry --------


def test_reconcile_region_fact_skips_the_model_for_deterministic_cases(
    tmp_path: Path, monkeypatch
) -> None:
    """An empty region and an exact duplicate need no model call.

    They are the two outcomes a model cannot improve on, and paying a timeout
    for them is what makes a conservative fallback expensive.
    """

    async def never(prompt: str, **kwargs: object) -> str:  # pragma: no cover
        raise AssertionError("the deterministic paths must not call a model")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", never)

    empty = write_guide(tmp_path / "empty.md")
    assert (
        asyncio.run(
            mp.reconcile_region_fact(empty, "memory", "Deploys run on Thursdays.",
                                     model="sonnet")
        )
        is None
    )

    seeded = write_guide(
        tmp_path / "seeded.md",
        memory_entries=["Deploys run on Thursdays. [2026-01-01]"],
    )
    assert (
        asyncio.run(
            mp.reconcile_region_fact(seeded, "memory", "Deploys run on Thursdays.",
                                     model="sonnet")
        )
        is None
    )


def test_reconcile_region_fact_defers_when_the_retry_also_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """A retry that cannot decide is not a licence to append either."""
    guide = write_guide(
        tmp_path / "CLAUDE.md", memory_entries=["Office is in Zurich. [2026-01-01]"]
    )

    async def timing_out(prompt: str, **kwargs: object) -> str:
        raise TimeoutError("still down")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", timing_out)

    decision = asyncio.run(
        mp.reconcile_region_fact(guide, "memory", "Office is in Berlin.",
                                 model="sonnet")
    )
    assert decision is not None
    assert decision["action"] == "defer"
    assert decision["reason"]
    assert decision["competing"] == ["Office is in Zurich. [2026-01-01]"]

    outcome, _promotable = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Office is in Berlin.",
        vault_root=tmp_path,
        decision=decision,
    )
    assert outcome == "deferred"
    entries, _diags = mt.read_region(guide, "memory")
    assert entries == ["Office is in Zurich. [2026-01-01]"]


# ---- Structured learnings -------------------------------------------------


def test_append_learning_writes_structured_entry(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert mp.append_learning(
        vault, "Airtable sort param returns 400; filter by field ID.", source="chat-a1"
    )
    text = (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if l.startswith("- ["))
    match = mp._LEARNING_LINE_RE.match(line)
    assert match is not None
    assert match.group("count") == "1"
    assert match.group("first") == match.group("last")
    assert match.group("sources") == "chat-a1"


def test_append_learning_recurrence_increments_instead_of_duplicating(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    fact = "Airtable sort param returns 400; filter by field ID."
    assert mp.append_learning(vault, fact, source="chat-a1")
    assert mp.append_learning(vault, fact, source="chat-b2")
    # Whitespace/case variations still count as the same learning.
    assert mp.append_learning(vault, fact.upper(), source="chat-b2")

    text = (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.startswith("- [")]
    assert len(lines) == 1
    match = mp._LEARNING_LINE_RE.match(lines[0])
    assert match is not None
    assert match.group("count") == "3"
    assert match.group("sources") == "chat-a1, chat-b2"  # dedup'd source


def test_append_learning_leaves_legacy_bullets_alone(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = vault / "Workspace" / "Learnings.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# Learnings\n\n## Active\n- legacy plain learning bullet\n",
        encoding="utf-8",
    )
    assert mp.append_learning(vault, "legacy plain learning bullet")
    text = path.read_text(encoding="utf-8")
    assert text.count("legacy plain learning bullet") == 1  # exact dup short-circuit

    assert mp.append_learning(vault, "A brand new structured learning.", source="chat-x")
    text = path.read_text(encoding="utf-8")
    assert "- legacy plain learning bullet" in text
    assert "(x1) A brand new structured learning." in text


# ── accept_region_fact: the UI accept path's guards ───────────────────────
#
# Accepting a queued fact used to call `update_region(action="add")` directly,
# which skipped every guard the archive-time path applies. These pin what a
# click now gets — and, just as importantly, what it must NOT have acquired.


def _guide_with(tmp_path: Path, region: str, entries: list[str]) -> Path:
    from ciao.memory_tool import ensure_regions, write_region

    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Guide\n", encoding="utf-8")
    ensure_regions(guide)
    if entries:
        write_region(guide, region, entries)
    return guide


def _entries(guide: Path, region: str) -> list[str]:
    from ciao.memory_audit import strip_learned_stamp
    from ciao.memory_tool import read_region

    entries, _diags = read_region(guide, region)
    return [strip_learned_stamp(e) for e in entries]


def test_accept_refuses_event_shaped_text(tmp_path):
    """The guard that matters most: always-loaded context must hold rules.

    An event-shaped bullet used to be written verbatim, which is exactly the
    rot the shipped memory audit flags.
    """
    guide = _guide_with(tmp_path, "memory", [])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="The user asked me to check the logs and I found the bug.",
        vault_root=tmp_path,
    )
    assert outcome == "unshaped"
    assert _entries(guide, "memory") == []


def test_accept_writes_a_state_shaped_fact_with_a_stamp(tmp_path):
    guide = _guide_with(tmp_path, "memory", [])
    outcome, promotable = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers tabs over spaces.",
        vault_root=tmp_path,
    )
    assert outcome == "written"
    assert promotable == "Prefers tabs over spaces."
    assert _entries(guide, "memory") == ["Prefers tabs over spaces."]
    # The stamp the aging audit reads. Without it a UI-accepted fact was
    # invisible to re-verification forever.
    from ciao.memory_tool import read_region

    raw, _ = read_region(guide, "memory")
    assert re.search(r"\[\d{4}-\d{2}-\d{2}\]$", raw[0])


def test_accept_drops_a_stamp_stripped_duplicate(tmp_path):
    """The same fact accepted twice is one entry, whatever day it was."""
    guide = _guide_with(tmp_path, "memory", ["Prefers tabs over spaces. [2020-01-01]"])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers tabs over spaces.",
        vault_root=tmp_path,
    )
    assert outcome == "duplicate"
    assert _entries(guide, "memory") == ["Prefers tabs over spaces."]


def test_accept_still_writes_past_the_advisory_cap(tmp_path):
    """The cap is ADVISORY and must stay that way.

    `update_region` says so outright: enforcing it "made the accept button dead
    for 67 of 130 queued proposals" on a real vault, and
    tests/test_memory_tool.py pins that. Routing accept through the guarded
    write must not quietly reinstate the wall.
    """
    guide = _guide_with(tmp_path, "memory", ["x" * 5000])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers tabs over spaces.",
        vault_root=tmp_path,
    )
    assert outcome == "written"
    assert "Prefers tabs over spaces." in _entries(guide, "memory")


def test_accept_makes_no_model_call(tmp_path, monkeypatch):
    """A click must not block on a provider.

    Reconciliation would be welcome here, but one `run_oneshot` per row is a
    120s timeout each and the batch endpoint accepts rows sequentially inside a
    single request — 30 rows would be up to an hour. It stays out of this path.
    """
    async def must_not_run(prompt, **kwargs):
        raise AssertionError("the accept path must not call a model")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", must_not_run)
    guide = _guide_with(tmp_path, "memory", ["Prefers tabs over spaces. [2020-01-01]"])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers spaces over tabs.",
        vault_root=tmp_path,
    )
    assert outcome == "written"


def test_accept_applies_a_reconcile_decision_it_is_handed(tmp_path):
    """The seam a future reconcile pass writes through."""
    guide = _guide_with(tmp_path, "memory", ["Prefers tabs over spaces. [2020-01-01]"])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers spaces over tabs.",
        vault_root=tmp_path,
        decision={
            "action": "update",
            "index": 1,
            "text": "Prefers spaces over tabs.",
            "old": "Prefers tabs over spaces. [2020-01-01]",
        },
    )
    assert outcome == "written"
    assert _entries(guide, "memory") == ["Prefers spaces over tabs."]


def test_a_covered_verdict_with_no_vault_appends_rather_than_dropping(tmp_path):
    """Nothing is dropped without a trace — the standing contract.

    `covered` is a model verdict, not a provable match. With no vault to log it
    to, honour the contract instead of the verdict: a duplicate is visible and
    removable, a silently dropped fact is neither.
    """
    guide = _guide_with(tmp_path, "memory", ["Prefers tabs over spaces. [2020-01-01]"])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers spaces over tabs.",
        vault_root=None,
        decision={"action": "covered"},
    )
    assert outcome == "written"
    assert "Prefers spaces over tabs." in _entries(guide, "memory")


# -- Decision recording is append-only, except where it must be idempotent ----


def test_record_promotion_once_still_records_a_different_outcome(tmp_path: Path) -> None:
    """A real promotion of a fact previously logged as "already known" is news."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)

    mp.record_promotion(
        queue, text="A fact.", kind="memory", via="auto", outcome="suppressed", once=True,
    )
    assert mp.record_promotion(
        queue, text="A fact.", kind="memory", via="pwa", once=True,
    ) is True

    assert [r["outcome"] for r in mp.read_decisions(queue)] == ["suppressed", ""]


def test_record_promotion_defaults_to_appending(tmp_path: Path) -> None:
    """Operator decisions are fresh events every time; only ``once`` dedupes."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)

    for _ in range(2):
        assert mp.record_promotion(queue, text="A fact.", kind="memory", via="pwa") is True

    assert len(mp.read_decisions(queue)) == 2


def test_read_decisions_numbers_rows_for_id_disambiguation(tmp_path: Path) -> None:
    """Two byte-identical rows must still get distinct history ids."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    log = mp.dismissed_log_path(queue)
    log.write_text('{"kind": "memory", "text": "Same."}\n' * 2, encoding="utf-8")

    rows = mp.read_decisions(queue)

    assert [r["seq"] for r in rows] == [0, 1]
    assert mp.history_row_id(rows[0], "personal") != mp.history_row_id(rows[1], "personal")
    # And the same row in two workspaces is two ids.
    assert mp.history_row_id(rows[0], "personal") != mp.history_row_id(rows[0], "work")


def test_sidecar_reads_are_cached_but_invalidate_on_append(tmp_path: Path) -> None:
    """Archiving asks "already decided?" once per proposal.

    Each call used to re-read and re-parse the whole sidecar, so one archive
    pass over a long-lived ledger was O(proposals x history). The cache is
    keyed on stat identity, so its correctness rests on noticing an append.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    assert mp.record_dismissal(queue, text="First.", kind="memory", via="pwa") is True
    sidecar = str(mp.dismissed_log_path(queue))

    reads: list[str] = []
    real_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        reads.append(str(self))
        return real_read_text(self, *args, **kwargs)

    with unittest.mock.patch.object(Path, "read_text", counting_read_text):
        # Repeated questions about an unchanged sidecar read it once.
        for _ in range(5):
            mp._has_decision(queue, key="dismissed_at", text="First.", outcome="")
        assert reads.count(sidecar) == 1, reads

    # An append must be seen: a stale cache would silently re-file or
    # re-suppress proposals.
    assert mp.record_dismissal(queue, text="Second.", kind="memory", via="pwa") is True
    assert mp._has_decision(queue, key="dismissed_at", text="Second.", outcome="") is True
    assert {r["text"] for r in mp.read_decisions(queue)} == {"First.", "Second."}


def test_legacy_row_ids_survive_a_new_decision(tmp_path: Path) -> None:
    """A decision in the current sidecar must not renumber the legacy one.

    ``seq`` used to count across the concatenation of ``.dismissed.jsonl`` then
    ``.dismissed.log``, so every append to the former shifted every legacy
    row's ``seq`` — and with it the ``history_row_id`` the History list keys
    its ``<li>`` on. That is the exact instability ``seq`` exists to prevent.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.with_suffix(".dismissed.log").write_text(
        '{"kind": "memory", "text": "Legacy fact."}\n', encoding="utf-8"
    )

    legacy_before = next(r for r in mp.read_decisions(queue) if r["text"] == "Legacy fact.")
    id_before = mp.history_row_id(legacy_before, "personal")

    assert mp.record_promotion(queue, text="A new fact.", kind="memory", via="pwa") is True

    legacy_after = next(r for r in mp.read_decisions(queue) if r["text"] == "Legacy fact.")
    assert mp.history_row_id(legacy_after, "personal") == id_before


def test_rows_in_different_sidecars_get_distinct_ids(tmp_path: Path) -> None:
    """Identical text at position 0 of each sidecar must not collide."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.with_suffix(".dismissed.jsonl").write_text(
        '{"kind": "memory", "text": "Same."}\n', encoding="utf-8"
    )
    queue.with_suffix(".dismissed.log").write_text(
        '{"kind": "memory", "text": "Same."}\n', encoding="utf-8"
    )

    rows = mp.read_decisions(queue)

    assert [r["seq"] for r in rows] == [0, 0]
    assert len({mp.history_row_id(r, "personal") for r in rows}) == 2


def test_suppressed_row_shows_in_history_without_blocking_a_refile(tmp_path: Path) -> None:
    """The archive-time "already applied" row is ledger-only.

    It is written under ``promoted_at`` so the History tab can show it, but
    nobody promoted the fact through the queue. Letting the dedupe readers see
    it made ``was_promoted`` true and ``append_proposals`` refuse the fact
    forever, so a reverted in-session edit could never be re-queued.
    """
    vault = tmp_path / "vault"
    queue = vault / mp._PROPOSALS_RELATIVE
    queue.parent.mkdir(parents=True)
    queue.write_text(mp._STUB_HEADER, encoding="utf-8")

    assert (
        mp.record_promotion(
            queue,
            text="Already applied fact.",
            kind="memory",
            via="auto",
            outcome="suppressed",
            once=True,
            history_only=True,
        )
        is True
    )

    # Visible in the ledger the History tab reads...
    rows = mp.read_decisions(queue)
    assert [(r["action"], r["outcome"]) for r in rows] == [("accepted", "suppressed")]

    # ...but invisible to every dedupe reader.
    assert mp.was_promoted(vault, "Already applied fact.") is False
    assert mp.was_dismissed(vault, "Already applied fact.") is False
    assert "Already applied fact." not in mp._dismissed_texts(queue)
    assert "Already applied fact." not in mp._promoted_texts(queue)

    # And `once=True` still suppresses the duplicate row on a second pass.
    assert (
        mp.record_promotion(
            queue,
            text="Already applied fact.",
            kind="memory",
            via="auto",
            outcome="suppressed",
            once=True,
            history_only=True,
        )
        is False
    )
    assert len(mp.read_decisions(queue)) == 1


def test_a_real_promotion_still_dedupes(tmp_path: Path) -> None:
    """The ledger-only flag must not weaken the normal promotion record."""
    vault = tmp_path / "vault"
    queue = vault / mp._PROPOSALS_RELATIVE
    queue.parent.mkdir(parents=True)
    queue.write_text(mp._STUB_HEADER, encoding="utf-8")

    assert mp.record_promotion(queue, text="Operator accepted.", kind="memory", via="pwa") is True

    assert mp.was_promoted(vault, "Operator accepted.") is True
    assert "Operator accepted." in mp._promoted_texts(queue)


def test_the_learned_date_still_reads_the_most_recent_stamp() -> None:
    """Stripping all stamps must not change how aging measures one.

    `find_aging_state` searches for a single trailing stamp to decide how old a
    fact is; the last one is the most recent promotion, which is what it should
    measure from.
    """
    from ciao import memory_audit as ma

    match = ma._LEARNED_STAMP_RE.search("fact [2026-01-01] [2026-09-02]")
    assert match is not None
    assert match.group(1) == "2026-09-02"


# ---- Source-evidence gate -------------------------------------------------
#
# Region promotion used to check a fact's *shape* only. These cover the second
# question: does any turn the user actually typed support it? Unsupported means
# queued for review — never written to always-loaded context, never dropped.


_EVIDENCE_TRANSCRIPT = "\n".join([
    json.dumps({
        "idx": 1, "type": "user",
        "content": [{"type": "text", "text": "always deploy on Thursdays"}],
    }),
    json.dumps({
        "idx": 2, "type": "assistant",
        "content": [{"type": "text", "text": "You could deploy on Thursdays."}],
    }),
    json.dumps({
        "idx": 3, "type": "user", "unattended": True,
        "content": [{"type": "text", "text": "[scheduled] run the nightly audit"}],
    }),
])
