"""The ``ciao memory-proposal-dismiss`` / ``memory-proposal-add`` CLI.

The nightly curation agent resolves proposals through the CLI, not the PWA, so
its decisions have to land in the same decision history the review page's
History tab reads, with ``via: "agent"``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.memory_proposals import read_decisions


# ── agent path (curation CLI) ─────────────────────────────────────────────


def test_cli_dismiss_records_an_agent_decision(tmp_path: Path) -> None:
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n- [memory] durable lesson  _(from: Decisions)_\n",
        encoding="utf-8",
    )

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "durable lesson",
    ])

    assert rc == 0
    decisions = read_decisions(queue)
    assert len(decisions) == 1
    assert decisions[0]["action"] == "dismissed"
    assert decisions[0]["kind"] == "memory"
    assert decisions[0]["via"] == "agent"


def test_cli_promote_then_dismiss_records_a_promotion(tmp_path: Path) -> None:
    """The curator files the fact first and drops the row second; that flow is
    a promotion, and only a bare rejection counts as dismissed."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n- [learnings] reuse the retry helper\n",
        encoding="utf-8",
    )

    rc = cli.main([
        "memory-proposal-dismiss",
        "--promoted",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "retry helper",
    ])

    assert rc == 0
    decisions = read_decisions(queue)
    assert len(decisions) == 1
    assert decisions[0]["action"] == "accepted"


def test_a_refused_cli_dismiss_records_nothing(tmp_path: Path) -> None:
    """An ambiguous substring removes nothing and decides nothing."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [memory] durable lesson one\n"
        "- [profile] durable lesson two\n",
        encoding="utf-8",
    )

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "durable lesson",
    ])

    assert rc == 1
    assert read_decisions(queue) == []


# ── memory-proposal-dismiss --text-file ───────────────────────────────────
#
# A proposal's text is arbitrary user prose. `memory-proposal-add` already
# keeps it out of argv for that reason; the dismissal names the same text and
# needed the same door, or every recipe that files a fact safely had to hand it
# back to the shell to remove it.


def _queue_with(tmp_path: Path, bullet: str) -> tuple[Path, Path]:
    """A workspace vault holding one queued proposal."""
    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(f"# Memory Proposals\n\n- {bullet}\n", encoding="utf-8")
    return vault, queue


def test_dismiss_reads_the_substring_from_a_file(tmp_path, monkeypatch):
    from ciao import cli

    hazardous = "prefers $(whoami) over `id`; never use \"quotes\""
    vault, queue = _queue_with(tmp_path, f"[memory] {hazardous}  _(from: Decisions)_")
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    needle = tmp_path / "needle.txt"
    # Trailing newline is an artifact of writing the file, never part of the
    # proposal it has to match.
    needle.write_text(hazardous + "\n", encoding="utf-8")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--text-file", str(needle),
    ])

    assert rc == 0
    assert hazardous not in queue.read_text(encoding="utf-8")


def test_dismiss_refuses_both_text_and_text_file(tmp_path, monkeypatch, capsys):
    from ciao import cli

    vault, _ = _queue_with(tmp_path, "[memory] durable lesson  _(from: Decisions)_")
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    needle = tmp_path / "needle.txt"
    needle.write_text("durable lesson", encoding="utf-8")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "durable lesson",
        "--text-file", str(needle),
    ])

    assert rc == 2
    assert "not both" in capsys.readouterr().err


def test_dismiss_reports_an_unreadable_text_file(tmp_path, monkeypatch, capsys):
    from ciao import cli

    vault, _ = _queue_with(tmp_path, "[memory] durable lesson  _(from: Decisions)_")
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--text-file", str(tmp_path / "missing.txt"),
    ])

    assert rc == 2
    assert "could not read" in capsys.readouterr().err


def test_dismiss_still_requires_some_text(tmp_path, monkeypatch, capsys):
    """Neither form given: the positional is optional now, not absent."""
    from ciao import cli

    vault, _ = _queue_with(tmp_path, "[memory] durable lesson  _(from: Decisions)_")
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
    ])

    assert rc == 2
    assert "required" in capsys.readouterr().err


def test_add_then_dismiss_round_trips_a_multiline_fact_file(tmp_path, monkeypatch):
    """The workflow the Workspace care schedule prompt prescribes, end to end.

    `memory-proposal-add` flattens whitespace because the queue is
    line-oriented Markdown; `remove_proposal_by_substring` then matches one
    line at a time. Without the same flattening on the dismiss side, a needle
    read from the very file the fact was filed from could never match its own
    bullet — the promotion would go unrecorded, the row would be re-asked every
    night, and the only way out would be the forbidden direct file edit.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    fact_file = tmp_path / "fact.txt"
    fact_file.write_text(
        "The user prefers $(whoami) shells\nand never uses `id`  directly.\n",
        encoding="utf-8",
    )

    assert cli.main([
        "memory-proposal-add",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--kind", "memory",
        "--text-file", str(fact_file),
    ]) == 0

    queue = vault / "Workspace" / "Memory-Proposals.md"
    assert "The user prefers $(whoami) shells and never uses `id` directly." in queue.read_text(
        encoding="utf-8"
    )

    assert cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--text-file", str(fact_file),
        "--promoted",
    ]) == 0

    assert "$(whoami)" not in queue.read_text(encoding="utf-8")


def test_dismiss_matches_a_row_written_with_repeated_whitespace(tmp_path, monkeypatch):
    """The Workspace care schedule prompt appends `[review]` questions to the queue directly.

    Those rows keep whatever whitespace the writer used, and
    `remove_proposal_by_substring` compares against the raw line — so
    flattening the needle unconditionally would break the form that worked
    before `--text-file` existed.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        '# Memory Proposals\n\n- [review] Keep "foo  bar" in ciao:memory?  _(from: curation)_\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        'Keep "foo  bar" in ciao:memory?',
    ])

    assert rc == 0
    assert "foo  bar" not in queue.read_text(encoding="utf-8")


def test_dismiss_reports_an_undecodable_text_file(tmp_path, monkeypatch, capsys):
    """UnicodeDecodeError is a ValueError, so `except OSError` never saw it.

    Left uncaught it escaped as a traceback and aborted an unattended run.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    binary = tmp_path / "needle.bin"
    binary.write_bytes(b"\xff\xfe\x00garbage")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--text-file", str(binary),
    ])

    assert rc == 2
    assert "could not read" in capsys.readouterr().err


def test_add_reports_an_undecodable_text_file(tmp_path, monkeypatch, capsys):
    """The add command had the same hole."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    binary = tmp_path / "fact.bin"
    binary.write_bytes(b"\xff\xfe\x00garbage")

    rc = cli.main([
        "memory-proposal-add",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--kind", "memory",
        "--text-file", str(binary),
    ])

    assert rc == 2
    assert "could not read" in capsys.readouterr().err


def test_dismiss_refuses_an_ambiguous_needle_instead_of_falling_back(tmp_path, monkeypatch, capsys):
    """The exact scenario the two-pass fallback would have got wrong.

    `remove_proposal_by_substring` reports no match and an ambiguous match
    identically, so retrying the flattened needle after an AMBIGUOUS first pass
    can uniquely hit a differently spaced row and delete the wrong proposal.
    Here `foo  bar` matches two rows; flattened it would uniquely match the
    third, which must NOT be removed.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [memory] foo  bar one  _(from: Decisions)_\n"
        "- [memory] foo  bar two  _(from: Decisions)_\n"
        "- [memory] foo bar three  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "foo  bar",
    ])

    assert rc == 1
    assert "longer, unique substring" in capsys.readouterr().err
    # Every row survives — above all the third, which the fallback would have
    # deleted and recorded a dismissal for.
    body = queue.read_text(encoding="utf-8")
    assert "foo bar three" in body
    assert "foo  bar one" in body
    assert "foo  bar two" in body


