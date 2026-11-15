"""The ``ciao memory-proposal-dismiss`` / ``memory-proposal-add`` CLI.

The nightly curation agent resolves proposals through the CLI, not the PWA, so
its decisions have to land in the same decision history the review page's
History tab reads, with ``via: "agent"``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.memory_proposals import read_decisions
from tests.cap_checkpoint_helpers import checkpoint_config


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


def test_cli_dismiss_preserves_queue_identity_and_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao import cli
    from ciao.proposal_tracking import stable_proposal_id
    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text("- [memory] durable lesson _(from: learning-review:abc@def)_\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_resolve_workspace_and_vaults", lambda args: (tmp_path, vault, tmp_path, "personal"))
    assert cli.main(["memory-proposal-dismiss", "--workspace", str(tmp_path),
                     "--vault-root", str(vault), "durable lesson"]) == 0
    decision = read_decisions(queue)[0]
    assert decision["source"] == "learning-review:abc@def" and decision["via"] == "agent"
    assert decision["proposal_id"] == stable_proposal_id(
        "personal", "personal/Workspace/Memory-Proposals.md", "memory",
        "durable lesson", "learning-review:abc@def", 0,
    )


def test_cli_replay_before_normal_preserves_header_sweep_and_reason(tmp_path: Path, monkeypatch) -> None:
    import threading
    from ciao import cli, memory_receipts as mr, proposal_actions
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.proposal_tracking import stable_proposal_id
    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    source = "learning-review:abc@def"
    queue.write_text("# Queue\n\n## 2026-11-14T12:00:00Z\n\n"
                     f"- [memory] A  fact _(from: {source})_\n\n", encoding="utf-8")
    reason = tmp_path / "reason.txt"
    reason.write_text("already resolved", encoding="utf-8")
    config = CiaoConfig(pwa_auth_token="test", workspace_root=tmp_path,
                        state_path=tmp_path / ".runtime/state.json", media_root=tmp_path / ".runtime/media",
                        vault_root=vault, workspaces={"personal": WorkspaceConfig(name="personal", vault_root=str(vault))})
    monkeypatch.setattr(cli, "_resolve_workspace_and_vaults", lambda args: (tmp_path, vault, tmp_path, "personal"))
    real = proposal_actions.record_decision
    entered = threading.Event()
    acquired = threading.Event()
    threads: list[threading.Thread] = []
    def replay_first(path, **kwargs):
        def competitor():
            entered.set()
            with mr.queue_lock(queue):
                acquired.set()
        thread = threading.Thread(target=competitor)
        threads.append(thread)
        thread.start()
        assert entered.wait(2) and not acquired.wait(0.2)
        mr.recover_pending(journal=mr.journal_path(vault, None), config=config)
        recovered = read_decisions(queue)
        assert len(recovered) == 1 and recovered[0]["reason"] == "already resolved" and recovered[0]["via"] == "agent"
        return real(path, **kwargs)
    monkeypatch.setattr(proposal_actions, "record_decision", replay_first)
    assert cli.main(["memory-proposal-dismiss", "--workspace", str(tmp_path), "--vault-root", str(vault),
                     "--reason-file", str(reason), "A  fact"]) == 0
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive() and acquired.is_set()
    rows = read_decisions(queue)
    assert len(rows) == 1
    assert rows[0]["via"] == "agent" and rows[0]["reason"] == "already resolved"
    assert rows[0]["source"] == source and rows[0]["identity_text"] == "A  fact"
    assert rows[0]["proposal_id"] == stable_proposal_id(
        "personal", "personal/Workspace/Memory-Proposals.md", "memory", "A  fact", source, 0)
    assert rows[0]["queue_receipt_id"]
    mr.recover_pending(journal=mr.journal_path(vault, None), config=config)
    assert len(read_decisions(queue)) == 1


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


def test_accept_without_checkpoint_does_not_require_safe_index_location(tmp_path: Path, monkeypatch) -> None:
    import tempfile
    root, vault, queue = _accepted_install(tmp_path)
    system_temp = vault / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0
    assert _REGION_NEEDLE not in queue.read_text()


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


def test_cap_checkpoint_uses_real_receipt_and_deduplicates_same_event(tmp_path: Path) -> None:
    import json
    from tests.cap_checkpoint_helpers import build_checkpoint, validate_checkpoint
    from ciao import memory_proposals, memory_receipts as mr, proposal_predictions as pp
    root, vault, queue = _accepted_install(tmp_path)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0
    sidecar = memory_proposals.dismissed_log_path(queue)
    line = sidecar.read_bytes()
    snapshots = {".dismissed.jsonl": line + line, ".dismissed.log": None}
    receipts = mr.read_receipts(mr.journal_path(vault, None))
    checkpoint = build_checkpoint(queue, snapshots, receipts)
    assert len(checkpoint["events"]) == 1
    assert checkpoint["events"][0]["receipt_id"] == read_decisions(queue)[0]["queue_receipt_id"]
    assert validate_checkpoint(checkpoint, snapshots)
    target = next(r for r in receipts if r["id"] == checkpoint["events"][0]["receipt_id"])
    broken = [{**r, "owner_landed": False} if r is target else r for r in receipts]
    with pytest.raises(ValueError, match="receipt"):
        build_checkpoint(queue, snapshots, broken)
    with pytest.raises(ValueError, match="duplicate receipt identity"):
        build_checkpoint(queue, snapshots, receipts + [target])
    legacy_first = {".dismissed.jsonl": None, ".dismissed.log": line}
    prior = build_checkpoint(queue, legacy_first, receipts)
    moved_duplicate = {".dismissed.jsonl": line, ".dismissed.log": line}
    updated = build_checkpoint(queue, moved_duplicate, receipts + [prior], previous_checkpoint=prior)
    assert updated["events"] == prior["events"]
    assert validate_checkpoint(updated, moved_duplicate)
    changed = json.loads(line)
    changed["promoted_at"] = "2030-01-01T00:00:00Z"
    snapshots[".dismissed.jsonl"] += (json.dumps(changed) + "\n").encode()
    with pytest.raises(ValueError, match="duplicate"):
        build_checkpoint(queue, snapshots, receipts)


def test_checkpoint_second_generation_after_real_receipt_compacted(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_proposals, memory_receipts as mr, proposal_predictions as pp
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    sidecar = memory_proposals.dismissed_log_path(queue)
    assert mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    first = next(row for row in mr.read_receipts(journal) if row.get("kind") == "cap_event_checkpoint")
    first_id = first["events"][0]["receipt_id"]
    assert first_id not in {r["id"] for r in mr.read_receipts(journal)}
    assert _run_handover("memory-proposal-accept", root, vault, second) == 0
    for i in range(3):
        mr._append(journal, {"id": f"later-noise-{i}", "kind": "other", "status": mr.APPLIED, "padding": "x" * 512})
    assert mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    receipts = mr.selected_queue_receipts_for_gate(journal)
    second_checkpoint = next(row for row in receipts if row.get("kind") == "cap_event_checkpoint")
    assert len(second_checkpoint["events"]) == 2
    assert first_id in {event["receipt_id"] for event in second_checkpoint["events"]}
    assert pp.stream_checkpoint_events(queue, second_checkpoint, receipts, config=checkpoint_config(queue))[0] == second_checkpoint["events"]
    original = sidecar.read_bytes()
    sidecar.unlink()
    with pytest.raises(ValueError, match="checkpoint"):
        pp.stream_checkpoint_events(queue, second_checkpoint, receipts, config=checkpoint_config(queue))
    sidecar.write_bytes(original.replace(b"Prefers uv", b"Changed uv", 1))
    with pytest.raises(ValueError, match="checkpoint"):
        pp.stream_checkpoint_events(queue, second_checkpoint, receipts, config=checkpoint_config(queue))
    sidecar.write_bytes(original)
    assert len(read_decisions(queue)) == 2


@pytest.mark.parametrize("case", ["enclosing", "other-workspace"])
def test_compaction_refuses_configured_backup_overlap(tmp_path: Path, monkeypatch, case: str) -> None:
    import tempfile
    from ciao import memory_receipts as mr
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.os_support.users import user_key
    root, vault, queue, journal, _ = _compaction_fault_fixture(tmp_path, monkeypatch)
    before = journal.read_bytes()
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    configured_vault = tmp_path if case == "enclosing" else vault
    other = system_temp if case == "other-workspace" else tmp_path / "other"
    config = CiaoConfig(
        pwa_auth_token="test", workspace_root=root, state_path=root / ".runtime" / "state.json",
        media_root=root / ".runtime" / "media", vault_root=configured_vault,
        workspaces={"work": WorkspaceConfig(name="work", vault_root=str(vault)),
                    "other": WorkspaceConfig(name="other", vault_root=str(other))},
    )
    with pytest.raises(ValueError, match="configured backup root"):
        mr.compact_cap_checkpoint(queue, vault, config=config)
    assert journal.read_bytes() == before
    assert not (system_temp / f"ciao-cap-index-{user_key()}").exists()


def test_checkpoint_compaction_allows_later_real_accepts_at_small_limit(tmp_path: Path, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta
    from ciao import curation_run, memory_proposals, memory_receipts as mr
    root, vault, queue = _handover_install(tmp_path, "all")
    monkeypatch.setattr(mr, "MAX_BYTES", 1024)
    monkeypatch.setattr(mr, "KEEP_LINES", 2)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=20)
    for i in range(10):
        instant = base + timedelta(seconds=i)
        class DecisionClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant if tz is not None else instant.replace(tzinfo=None)
        monkeypatch.setattr(memory_proposals, "datetime", DecisionClock)
        fact = f"Prefers build option number {i} for projects."
        with queue.open("a", encoding="utf-8") as handle:
            handle.write(f"- [memory] {fact} _(from: Decisions)_\n")
        _store_prediction(queue, "accept", text=fact)
        holder = f"run-{i}"
        holder = curation_run.begin_run(vault, holder=holder, ttl_s=3600, now=instant).holder
        assert _run_handover("memory-proposal-accept", root, vault, fact) == 0
        curation_run.end_run(vault, holder=holder)
        mr._append(mr.journal_path(vault, None), {"id": f"noise-{i}", "kind": "other", "status": mr.APPLIED, "padding": "x" * 256})
    assert len(read_decisions(queue)) == 10
    assert mr.journal_path(vault, None).stat().st_size < 16 * mr.MAX_BYTES
    receipts = mr.read_receipts(mr.journal_path(vault, None))
    assert any(r.get("kind") == "cap_event_checkpoint" for r in receipts)
    first_id = read_decisions(queue)[0]["queue_receipt_id"]
    assert first_id not in {r["id"] for r in receipts}


def test_compacted_cap_deduplicates_and_handles_lease_change_during_accept(tmp_path: Path, monkeypatch) -> None:
    import threading
    from datetime import UTC, datetime
    from ciao import curation_run, memory_proposals, memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue = _handover_install(tmp_path, "all")
    monkeypatch.setattr(mr, "MAX_BYTES", 1024)
    monkeypatch.setattr(mr, "KEEP_LINES", 2)
    instant = datetime.now(UTC).replace(microsecond=0)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is not None else instant.replace(tzinfo=None)
    monkeypatch.setattr(memory_proposals, "datetime", FixedDatetime)
    old_holder = curation_run.begin_run(vault, holder="old", ttl_s=3600, now=instant).holder
    def add_and_predict(i):
        fact = f"Prefers build option number {i} for projects."
        with queue.open("a", encoding="utf-8") as handle:
            handle.write(f"- [memory] {fact} _(from: Decisions)_\n")
        _store_prediction(queue, "accept", text=fact)
        return fact
    for i in range(4):
        assert _run_handover("memory-proposal-accept", root, vault, add_and_predict(i)) == 0
    sidecar = memory_proposals.dismissed_log_path(queue)
    first_line = sidecar.read_bytes().splitlines(keepends=True)[0]
    with sidecar.open("ab") as handle:
        handle.write(first_line)
    real_accept = memory_proposals.accept_region_fact
    attempted, completed = threading.Event(), threading.Event()
    def transition():
        attempted.set()
        curation_run.end_run(vault, holder=old_holder)
        curation_run.begin_run(vault, holder="new", ttl_s=3600, now=instant)
        completed.set()
    worker = threading.Thread(target=transition)
    def switch_lease(*args, **kwargs):
        worker.start()
        assert attempted.wait(2) and not completed.wait(0.2)
        return real_accept(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(memory_proposals, "accept_region_fact", switch_lease)
        assert _run_handover("memory-proposal-accept", root, vault, add_and_predict(4)) == 0
    worker.join(3)
    assert completed.is_set()
    sixth = add_and_predict(5)
    before = guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, sixth) == 1
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


def _compaction_fault_fixture(tmp_path: Path, monkeypatch):
    from ciao import memory_receipts as mr
    root, vault, queue = _accepted_install(tmp_path)
    second = "Keeps a concise changelog."
    with queue.open("a", encoding="utf-8") as handle:
        handle.write(f"- [memory] {second} _(from: Decisions)_\n")
    _store_prediction(queue, "accept", text=second)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0
    monkeypatch.setattr(mr, "MAX_BYTES", 1024)
    monkeypatch.setattr(mr, "KEEP_LINES", 2)
    journal = mr.journal_path(vault, None)
    for i in range(3):
        mr._append(journal, {"id": f"noise-{i}", "kind": "other", "status": mr.APPLIED, "padding": "x" * 512})
    return root, vault, queue, journal, second


def _second_compaction_fault_fixture(tmp_path: Path, monkeypatch):
    from ciao import memory_receipts as mr
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    assert mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    assert _run_handover("memory-proposal-accept", root, vault, second) == 0
    third = "Keeps review notes concise."
    with queue.open("a", encoding="utf-8") as handle:
        handle.write(f"- [memory] {third} _(from: Decisions)_\n")
    _store_prediction(queue, "accept", text=third)
    for i in range(3):
        mr._append(journal, {"id": f"second-noise-{i}", "kind": "other", "status": mr.APPLIED, "padding": "x" * 512})
    return root, vault, queue, journal, third


def test_accept_reports_ambiguous_checkpoint_slot_for_manual_inspection(tmp_path: Path, monkeypatch, capsys) -> None:
    from ciao import proposal_predictions as pp
    root, vault, queue, _journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime-slot"
    runtime.mkdir(mode=0o700)
    slot = runtime / pp._CAP_OPERATION_SLOT
    slot.mkdir(mode=0o700)
    (slot / "unknown").write_text("preserve")
    monkeypatch.setattr(pp, "_cap_runtime", lambda queue, config: runtime)
    capsys.readouterr()
    for _ in range(2):
        assert _run_handover("memory-proposal-accept", root, vault, second) == 1
        error = capsys.readouterr().err
        assert "runtime slot needs manual inspection" in error and str(slot) in error
        assert "before retrying" in error
        assert second in queue.read_text()
    assert (slot / "unknown").read_text() == "preserve"
    assert list(runtime.glob("op-*")) == [slot]
    guide = (Path(__file__).resolve().parents[1] / "docs" / "DEVELOPMENT.md").read_text()
    assert "lock_exclusive(fd, blocking=False)" in guide
    assert "preserve anything whose ownership is uncertain" in guide
    assert "move or rename that exact slot" in guide


def test_checkpointed_accept_with_history_past_old_total_limit(tmp_path: Path, monkeypatch) -> None:
    import json
    from ciao import memory_proposals, memory_receipts as mr
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    assert mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    assert any(row.get("kind") == "cap_event_checkpoint" for row in mr.read_receipts(journal))
    row = json.dumps({"text": "unrelated " + "x" * 100, "kind": "memory",
                      "dismissed_at": "2025-01-01T00:00:00Z"}) + "\n"
    with queue.with_suffix(".dismissed.log").open("a", encoding="utf-8") as handle:
        handle.write(row * 10)
    assert queue.with_suffix(".dismissed.log").stat().st_size > 512
    assert _run_handover("memory-proposal-accept", root, vault, second) == 0


def test_cli_second_checkpoint_compacts_above_old_history_limit(tmp_path: Path, monkeypatch) -> None:
    import json
    from ciao import memory_proposals, memory_receipts as mr
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    assert mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    assert _run_handover("memory-proposal-accept", root, vault, second) == 0
    third = "Keeps review notes concise."
    with queue.open("a", encoding="utf-8") as handle:
        handle.write(f"- [memory] {third} _(from: Decisions)_\n")
    _store_prediction(queue, "accept", text=third)
    for i in range(3):
        mr._append(journal, {"id": f"later-{i}", "kind": "other", "status": mr.APPLIED, "padding": "x" * 512})
    sidecar = queue.with_suffix(".dismissed.log")
    with sidecar.open("a", encoding="utf-8") as handle:
        handle.write((json.dumps({"text": "old " + "x" * 100, "dismissed_at": "2020-01-01T00:00:00Z"}) + "\n") * 10)
    assert sidecar.stat().st_size > 512
    assert _run_handover("memory-proposal-accept", root, vault, third) == 0
    checkpoint = next(row for row in mr.read_receipts(journal) if row.get("kind") == "cap_event_checkpoint")
    assert checkpoint["sidecars"][".dismissed.log"]["length"] == sidecar.stat().st_size


def test_checkpointed_budget_streams_unrelated_history_bounded(tmp_path: Path) -> None:
    import json
    import tracemalloc
    from tests.cap_checkpoint_helpers import build_checkpoint
    from ciao import cli, memory_receipts as mr, proposal_predictions as pp
    root, vault, queue = _accepted_install(tmp_path)
    sidecar = queue.with_suffix(".dismissed.log")
    line = (json.dumps({"text": "old", "via": "agent", "kind": "memory",
                        "promoted_at": "2020-01-01T00:00:00Z"}) + "\n").encode()
    sidecar.write_bytes(line)
    checkpoint = build_checkpoint(queue, {".dismissed.jsonl": None, ".dismissed.log": line}, [])
    mr._append(mr.journal_path(vault, None), checkpoint)
    with sidecar.open("ab") as handle:
        for _ in range(30000):
            handle.write(b'{"text":"unrelated"}\n')
    tracemalloc.start()
    try:
        with mr.queue_lock(queue):
            assert cli._handover_accept_budget(vault, queue, config=checkpoint_config(queue)) == ""
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024


@pytest.mark.parametrize("conflict", [False, True])
def test_checkpointed_accept_checks_old_compacted_receipt_replay(tmp_path: Path, monkeypatch, conflict: bool) -> None:
    from ciao import memory_proposals, memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    original = memory_proposals.dismissed_log_path(queue).read_bytes().splitlines(keepends=True)[0]
    rid = read_decisions(queue)[0]["queue_receipt_id"]
    assert mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    assert rid not in {row["id"] for row in mr.read_receipts(journal)}
    with queue.with_suffix(".dismissed.log").open("ab") as handle:
        handle.write(original.replace(b"Prefers uv", b"Changed uv") if conflict else original)
    before = guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, second) == (1 if conflict else 0)
    if conflict:
        assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


@pytest.mark.skipif(__import__("os").name == "nt", reason="Windows may prevent replacing an open file")
def test_checkpoint_rescan_rejects_sidecar_replacement(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_proposals, memory_receipts as mr, proposal_predictions as pp
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    assert mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    rows = mr.selected_queue_receipts_for_gate(journal)
    checkpoint = next(row for row in rows if row.get("kind") == "cap_event_checkpoint")
    sidecar = memory_proposals.dismissed_log_path(queue)
    replacement = sidecar.with_name("replacement")
    replacement.write_bytes(sidecar.read_bytes())
    original = memory_proposals.DecisionSidecarStream.__iter__
    calls = []
    def replace_during_rescan(stream):
        if stream.path == sidecar:
            calls.append(True)
        for item in original(stream):
            if stream.path == sidecar and len(calls) == 2 and replacement.exists():
                replacement.replace(sidecar)
            yield item
    monkeypatch.setattr(memory_proposals.DecisionSidecarStream, "__iter__", replace_during_rescan)
    with pytest.raises(ValueError):
        pp.stream_checkpoint_events(queue, checkpoint, rows, config=checkpoint_config(queue))
    assert len(calls) >= 2 and not replacement.exists()


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_actual_compaction_detects_sidecar_loss(tmp_path: Path, monkeypatch, damage: str) -> None:
    from ciao import memory_proposals, memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    first_receipt = read_decisions(queue)[0]["queue_receipt_id"]
    assert mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    assert first_receipt not in {r["id"] for r in mr.read_receipts(journal)}
    sidecar = memory_proposals.dismissed_log_path(queue)
    if damage == "missing":
        sidecar.unlink()
    else:
        sidecar.write_bytes(sidecar.read_bytes().replace(b"Prefers uv", b"Changed uv", 1))
    before = guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


def test_compaction_streams_preexisting_oversized_journal(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_receipts as mr
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    for i in range(25):
        mr._append(journal, {"id": f"extra-{i}", "kind": "other", "status": mr.APPLIED, "padding": "x" * 512})
    assert 16 * mr.MAX_BYTES < journal.stat().st_size < 64 * mr.MAX_BYTES
    assert _run_handover("memory-proposal-accept", root, vault, second) == 0
    assert journal.stat().st_size < 16 * mr.MAX_BYTES


def test_refused_handover_does_not_compact_journal(tmp_path: Path, monkeypatch) -> None:
    import json
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    registry = root / ".runtime" / "workspaces.json"
    data = json.loads(registry.read_text())
    data[0]["memory_handover"] = "off"
    registry.write_text(json.dumps(data))
    before = journal.read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert (journal.read_bytes(), queue.read_bytes()) == before


def test_compaction_above_streaming_limit_fails_without_effect(tmp_path: Path, monkeypatch) -> None:
    import json
    from ciao import memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    with journal.open("ab") as handle:
        for i in range(150):
            handle.write((json.dumps({"id": f"bulk-{i}", "kind": "other", "status": mr.APPLIED,
                                      "padding": "x" * 512}) + "\n").encode())
    assert journal.stat().st_size > 64 * mr.MAX_BYTES
    before = journal.read_bytes(), guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert (journal.read_bytes(), guide_path(root).read_bytes(), queue.read_bytes()) == before


def test_compaction_preserves_unresolved_and_rejects_malformed_evidence(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    mr._append(journal, {"id": "unresolved", "kind": "queue_resolve", "status": mr.CONFLICT,
                         "proposal_id": "p", "queue": str(queue)})
    assert mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    assert any(r.get("id") == "unresolved" and r.get("status") == mr.CONFLICT for r in mr.read_receipts(journal))
    with journal.open("ab") as handle:
        handle.write(b'{"id":"malformed"\n')
    before = journal.read_bytes(), guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert (journal.read_bytes(), guide_path(root).read_bytes(), queue.read_bytes()) == before


def test_compaction_does_not_drop_invalid_selected_receipt(tmp_path: Path, monkeypatch) -> None:
    import json
    from ciao import memory_receipts as mr
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    selected = next(row for row in rows if row.get("kind") == "queue_resolve" and row.get("status") == mr.APPLIED
                    and row.get("proposal_id"))
    selected["after_revision"] = "invalid"
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows))
    before = journal.read_bytes()
    assert not mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    assert journal.read_bytes() == before
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1


def test_compaction_keeps_recent_full_undo_receipt(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_receipts as mr
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    receipt = {"id": "recent-note", "kind": "note_apply", "status": mr.APPLIED,
               "before_text": "before", "after_text": "after", "workspace": "work"}
    mr._append(journal, receipt)
    assert mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    kept = next(r for r in mr.read_receipts(journal) if r.get("id") == "recent-note")
    assert kept["before_text"] == "before" and kept["after_text"] == "after"
    assert mr.is_undoable(kept)


@pytest.mark.parametrize("generation", [1, 2])
@pytest.mark.parametrize("failure", ["sidecar_fsync", "temp_fsync", "before_replace", "after_replace"])
def test_compaction_fault_keeps_old_or_new_evidence(tmp_path: Path, monkeypatch, failure: str, generation: int) -> None:
    import os
    import stat
    from ciao import memory_proposals, memory_receipts as mr
    from ciao.workspace_guide import guide_path
    fixture = _compaction_fault_fixture if generation == 1 else _second_compaction_fault_fixture
    root, vault, queue, journal, second = fixture(tmp_path, monkeypatch)
    sidecar = memory_proposals.dismissed_log_path(queue)
    sidecar_inode = sidecar.stat().st_ino
    original_sync, original_replace = os.fsync, os.replace
    before_journal = journal.read_bytes()
    before = guide_path(root).read_bytes(), queue.read_bytes()
    faults = []
    def sync(fd):
        if ((failure == "sidecar_fsync" and os.fstat(fd).st_ino == sidecar_inode)
                or (failure == "temp_fsync" and stat.S_ISREG(os.fstat(fd).st_mode)
                    and os.fstat(fd).st_ino != sidecar_inode)):
            faults.append(True)
            raise OSError("sidecar fsync failed")
        return original_sync(fd)
    def replace(source, target):
        if Path(source).name.startswith(".cap-checkpoint-"):
            faults.append(True)
            if failure == "after_replace":
                original_replace(source, target)
            raise OSError("checkpoint replace failed")
        return original_replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", sync)
        if failure in ("before_replace", "after_replace"):
            patch.setattr(os, "replace", replace)
        assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert faults and (guide_path(root).read_bytes(), queue.read_bytes()) == before
    if failure != "after_replace":
        assert journal.read_bytes() == before_journal
    else:
        assert journal.read_bytes() != before_journal
        assert any(r.get("kind") == "cap_event_checkpoint" for r in mr.read_receipts(journal))
    assert _run_handover("memory-proposal-accept", root, vault, second) == 0


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX directory fencing")
@pytest.mark.parametrize("generation", [1, 2])
def test_compaction_directory_fence_error_after_replace_fails_closed(tmp_path: Path, monkeypatch, generation: int) -> None:
    import os
    import stat
    from ciao import memory_receipts as mr
    from ciao.workspace_guide import guide_path
    fixture = _compaction_fault_fixture if generation == 1 else _second_compaction_fault_fixture
    root, vault, queue, journal, second = fixture(tmp_path, monkeypatch)
    before = guide_path(root).read_bytes(), queue.read_bytes()
    real_fsync = os.fsync
    directories = []
    def fail_second_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directories.append(True)
            if len(directories) == 2:
                raise OSError("journal directory fence failed")
        return real_fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_second_directory)
        assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert len(directories) == 2
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before
    assert any(row.get("kind") == "cap_event_checkpoint" for row in mr.read_receipts(journal))
    assert _run_handover("memory-proposal-accept", root, vault, second) == 0


def test_compaction_keeps_queue_lock_against_concurrent_writer(tmp_path: Path, monkeypatch) -> None:
    import threading
    from ciao import memory_receipts as mr, proposal_predictions
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    entered, acquired = threading.Event(), threading.Event()
    workers = []
    real = proposal_predictions.stream_checkpoint_events
    def fenced(*args, **kwargs):
        def competing():
            entered.set()
            with mr.queue_lock(queue):
                acquired.set()
        thread = threading.Thread(target=competing)
        workers.append(thread)
        thread.start()
        assert entered.wait(2) and not acquired.wait(0.2)
        return real(*args, **kwargs)
    monkeypatch.setattr(proposal_predictions, "stream_checkpoint_events", fenced)
    assert mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    workers[0].join(3)
    assert acquired.is_set() and not workers[0].is_alive()


def test_compaction_refuses_external_journal_change_before_replace(tmp_path: Path, monkeypatch) -> None:
    import json
    import os
    import stat
    from ciao import memory_proposals, memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue, journal, second = _compaction_fault_fixture(tmp_path, monkeypatch)
    sidecar = memory_proposals.dismissed_log_path(queue)
    sidecar_inode = sidecar.stat().st_ino
    original = os.fsync
    changed = []
    def interpose(fd):
        result = original(fd)
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_ino != sidecar_inode and not changed:
            with journal.open("ab") as handle:
                handle.write((json.dumps({"id": "external", "kind": "other", "status": mr.APPLIED}) + "\n").encode())
            changed.append(True)
        return result
    before = guide_path(root).read_bytes(), queue.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", interpose)
        assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert changed and any(row.get("id") == "external" for row in mr.read_receipts(journal))
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


@pytest.mark.parametrize("generation", [1, 2])
def test_compaction_refuses_sidecar_change_after_temp_fence(tmp_path: Path, monkeypatch, generation: int) -> None:
    import os
    import stat
    from ciao import memory_proposals
    from ciao.workspace_guide import guide_path
    fixture = _compaction_fault_fixture if generation == 1 else _second_compaction_fault_fixture
    root, vault, queue, journal, fact = fixture(tmp_path, monkeypatch)
    sidecar = memory_proposals.dismissed_log_path(queue)
    sidecar_inode = sidecar.stat().st_ino
    before = journal.read_bytes(), guide_path(root).read_bytes(), queue.read_bytes()
    real_fsync = os.fsync
    changed = []
    def mutate_after_temp(fd):
        result = real_fsync(fd)
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_ino != sidecar_inode and not changed:
            with sidecar.open("ab") as handle:
                handle.write(b'{"text":"concurrent"}\n')
            changed.append(True)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", mutate_after_temp)
        assert _run_handover("memory-proposal-accept", root, vault, fact) == 1
    assert changed
    assert (journal.read_bytes(), guide_path(root).read_bytes(), queue.read_bytes()) == before


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="process-crash test needs fork")
@pytest.mark.parametrize("generation", [1, 2])
@pytest.mark.parametrize("after_replace", [False, True])
def test_compaction_process_crash_keeps_old_or_new_journal(tmp_path: Path, monkeypatch, after_replace: bool, generation: int) -> None:
    import os
    from ciao import memory_receipts as mr
    from ciao.workspace_guide import guide_path
    fixture = _compaction_fault_fixture if generation == 1 else _second_compaction_fault_fixture
    root, vault, queue, journal, second = fixture(tmp_path, monkeypatch)
    old = journal.read_bytes()
    before = guide_path(root).read_bytes(), queue.read_bytes()
    pid = os.fork()
    if pid == 0:
        real_replace = os.replace
        def crash_replace(source, target):
            if Path(source).name.startswith(".cap-checkpoint-"):
                if after_replace:
                    real_replace(source, target)
                os._exit(42)
            return real_replace(source, target)
        os.replace = crash_replace
        _run_handover("memory-proposal-accept", root, vault, second)
        os._exit(2)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 42
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before
    if after_replace:
        assert journal.read_bytes() != old
        assert any(row.get("kind") == "cap_event_checkpoint" for row in mr.read_receipts(journal))
    else:
        assert journal.read_bytes() == old
    assert _run_handover("memory-proposal-accept", root, vault, second) == 0


@pytest.mark.parametrize("generation", [1, 2])
def test_compaction_windows_process_crash_fence_mode(tmp_path: Path, monkeypatch, generation: int) -> None:
    import os
    import stat
    from ciao import memory_proposals, memory_receipts as mr
    fixture = _compaction_fault_fixture if generation == 1 else _second_compaction_fault_fixture
    root, vault, queue, journal, second = fixture(tmp_path, monkeypatch)
    synced = []
    class WindowsOS:
        name = "nt"
        def __getattr__(self, name):
            return getattr(os, name)
        def fsync(self, fd):
            synced.append(os.fstat(fd).st_mode)
            return os.fsync(fd)
    monkeypatch.setattr(mr, "os", WindowsOS())
    monkeypatch.setattr(memory_proposals, "os", WindowsOS())
    assert mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    assert len(synced) >= 2 and all(stat.S_ISREG(mode) for mode in synced)
    assert any(row.get("kind") == "cap_event_checkpoint" for row in mr.read_receipts(journal))
    assert _run_handover("memory-proposal-accept", root, vault, second) == 0


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="process-crash simulation needs fork")
@pytest.mark.parametrize("generation", [1, 2])
def test_windows_mode_process_crash_after_checkpoint_replace(tmp_path: Path, monkeypatch, generation: int) -> None:
    import os
    from ciao import memory_proposals, memory_receipts as mr
    from ciao.workspace_guide import guide_path
    fixture = _compaction_fault_fixture if generation == 1 else _second_compaction_fault_fixture
    root, vault, queue, journal, second = fixture(tmp_path, monkeypatch)
    before = guide_path(root).read_bytes(), queue.read_bytes()
    pid = os.fork()
    if pid == 0:
        class WindowsOS:
            name = "nt"
            def __getattr__(self, name):
                return getattr(os, name)
            def replace(self, source, target):
                if Path(source).name.startswith(".cap-checkpoint-"):
                    os.replace(source, target)
                    os._exit(42)
                return os.replace(source, target)
        mr.os = WindowsOS()
        memory_proposals.os = WindowsOS()
        _run_handover("memory-proposal-accept", root, vault, second)
        os._exit(2)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 42
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before
    assert any(row.get("kind") == "cap_event_checkpoint" for row in mr.read_receipts(journal))
    assert _run_handover("memory-proposal-accept", root, vault, second) == 0


@pytest.mark.parametrize("damage", ["none", "missing", "changed"])
def test_accept_gate_checks_persisted_checkpoint_sidecar_binding(tmp_path: Path, capsys, monkeypatch, damage: str) -> None:
    import json
    from tests.cap_checkpoint_helpers import build_checkpoint
    from ciao import memory_proposals, memory_receipts as mr, proposal_predictions as pp
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    sidecar = memory_proposals.dismissed_log_path(queue)
    row = {"text": "Old fact", "kind": "memory", "via": "agent", "promoted_at": "2020-01-01T00:00:00Z"}
    raw = (json.dumps(row) + "\n").encode()
    sidecar.write_bytes(raw)
    checkpoint = build_checkpoint(queue, {".dismissed.jsonl": raw, ".dismissed.log": None}, [])
    mr._append(mr.journal_path(vault, None), checkpoint)
    if damage == "missing":
        sidecar.unlink()
    elif damage == "changed":
        sidecar.write_bytes(raw.replace(b"Old fact", b"Bad fact"))
    calls = []
    real = pp.stream_checkpoint_events
    def observe(*args, **kwargs):
        calls.append(args[1])
        return real(*args, **kwargs)
    monkeypatch.setattr(pp, "stream_checkpoint_events", observe)
    before = guide_path(root).read_bytes(), queue.read_bytes()
    rc = _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE)
    assert calls == [checkpoint]
    if damage == "none":
        assert rc == 0
    else:
        assert rc == 1 and "checkpoint" in capsys.readouterr().err.lower()
        assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


def test_checkpoint_cannot_be_trimmed_or_hidden_by_same_id(tmp_path: Path, monkeypatch, capsys) -> None:
    import json
    from tests.cap_checkpoint_helpers import build_checkpoint
    from ciao import memory_proposals, memory_receipts as mr, proposal_predictions as pp
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    sidecar = memory_proposals.dismissed_log_path(queue)
    raw = (json.dumps({"text": "Old fact"}) + "\n").encode()
    sidecar.write_bytes(raw)
    journal = mr.journal_path(vault, None)
    checkpoint = build_checkpoint(queue, {".dismissed.jsonl": raw, ".dismissed.log": None}, [])
    mr._append(journal, checkpoint)
    monkeypatch.setattr(mr, "MAX_BYTES", 128)
    monkeypatch.setattr(mr, "KEEP_LINES", 1)
    for i in range(3):
        mr._append(journal, {"id": f"noise-{i}", "kind": "other", "status": mr.APPLIED, "padding": "x" * 100})
    assert checkpoint["id"] in {r["id"] for r in mr.read_receipts(journal)}
    mr._append(journal, {"id": checkpoint["id"], "kind": "other", "status": mr.APPLIED})
    before = guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert "receipt journal cannot be verified" in capsys.readouterr().err
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


@pytest.mark.parametrize("command", ["accept", "dismiss"])
@pytest.mark.parametrize("status", ["applied", "prepared"])
def test_cli_queue_receipt_fault_reports_real_outcome(tmp_path: Path, monkeypatch, capsys, command: str, status: str) -> None:
    from ciao import memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    original = mr._append
    failures = []
    def fail(path, payload, *args, **kwargs):
        if (payload.get("kind") == "queue_resolve" and payload.get("status") == status
                and (status == "prepared" or payload.get("owner_landed"))):
            failures.append(True)
            raise OSError("injected queue receipt failure")
        return original(path, payload, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(mr, "_append", fail)
        rc = _run_handover(f"memory-proposal-{command}", root, vault, _REGION_NEEDLE)
    assert rc == 1 and failures
    output = capsys.readouterr().err.lower()
    decisions = read_decisions(queue)
    if status == "applied":
        assert _REGION_NEEDLE not in queue.read_text()
        assert len(decisions) == 1
        assert decisions[0]["action"] == ("accepted" if command == "accept" else "dismissed")
        assert decisions[0]["via"] == "agent" and decisions[0]["queue_receipt_id"]
        assert "durable queue receipt confirmation failed" in output
        if command == "accept":
            assert decisions[0]["destination"] == "ciao:memory" and decisions[0]["receipt_id"]
            from datetime import datetime
            from ciao import curation_run, proposal_predictions
            lease = curation_run.active_lease(vault)
            assert lease is not None
            assert proposal_predictions.count_agent_accepts_since(
                queue, datetime.fromisoformat(lease["started_at"])
            ) == 1
    else:
        assert _REGION_NEEDLE in queue.read_text() and not decisions
    if command == "accept" and status == "applied":
        assert _REGION_NEEDLE in guide_path(root).read_text()
        assert "destination ciao:memory" in output and "inspect before retrying" in output
    elif command == "accept":
        assert _REGION_NEEDLE not in guide_path(root).read_text()
        assert "before destination write" in output
    journal = mr.journal_path(vault, None)
    if status == "applied":
        assert any(r.get("kind") == "queue_resolve" and r.get("status") == mr.PREPARED
                   for r in mr.read_receipts(journal))


def test_agent_prepare_failure_prevents_destination_effect(tmp_path: Path, monkeypatch, capsys) -> None:
    from ciao import memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    guide = guide_path(root)
    before_guide, before_queue = guide.read_bytes(), queue.read_bytes()
    original = mr._append
    faults = []
    def fail_prepare(path, payload, *args, **kwargs):
        if payload.get("kind") == "queue_resolve" and payload.get("status") == mr.PREPARED:
            faults.append(True)
            raise OSError("prepared append failed")
        return original(path, payload, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(mr, "_append", fail_prepare)
        assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert faults and guide.read_bytes() == before_guide and queue.read_bytes() == before_queue
    assert not read_decisions(queue)
    assert "written" not in capsys.readouterr().err.lower()
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0


def test_agent_prepare_error_after_append_preserves_ambiguous_intent(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from ciao import memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    before = guide_path(root).read_bytes()
    original = mr._append
    def fail_after_append(path, payload, *args, **kwargs):
        result = original(path, payload, *args, **kwargs)
        if payload.get("kind") == "queue_resolve" and payload.get("status") == mr.PREPARED:
            raise OSError("uncertain preparation")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(mr, "_append", fail_after_append)
        assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert guide_path(root).read_bytes() == before and _REGION_NEEDLE in queue.read_text()
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    config = SimpleNamespace(workspace_names=lambda: ["work"], workspace_vault_root=lambda name: vault)
    recovered = mr.recover_pending(journal=mr.journal_path(vault, None), config=config)
    assert any(r.get("status") == mr.CONFLICT and r.get("destination_intent") is True for r in recovered.conflicts)
    assert not read_decisions(queue)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1


@pytest.mark.skipif(__import__("os").name == "nt", reason="Windows has no directory fence in this protocol")
def test_agent_directory_fence_failure_prevents_destination_effect(tmp_path: Path, monkeypatch) -> None:
    import os
    import stat
    from ciao import memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    before = guide_path(root).read_bytes()
    fsync = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fence failed")
        return fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(mr.os, "fsync", fail_directory)
        assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert guide_path(root).read_bytes() == before and _REGION_NEEDLE in queue.read_text()
    assert any(r.get("status") == mr.PREPARED and r.get("destination_intent") is True
               for r in mr.read_receipts(mr.journal_path(vault, None)))


@pytest.mark.parametrize("fault", ["none", "append", "fsync"])
def test_windows_receipt_order_preserves_accept_and_refuses_failed_prepare(tmp_path: Path, monkeypatch, fault: str) -> None:
    import os
    from ciao import memory_proposals, memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    journal = mr.journal_path(vault, None)
    before = guide_path(root).read_bytes()
    real_append = mr._append
    real_accept = memory_proposals.accept_region_fact
    entered = []
    class WindowsOS:
        name = "nt"
        def __getattr__(self, key):
            return getattr(os, key)
        def fsync(self, fd):
            if fault == "fsync":
                raise OSError("journal flush failed")
            return os.fsync(fd)
    def append(path, payload, *args, **kwargs):
        if fault == "append" and payload.get("kind") == "queue_resolve" and payload.get("status") == mr.PREPARED:
            raise OSError("prepared append failed")
        return real_append(path, payload, *args, **kwargs)
    def accept(*args, **kwargs):
        rows = mr.read_receipts(journal)
        assert any(r.get("status") == mr.PREPARED and r.get("destination_intent") is True
                   and r.get("proposal_id") for r in rows)
        entered.append(True)
        return real_accept(*args, **kwargs)
    monkeypatch.setattr(mr, "os", WindowsOS())
    monkeypatch.setattr(mr, "_append", append)
    monkeypatch.setattr(memory_proposals, "accept_region_fact", accept)
    rc = _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE)
    if fault != "none":
        assert rc == 1 and not entered
        assert guide_path(root).read_bytes() == before and _REGION_NEEDLE in queue.read_text()
        assert not read_decisions(queue)
    else:
        assert rc == 0 and entered
        assert _REGION_NEEDLE in guide_path(root).read_text() and _REGION_NEEDLE not in queue.read_text()
        assert len(read_decisions(queue)) == 1


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="process-crash test needs fork")
def test_process_crash_after_destination_blocks_later_accept(tmp_path: Path) -> None:
    import os
    from ciao import memory_proposals, memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    second = "Keeps a concise changelog."
    with queue.open("a", encoding="utf-8") as handle:
        handle.write(f"- [memory] {second} _(from: Decisions)_\n")
    _store_prediction(queue, "accept", text=second)
    pid = os.fork()
    if pid == 0:
        real = memory_proposals.accept_region_fact
        def exit_after_effect(*args, **kwargs):
            result = real(*args, **kwargs)
            os._exit(42)
            return result
        memory_proposals.accept_region_fact = exit_after_effect
        _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE)
        os._exit(2)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 42
    assert _REGION_NEEDLE in guide_path(root).read_text()
    assert _REGION_NEEDLE in queue.read_text() and not read_decisions(queue)
    assert any(r.get("status") == mr.PREPARED and r.get("destination_intent") is True
               for r in mr.read_receipts(mr.journal_path(vault, None)))
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert second not in guide_path(root).read_text()


@pytest.mark.parametrize("command", ["accept", "dismiss"])
def test_cli_reports_decision_write_failure_after_receipt_failure(tmp_path: Path, monkeypatch, capsys, command: str) -> None:
    from ciao import memory_receipts as mr, proposal_actions
    root, vault, queue = _accepted_install(tmp_path)
    original = mr._append
    def fail_receipt(path, payload, *args, **kwargs):
        if payload.get("kind") == "queue_resolve" and payload.get("status") == mr.APPLIED:
            raise OSError("queue receipt failed")
        return original(path, payload, *args, **kwargs)
    def fail_decision(*args, **kwargs):
        raise OSError("decision failed")
    monkeypatch.setattr(mr, "_append", fail_receipt)
    monkeypatch.setattr(proposal_actions, "record_decision", fail_decision)
    assert _run_handover(f"memory-proposal-{command}", root, vault, _REGION_NEEDLE) == 1
    assert _REGION_NEEDLE not in queue.read_text() and not read_decisions(queue)
    assert "neither the decision nor" in capsys.readouterr().err.lower()


def test_dual_fault_blocks_fresh_automatic_accept_even_after_recovery(tmp_path: Path, monkeypatch, capsys) -> None:
    from types import SimpleNamespace
    from ciao import memory_receipts as mr, proposal_actions
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    second = "Keeps a concise changelog."
    with queue.open("a", encoding="utf-8") as handle:
        handle.write(f"- [memory] {second} _(from: Decisions)_\n")
    _store_prediction(queue, "accept", text=second)
    original = mr._append
    def fail_receipt(path, payload, *args, **kwargs):
        if payload.get("kind") == "queue_resolve" and payload.get("status") == mr.APPLIED:
            raise OSError("queue receipt failed")
        return original(path, payload, *args, **kwargs)
    def fail_decision(*args, **kwargs):
        raise OSError("decision failed")
    with monkeypatch.context() as patch:
        patch.setattr(mr, "_append", fail_receipt)
        patch.setattr(proposal_actions, "record_decision", fail_decision)
        assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert not read_decisions(queue)
    guide_before = guide_path(root).read_bytes()
    queue_before = queue.read_bytes()
    assert _REGION_NEEDLE in guide_before.decode() and second not in guide_before.decode()
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert "receipt" in capsys.readouterr().err.lower()
    assert guide_path(root).read_bytes() == guide_before and queue.read_bytes() == queue_before
    journal = mr.journal_path(vault, None)
    config = SimpleNamespace(workspace_names=lambda: ["work"], workspace_vault_root=lambda name: vault)
    recovered = mr.recover_pending(journal=journal, config=config)
    assert any(r.get("kind") == "queue_resolve" and r.get("status") == mr.CONFLICT for r in recovered.conflicts)
    assert not read_decisions(queue)
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert guide_path(root).read_bytes() == guide_before and queue.read_bytes() == queue_before


def test_applied_without_decision_blocks_until_real_recovery(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from ciao import memory_receipts as mr, proposal_actions
    root, vault, queue = _accepted_install(tmp_path)
    second = "Keeps a concise changelog."
    with queue.open("a", encoding="utf-8") as handle:
        handle.write(f"- [memory] {second} _(from: Decisions)_\n")
    _store_prediction(queue, "accept", text=second)
    def fail_decision(*args, **kwargs):
        raise OSError("decision failed")
    with monkeypatch.context() as patch:
        patch.setattr(proposal_actions, "record_decision", fail_decision)
        assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert not read_decisions(queue)
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    config = SimpleNamespace(workspace_names=lambda: ["work"], workspace_vault_root=lambda name: vault)
    mr.recover_pending(journal=mr.journal_path(vault, None), config=config)
    assert len(read_decisions(queue)) == 1
    assert _run_handover("memory-proposal-accept", root, vault, second) == 0
    assert len(read_decisions(queue)) == 2


def test_unchanged_queue_after_destination_effect_recovers_as_conflict(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from ciao import memory_proposals, memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    second = "Keeps a concise changelog."
    with queue.open("a", encoding="utf-8") as handle:
        handle.write(f"- [memory] {second} _(from: Decisions)_\n")
    _store_prediction(queue, "accept", text=second)
    with monkeypatch.context() as patch:
        def fail_remove(*args, **kwargs):
            raise OSError("queue write failed")
        patch.setattr(memory_proposals, "remove_proposal_by_substring", fail_remove)
        assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert _REGION_NEEDLE in guide_path(root).read_text() and not read_decisions(queue)
    config = SimpleNamespace(workspace_names=lambda: ["work"], workspace_vault_root=lambda name: vault)
    recovered = mr.recover_pending(journal=mr.journal_path(vault, None), config=config)
    assert any(row.get("status") == mr.CONFLICT and row.get("destination_intent") is True
               for row in recovered.conflicts)
    before = guide_path(root).read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert guide_path(root).read_bytes() == before


def test_missing_queue_recovery_keeps_landed_agent_intent_blocking(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from ciao import memory_proposals, memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    second = "Keeps a concise changelog."
    with queue.open("a", encoding="utf-8") as handle:
        handle.write(f"- [memory] {second} _(from: Decisions)_\n")
    _store_prediction(queue, "accept", text=second)
    def fail_remove(*args, **kwargs):
        raise OSError("queue write failed")
    with monkeypatch.context() as patch:
        patch.setattr(memory_proposals, "remove_proposal_by_substring", fail_remove)
        assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert _REGION_NEEDLE in guide_path(root).read_text() and not read_decisions(queue)
    image = queue.read_bytes()
    queue.unlink()
    config = SimpleNamespace(workspace_names=lambda: ["work"], workspace_vault_root=lambda name: vault)
    recovered = mr.recover_pending(journal=mr.journal_path(vault, None), config=config)
    assert any(r.get("status") == mr.CONFLICT and r.get("destination_intent") is True for r in recovered.conflicts)
    queue.write_bytes(image)
    before = guide_path(root).read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert guide_path(root).read_bytes() == before and not read_decisions(queue)


def test_unchanged_selected_prepare_can_recover_and_legacy_receipts_do_not_block(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from ciao import memory_receipts as mr
    from ciao.proposal_tracking import walk_proposal_queue
    root, vault, queue = _accepted_install(tmp_path)
    entry = next(iter(walk_proposal_queue("work", "work/Workspace/Memory-Proposals.md", queue.read_text())))
    selected = {"line": entry.line, "id": entry.proposal_id, "kind": entry.bullet.kind,
                "text": entry.bullet.text, "source": entry.bullet.source}
    with mr.queue_resolution(queue, removed_text=entry.bullet.text, kind=entry.bullet.kind,
                             promoted=True, actor="agent", source="cli", workspace="work",
                             vault_root=vault, selected=selected):
        pass
    journal = mr.journal_path(vault, None)
    mr._append(journal, {"id": "legacy", "kind": "queue_resolve", "status": mr.PREPARED,
                         "queue": str(queue), "fact_text": "old legacy entry"})
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    config = SimpleNamespace(workspace_names=lambda: ["work"], workspace_vault_root=lambda name: vault)
    recovered = mr.recover_pending(journal=journal, config=config)
    assert any(r.get("status") == mr.ROLLED_BACK and r.get("proposal_id") == entry.proposal_id
               for r in recovered.reconciled)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0


@pytest.mark.parametrize("status", ["conflict", "applied"])
def test_trim_keeps_ambiguous_or_unaccounted_selected_receipts(tmp_path: Path, monkeypatch, status: str) -> None:
    from ciao import memory_receipts as mr
    root, vault, queue = _accepted_install(tmp_path)
    journal = mr.journal_path(vault, None)
    monkeypatch.setattr(mr, "MAX_BYTES", 400)
    monkeypatch.setattr(mr, "KEEP_LINES", 2)
    mr._append(journal, {"id": "ambiguous", "kind": "queue_resolve", "status": status,
                         "proposal_id": "selected", "actor": "agent", "source": "cli",
                         "promoted": True, "queue": str(queue)})
    for i in range(5):
        mr._append(journal, {"id": f"later-{i}", "kind": "other", "status": mr.APPLIED, "padding": "x" * 120})
    assert any(row["id"] == "ambiguous" for row in mr.read_receipts(journal))


def test_corrupt_journal_blocks_automatic_accept_without_effect(tmp_path: Path, capsys) -> None:
    from ciao import memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    journal = mr.journal_path(vault, None)
    journal.write_text('{"id":"incomplete"', encoding="utf-8")
    before_queue = queue.read_bytes()
    before_guide = guide_path(root).read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert "receipt journal cannot be verified" in capsys.readouterr().err
    assert queue.read_bytes() == before_queue and guide_path(root).read_bytes() == before_guide


@pytest.mark.parametrize("count", [1, 2])
def test_accept_gate_counts_each_valid_legacy_agent_decision(tmp_path: Path, monkeypatch, count: int) -> None:
    import json
    from ciao import curation_run, proposal_predictions
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    monkeypatch.setattr(proposal_predictions, "HANDOVER_ACCEPT_CAP", 2)
    lease = curation_run.active_lease(vault)
    assert lease is not None
    legacy = {"promoted_at": lease["started_at"], "via": "agent", "kind": "memory", "text": "Another fact"}
    queue.with_suffix(".dismissed.log").write_text((json.dumps(legacy) + "\n") * count, encoding="utf-8")
    before = guide_path(root).read_bytes(), queue.read_bytes()
    rc = _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE)
    assert rc == (0 if count == 1 else 1)
    if count == 2:
        assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


@pytest.mark.parametrize("suffix", [".dismissed.jsonl", ".dismissed.log"])
def test_accept_refuses_duplicate_key_decision_rows(tmp_path: Path, suffix: str) -> None:
    from ciao import curation_run, memory_receipts
    from ciao.workspace_guide import guide_path

    root, vault, queue = _accepted_install(tmp_path)
    lease = curation_run.active_lease(vault)
    assert lease is not None
    row = ('{"text":"fact","via":"agent","via":"operator","kind":"memory",'
           f'"promoted_at":"{lease["started_at"]}"' + "}\n")
    queue.with_suffix(suffix).write_text(row * 5, encoding="utf-8")
    before = guide_path(root).read_bytes(), queue.read_bytes(), queue.with_suffix(suffix).read_bytes()
    journal = memory_receipts.journal_path(vault, None)
    before_journal = journal.read_bytes() if journal.exists() else None
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert (guide_path(root).read_bytes(), queue.read_bytes(), queue.with_suffix(suffix).read_bytes()) == before
    assert (journal.read_bytes() if journal.exists() else None) == before_journal


def test_accept_with_history_past_old_total_limit(tmp_path: Path, monkeypatch) -> None:
    import json
    from ciao import memory_proposals
    root, vault, queue = _accepted_install(tmp_path)
    line = json.dumps({"text": "Unrelated old decision " + "x" * 100, "via": "operator",
                       "dismissed_at": "2025-01-01T00:00:00+00:00"}) + "\n"
    sidecar = queue.with_suffix(".dismissed.log")
    sidecar.write_text(line * 10, encoding="utf-8")
    assert sidecar.stat().st_size > 512
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0


def test_accept_reports_landed_destination_when_decision_write_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    from ciao import proposal_actions
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    def fail(*args, **kwargs):
        raise OSError("decision disk failure")
    monkeypatch.setattr(proposal_actions, "record_decision", fail)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    error = capsys.readouterr().err.lower()
    assert "destination" in error and "written" in error and "inspect" in error
    assert _REGION_NEEDLE in guide_path(root).read_text()
    assert _REGION_NEEDLE not in queue.read_text()


def test_dismiss_reports_removed_queue_when_decision_write_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    from ciao import proposal_actions
    root, vault, queue = _accepted_install(tmp_path)
    def fail(*args, **kwargs):
        raise OSError("decision disk failure")
    monkeypatch.setattr(proposal_actions, "record_decision", fail)
    assert _run_handover("memory-proposal-dismiss", root, vault, _REGION_NEEDLE) == 1
    error = capsys.readouterr().err.lower()
    assert "removed" in error and "decision" in error and "inspect" in error
    assert _REGION_NEEDLE not in queue.read_text()


def test_accept_settlement_does_not_materialize_decision_history(tmp_path: Path, monkeypatch) -> None:
    import tracemalloc
    from ciao import memory_proposals
    warm_root, warm_vault, _ = _accepted_install(tmp_path / "warm")
    assert _run_handover("memory-proposal-accept", warm_root, warm_vault, _REGION_NEEDLE) == 0
    root, vault, queue = _accepted_install(tmp_path)
    row = b'{"text":"unrelated","dismissed_at":"2020-01-01T00:00:00Z"}\n'
    with queue.with_suffix(".dismissed.log").open("wb") as handle:
        for _ in range(30000):
            handle.write(row)
    def no_materialization(*args, **kwargs):
        raise AssertionError("decision history was materialized")
    monkeypatch.setattr(memory_proposals, "read_decisions", no_materialization)
    tracemalloc.start()
    try:
        assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024


def test_uncheckpointed_budget_streams_unrelated_history_bounded(tmp_path: Path) -> None:
    import tracemalloc
    from ciao import cli, memory_receipts
    root, vault, queue = _accepted_install(tmp_path)
    row = b'{"text":"Unrelated","via":"operator","dismissed_at":"2025-01-01T00:00:00Z"}\n'
    with queue.with_suffix(".dismissed.log").open("wb") as handle:
        for _ in range(30000):
            handle.write(row)
    tracemalloc.start()
    try:
        with memory_receipts.queue_lock(queue):
            assert cli._handover_accept_budget(vault, queue, config=checkpoint_config(queue)) == ""
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024


def test_accept_prediction_freshness_streams_large_history(tmp_path: Path, monkeypatch) -> None:
    import tracemalloc
    from ciao import cli, memory_proposals
    from ciao.proposal_kinds import parse_bullet
    root, vault, queue = _accepted_install(tmp_path)
    row = b'{"text":"Unrelated","kind":"memory","dismissed_at":"2025-01-01T00:00:00Z"}\n'
    with queue.with_suffix(".dismissed.log").open("wb") as handle:
        for _ in range(30000):
            handle.write(row)
    def no_materialization(*args, **kwargs):
        raise AssertionError("freshness must not materialize decision history")
    monkeypatch.setattr(memory_proposals, "read_decisions", no_materialization)
    bullet = parse_bullet(_REGION_BULLET)
    assert bullet is not None
    tracemalloc.start()
    try:
        assert cli._handover_gate(root, "work", queue, bullet, "accept") == ""
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024


@pytest.mark.parametrize("count, expected", [(1, 0), (5, 1)])
def test_uncheckpointed_repeated_receipt_rows_keep_existing_cap_count(
    tmp_path: Path, count: int, expected: int,
) -> None:
    import json
    from ciao import curation_run
    root, vault, queue = _accepted_install(tmp_path)
    lease = curation_run.active_lease(vault)
    assert lease is not None
    row = {"text": "Other fact", "kind": "memory", "via": "agent",
           "queue_receipt_id": "same-old-receipt", "promoted_at": lease["started_at"]}
    queue.with_suffix(".dismissed.log").write_text((json.dumps(row) + "\n") * count, encoding="utf-8")
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == expected


@pytest.mark.parametrize("transition_at", ["admission", "decision"])
def test_earlier_lease_waits_for_accept_settlement_and_obeys_cap(tmp_path: Path, monkeypatch, transition_at: str) -> None:
    import json
    import threading
    from datetime import datetime, timedelta
    from ciao import cli, curation_run, proposal_actions, proposal_predictions

    root, vault, queue = _accepted_install(tmp_path)
    old = curation_run.active_lease(vault)
    assert old is not None
    started = datetime.fromisoformat(old["started_at"])
    event_time = started - timedelta(seconds=10)
    legacy = {"promoted_at": event_time.isoformat(), "via": "agent", "kind": "memory", "text": "Another fact"}
    queue.with_suffix(".dismissed.log").write_text(
        (json.dumps(legacy) + "\n") * (proposal_predictions.HANDOVER_ACCEPT_CAP + 1), encoding="utf-8",
    )
    real_budget = cli._handover_accept_budget
    attempted, completed = threading.Event(), threading.Event()
    new_holders = []

    def transition():
        attempted.set()
        curation_run.end_run(vault, holder=old["holder"])
        new = curation_run.begin_run(vault, holder="earlier", now=started - timedelta(seconds=20), ttl_s=3600)
        assert datetime.fromisoformat(new.started_at) < event_time < started
        new_holders.append(new.holder)
        completed.set()

    worker = threading.Thread(target=transition)
    def change_after_budget(vault_path, queue_path, *, config):
        refusal = real_budget(vault_path, queue_path, config=config)
        assert not refusal
        worker.start()
        assert attempted.wait(2) and not completed.wait(0.2)
        return refusal

    real_decision = proposal_actions.record_decision
    def change_before_decision(*args, **kwargs):
        worker.start()
        assert attempted.wait(2) and not completed.wait(0.2)
        return real_decision(*args, **kwargs)

    with monkeypatch.context() as patch:
        if transition_at == "admission":
            patch.setattr(cli, "_handover_accept_budget", change_after_budget)
        else:
            patch.setattr(proposal_actions, "record_decision", change_before_decision)
        assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0
    worker.join(3)
    assert completed.is_set()
    assert curation_run.active_lease(vault)["holder"] == new_holders[0]
    assert cli._handover_accept_budget(vault, queue, config=checkpoint_config(queue))


def test_lease_expiry_after_admission_does_not_interrupt_settlement(tmp_path: Path, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta
    from ciao import curation_run, memory_proposals

    root, vault, queue = _accepted_install(tmp_path)
    lease = curation_run.active_lease(vault)
    assert lease is not None
    current = datetime.now(UTC)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current if tz is not None else current.replace(tzinfo=None)
    monkeypatch.setattr(curation_run, "datetime", Clock)
    real_accept = memory_proposals.accept_region_fact
    def expire(*args, **kwargs):
        nonlocal current
        current = datetime.fromisoformat(lease["expires_at"]) + timedelta(seconds=1)
        assert curation_run.active_lease(vault) is None
        return real_accept(*args, **kwargs)
    monkeypatch.setattr(memory_proposals, "accept_region_fact", expire)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0
    assert len(read_decisions(queue)) == 1


def test_begin_samples_production_clock_after_state_lock(tmp_path: Path, monkeypatch) -> None:
    import threading
    from datetime import UTC, datetime, timedelta
    from ciao import curation_run

    vault = tmp_path / "vault"
    current = datetime.now(UTC).replace(microsecond=0)
    calls = []
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(current)
            return current
    monkeypatch.setattr(curation_run, "datetime", Clock)
    attempted = threading.Event()
    leases = []
    def begin():
        leases.append(curation_run.begin_run(vault, holder="waiting"))
    with curation_run._state_lock(vault):
        real_lock = curation_run.lock_exclusive
        def signal_lock(fd, **kwargs):
            attempted.set()
            return real_lock(fd, **kwargs)
        monkeypatch.setattr(curation_run, "lock_exclusive", signal_lock)
        worker = threading.Thread(target=begin)
        worker.start()
        assert attempted.wait(2)
        assert not calls
        current += timedelta(seconds=30)
    worker.join(3)
    assert not worker.is_alive()
    assert datetime.fromisoformat(leases[0].started_at) == current


def test_future_lease_start_refuses_automatic_accept(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta
    from ciao import curation_run
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    lease = curation_run.active_lease(vault)
    assert lease is not None
    curation_run.end_run(vault, holder=lease["holder"])
    curation_run.begin_run(vault, holder="future", now=datetime.now(UTC) + timedelta(minutes=1))
    before = guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


def test_clock_rollback_during_accept_blocks_later_accepts_and_compaction(tmp_path: Path, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta
    from ciao import curation_run, memory_proposals, memory_receipts as mr
    from ciao.workspace_guide import guide_path

    root, vault, queue = _accepted_install(tmp_path)
    second = "Keeps a concise changelog."
    with queue.open("a", encoding="utf-8") as handle:
        handle.write(f"- [memory] {second} _(from: Decisions)_\n")
    _store_prediction(queue, "accept", text=second)
    lease = curation_run.active_lease(vault)
    assert lease is not None
    backdated = datetime.fromisoformat(lease["started_at"]) - timedelta(seconds=10)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return backdated if tz is not None else backdated.replace(tzinfo=None)
    real_accept = memory_proposals.accept_region_fact
    def rollback(*args, **kwargs):
        monkeypatch.setattr(memory_proposals, "datetime", Clock)
        return real_accept(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(memory_proposals, "accept_region_fact", rollback)
        assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0
    monkeypatch.setattr(mr, "MAX_BYTES", 1024)
    monkeypatch.setattr(mr, "KEEP_LINES", 2)
    journal = mr.journal_path(vault, None)
    for i in range(3):
        mr._append(journal, {"id": f"noise-{i}", "kind": "other", "status": mr.APPLIED, "padding": "x" * 512})
    assert not mr.compact_cap_checkpoint(queue, vault, config=checkpoint_config(queue))
    before = guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="process-crash test needs fork")
def test_accept_crash_after_intent_blocks_next_run(tmp_path: Path) -> None:
    import os
    from ciao import curation_run, memory_proposals
    from ciao.workspace_guide import guide_path

    root, vault, queue = _accepted_install(tmp_path)
    before = guide_path(root).read_bytes(), queue.read_bytes()
    pid = os.fork()
    if pid == 0:
        def crash(*args, **kwargs):
            os._exit(42)
        memory_proposals.accept_region_fact = crash
        _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE)
        os._exit(2)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 42
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before
    old = curation_run.active_lease(vault)
    assert old is not None
    curation_run.end_run(vault, holder=old["holder"])
    curation_run.begin_run(vault, holder="next", ttl_s=3600)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


@pytest.mark.parametrize("content", [
    b'{"text":"broken"', b'{"text":"valid but incomplete"}', b"\xff\n",
    b'{"text":"fact","via":"agent","promoted_at":"invalid"}\n',
])
def test_accept_gate_rejects_corrupt_or_truncated_sidecar(tmp_path: Path, capsys, content: bytes) -> None:
    from ciao import memory_proposals
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    memory_proposals.dismissed_log_path(queue).write_bytes(content)
    before = guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert "decision" in capsys.readouterr().err.lower()
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


def test_accept_gate_rejects_unreadable_sidecar(tmp_path: Path, monkeypatch, capsys) -> None:
    import os
    from ciao import memory_proposals
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    sidecar = memory_proposals.dismissed_log_path(queue)
    sidecar.write_text('{"text":"existing"}\n')
    real_open = os.open
    def denied(path, flags, *args, **kwargs):
        if Path(path) == sidecar:
            raise PermissionError("injected unreadable sidecar")
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(memory_proposals.os, "open", denied)
    before = guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert "decision" in capsys.readouterr().err.lower()
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


def test_accept_gate_reads_strict_decisions_under_queue_lock(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_proposals, memory_receipts
    root, vault, queue = _accepted_install(tmp_path)
    memory_proposals.dismissed_log_path(queue).write_text('{"text":"other"}\n')
    real = memory_proposals.DecisionSidecarStream.__iter__
    calls = []
    def checked(stream):
        assert memory_receipts._QUEUE_LOCK_DEPTH.depths.get(str(queue.resolve()), 0)
        calls.append(True)
        return real(stream)
    monkeypatch.setattr(memory_proposals.DecisionSidecarStream, "__iter__", checked)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0
    assert calls


@pytest.mark.skipif(__import__("os").name == "nt", reason="Windows may prevent replacing an open file")
def test_accept_gate_rejects_sidecar_replaced_during_read(tmp_path: Path, monkeypatch, capsys) -> None:
    import os
    from ciao import memory_proposals
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    sidecar = memory_proposals.dismissed_log_path(queue)
    sidecar.write_text('{"text":"original"}\n')
    replacement = sidecar.with_name("replacement")
    replacement.write_text('{"text":"replaced"}\n')
    original_stat = os.fstat
    inode = sidecar.stat().st_ino
    swapped = []
    def replace_after_open(fd):
        result = original_stat(fd)
        if result.st_ino == inode and not swapped:
            os.replace(replacement, sidecar)
            swapped.append(True)
        return result
    monkeypatch.setattr(os, "fstat", replace_after_open)
    before = guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert swapped and "decision" in capsys.readouterr().err.lower()
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


@pytest.mark.parametrize("extra", [0, 1])
def test_accept_gate_decision_row_size_boundary(tmp_path: Path, extra: int) -> None:
    from ciao import memory_proposals
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    prefix, suffix = b'{"text":"', b'"}'
    line = prefix + b"x" * (2 * 1024 * 1024 + extra - len(prefix) - len(suffix)) + suffix
    memory_proposals.dismissed_log_path(queue).write_bytes(line + b"\n")
    before = guide_path(root).read_bytes(), queue.read_bytes()
    rc = _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE)
    assert rc == (1 if extra else 0)
    if extra:
        assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


@pytest.mark.parametrize("loss", ["missing", "empty"])
def test_accept_gate_rejects_missing_sidecar_even_from_earlier_lease(tmp_path: Path, capsys, loss: str) -> None:
    from datetime import UTC, datetime, timedelta
    from ciao import curation_run, memory_proposals
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    second = "Keeps a concise changelog."
    with queue.open("a", encoding="utf-8") as handle:
        handle.write(f"- [memory] {second} _(from: Decisions)_\n")
    _store_prediction(queue, "accept", text=second)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0
    curation_run.end_run(vault, holder=curation_run.active_lease(vault)["holder"])
    curation_run.begin_run(vault, holder="later", ttl_s=3600, now=datetime.now(UTC) + timedelta(seconds=2))
    sidecar = memory_proposals.dismissed_log_path(queue)
    if loss == "missing":
        sidecar.unlink()
    else:
        sidecar.write_bytes(b"")
    before = guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, second) == 1
    assert "decision" in capsys.readouterr().err.lower()
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


def test_append_and_trim_do_not_launder_malformed_journal(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    journal = mr.journal_path(vault, None)
    journal.write_text('{"id":"broken"\n', encoding="utf-8")
    monkeypatch.setattr(mr, "MAX_BYTES", 128)
    monkeypatch.setattr(mr, "KEEP_LINES", 1)
    for i in range(3):
        mr._append(journal, {"id": f"later-{i}", "kind": "other", "status": mr.APPLIED, "padding": "x" * 128})
    assert '{"id":"broken"' in journal.read_text()
    before = guide_path(root).read_bytes(), queue.read_bytes()
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 1
    assert (guide_path(root).read_bytes(), queue.read_bytes()) == before


@pytest.mark.parametrize("extra", [0, 1])
def test_journal_gate_exact_64mib_boundary(tmp_path: Path, capsys, extra: int) -> None:
    from ciao import memory_receipts as mr
    from ciao.workspace_guide import guide_path
    root, vault, queue = _accepted_install(tmp_path)
    journal = mr.journal_path(vault, None)
    limit = 16 * mr.MAX_BYTES
    prefix = b'{"id":"legacy","kind":"other","status":"applied"}'
    with journal.open("wb") as handle:
        handle.write(prefix)
        remaining = limit + extra - len(prefix) - 1
        chunk = b" " * (1024 * 1024)
        for _ in range(remaining // len(chunk)):
            handle.write(chunk)
        handle.write(chunk[:remaining % len(chunk)])
        handle.write(b"\n")
    assert journal.stat().st_size == limit + extra
    before = guide_path(root).read_bytes(), queue.read_bytes()
    rc = _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE)
    if extra:
        assert rc == 1 and "receipt journal cannot be verified" in capsys.readouterr().err
        assert (guide_path(root).read_bytes(), queue.read_bytes()) == before
    else:
        assert rc == 0
        assert _REGION_NEEDLE in guide_path(root).read_text() and _REGION_NEEDLE not in queue.read_text()


def test_accept_holds_queue_authority_during_region_write(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_receipts, memory_tool

    root, vault, queue = _accepted_install(tmp_path)
    original = memory_tool.ensure_regions

    def checked(guide):
        assert getattr(memory_receipts._QUEUE_LOCK_DEPTH, "depths", {}).get(str(queue.resolve()), 0)
        return original(guide)

    monkeypatch.setattr(memory_tool, "ensure_regions", checked)
    assert _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE) == 0


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


def test_replayed_cli_accept_preserves_metadata_and_cap(tmp_path: Path, monkeypatch) -> None:
    from ciao import curation_run, memory_receipts as mr, proposal_actions
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.proposal_predictions import HANDOVER_ACCEPT_CAP

    facts = [f"Fact number {i} stays true for installs." for i in range(HANDOVER_ACCEPT_CAP + 1)]
    root, vault, queue = _handover_install(tmp_path, "all", *[f"- [memory] {f}" for f in facts])
    config = CiaoConfig(pwa_auth_token="test", workspace_root=root, state_path=root / ".runtime/state.json",
                        media_root=root / ".runtime/media", vault_root=vault,
                        workspaces={"work": WorkspaceConfig(name="work", vault_root=str(vault))})
    curation_run.begin_run(vault, holder="test-run", ttl_s=3600)
    for fact in facts:
        _store_prediction(queue, "accept", fact)
    original = proposal_actions.record_decision
    def replay_first(path, **kwargs):
        mr.recover_pending(journal=mr.journal_path(vault, None), config=config)
        recovered = read_decisions(queue)[-1]
        assert recovered["via"] == "agent"
        assert recovered["destination"] == "ciao:memory"
        assert recovered["outcome"] == kwargs["outcome"]
        assert recovered["receipt_id"] == kwargs["receipt_id"] and recovered["receipt_id"]
        assert recovered["queue_receipt_id"] == kwargs["queue_receipt_id"] and recovered["queue_receipt_id"]
        return original(path, **kwargs)
    monkeypatch.setattr(proposal_actions, "record_decision", replay_first)
    assert [_run_handover("memory-proposal-accept", root, vault, fact) for fact in facts] == (
        [0] * HANDOVER_ACCEPT_CAP + [1]
    )
    assert len(read_decisions(queue)) == HANDOVER_ACCEPT_CAP
    assert facts[-1] in queue.read_text(encoding="utf-8")


@pytest.mark.parametrize("over", [False, True])
def test_cli_selected_image_capacity_before_guide_write(tmp_path: Path, over: bool) -> None:
    from ciao import memory_receipts
    from ciao.workspace_guide import guide_path

    root, vault, queue = _accepted_install(tmp_path)
    initial = queue.read_text(encoding="utf-8")
    size = memory_receipts.MAX_IMAGE_CHARS + int(over)
    queue.write_text(initial + "#" + "x" * (size - len(initial) - 2) + "\n", encoding="utf-8")
    guide = guide_path(root)
    before_guide, before_queue = guide.read_bytes(), queue.read_bytes()
    result = _run_handover("memory-proposal-accept", root, vault, _REGION_NEEDLE)
    if over:
        assert result == 1 and guide.read_bytes() == before_guide and queue.read_bytes() == before_queue
    else:
        assert result == 0 and guide.read_bytes() != before_guide
        rows = memory_receipts.read_receipts(memory_receipts.journal_path(vault, None))
        selected = [row for row in rows if row.get("kind") == "queue_resolve" and row.get("proposal_id")]
        assert selected and selected[-1]["before_text"] is not None and selected[-1]["after_text"] is not None


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


@pytest.mark.parametrize("promoted", [False, True])
def test_cli_dismiss_refuses_a_skill_draft_row_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], promoted: bool
) -> None:
    """A skill-draft row is settled with skill-draft-approve/reject, never here.

    The queue cannot settle a draft: the sidecar would stay pending with no
    bullet, and a re-filed copy would re-enter the queue under a new id. The
    refusal lands before anything is written, so the queue and decisions stay as
    they were.
    """
    from ciao import cli
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.upstream_drafts import (
        DRAFT_PENDING,
        UPSTREAM_ISSUE,
        file_draft,
        read_records,
    )
    from ciao.workspace_reroot import mark_born_per_root

    vault = tmp_path / "memory-vault"
    runtime = tmp_path / ".runtime"
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=vault,
        workspaces={"work": WorkspaceConfig(name="work", vault_root=str(vault))},
    )
    mark_born_per_root(tmp_path, runtime, ["work"])
    draft = file_draft(
        config,
        "work",
        target=UPSTREAM_ISSUE,
        skill="web-research",
        title="Document the offline fallback for a timed-out fetch",
        change="State that a fetch that times out must be retried once with a longer budget.",
        body="A search fetch that times out is reported as no results. Retry once first.",
        repository="example/tools",
        version="1.4.0",
        private_evidence="chat-42 turn 7: the fetch reported no results",
    )
    queue = vault / "Workspace" / "Memory-Proposals.md"
    before = queue.read_text(encoding="utf-8")
    assert f"skill-draft:{draft.id}" in before

    rc = cli.main([
        "memory-proposal-dismiss",
        *(["--promoted"] if promoted else []),
        "--workspace", str(tmp_path),
        "--vault-root", str(vault),
        draft.id,
    ])

    assert rc == 2
    err = capsys.readouterr().err
    assert "skill-draft-approve" in err
    assert "skill-draft-reject" in err
    assert draft.id in err
    assert queue.read_text(encoding="utf-8") == before
    assert read_decisions(queue) == []
    assert read_records(config, "work")[0].lifecycle == DRAFT_PENDING
