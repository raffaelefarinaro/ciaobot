"""The ``ciao memory-proposal-dismiss`` / ``memory-proposal-add`` CLI.

The nightly curation agent resolves proposals through the CLI, not the PWA, so
its decisions have to land in the same decision history the review page's
History tab reads, with ``via: "agent"``.
"""

from __future__ import annotations

from pathlib import Path

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