def test_dismiss_refuses_when_raw_and_flattened_name_different_rows(tmp_path, monkeypatch, capsys):
    """The one-raw/one-flattened collision.

    A directly written row keeps its double space; a row filed through
    `memory-proposal-add` was normalised. A needle of `foo  bar` then matches
    the first uniquely and its flattened form matches the second uniquely — so
    taking whichever was tried first deletes a row nobody named and records an
    outcome for it, while the row the caller meant stays queued.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [review] foo  bar written directly  _(from: curation)_\n"
        "- [memory] foo bar filed through add  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "foo  bar",
    ])

    assert rc == 1
    assert "longer, unique substring" in capsys.readouterr().err
    body = queue.read_text(encoding="utf-8")
    assert "written directly" in body
    assert "filed through add" in body


def test_add_reads_the_payload_from_a_file(tmp_path, monkeypatch):
    """The payload is user-controlled too — a doc path or a person's name.

    `--text-file` kept the fact out of argv; a payload containing spaces, `;`,
    `$()` or quotes had no such door and was interpolated into the command.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    payload = tmp_path / "payload.txt"
    payload.write_text("Projects/A $(whoami) & B.md\n", encoding="utf-8")
    fact = tmp_path / "fact.txt"
    fact.write_text("The doc owns the rollout plan.\n", encoding="utf-8")

    rc = cli.main([
        "memory-proposal-add",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--kind", "project",
        "--payload-file", str(payload),
        "--text-file", str(fact),
    ])

    assert rc == 0
    body = (vault / "Workspace" / "Memory-Proposals.md").read_text(encoding="utf-8")
    assert "Projects/A $(whoami) & B.md" in body


def test_add_refuses_both_payload_forms(tmp_path, monkeypatch, capsys):
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    payload = tmp_path / "payload.txt"
    payload.write_text("Projects/A.md", encoding="utf-8")

    rc = cli.main([
        "memory-proposal-add",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--kind", "project",
        "--payload", "Projects/B.md",
        "--payload-file", str(payload),
        "a fact",
    ])

    assert rc == 2
    assert "not both" in capsys.readouterr().err


def test_add_says_previously_dismissed_rather_than_already_queued(tmp_path, monkeypatch, capsys):
    """A user who changed their mind must not be told their request landed.

    `append_proposals` returns None both for a fact already queued and for one
    already dismissed, and the CLI reported both as "already in the queue" —
    so an explicit /remember of a previously rejected fact created no row while
    the agent confirmed success.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    fact = "Prefers tabs over spaces."

    assert cli.main([
        "memory-proposal-add", "--workspace", str(tmp_path),
        "--vault-root", str(vault), "--kind", "memory", fact,
    ]) == 0
    assert cli.main([
        "memory-proposal-dismiss", "--workspace", str(tmp_path),
        "--vault-root", str(vault), fact,
    ]) == 0
    capsys.readouterr()

    assert cli.main([
        "memory-proposal-add", "--workspace", str(tmp_path),
        "--vault-root", str(vault), "--kind", "memory", fact,
    ]) == 0
    out = capsys.readouterr().out
    assert "Previously dismissed" in out
    assert "Already in the queue" not in out


def test_add_still_says_already_queued_for_a_pending_row(tmp_path, monkeypatch, capsys):
    """The other None case keeps its own, different message."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    fact = "Prefers tabs over spaces."

    for _ in range(2):
        assert cli.main([
            "memory-proposal-add", "--workspace", str(tmp_path),
            "--vault-root", str(vault), "--kind", "memory", fact,
        ]) == 0
    out = capsys.readouterr().out
    assert "Already in the queue" in out
    assert "Previously dismissed" not in out


def test_add_does_not_call_a_promoted_fact_dismissed(tmp_path, monkeypatch, capsys):
    """`record_promotion` writes to the same sidecar as `record_dismissal`.

    Reading every logged text would tell someone their fact had been rejected
    when it was in fact accepted and may already be live in the region — the
    opposite of what happened, and it would invite them to re-file something
    that is already there.
    """
    from ciao import cli
    from ciao.memory_proposals import record_promotion

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text("# Memory Proposals\n", encoding="utf-8")
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    fact = "Prefers tabs over spaces."

    record_promotion(queue, text=fact, kind="memory")
    capsys.readouterr()

    assert cli.main([
        "memory-proposal-add", "--workspace", str(tmp_path),
        "--vault-root", str(vault), "--kind", "memory", fact,
    ]) == 0
    out = capsys.readouterr().out
    assert "Previously dismissed" not in out
    # Nor "already in the queue": a promoted fact is not in the queue at all,
    # it is in its destination. Three states, three messages.
    assert "Already in the queue" not in out
    assert "Already promoted" in out


def test_add_dedupes_dismissed_direct_row_with_repeated_whitespace(
    tmp_path, monkeypatch, capsys
):
    """Sidecar dedupe uses the same whitespace normalization as queue writes."""
    from ciao import cli
    from ciao.memory_proposals import record_dismissal

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [memory] Prefers   tabs over spaces.  _(from: curation)_\n",
        encoding="utf-8",
    )
    record_dismissal(queue, text="Prefers   tabs over spaces.", kind="memory")
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    assert cli.main(
        [
            "memory-proposal-add",
            "--workspace",
            str(tmp_path),
            "--vault-root",
            str(vault),
            "--kind",
            "memory",
            "Prefers tabs over spaces.",
        ]
    ) == 0
    out = capsys.readouterr().out
    assert "Previously dismissed" in out
    assert queue.read_text(encoding="utf-8").count("Prefers") == 1


def test_add_can_refile_dismissed_fact_when_explicitly_allowed(
    tmp_path, monkeypatch, capsys
):
    """An explicit reconsideration can bypass dismissal, but not promotion."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    fact = "Prefers tabs over spaces."

    assert cli.main(
        ["memory-proposal-add", "--workspace", str(tmp_path), "--vault-root", str(vault), fact]
    ) == 0
    assert cli.main(
        [
            "memory-proposal-dismiss",
            "--workspace",
            str(tmp_path),
            "--vault-root",
            str(vault),
            fact,
        ]
    ) == 0
    capsys.readouterr()

    assert cli.main(
        [
            "memory-proposal-add",
            "--workspace",
            str(tmp_path),
            "--vault-root",
            str(vault),
            "--allow-dismissed",
            fact,
        ]
    ) == 0
    assert "Queued" in capsys.readouterr().out


def test_cli_dismiss_removes_a_row_whose_text_prefixes_another(
    tmp_path: Path, monkeypatch
) -> None:
    """A row resolved uniquely by its source suffix must actually be removed.

    The command resolved the row by needle, then handed the *resolved full
    text* to `remove_proposal_by_substring`. That matcher is a casefolded
    substring test requiring exactly one hit, so `Use Python` also matched the
    surviving `Use Python 3 here.` and the removal refused — reporting "No
    unique memory proposal matched" for the row it had just resolved uniquely.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [memory] Use Python  _(from: Alpha)_\n"
        "- [memory] Use Python 3 here.  _(from: Beta)_\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "(from: Alpha)",
    ])

    assert rc == 0
    remaining = queue.read_text(encoding="utf-8")
    assert "(from: Alpha)" not in remaining
    assert "Use Python 3 here." in remaining

    decisions = read_decisions(queue)
    assert len(decisions) == 1
    assert decisions[0]["action"] == "dismissed"

    # The receipt names the resolved row, not the needle that found it, so
    # History shows the fact rather than the disambiguating fragment.
    from ciao import memory_receipts as mr

    resolves = [
        r for r in mr.read_receipts(mr.journal_path(vault, queue.parent))
        if r["kind"] == "queue_resolve"
    ]
    assert resolves
    assert resolves[-1]["removed_text"] == "Use Python"
    assert resolves[-1]["status"] == mr.APPLIED


