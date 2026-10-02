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
from datetime import date
from pathlib import Path

import pytest

from ciao import memory_proposals as mp
from ciao import memory_tool as mt
from ciao import proposal_tracking
from ciao.learning_records import SECTION_ACTIVE, LearningRecord, parse_learnings


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


# ---- Entity notes: the typed, registry-routed writer ------------------------
#
# `write_people_note` wrote `tags: [person]` and no `type:` at all, into a
# hardcoded `People/`, so an accepted `[people]` proposal produced a note the
# linter flags and a category the owner added had no writer for. The writer is
# now the category's: the folder and the `type:` both come from the registry.
#
# The two roots are named apart, because they are apart on a pre-re-rooting
# install: `vault_root` is the notes root the note is written under,
# `registry_root` the agent vault root holding `entity-types.yaml`. Most of
# these tests point both at one directory — the re-rooted shape — and the one
# that differs says so.


def _entity_vault(tmp_path: Path, overrides: str = "") -> Path:
    """A vault whose category registry carries *overrides* (YAML, if any)."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    if overrides:
        (vault / "entity-types.yaml").write_text(overrides, encoding="utf-8")
    return vault


def test_entity_note_carries_type_updated_and_tags(tmp_path: Path) -> None:
    vault = _entity_vault(tmp_path)

    written = mp.write_entity_note(
        vault, mp.PERSON_TYPE_ID, "Mo", "Leads the pilot.", registry_root=vault
    )
    assert written == "written"

    note = vault / "People" / "Mo.md"
    assert note.is_file()
    text = note.read_text(encoding="utf-8")
    assert text.startswith("---\ntype: person\n")
    assert f"updated: {date.today().isoformat()}\n" in text
    assert "tags: [person]" in text
    assert text.endswith("---\n# Mo\n\nLeads the pilot.\n")


def test_entity_note_lands_in_a_custom_category_folder(tmp_path: Path) -> None:
    """A category the owner added is written like any other, in its own folder."""
    vault = _entity_vault(
        tmp_path,
        overrides=(
            "- id: customer\n"
            "  label: Customer\n"
            "  kind: entity\n"
            "  folder: Customers\n"
            "  enabled: true\n"
        ),
    )

    written = mp.write_entity_note(
        vault, "customer", "Acme", "Signs off in March.", registry_root=vault
    )
    assert written == "written"

    note = vault / "Customers" / "Acme.md"
    assert note.is_file(), "a custom category's folder, not a hardcoded People/"
    assert "type: customer" in note.read_text(encoding="utf-8")


def test_entity_note_reads_its_category_at_the_registry_root(tmp_path: Path) -> None:
    """The two roots are named separately, so an edit at one is honoured.

    The category lives in the agent vault root — `entity-types.yaml` beside
    `VOCABULARY.md`, the file the category editor writes — while the note lands
    in a workspace's notes root, where that person's other notes already are.
    On an install that has not re-rooted those are different directories, and
    reading the category from the notes root is how an owner's
    `person.folder: Humans` ended up invisible to the accept meant to honour it.
    """
    agent_root = _entity_vault(tmp_path, overrides="- id: person\n  folder: Humans\n")
    notes_root = tmp_path / "memory-vault" / "personal"
    notes_root.mkdir(parents=True)

    written = mp.write_entity_note(
        notes_root,
        mp.PERSON_TYPE_ID,
        "Mo",
        "Leads the pilot.",
        registry_root=agent_root,
    )
    assert written == "written"

    assert (notes_root / "Humans" / "Mo.md").is_file(), notes_root.name
    assert not (notes_root / "People").exists(), "the stock folder, not the owner's"


@pytest.mark.parametrize(
    ("type_id", "overrides", "label"),
    [
        # A category with no folder has nowhere to put a note.
        (
            "ghost",
            "- id: ghost\n  label: Ghost\n  kind: entity\n  folder: \"\"\n  enabled: true\n",
            "a category with no folder",
        ),
        # `note` is a stock category with no folder: same refusal, no config.
        ("note", "", "a stock folderless category"),
        # An id the vault's registry does not hold at all.
        ("nonexistent", "", "an unknown type_id"),
        # Disabled: out of every derived view on purpose, so not writable to.
        (
            "person",
            "- id: person\n  enabled: false\n",
            "a category the owner disabled",
        ),
    ],
)
def test_entity_note_refuses_a_category_it_cannot_write(
    tmp_path: Path, type_id: str, overrides: str, label: str
) -> None:
    vault = _entity_vault(tmp_path, overrides=overrides)

    assert (
        mp.write_entity_note(vault, type_id, "Mo", "A fact.", registry_root=vault)
        == "refused"
    ), label
    assert mp.entity_note_path(vault, type_id, "Mo", registry_root=vault) is None, label
    # A refusal touches nothing: no folder created, no stray note beside the vault.
    expected = ["entity-types.yaml"] if overrides else []
    assert [entry.name for entry in vault.iterdir()] == expected, label


@pytest.mark.parametrize(
    "folder",
    [
        # An absolute folder: the note would land outside the vault entirely, and
        # the accept's `relative_to(vault)` would raise instead of reporting it.
        pytest.param("{escaped}", id="absolute"),
        # A `..` segment, which `validate_entries` does not police: the registry
        # file is editable in the vault, not only through the category editor.
        pytest.param("../escaped", id="parent-relative"),
        pytest.param("People/../../escaped", id="escaping-mid-path"),
    ],
)
def test_entity_note_refuses_a_folder_that_leaves_the_vault(
    tmp_path: Path, folder: str
) -> None:
    """`entry.folder` is a value the owner typed, and it is checked before use.

    Both shapes were acted on before this check: the note was written outside
    the vault, or the caller's `relative_to` raised — a 500 on an accept rather
    than a refused row. A refusal is the same shape as every other one.
    """
    escaped = tmp_path / "escaped"
    spelled = folder.format(escaped=escaped.as_posix())
    vault = _entity_vault(tmp_path, overrides=f"- id: person\n  folder: {spelled}\n")

    outcome = mp.write_entity_note(
        vault, mp.PERSON_TYPE_ID, "Mo", "A fact.", registry_root=vault
    )

    assert outcome == "refused", folder
    path = mp.entity_note_path(vault, mp.PERSON_TYPE_ID, "Mo", registry_root=vault)
    assert path is None, path
    assert not escaped.exists(), "nothing was written outside the vault"
    assert [entry.name for entry in vault.iterdir()] == ["entity-types.yaml"], folder


def test_entity_note_refuses_an_unusable_name(tmp_path: Path) -> None:
    vault = _entity_vault(tmp_path)

    refused = mp.write_entity_note(
        vault, mp.PERSON_TYPE_ID, "///", "A fact.", registry_root=vault
    )
    assert refused == "refused"
    unusable = mp.entity_note_path(
        vault, mp.PERSON_TYPE_ID, "///", registry_root=vault
    )
    assert unusable is None


def test_entity_note_never_overwrites_an_existing_note(tmp_path: Path) -> None:
    """`exists` is not a boolean failure: the caller merges, so nothing is lost."""
    vault = _entity_vault(tmp_path)
    note = vault / "People" / "Mo.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\ntype: person\n---\n# Mo\n\nCurated already.\n", encoding="utf-8")

    outcome = mp.write_entity_note(
        vault, mp.PERSON_TYPE_ID, "Mo", "A new fact.", registry_root=vault
    )
    assert outcome == "exists"
    assert "Curated already." in note.read_text(encoding="utf-8")
    assert "A new fact." not in note.read_text(encoding="utf-8")


def test_render_entity_note_is_exactly_what_the_write_lands(tmp_path: Path) -> None:
    """The preview renders through the same function, so the two cannot disagree."""
    vault = _entity_vault(tmp_path)
    today = "2026-01-02"

    written = mp.render_entity_note("person", "Mo", "F.", today=today)
    assert written == (
        "---\ntype: person\nupdated: 2026-01-02\ntags: [person]\n---\n# Mo\n\nF.\n"
    )

    real = mp.render_entity_note("person", "Mo", "F.")
    written = mp.write_entity_note(vault, "person", "Mo", "F.", registry_root=vault)
    assert written == "written"
    assert (vault / "People" / "Mo.md").read_text(encoding="utf-8") == real


# ---- Structured learnings -------------------------------------------------


def _learnings(vault: Path) -> str:
    return (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")


def _one_active_record(vault: Path) -> LearningRecord:
    """The single record under ``## Active``, failing loudly on any other shape.

    Read back through the canonical model rather than through a private regex
    of this module's: the line the writer emits is a machine record, and a test
    that asserted on it with a hand-rolled pattern would keep passing after the
    writer had stopped writing anything.
    """
    document = parse_learnings(_learnings(vault), workspace=vault.name)
    active = [
        entry.record
        for entry in document.entries
        if entry.section == SECTION_ACTIVE
    ]
    assert len(active) == 1 and active[0] is not None, document.diagnostics
    return active[0]


def test_append_learning_writes_structured_entry(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert mp.append_learning(
        vault, "Airtable sort param returns 400; filter by field ID.", workspace="vault", source="chat-a1"
    )
    record = _one_active_record(vault)
    assert record.text == "Airtable sort param returns 400; filter by field ID."
    assert record.count == 1
    assert record.first_seen == record.last_seen
    # The observation is the evidence, and it is persisted rather than parsed
    # back out of the human-readable citation list.
    assert [o.source for o in record.observations] == ["chat-a1"]


def test_append_learning_recurrence_counts_evidence_not_replays(
    tmp_path: Path,
) -> None:
    """A second sighting counts; re-filing the first one does not.

    The old writer incremented whenever the normalized statement matched, so a
    retry that re-quoted the same episode from the same source — the common case
    behind a duplicate accept — inflated recurrence and drifted a learning
    towards the x3 promotion threshold on no new evidence at all.
    """
    vault = tmp_path / "vault"
    fact = "Airtable sort param returns 400; filter by field ID."
    assert mp.append_learning(vault, fact, workspace="vault", source="chat-a1")
    assert mp.append_learning(vault, fact, workspace="vault", source="chat-b2")
    # Whitespace and case variations are still the same statement, and chat-b2
    # is still the source already counted, so this changes nothing.
    assert mp.append_learning(vault, fact.upper(), workspace="vault", source="chat-b2")

    text = _learnings(vault)
    assert text.count(fact) == 1
    record = _one_active_record(vault)
    assert record.count == 2
    assert [o.source for o in record.observations] == ["chat-a1", "chat-b2"]


def test_append_learning_leaves_legacy_bullets_alone(tmp_path: Path) -> None:
    """An owner-written bullet survives a write that records no evidence.

    A source-less sighting is refused by the model — nothing can tell it apart
    from a new one — so there is nothing to converge the line towards, and
    re-rendering it as a canonical entry would rewrite the owner's prose on the
    strength of a no-op.
    """
    vault = tmp_path / "vault"
    path = vault / "Workspace" / "Learnings.md"
    original = "# Learnings\n\n## Active\n- legacy plain learning bullet\n"
    path.parent.mkdir(parents=True)
    path.write_text(original, encoding="utf-8")
    assert mp.append_learning(vault, "legacy plain learning bullet", workspace="vault")
    assert path.read_text(encoding="utf-8") == original

    assert mp.append_learning(
        vault, "A brand new structured learning.", workspace="vault", source="chat-x"
    )
    text = path.read_text(encoding="utf-8")
    assert "- legacy plain learning bullet" in text
    assert "(x1) A brand new structured learning." in text


# ── `/remember` provenance (request) ───────────────────────────────────────


def test_a_remember_sighting_with_no_turn_records_the_request(
    tmp_path: Path,
) -> None:
    """The `/remember` case, and the reason `request` exists at all.

    A lesson is usually remembered in a chat that is never archived, so there
    is no transcript turn to cite. The two ways out are not equivalent: citing a
    manufactured turn is a reference to a conversation that never happened,
    while the request id is the sighting's real, re-readable origin.
    """
    vault = tmp_path / "vault"
    fact = "Pin the Node version before running the suite."
    assert mp.append_learning(vault, fact, workspace="vault", request="req-7")

    record = _one_active_record(vault)
    assert record.count == 1
    assert [(o.request, o.source, o.turn) for o in record.observations] == [
        ("req-7", "", None)
    ]
    # And it reads back off the file, cited as a request rather than a chat.
    line = next(
        line
        for line in _learnings(vault).splitlines()
        if "Pin the Node version" in line
    )
    assert "— sources: req:req-7" in line
    # The identity is the request alone, which is what makes a retry a no-op.
    document = parse_learnings(_learnings(vault), workspace=vault.name)
    reparsed = [e.record for e in document.entries if e.record is not None][0]
    assert reparsed.observations[0].identity == ("request", "req-7")
    assert reparsed.observations[0].citation == "req:req-7"


def test_a_remember_retry_does_not_inflate_recurrence(tmp_path: Path) -> None:
    """The dedupe the request id buys, and the reason it is carried at all.

    A `/remember` filed twice is one sighting. Without a stable identity the
    second accept would count as new evidence and push a lesson towards a
    promotion threshold nobody earned.
    """
    vault = tmp_path / "vault"
    fact = "Pin the Node version before running the suite."
    assert mp.append_learning(vault, fact, workspace="vault", request="req-7")
    assert mp.append_learning(vault, fact, workspace="vault", request="req-7")

    record = _one_active_record(vault)
    assert record.count == 1
    assert len(record.observations) == 1
    # A *different* request is a different sighting, and does count.
    assert mp.append_learning(vault, fact, workspace="vault", request="req-8")
    assert _one_active_record(vault).count == 2


def test_a_remember_may_carry_both_a_source_and_a_request(tmp_path: Path) -> None:
    """An archived `/remember` has a real chat id, and it may keep it.

    The two are different facts, so both are kept: the citation shows the
    archive because that is the re-readable provenance, and the identity keys on
    the request because it is the narrower one.
    """
    vault = tmp_path / "vault"
    fact = "Pin the Node version before running the suite."
    assert mp.append_learning(vault, fact, workspace="vault", source="chat-9", request="req-7")

    record = _one_active_record(vault)
    assert [(o.source, o.request) for o in record.observations] == [("chat-9", "req-7")]
    assert record.observations[0].identity == ("request", "req-7")
    assert record.observations[0].citation == "chat-9"


def test_a_request_is_not_a_source_and_does_not_manufacture_a_turn(
    tmp_path: Path,
) -> None:
    """No chat id, no turn number, no archive path — the line says `req:`.

    Every one of those would be an invented citation, and the request field is
    the honest alternative; a test that only checked the count would pass even
    if the writer had quietly rendered a `chat-*` source.
    """
    vault = tmp_path / "vault"
    assert mp.append_learning(
        vault, "A lesson with no transcript behind it.", workspace="vault", request="r1"
    )
    text = _learnings(vault)
    assert "chat-" not in text
    assert "#1" not in text
    assert "req:r1" in text


def test_the_preview_renders_the_same_provenance_the_accept_writes(
    tmp_path: Path,
) -> None:
    """The card and the write cannot disagree about where the sighting came from.

    This is the same argument the `source` field was fixed for: a preview that
    drops the citation shows a shorter sources list than the line the accept is
    about to produce, and the person reviewing it never saw the real one.
    """
    vault = tmp_path / "vault"
    before = mp.LEARNINGS_STUB
    rendered, operation = mp.render_learning_append(
        before, "A lesson.", workspace=vault.name, request="req-3"
    )
    assert operation == "add"
    assert "req:req-3" in rendered


# ── The bullet grammar's `request` tail ────────────────────────────────────


def test_a_bullet_round_trips_its_request_through_the_queue(tmp_path: Path) -> None:
    """The `_(request: …)_` tail is a field, not more text in the source.

    Folding it into the source would make a request id read back as a chat id,
    which is the invented-citation failure this field exists to remove — so the
    two are parsed apart and both survive the round trip.
    """
    from ciao.proposal_kinds import parse_bullet

    bullet = mp.MemoryProposal(
        target="learnings",
        text="Pin the Node version before running the suite.",
        source_section="/remember",
        request="req-7",
    ).as_bullet()
    parsed = parse_bullet(bullet)
    assert parsed is not None
    assert parsed.kind == "learnings"
    assert parsed.text == "Pin the Node version before running the suite."
    assert parsed.source == "/remember"
    assert parsed.request == "req-7"

    vault = tmp_path / "vault"
    mp.append_proposals([mp.MemoryProposal(
        target="learnings",
        text="Pin the Node version before running the suite.",
        source_section="/remember",
        request="req-7",
    )], vault)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    row = next(
        row
        for row in mp.list_proposals(queue)
        if row["kind"] == "learnings"
    )
    assert row["source"] == "/remember"
    assert row["request"] == "req-7"


def test_a_bullet_written_before_the_request_field_parses_unchanged(
    tmp_path: Path,
) -> None:
    """Every bullet already in an installed vault has no request tail."""
    from ciao.proposal_kinds import parse_bullet

    for line in (
        "- [memory] Prefers short answers.  _(from: curation)_",
        "- [project ./projects/x/doc.md] Ships on Friday.  _(from: chat-1)_",
        "- [learnings] No tail at all",
        "- [note_edit abc123] Retire this.  _(from: /remember)_",
    ):
        parsed = parse_bullet(line)
        assert parsed is not None, line
        assert parsed.request == "", line


def test_a_closing_paren_in_a_request_cannot_end_the_tail_early() -> None:
    """The bullet is one line with its own delimiters, so a `)` is stripped.

    Left in, it would close the tail early and the rest of the line would read
    as body text — a row that stops parsing without anything looking wrong.
    """
    from ciao.proposal_kinds import parse_bullet

    bullet = mp.MemoryProposal(
        target="learnings",
        text="A lesson.",
        source_section="/remember",
        request="req) and then some prose",
    ).as_bullet()
    parsed = parse_bullet(bullet)
    assert parsed is not None
    assert parsed.text == "A lesson."
    assert parsed.request == "req and then some prose"


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


# ---- Note retyping (issue #647) ---------------------------------------------
#
# `set_note_type` is the public half of what `vault_migration._retype_frontmatter`
# keeps private, and it is deliberately not the same function: the migration
# refuses a note it did not plan for, because a mismatch there means the vault
# moved under the pass. A category accept has already checked the baseline in
# its own caller, so what is left is "give this note this type", and a note with
# no frontmatter at all is a legitimate member of a cluster.


def test_set_note_type_rewrites_only_the_type_line(tmp_path: Path) -> None:
    note = tmp_path / "N.md"
    note.write_text(
        "---\ntitle: Keeps the soup warm\ntype: recipe-book\ntags: [food]\n---\n\n# Keeps the soup warm\n\nBody.\n",
        encoding="utf-8",
    )

    assert mp.set_note_type(note, "recipe") is True

    assert note.read_text(encoding="utf-8") == (
        "---\ntitle: Keeps the soup warm\ntype: recipe\ntags: [food]\n---\n\n"
        "# Keeps the soup warm\n\nBody.\n"
    )
    assert mp.read_note_type(note) == "recipe"


def test_set_note_type_creates_the_block_for_a_note_with_no_frontmatter(
    tmp_path: Path,
) -> None:
    note = tmp_path / "N.md"
    note.write_text("# Keeps the soup warm\n\nBody.\n", encoding="utf-8")

    assert mp.read_note_type(note) == ""
    assert mp.set_note_type(note, "recipe") is True
    assert note.read_text(encoding="utf-8") == (
        "---\ntype: recipe\n---\n\n# Keeps the soup warm\n\nBody.\n"
    )
    assert mp.read_note_type(note) == "recipe"


def test_set_note_type_adds_the_line_when_the_block_has_none(tmp_path: Path) -> None:
    note = tmp_path / "N.md"
    note.write_text("---\ntags: [food]\n---\n\n# Keeps the soup warm\n", encoding="utf-8")

    assert mp.set_note_type(note, "recipe") is True
    assert note.read_text(encoding="utf-8") == (
        "---\ntype: recipe\ntags: [food]\n---\n\n# Keeps the soup warm\n"
    )


def test_set_note_type_is_a_no_op_on_a_note_already_typed_that(tmp_path: Path) -> None:
    """An accept retried against a half-finished cluster has to converge rather
    than fail, so an unchanged note reports success."""
    note = tmp_path / "N.md"
    original = "---\ntype: recipe\n---\n\n# Keeps the soup warm\n"
    note.write_text(original, encoding="utf-8")

    assert mp.set_note_type(note, "recipe") is True
    assert note.read_text(encoding="utf-8") == original


def test_set_note_type_reports_a_note_it_cannot_write(tmp_path: Path) -> None:
    """False means the file could not be read or written — never that the write
    was skipped for a judgement reason, so a caller can tell the two apart."""
    assert mp.set_note_type(tmp_path / "missing.md", "recipe") is False
    assert mp.set_note_type(tmp_path / "N.md", "") is False


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