def test_cli_dismiss_still_refuses_an_ambiguous_needle(
    tmp_path: Path, monkeypatch
) -> None:
    """The uniqueness guard must survive the needle change."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [memory] Use Python  _(from: Alpha)_\n"
        "- [memory] Use Python 3 here.  _(from: Beta)_\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    rc = cli.main([
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "Use Python",
    ])

    assert rc != 0
    assert queue.read_text(encoding="utf-8").count("- [memory]") == 2


# ── `/remember` provenance (--request) ─────────────────────────────────────


def test_cli_request_records_the_lesson_provenance(tmp_path: Path) -> None:
    """`/remember` in an unarchived chat files with a request, not a chat.

    The two are separate fields on the bullet and separate arguments here,
    because a request id folded into `--source` would read back as a chat id —
    the invented citation the field exists to remove.
    """
    from ciao import cli
    from ciao.memory_proposals import list_proposals

    vault = tmp_path / "memory-vault"
    fact = tmp_path / "fact.md"
    fact.write_text("Pin the Node version before running the suite.", encoding="utf-8")

    rc = cli.main([
        "memory-proposal-add",
        "--kind", "learnings",
        "--source", "/remember",
        "--request", "req-7",
        "--text-file", str(fact),
        "--json",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
    ])

    assert rc == 0
    queue = vault / "Workspace" / "Memory-Proposals.md"
    row = next(row for row in list_proposals(queue) if row["kind"] == "learnings")
    assert row["source"] == "/remember"
    assert row["request"] == "req-7"
    # The citation a person reads on the queue line is the request, not prose.
    assert "_(request: req-7)_" in queue.read_text(encoding="utf-8")


def test_cli_request_is_refused_on_a_kind_that_never_records_it(tmp_path: Path) -> None:
    """Only the `[learnings]` accept hands a request to the writer.

    Storing one anywhere else would be a field no accept reads, which is a
    citation that looks real and is not — so it is refused by name rather than
    quietly written where nothing consumes it.
    """
    from ciao import cli

    fact = tmp_path / "fact.md"
    fact.write_text("Prefers short answers.", encoding="utf-8")

    rc = cli.main([
        "memory-proposal-add",
        "--kind", "memory",
        "--request", "req-7",
        "--text-file", str(fact),
        "--workspace", str(tmp_path),
        "--vault-root", str(tmp_path / "memory-vault"),
    ])

    assert rc == 2
    assert not (tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md").exists()


def test_cli_request_must_be_a_plain_identifier(tmp_path: Path) -> None:
    """It is cited on a one-line bullet, so a delimiter in it would break parsing.

    `)` closes the `_(request: …)_` tail, so a request carrying one would make
    the rest of the line read as body text — a row that stops parsing without
    anything looking wrong.
    """
    from ciao import cli

    fact = tmp_path / "fact.md"
    fact.write_text("Pin the Node version before running the suite.", encoding="utf-8")

    for bad in ("req) prose", "a b", "req\n7", "", "x" * 65):
        rc = cli.main([
            "memory-proposal-add",
            "--kind", "learnings",
            "--request", bad,
            "--text-file", str(fact),
            "--workspace", str(tmp_path),
            "--vault-root", str(tmp_path / "memory-vault"),
        ])
        # An empty request is simply absent, so it is accepted with no tail.
        assert rc == (0 if not bad else 2), bad

    queue = tmp_path / "memory-vault" / "Workspace" / "Memory-Proposals.md"
    from ciao.memory_proposals import list_proposals

    rows = [row for row in list_proposals(queue) if row["kind"] == "learnings"]
    assert len(rows) == 1 and rows[0]["request"] == ""


# ── decider: add flag and escalate ────────────────────────────────────────

_PEOPLE_BULLET = "- [people Mo] Mo leads platform.  _(from: chat-1)_  _(decide: nightly; r1)_"
_OTHER_BULLET = "- [memory] Prefer uv.  _(from: chat-2)_"


def _decider_queue(tmp_path: Path, *bullets: str) -> tuple[Path, Path]:
    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n## 2026-01-01T00:00:00Z\n\n" + "\n".join(bullets) + "\n",
        encoding="utf-8",
    )
    return vault, queue


def _escalate(tmp_path: Path, vault: Path, needle: str, reason: str | None) -> int:
    from ciao import cli

    text_file = tmp_path / "needle.txt"
    text_file.write_text(needle, encoding="utf-8")
    argv = [
        "memory-proposal-escalate",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--text-file", str(text_file),
    ]
    if reason is not None:
        reason_file = tmp_path / "reason.txt"
        reason_file.write_text(reason, encoding="utf-8")
        argv += ["--reason-file", str(reason_file)]
    return cli.main(argv)


def test_cli_add_rejects_an_unknown_decider(tmp_path: Path) -> None:
    """argparse refuses a decider outside the closed set."""
    import pytest

    from ciao import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["memory-proposal-add", "--workspace", str(tmp_path), "--decide", "maybe", "x"])
    assert exc.value.code == 2


def test_cli_escalate_retags_in_place_under_the_lock(tmp_path: Path, monkeypatch) -> None:
    """Escalation rewrites one line under queue_lock and records no decision."""
    import contextlib

    from ciao import memory_receipts

    vault, queue = _decider_queue(tmp_path, _PEOPLE_BULLET, _OTHER_BULLET)
    entered: list[Path] = []
    real_lock = memory_receipts.queue_lock

    @contextlib.contextmanager
    def recording_lock(path: Path):
        entered.append(path)
        with real_lock(path):
            yield

    monkeypatch.setattr(memory_receipts, "queue_lock", recording_lock)
    rc = _escalate(tmp_path, vault, "Mo leads", "Checked People/Mo.md; only you know.")

    assert rc == 0
    assert entered
    lines = queue.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# Memory Proposals"
    assert lines[2] == "## 2026-01-01T00:00:00Z"
    assert lines[4] == (
        "- [people Mo] Mo leads platform.  _(from: chat-1)_"
        "  _(decide: you; Checked People/Mo.md; only you know.)_"
    )
    assert lines[5] == _OTHER_BULLET
    assert read_decisions(queue) == []


def test_cli_escalate_adds_a_tail_to_a_bullet_with_none(tmp_path: Path) -> None:
    """A bullet with no decide tail gains one at the end."""
    vault, queue = _decider_queue(tmp_path, _OTHER_BULLET)
    assert _escalate(tmp_path, vault, "Prefer uv", "r") == 0
    assert queue.read_text(encoding="utf-8").splitlines()[-1] == _OTHER_BULLET + "  _(decide: you; r)_"


def test_cli_escalate_replaces_an_existing_you_reason(tmp_path: Path) -> None:
    """Re-escalating is idempotent: one tail, the latest reason."""
    vault, queue = _decider_queue(tmp_path, _OTHER_BULLET)
    assert _escalate(tmp_path, vault, "Prefer uv", "first") == 0
    assert _escalate(tmp_path, vault, "Prefer uv", "second") == 0
    last = queue.read_text(encoding="utf-8").splitlines()[-1]
    assert last == _OTHER_BULLET + "  _(decide: you; second)_"
    assert last.count("_(decide:") == 1


def test_cli_escalate_preserves_the_proposal_id(tmp_path: Path) -> None:
    """The decider is outside the hashed fields, so escalation keeps the id."""
    from ciao.proposal_tracking import walk_proposal_queue

    vault, queue = _decider_queue(tmp_path, _PEOPLE_BULLET, _OTHER_BULLET)
    rel = "ws/Workspace/Memory-Proposals.md"
    before = [e.proposal_id for e in walk_proposal_queue("ws", rel, queue.read_text(encoding="utf-8"))]
    assert _escalate(tmp_path, vault, "Mo leads", "r") == 0
    after = [e.proposal_id for e in walk_proposal_queue("ws", rel, queue.read_text(encoding="utf-8"))]
    assert before == after and len(before) == 2


def test_cli_escalate_refuses_ambiguous_missing_and_empty(tmp_path: Path) -> None:
    """Two matches or none exit 1 and change nothing; bad reason input exits 2."""
    vault, queue = _decider_queue(tmp_path, "- [memory] lesson one  _(from: a)_", "- [memory] lesson two  _(from: a)_")
    original = queue.read_text(encoding="utf-8")
    assert _escalate(tmp_path, vault, "lesson", "r") == 1
    assert queue.read_text(encoding="utf-8") == original
    assert _escalate(tmp_path, vault, "absent", "r") == 1
    assert _escalate(tmp_path, vault, "lesson one", "  \n") == 2
    assert _escalate(tmp_path, vault, "lesson one", None) == 2
    assert queue.read_text(encoding="utf-8") == original


def test_cli_escalate_matches_the_flattened_form(tmp_path: Path) -> None:
    """A needle with repeated whitespace still resolves, like dismiss."""
    vault, queue = _decider_queue(tmp_path, "- [memory] spaced lesson  _(from: a)_")
    assert _escalate(tmp_path, vault, "spaced   lesson", "r") == 0
    assert "_(decide: you; r)_" in queue.read_text(encoding="utf-8")


def test_dismiss_still_works_on_a_bullet_with_a_decide_tail(tmp_path: Path) -> None:
    """Dismissal records the bare text, never the decide tail."""
    from ciao import cli

    vault, queue = _decider_queue(tmp_path, _PEOPLE_BULLET)
    rc = cli.main([
        "memory-proposal-dismiss", "--workspace", str(tmp_path),
        "--vault-root", str(vault), "Mo leads",
    ])
    assert rc == 0
    decisions = read_decisions(queue)
    assert len(decisions) == 1
    assert decisions[0]["text"] == "Mo leads platform."


# ── decision reasons (#1734) ──────────────────────────────────────────────


def _reason_queue(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n- [memory] durable lesson  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    return vault, queue


def _dismiss_args(tmp_path: Path, vault: Path, *extra: str) -> list[str]:
    return [
        "memory-proposal-dismiss",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        *extra,
        "durable lesson",
    ]


def test_cli_dismiss_records_a_reason_from_a_file(tmp_path: Path) -> None:
    from ciao import cli

    vault, queue = _reason_queue(tmp_path)
    reason_file = tmp_path / "reason.txt"
    reason_file.write_text("one-off\n", encoding="utf-8")

    rc = cli.main(_dismiss_args(tmp_path, vault, "--reason-file", str(reason_file)))

    assert rc == 0
    decisions = read_decisions(queue)
    assert decisions[0]["reason"] == "one-off"
    assert decisions[0]["via"] == "agent"
    assert decisions[0]["action"] == "dismissed"


def test_cli_promoted_dismiss_records_the_reason_on_the_accepted_row(tmp_path: Path) -> None:
    from ciao import cli

    vault, queue = _reason_queue(tmp_path)
    reason_file = tmp_path / "reason.txt"
    reason_file.write_text("yes, everywhere", encoding="utf-8")

    rc = cli.main(
        _dismiss_args(tmp_path, vault, "--promoted", "--reason-file", str(reason_file))
    )

    assert rc == 0
    decisions = read_decisions(queue)
    assert decisions[0]["action"] == "accepted"
    assert decisions[0]["reason"] == "yes, everywhere"


def test_cli_over_cap_or_unreadable_reason_file_exits_2_and_changes_nothing(
    tmp_path: Path,
) -> None:
    from ciao import cli
    from ciao.memory_receipts import journal_path

    vault, queue = _reason_queue(tmp_path)
    before = queue.read_bytes()
    too_long = tmp_path / "long.txt"
    too_long.write_text("x" * 1001, encoding="utf-8")

    for reason_file in (too_long, tmp_path / "missing.txt"):
        rc = cli.main(_dismiss_args(tmp_path, vault, "--reason-file", str(reason_file)))
        assert rc == 2
        assert queue.read_bytes() == before
        assert read_decisions(queue) == []
        assert not journal_path(vault, None).exists()


def test_cli_without_reason_file_is_unchanged(tmp_path: Path) -> None:
    from ciao import cli

    vault, queue = _reason_queue(tmp_path)

    assert cli.main(_dismiss_args(tmp_path, vault)) == 0
    assert read_decisions(queue)[0]["reason"] == ""


# ── credential-adjacent reason files are refused (#1734) ───────────────────


def _refused_reason_paths(tmp_path: Path) -> list[Path]:
    """A `.env`-shaped file and one under `.runtime/`, both holding a secret."""
    env_file = tmp_path / ".env"
    env_file.write_text("TOKEN=s3cret", encoding="utf-8")
    runtime_file = tmp_path / ".runtime" / "reason.txt"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("s3cret-token", encoding="utf-8")
    return [env_file, runtime_file]


def _no_sidecar(queue: Path) -> bool:
    from ciao.memory_proposals import dismissed_log_path

    return not dismissed_log_path(queue).exists()


def test_cli_dismiss_refuses_a_credential_adjacent_reason_file(tmp_path: Path, capsys) -> None:
    from ciao import cli

    vault, queue = _reason_queue(tmp_path)
    before = queue.read_bytes()

    for path in _refused_reason_paths(tmp_path):
        rc = cli.main(_dismiss_args(tmp_path, vault, "--reason-file", str(path)))
        assert rc == 2
        assert "credential" in capsys.readouterr().err
        assert queue.read_bytes() == before
        assert _no_sidecar(queue)


def test_cli_add_refuses_a_credential_adjacent_decide_reason_file(tmp_path: Path, capsys) -> None:
    from ciao import cli

    vault, queue = _decider_queue(tmp_path, _OTHER_BULLET)
    before = queue.read_bytes()
    text_file = tmp_path / "text.txt"
    text_file.write_text("A new durable fact.", encoding="utf-8")

    for path in _refused_reason_paths(tmp_path):
        rc = cli.main([
            "memory-proposal-add",
            "--workspace", str(tmp_path),
            "--vault-root", str(vault),
            "--kind", "memory",
            "--text-file", str(text_file),
            "--decide", "you",
            "--decide-reason-file", str(path),
        ])
        assert rc == 2
        assert "credential" in capsys.readouterr().err
        assert queue.read_bytes() == before


def test_cli_escalate_refuses_a_credential_adjacent_reason_file(tmp_path: Path, capsys) -> None:
    from ciao import cli

    vault, queue = _decider_queue(tmp_path, _OTHER_BULLET)
    before = queue.read_bytes()
    text_file = tmp_path / "needle.txt"
    text_file.write_text("Prefer uv", encoding="utf-8")

    for path in _refused_reason_paths(tmp_path):
        rc = cli.main([
            "memory-proposal-escalate",
            "--workspace", str(tmp_path),
            "--vault-root", str(vault),
            "--text-file", str(text_file),
            "--reason-file", str(path),
        ])
        assert rc == 2
        assert "credential" in capsys.readouterr().err
        assert queue.read_bytes() == before


# ── shadow verdicts and handover (#1737) ──────────────────────────────────

_REGION_BULLET = "- [memory] Prefers uv over pip for installs.  _(from: chat-1)_"
_REGION_NEEDLE = "Prefers uv over pip"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _predict(
    tmp_path: Path,
    vault: Path,
    needle: str,
    verdict: str,
    reason: str = "because",
    *extra: str,
) -> int:
    from ciao import cli

    argv = [
        "memory-proposal-predict",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--text-file", str(_write(tmp_path, "needle.txt", needle)),
        "--verdict", verdict,
        "--reason-file", str(_write(tmp_path, "reason.txt", reason)),
        *extra,
    ]
    return cli.main(argv)


_INSTALLED: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _installed_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service definition the handover gate trusts names ``_INSTALLED['root']``."""
    from types import SimpleNamespace

    _INSTALLED.clear()
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(workspace=_INSTALLED.get("root", "")),
    )


def _handover_install(tmp_path: Path, handover: str | None, *bullets: str) -> tuple[Path, Path, Path]:
    """An install root whose registry names workspace `work`; returns ``(root, vault, queue)``."""
    import json

    from ciao.memory_tool import ensure_regions
    from ciao.workspace_guide import guide_path

    root = tmp_path / "workspace"
    vault = root / "memory-vault" / "work"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "# Memory Proposals\n\n## 2026-01-01T00:00:00Z\n\n" + "\n".join(bullets) + "\n",
        encoding="utf-8",
    )
    (root / ".runtime").mkdir()
    entry: dict[str, str] = {"name": "work", "vault_root": str(vault)}
    if handover is not None:
        entry["memory_handover"] = handover
    (root / ".runtime" / "workspaces.json").write_text(json.dumps([entry]), encoding="utf-8")
    guide = guide_path(root)
    guide.write_text("# Guide\n", encoding="utf-8")
    ensure_regions(guide)
    _INSTALLED["root"] = str(root)
    return root, vault, queue


def _run_handover(
    command: str, root: Path, vault: Path, needle: str, *extra: str
) -> int:
    from ciao import cli

    needle_file = _write(root.parent, "handover-needle.txt", needle)
    return cli.main([
        command,
        "--workspace", str(root),
        "--vault-root", str(vault),
        "--text-file", str(needle_file),
        *extra,
    ])


def _store_prediction(queue: Path, verdict: str, text: str = "Prefers uv over pip for installs.") -> None:
    from ciao.proposal_predictions import record_prediction

    record_prediction(queue, kind="memory", text=text, verdict=verdict, reason="because")


def _store_fresh_prediction(queue: Path, verdict: str, text: str = "Prefers uv over pip for installs.") -> None:
    """Append a prediction one second after the newest decision on *text*."""
    import json
    from datetime import datetime, timedelta

    from ciao.proposal_predictions import predictions_path

    decided = [row["ts"] for row in read_decisions(queue) if row.get("text") == text]
    after = max(datetime.fromisoformat(ts) for ts in decided) + timedelta(seconds=1)
    row = {
        "predicted_at": after.isoformat(timespec="seconds"),
        "kind": "memory",
        "text": text,
        "verdict": verdict,
        "reason": "fresh after the decision",
    }
    with predictions_path(queue).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def test_predict_records_a_verdict_without_touching_the_queue(tmp_path: Path, capsys) -> None:
    import json

    from ciao.proposal_predictions import predictions_path, read_predictions

    vault, queue = _decider_queue(tmp_path, _REGION_BULLET)
    before = queue.read_bytes()

    rc = _predict(tmp_path, vault, _REGION_NEEDLE, "accept", "stable preference", "--json")

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["recorded"] is True
    assert queue.read_bytes() == before
    rows = read_predictions(queue)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "accept"
    assert predictions_path(queue).exists()
    assert read_decisions(queue) == []


def test_predict_refuses_non_region_rows(tmp_path: Path, capsys) -> None:
    from ciao.proposal_predictions import predictions_path

    vault, queue = _decider_queue(tmp_path, "- [people Mo] Mo likes tea.")

    rc = _predict(tmp_path, vault, "Mo likes tea", "accept")

    assert rc == 1
    assert "only recorded for [memory] and [profile]" in capsys.readouterr().err
    assert not predictions_path(queue).exists()


def test_predict_matches_the_flattened_form_and_refuses_ambiguity(tmp_path: Path) -> None:
    from ciao.proposal_predictions import read_predictions

    vault, queue = _decider_queue(
        tmp_path,
        "- [memory] Prefers uv over pip.",
        "- [memory] Prefers poetry sometimes.",
        "- [memory] Prefers poetry rarely.",
    )

    assert _predict(tmp_path, vault, "Prefers   uv over pip", "dismiss") == 0
    assert _predict(tmp_path, vault, "Prefers poetry", "dismiss") == 1
    assert len(read_predictions(queue)) == 1


def test_predict_requires_verdict_and_reason(tmp_path: Path) -> None:
    import pytest

    from ciao import cli

    vault, queue = _decider_queue(tmp_path, _REGION_BULLET)
    needle = _write(tmp_path, "needle.txt", _REGION_NEEDLE)
    base = [
        "memory-proposal-predict",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--text-file", str(needle),
    ]
    with pytest.raises(SystemExit) as exc:
        cli.main([*base, "--reason-file", str(_write(tmp_path, "r.txt", "why"))])
    assert exc.value.code == 2
    assert cli.main([*base, "--verdict", "accept"]) == 2
    empty = _write(tmp_path, "empty.txt", "  \n")
    assert cli.main([*base, "--verdict", "accept", "--reason-file", str(empty)]) == 2


def test_predict_refuses_a_credential_adjacent_reason_file(tmp_path: Path, capsys) -> None:
    from ciao import cli

    vault, queue = _decider_queue(tmp_path, _REGION_BULLET)
    denied_path = tmp_path / ".runtime" / "reason.txt"
    denied_path.parent.mkdir()
    denied_path.write_text("why", encoding="utf-8")

    rc = cli.main([
        "memory-proposal-predict",
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        "--text-file", str(_write(tmp_path, "needle.txt", _REGION_NEEDLE)),
        "--verdict", "accept",
        "--reason-file", str(denied_path),
    ])

    assert rc == 2
    assert "credential" in capsys.readouterr().err


def test_predict_twice_identical_reports_not_recorded(tmp_path: Path, capsys) -> None:
    import json

    vault, queue = _decider_queue(tmp_path, _REGION_BULLET)

    assert _predict(tmp_path, vault, _REGION_NEEDLE, "accept", "why", "--json") == 0
    capsys.readouterr()
    assert _predict(tmp_path, vault, _REGION_NEEDLE, "accept", "why", "--json") == 0
    assert json.loads(capsys.readouterr().out)["recorded"] is False


def test_memory_proposals_shows_the_prediction(tmp_path: Path, capsys) -> None:
    import json

    from ciao import cli

    vault, queue = _decider_queue(tmp_path, _REGION_BULLET, "- [people Mo] Mo likes tea.")
    _store_prediction(queue, "dismiss")
    base = ["memory-proposals", "--workspace", str(tmp_path), "--vault-root", str(vault)]

    assert cli.main(base) == 0
    out = capsys.readouterr().out
    assert "(predicted: dismiss; because)" in out
    assert cli.main([*base, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["proposals"]
    by_kind = {row["kind"]: row for row in rows}
    assert by_kind["memory"]["predicted"] == "dismiss"
    assert by_kind["memory"]["predicted_reason"] == "because"
    assert by_kind["people"]["predicted"] == ""
    assert by_kind["people"]["predicted_reason"] == ""


@pytest.mark.parametrize("stored", [None, "off"], ids=["missing-key", "explicit-off"])
def test_dismiss_handover_refuses_when_setting_is_off(
    tmp_path: Path, capsys, stored: str | None
) -> None:
    root, vault, queue = _handover_install(tmp_path, stored, _REGION_BULLET)
    _store_prediction(queue, "dismiss")
    before = queue.read_bytes()

    rc = _run_handover("memory-proposal-dismiss", root, vault, _REGION_NEEDLE, "--handover")

    assert rc == 1
    assert "handover refused" in capsys.readouterr().err
    assert queue.read_bytes() == before
    assert read_decisions(queue) == []


def test_dismiss_handover_refuses_without_a_dismiss_prediction_and_for_accept_prediction(
    tmp_path: Path,
) -> None:
    root, vault, queue = _handover_install(tmp_path, "all", _REGION_BULLET)
    before = queue.read_bytes()
    assert _run_handover("memory-proposal-dismiss", root, vault, _REGION_NEEDLE, "--handover") == 1
    _store_prediction(queue, "accept")
    assert _run_handover("memory-proposal-dismiss", root, vault, _REGION_NEEDLE, "--handover") == 1
    assert queue.read_bytes() == before
    assert read_decisions(queue) == []


def test_dismiss_handover_refuses_decide_you_and_non_region_rows(tmp_path: Path) -> None:
    root, vault, queue = _handover_install(
        tmp_path,
        "all",
        "- [memory] Prefers uv over pip for installs.  _(decide: you; needs the owner)_",
        "- [people Mo] Mo likes tea.",
    )
    _store_prediction(queue, "dismiss")
    before = queue.read_bytes()

    assert _run_handover("memory-proposal-dismiss", root, vault, _REGION_NEEDLE, "--handover") == 1
    assert _run_handover("memory-proposal-dismiss", root, vault, "Mo likes tea", "--handover") == 1
    assert queue.read_bytes() == before


def test_dismiss_handover_with_promoted_is_a_usage_error(tmp_path: Path) -> None:
    root, vault, queue = _handover_install(tmp_path, "all", _REGION_BULLET)
    _store_prediction(queue, "dismiss")

    rc = _run_handover(
        "memory-proposal-dismiss", root, vault, _REGION_NEEDLE, "--handover", "--promoted"
    )

    assert rc == 2
    assert _REGION_NEEDLE in queue.read_text(encoding="utf-8")


def test_dismiss_handover_settles_a_predicted_dismiss_as_agent(tmp_path: Path) -> None:
    for handover in ("dismiss", "all"):
        case = tmp_path / handover
        case.mkdir()
        root, vault, queue = _handover_install(case, handover, _REGION_BULLET)
        _store_prediction(queue, "dismiss")

        rc = _run_handover("memory-proposal-dismiss", root, vault, _REGION_NEEDLE, "--handover")

        assert rc == 0
        assert _REGION_NEEDLE not in queue.read_text(encoding="utf-8")
        decisions = read_decisions(queue)
        assert len(decisions) == 1
        assert decisions[0]["via"] == "agent"
        assert decisions[0]["action"] == "dismissed"


def test_dismiss_without_the_flag_is_unchanged_for_region_rows(tmp_path: Path) -> None:
    root, vault, queue = _handover_install(tmp_path, None, _REGION_BULLET)

    rc = _run_handover("memory-proposal-dismiss", root, vault, _REGION_NEEDLE)

    assert rc == 0
    decisions = read_decisions(queue)
    assert len(decisions) == 1
    assert decisions[0]["via"] == "agent"


def test_accept_refuses_unless_handover_is_all(tmp_path: Path, capsys) -> None:
    for handover in ("off", "dismiss"):
        case = tmp_path / handover
        case.mkdir()
        root, vault, queue = _handover_install(case, handover, _REGION_BULLET)
        _store_prediction(queue, "accept")
        before = queue.read_bytes()

        rc = _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE)

        assert rc == 1
        assert "handover refused" in capsys.readouterr().err
        assert queue.read_bytes() == before
        assert read_decisions(queue) == []


def test_accept_refuses_without_an_accept_prediction_and_for_decide_you(tmp_path: Path) -> None:
    for name, bullet, prediction in (
        ("none", _REGION_BULLET, None),
        ("dismiss", _REGION_BULLET, "dismiss"),
        (
            "you",
            "- [memory] Prefers uv over pip for installs.  _(decide: you; owner call)_",
            "accept",
        ),
    ):
        case = tmp_path / name
        case.mkdir()
        root, vault, queue = _handover_install(case, "all", bullet)
        if prediction:
            _store_prediction(queue, prediction)
        before = queue.read_bytes()

        assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
        assert queue.read_bytes() == before
        assert read_decisions(queue) == []


def _accepted_install(tmp_path: Path, bullet: str = _REGION_BULLET) -> tuple[Path, Path, Path]:
    from ciao import curation_run

    root, vault, queue = _handover_install(tmp_path, "all", bullet)
    _store_prediction(queue, "accept")
    curation_run.begin_run(vault, holder="test-run", ttl_s=3600)
    return root, vault, queue


def test_accept_writes_the_region_with_a_receipt_and_records_the_decision(
    tmp_path: Path, capsys
) -> None:
    import json

    from ciao import memory_receipts
    from ciao.workspace_guide import guide_path

    root, vault, queue = _accepted_install(tmp_path)

    rc = _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE, "--json")

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["accepted"] is True
    guide = guide_path(root).read_text(encoding="utf-8")
    assert "Prefers uv over pip for installs." in guide
    assert "Prefers uv over pip" not in queue.read_text(encoding="utf-8")
    decisions = read_decisions(queue)
    assert len(decisions) == 1
    assert decisions[0]["via"] == "agent"
    assert decisions[0]["action"] == "accepted"
    assert decisions[0]["outcome"] == "written"
    assert decisions[0]["destination"] == "ciao:memory"
    receipt_id = decisions[0]["receipt_id"]
    assert receipt_id
    journal = memory_receipts.journal_path(vault, guide_path(root))
    receipt = memory_receipts.find_receipt(journal, receipt_id)
    assert receipt is not None
    assert memory_receipts.is_undoable(receipt)


def test_accept_receipt_undo_restores_the_region(tmp_path: Path) -> None:
    from ciao import memory_receipts
    from ciao.workspace_guide import guide_path

    root, vault, queue = _accepted_install(tmp_path)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0
    receipt_id = read_decisions(queue)[0]["receipt_id"]

    memory_receipts.undo_receipt(
        receipt_id, vault_root=vault, journal=memory_receipts.journal_path(vault, guide_path(root))
    )

    assert "Prefers uv over pip for installs." not in guide_path(root).read_text(encoding="utf-8")


def test_accept_leaves_an_event_shaped_row_queued(tmp_path: Path, capsys) -> None:
    fact = "The user asked me to check the logs and I found the bug."
    root, vault, queue = _handover_install(tmp_path, "all", f"- [memory] {fact}")
    _store_prediction(queue, "accept", fact)
    from ciao import curation_run

    curation_run.begin_run(vault, holder="test-run", ttl_s=3600)
    before = queue.read_bytes()

    rc = _run_handover("memory-proposal-accept", root, vault, "asked me to check the logs")

    assert rc == 1
    assert "event" in capsys.readouterr().err
    assert queue.read_bytes() == before
    assert read_decisions(queue) == []


def test_accept_duplicate_removes_the_row_and_records_duplicate(tmp_path: Path) -> None:
    root, vault, queue = _accepted_install(tmp_path)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0
    # The same fact queued again, with a fresh prediction stamped after the first
    # decision: the earlier one is stale once a later decision exists (#1759), and
    # whether the accept lands in the same second as it must not matter.
    queue.write_text(
        queue.read_text(encoding="utf-8") + "\n" + _REGION_BULLET + "\n", encoding="utf-8"
    )
    _store_fresh_prediction(queue, "accept")

    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0

    decisions = read_decisions(queue)
    assert [row["outcome"] for row in decisions] == ["written", "duplicate"]
    assert _REGION_NEEDLE not in queue.read_text(encoding="utf-8")


def _smuggle_handover_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("memory_handover", "MEMORY_HANDOVER", "CIAO_MEMORY_HANDOVER"):
        monkeypatch.setenv(name, "all")


def test_accept_reads_handover_from_the_registry_not_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    root, vault, queue = _accepted_install(allowed)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0

    refused = tmp_path / "refused"
    refused.mkdir()
    root, vault, queue = _handover_install(refused, "off", _REGION_BULLET)
    _store_prediction(queue, "accept")
    _smuggle_handover_env(monkeypatch)
    before = queue.read_bytes()

    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert queue.read_bytes() == before


def test_dismiss_handover_reads_the_registry_not_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, vault, queue = _handover_install(tmp_path, "off", _REGION_BULLET)
    _store_prediction(queue, "dismiss")
    _smuggle_handover_env(monkeypatch)
    before = queue.read_bytes()

    assert _run_handover("memory-proposal-dismiss", root, vault, _REGION_NEEDLE, "--handover") == 1
    assert queue.read_bytes() == before
    assert read_decisions(queue) == []


# ── review round 1 (#1737) ────────────────────────────────────────────────


def _write_decision(queue: Path, text: str, ts: str, key: str = "dismissed_at") -> None:
    import json

    from ciao.memory_proposals import dismissed_log_path

    log = dismissed_log_path(queue)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({key: ts, "kind": "memory", "text": text, "via": "pwa"}) + "\n")


def test_handover_gate_refuses_a_prediction_the_operator_has_since_overruled(
    tmp_path: Path, capsys
) -> None:
    """A fact dismissed after it was predicted and then re-queued is a new question."""
    from ciao import curation_run

    root, vault, queue = _handover_install(tmp_path, "all", _REGION_BULLET)
    _store_prediction(queue, "accept")
    _write_decision(queue, "Prefers uv over pip for installs.", "2999-01-01T00:00:00+00:00")
    curation_run.begin_run(vault, holder="test-run", ttl_s=3600)
    before = queue.read_bytes()

    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert "no stored accept prediction" in capsys.readouterr().err
    assert queue.read_bytes() == before


def test_handover_gate_ignores_a_registry_under_the_workspace_argument(
    tmp_path: Path, capsys
) -> None:
    """A fake `.runtime/workspaces.json` named by --workspace cannot grant handover."""
    import json
    import shutil

    root, vault, queue = _handover_install(tmp_path, "off", _REGION_BULLET)
    _store_prediction(queue, "dismiss")
    fake = tmp_path / "fake"
    (fake / ".runtime").mkdir(parents=True)
    (fake / ".runtime" / "workspaces.json").write_text(
        json.dumps([{"name": "work", "vault_root": str(vault), "memory_handover": "all"}]),
        encoding="utf-8",
    )
    before = queue.read_bytes()

    rc = _run_handover("memory-proposal-dismiss", fake, vault, _REGION_NEEDLE, "--handover")

    assert rc == 1
    assert "handover refused" in capsys.readouterr().err
    assert queue.read_bytes() == before
    shutil.rmtree(fake)


def test_handover_gate_refuses_a_vault_that_is_not_the_registered_one(
    tmp_path: Path, capsys
) -> None:
    import json

    root, vault, queue = _handover_install(tmp_path, "all", _REGION_BULLET)
    other = root / "memory-vault" / "elsewhere"
    other_queue = other / "Workspace" / "Memory-Proposals.md"
    other_queue.parent.mkdir(parents=True)
    other_queue.write_text(queue.read_text(encoding="utf-8"), encoding="utf-8")
    registry = root / ".runtime" / "workspaces.json"
    registry.write_text(
        json.dumps([
            {"name": "work", "vault_root": str(vault), "memory_handover": "all"},
            {"name": "elsewhere", "vault_root": str(other)},
        ]),
        encoding="utf-8",
    )
    _store_prediction(other_queue, "dismiss")
    # A caller-side registry that names the unregistered vault as `work`.
    fake = tmp_path / "fake"
    (fake / ".runtime").mkdir(parents=True)
    (fake / ".runtime" / "workspaces.json").write_text(
        json.dumps([{"name": "work", "vault_root": str(other), "memory_handover": "all"}]),
        encoding="utf-8",
    )
    before = other_queue.read_bytes()

    rc = _run_handover("memory-proposal-dismiss", fake, other, _REGION_NEEDLE, "--handover")

    assert rc == 1
    assert other_queue.read_bytes() == before


def test_handover_gate_refuses_when_no_service_definition_names_an_install(
    tmp_path: Path,
) -> None:
    root, vault, queue = _handover_install(tmp_path, "all", _REGION_BULLET)
    _store_prediction(queue, "dismiss")
    _INSTALLED.clear()
    before = queue.read_bytes()

    assert _run_handover("memory-proposal-dismiss", root, vault, _REGION_NEEDLE, "--handover") == 1
    assert queue.read_bytes() == before


def test_accept_is_capped_per_curation_run(tmp_path: Path, capsys) -> None:
    from ciao.proposal_predictions import HANDOVER_ACCEPT_CAP

    facts = [f"Fact number {i} stays true for installs." for i in range(HANDOVER_ACCEPT_CAP + 1)]
    root, vault, queue = _handover_install(tmp_path, "all", *[f"- [memory] {f}" for f in facts])
    from ciao import curation_run

    curation_run.begin_run(vault, holder="test-run", ttl_s=3600)
    for fact in facts:
        _store_prediction(queue, "accept", fact)

    codes = [_run_handover("memory-proposal-accept", root, vault, fact) for fact in facts]

    assert codes == [0] * HANDOVER_ACCEPT_CAP + [1]
    assert "cap" in capsys.readouterr().err
    assert facts[-1] in queue.read_text(encoding="utf-8")
    assert len(read_decisions(queue)) == HANDOVER_ACCEPT_CAP


def test_accept_needs_a_live_curation_lease(tmp_path: Path, capsys) -> None:
    root, vault, queue = _handover_install(tmp_path, "all", _REGION_BULLET)
    _store_prediction(queue, "accept")
    before = queue.read_bytes()

    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert "curation run" in capsys.readouterr().err
    assert queue.read_bytes() == before


@pytest.mark.parametrize("command", ["memory-proposal-predict", "memory-proposal-accept"])
def test_new_commands_refuse_a_credential_adjacent_text_file(
    tmp_path: Path, capsys, command: str
) -> None:
    from ciao import cli

    root, vault, queue = _handover_install(tmp_path, "all", _REGION_BULLET)
    denied_path = root / ".runtime" / "needle.txt"
    denied_path.write_text(_REGION_NEEDLE, encoding="utf-8")
    argv = [
        command, "--workspace", str(root), "--vault-root", str(vault),
        "--text-file", str(denied_path),
    ]
    if command == "memory-proposal-predict":
        argv += ["--verdict", "accept", "--reason-file", str(_write(tmp_path, "r.txt", "why"))]

    assert cli.main(argv) == 2
    assert "credential" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["memory-proposal-predict", "memory-proposal-accept"])
def test_new_commands_do_not_echo_a_long_needle(tmp_path: Path, capsys, command: str) -> None:
    root, vault, queue = _handover_install(tmp_path, "all", _REGION_BULLET)
    needle = "NOSUCHROW" + "x" * 400
    extra = ["--verdict", "accept", "--reason-file", str(_write(tmp_path, "r.txt", "why"))]

    from ciao import cli

    argv = [
        command, "--workspace", str(root), "--vault-root", str(vault),
        "--text-file", str(_write(tmp_path, "long.txt", needle)),
    ]
    if command == "memory-proposal-predict":
        argv += extra
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "NOSUCHROW" in err
    assert "x" * 100 not in err


def test_cli_dismiss_refuses_a_nested_runtime_or_env_variant_reason_file(
    tmp_path: Path, capsys
) -> None:
    from ciao import cli

    vault, queue = _reason_queue(tmp_path)
    before = queue.read_bytes()
    nested = tmp_path / ".runtime" / "a" / "b" / "file.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("s3cret-token", encoding="utf-8")
    env_local = tmp_path / ".env.local"
    env_local.write_text("TOKEN=s3cret", encoding="utf-8")

    for path in (nested, env_local):
        rc = cli.main(_dismiss_args(tmp_path, vault, "--reason-file", str(path)))
        assert rc == 2
        assert "credential" in capsys.readouterr().err
        assert queue.read_bytes() == before
        assert _no_sidecar(queue)


def test_review_path_encodes_workspace_and_never_contains_proposal_text() -> None:
    from urllib.parse import parse_qs, urlsplit

    from ciao.proposal_tracking import proposal_review_path

    review_key = "prv_" + "ab" * 32
    path = proposal_review_path("wörk space", review_key)

    split = urlsplit(path)
    assert split.path == "/memory/review"
    assert parse_qs(split.query) == {
        "show": ["suggested"],
        "workspace": ["wörk space"],
        "proposal": [review_key],
    }
    assert set(parse_qs(split.query, keep_blank_values=True)) == {"show", "workspace", "proposal"}


def _scan_config(root: Path):  # noqa: ANN202 — CiaoConfig, imported lazily like the CLI
    from ciao.config import CiaoConfig

    return CiaoConfig.from_env({"CIAO_WORKSPACE": str(root), "PWA_AUTH_TOKEN": "t"})


def _write_skill_record(config, change: str) -> None:  # noqa: ANN001
    from ciao import skill_proposals

    skill_proposals.upsert_proposal(
        config,
        skill_proposals.SkillProposal(
            id=skill_proposals.proposal_id("work", "2026-08-09-defuddle"),
            workspace="work",
            skill="2026-08-09-defuddle",
            canonical_path="/agent/skills/2026-08-09-defuddle/SKILL.md",
            reviewed_revision="a" * 64,
            title="Skill reflection: 2026-08-09-defuddle",
            problem="Repeated fetch failures.",
            change=change,
            rationale="It handles blocked pages.",
            sources=(
                skill_proposals.SkillEvidence(
                    chat_id="sess-a1",
                    archive="2026-08-09T10:00:00Z",
                    turn="",
                    excerpt="outcome=needs_review corrections=1 errors=0 turns=3",
                ),
            ),
            lifecycle=skill_proposals.PENDING,
            chat_id="",
            updated_at="2026-08-09T10:00:00Z",
        ),
    )


def test_review_key_distinguishes_target_and_request(tmp_path: Path) -> None:
    from ciao.proposal_tracking import proposal_review_key, skill_review_key
    from ciao.web.proposal_service import _scan_proposal_rows

    root, _vault, queue = _handover_install(
        tmp_path,
        None,
        "- [people Mo] Mo likes tea.  _(from: Chat)_",
        "- [people Ada] Mo likes tea.  _(from: Chat)_",
        "- [learnings] Retry with backoff.  _(from: Chat)_  _(request: req-1)_",
        "- [learnings] Retry with backoff.  _(from: Chat)_  _(request: req-2)_",
        "- [note_edit sidecar-1] Tighten the intro.  _(from: Chat)_",
    )
    config = _scan_config(root)

    rows, _by_id = _scan_proposal_rows(config)

    keys = [row["review_key"] for row in rows]
    assert len(set(keys)) == 5
    assert keys[0] == proposal_review_key("work", "people", "Mo likes tea.", "Chat", "Mo", "")
    # A note_edit row with no sidecar on file has its target blanked, so the
    # key must come from the bullet's own sidecar id, never the enriched path.
    assert rows[4]["target"] != "sidecar-1"
    assert keys[4] == proposal_review_key(
        "work", "note_edit", "Tighten the intro.", "Chat", "sidecar-1", ""
    )
    # The decider is not part of the question: the same bullet settled by a
    # different decider keeps its key.
    queue.write_text(
        queue.read_text(encoding="utf-8").replace(
            "Mo likes tea.  _(from: Chat)_\n",
            "Mo likes tea.  _(from: Chat)_  _(decide: nightly; repeated)_\n",
            1,
        ),
        encoding="utf-8",
    )
    decided_rows, _ = _scan_proposal_rows(config)
    assert decided_rows[0]["review_key"] == keys[0]
    assert decided_rows[0]["decider"] == "nightly"

    # Skill rows: the key follows the change text, not the filename.
    _write_skill_record(config, "Add a defuddle fallback.")
    first = [r for r in _scan_proposal_rows(config)[0] if r["kind"] == "skill"][0]
    assert first["review_key"] == skill_review_key("work", first["id"], "Add a defuddle fallback.")

    _write_skill_record(config, "Add a different fallback.")
    second = [r for r in _scan_proposal_rows(config)[0] if r["kind"] == "skill"][0]
    assert second["id"] == first["id"]
    assert second["review_key"] != first["review_key"]


def test_review_path_matches_web_identity(tmp_path: Path, capsys) -> None:
    """List rows carry the same id and review link the PWA scan hands out."""
    import json

    from ciao import cli
    from ciao.proposal_tracking import stable_proposal_id
    from ciao.web.proposal_service import _scan_proposal_rows

    root, vault, queue = _handover_install(
        tmp_path,
        None,
        "- [people Mo] Mo likes tea.  _(from: Chat)_",
        "- [memory] Prefers tabs.  _(from: Chat)_",
        "- [memory] Prefers tabs.  _(from: Chat)_",
    )
    listing = ["memory-proposals", "--workspace", str(root), "--vault-root", str(vault), "--json"]

    assert cli.main(listing) == 0
    listed = json.loads(capsys.readouterr().out)["proposals"]
    web_rows, _by_id = _scan_proposal_rows(_scan_config(root))
    web = {row["id"]: row for row in web_rows}

    assert [row["id"] for row in listed] == list(web)
    for row in listed:
        assert row["review_key"] == web[row["id"]]["review_key"]
        assert row["review_path"] == web[row["id"]]["review_path"]
    # The action id bytes are the unchanged stable id, ordinal and all.
    assert listed[2]["id"] == stable_proposal_id(
        "work", "work/Workspace/Memory-Proposals.md", "memory", "Prefers tabs.", "Chat", 1
    )

    keys = {row["text"]: row["review_key"] for row in listed}
    # Reordering the bullets keeps every question's link.
    queue.write_text(
        "# Memory Proposals\n\n## 2026-01-01T00:00:00Z\n\n"
        "- [memory] Prefers tabs.  _(from: Chat)_\n"
        "- [people Mo] Mo likes tea.  _(from: Chat)_\n"
        "- [memory] Prefers tabs.  _(from: Chat)_\n",
        encoding="utf-8",
    )
    assert cli.main(listing) == 0
    reordered = json.loads(capsys.readouterr().out)["proposals"]
    assert {row["text"]: row["review_key"] for row in reordered} == keys

    # Deleting one of two equivalent duplicates keeps the survivor's link.
    queue.write_text(
        "# Memory Proposals\n\n## 2026-01-01T00:00:00Z\n\n"
        "- [people Mo] Mo likes tea.  _(from: Chat)_\n"
        "- [memory] Prefers tabs.  _(from: Chat)_\n",
        encoding="utf-8",
    )
    assert cli.main(listing) == 0
    survivor = json.loads(capsys.readouterr().out)["proposals"]
    assert [row["review_key"] for row in survivor if row["text"] == "Prefers tabs."] == [keys["Prefers tabs."]]


def test_add_review_path_only_for_pending_proposal(tmp_path: Path, capsys) -> None:
    import json

    from ciao import cli
    from ciao.memory_proposals import record_promotion
    from ciao.proposal_tracking import proposal_review_path
    from ciao.web.proposal_service import _scan_proposal_rows

    root, vault, queue = _handover_install(
        tmp_path, None, "- [people Mo] Mo likes tea.  _(from: Chat)_"
    )
    base = ["memory-proposal-add", "--workspace", str(root), "--vault-root", str(vault), "--json"]

    def add(text: str, *extra: str) -> dict:
        assert cli.main([*base, "--kind", "memory", *extra, text]) == 0
        return json.loads(capsys.readouterr().out)

    first = add("Prefers tabs.")
    assert first["queued"] is True
    assert first["review_key"].startswith("prv_") and len(first["review_key"]) == 68
    assert first["review_path"] == proposal_review_path("work", first["review_key"])
    web = {row["id"]: row for row in _scan_proposal_rows(_scan_config(root))[0]}
    assert web[first["id"]]["review_key"] == first["review_key"]
    assert web[first["id"]]["review_path"] == first["review_path"]

    duplicate = add("Prefers tabs.")
    assert duplicate["duplicate"] is True
    assert (duplicate["review_key"], duplicate["review_path"]) == (
        first["review_key"],
        first["review_path"],
    )

    # A multiline, delimiter-bearing fact links through the bullet it was stored as.
    tricky = add("Uses (uv) ] marks\nacross lines.")
    assert tricky["queued"] is True
    assert tricky["review_key"] is not None
    assert _web_row_key(root, tricky["id"]) == tricky["review_key"]

    assert cli.main([
        "memory-proposal-dismiss", "--workspace", str(root), "--vault-root", str(vault),
        "Prefers tabs.",
    ]) == 0
    capsys.readouterr()
    dismissed = add("Prefers tabs.")
    assert dismissed["dismissed_before"] is True
    assert (dismissed["id"], dismissed["review_key"], dismissed["review_path"]) == (None, None, None)

    record_promotion(queue, text="Keeps a lockfile.", kind="memory")
    promoted = add("Keeps a lockfile.")
    assert promoted["promoted_before"] is True
    assert promoted["review_key"] is None and promoted["review_path"] is None

    # Text-only dedupe suppresses a same-text fact aimed at another person.
    suppressed = add("Mo likes tea.", "--kind", "people", "--payload", "Ada")
    assert suppressed["duplicate"] is True
    assert suppressed["review_key"] is None and suppressed["review_path"] is None


def _web_row_key(root: Path, proposal_id: str) -> str:
    from ciao.web.proposal_service import _scan_proposal_rows

    rows, _by_id = _scan_proposal_rows(_scan_config(root))
    return next(row["review_key"] for row in rows if row["id"] == proposal_id)


def test_review_links_preserve_proposal_workspace_resolution(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import json

    from ciao import cli
    from ciao.proposal_tracking import proposal_review_path

    root, vault, _queue = _handover_install(
        tmp_path, None, "- [memory] Prefers tabs.  _(from: Chat)_"
    )

    assert cli.main([
        "memory-proposals", "--workspace", str(root), "--vault-root", str(vault), "--json",
    ]) == 0
    registered = json.loads(capsys.readouterr().out)["proposals"][0]
    assert registered["review_key"] is not None
    assert registered["review_path"] == proposal_review_path("work", registered["review_key"])

    # Implicit resolution, with no flags, names the same registered vault.
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    assert cli.main(["memory-proposals", "--json"]) == 0
    implicit = json.loads(capsys.readouterr().out)["proposals"][0]
    assert implicit["review_key"] == registered["review_key"]

    # A vault outside the registry, whose directory happens to be called `work`,
    # must not impersonate the registered owner.
    impostor = tmp_path / "elsewhere" / "work"
    (impostor / "Workspace").mkdir(parents=True)
    (impostor / "Workspace" / "Memory-Proposals.md").write_text(
        "# Memory Proposals\n\n- [memory] Prefers tabs.  _(from: Chat)_\n", encoding="utf-8"
    )
    assert cli.main([
        "memory-proposals", "--workspace", str(root), "--vault-root", str(impostor), "--json",
    ]) == 0
    loose = json.loads(capsys.readouterr().out)["proposals"][0]
    assert loose["text"] == "Prefers tabs."
    assert (loose["id"], loose["review_key"], loose["review_path"]) == (None, None, None)


def test_review_link_null_when_registry_config_invalid(tmp_path: Path, monkeypatch, capsys) -> None:
    import json

    from ciao import cli
    from ciao.config import CiaoConfig

    root, vault, _queue = _handover_install(
        tmp_path, None, "- [memory] Prefers tabs.  _(from: Chat)_"
    )
    # The explicit filesystem --workspace names the install; the vault comes from
    # the environment, so the resolver never reads the registry.
    monkeypatch.setenv("CIAO_VAULT_ROOT", str(vault))

    def unreadable(*_args, **_kwargs):
        raise ValueError("workspace registry is unreadable")

    monkeypatch.setattr(CiaoConfig, "from_env", unreadable)

    assert cli.main([
        "memory-proposal-add", "--workspace", str(root), "--json", "--kind", "memory", "Uses uv.",
    ]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["queued"] is True
    assert (added["id"], added["review_key"], added["review_path"]) == (None, None, None)

    assert cli.main(["memory-proposals", "--workspace", str(root), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["proposals"]
    assert {row["text"] for row in listed} == {"Prefers tabs.", "Uses uv."}
    assert all(
        (row["id"], row["review_key"], row["review_path"]) == (None, None, None)
        for row in listed
    )
