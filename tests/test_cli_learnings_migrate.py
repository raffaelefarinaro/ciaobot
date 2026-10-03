"""Tests for ``ciao.learnings_migrate`` and ``ciao learnings-migrate``.

Every ``Learnings.md`` here is written inline in a tmp vault. Nothing reads a
real workspace, and the migration command is only ever pointed at a directory
this test made.

The order follows the three properties the command is sold on — dry-run first,
lossless, reversible — because that is the order in which a failure explains
itself. The end-to-end recurrence contract lives in
``tests/test_learnings_writer.py``, next to the writer it is a contract of.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao import cli
from ciao import learnings_migrate as lm
from ciao.learning_records import METADATA_MARKER
from ciao.memory_receipts import content_revision


# A file with one entry of every shape the command has to cope with: a legacy
# date/category/confidence line, a bare bullet, and a current canonical line
# that carries no machine comment yet.
LEGACY_DOC = (
    "---\n"
    "tags: [ciao, learnings]\n"
    "---\n"
    "# Learnings\n"
    "\n"
    "## Format\n"
    "Entries look like `- [key] [first -> last] (xN) text`.\n"
    "\n"
    "## Active\n"
    "- [2024-05-01] debugging: retries need a fresh token — confidence: high\n"
    "- A plain bullet with no structure at all.\n"
    "- [airtable-sort] [2024-01-02 → 2024-03-04] (x3) Airtable sort param "
    "returns 400 — sources: chat-a, chat-b\n"
    "\n"
    "## Promoted / Resolved\n"
    "- [pin-node] [2024-01-01 → 2024-02-01] (x4) Pin the Node version.\n"
)


def _vault(tmp_path: Path, body: str = LEGACY_DOC) -> tuple[Path, Path]:
    """A vault whose ``Learnings.md`` holds *body*, and a runtime root beside it."""
    vault = tmp_path / "memory-vault" / "personal"
    (vault / "Workspace").mkdir(parents=True)
    (vault / "Workspace" / "Learnings.md").write_bytes(body.encode("utf-8"))
    return vault, tmp_path / ".runtime"


def _learnings(vault: Path) -> str:
    return (vault / "Workspace" / "Learnings.md").read_bytes().decode("utf-8")


def _receipts(runtime: Path) -> list[Path]:
    return sorted((runtime / "migration").glob("learnings-*.json"))


def _run(*argv: str) -> int:
    return cli.main(list(argv))


def _spans(output: str) -> list[tuple[str, str]]:
    """The before/after pairs a run printed, without the surrounding prose.

    The verb differs between a preview and an apply — that is the point of the
    flag — so what has to be identical is the edit each run describes.
    """
    pairs: list[tuple[str, str]] = []
    before: str | None = None
    for line in output.splitlines():
        if line.startswith("    -> "):
            pairs.append((before or "", line.removeprefix("    -> ")))
            before = None
        elif line.startswith("  ") and line.strip():
            before = line
    return pairs


# ---- dry-run first ---------------------------------------------------------


def test_the_dry_run_reports_every_entry_and_writes_nothing(tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    before = _learnings(vault)

    code = _run(
        "learnings-migrate", "--vault-root", str(vault), "--runtime-root", str(runtime)
    )

    assert code == 0
    assert _learnings(vault) == before
    # A dry run is not undoable, so it must not leave a receipt claiming to be.
    assert _receipts(runtime) == []


def test_the_dry_run_previews_are_byte_identical_to_the_apply(
    tmp_path: Path, capsys
) -> None:
    """A preview that differs from the apply is a lie, not an approximation.

    The preview is the same computation, so this pins that rather than trusting
    it: a run that computed its spans one way and wrote them another would leave
    a receipt describing an edit the file never received.
    """
    vault, runtime = _vault(tmp_path)
    _run("learnings-migrate", "--vault-root", str(vault), "--runtime-root", str(runtime))
    previewed = _spans(capsys.readouterr().out)

    _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
    )
    applied = _spans(capsys.readouterr().out)

    assert previewed == applied
    assert previewed


def test_a_missing_learnings_file_is_reported_rather_than_created(
    tmp_path: Path, capsys
) -> None:
    vault, runtime = _vault(tmp_path)
    (vault / "Workspace" / "Learnings.md").unlink()

    code = _run(
        "learnings-migrate", "--vault-root", str(vault), "--runtime-root", str(runtime)
    )

    assert code == 0
    assert "does not exist" in capsys.readouterr().out
    assert not (vault / "Workspace" / "Learnings.md").exists()
    assert _receipts(runtime) == []


# ---- lossless --------------------------------------------------------------


def test_the_command_lands_exactly_what_the_model_migrates(tmp_path: Path) -> None:
    """The model's `migrate_learnings` is the reference; the command must match it.

    The command computes its own text so its receipt offsets address the bytes
    it actually wrote. That is only safe while the two agree, and this is what
    holds them together — the pure model is tested over the same content in
    `tests/test_learning_records.py`, and this is the check that the production
    caller did not quietly grow a second answer.
    """
    from ciao.learning_records import migrate_learnings

    vault, runtime = _vault(tmp_path)
    _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
    )

    expected, diagnostics = migrate_learnings(LEGACY_DOC, workspace=vault.name)

    assert _learnings(vault) == expected
    assert not diagnostics


def test_every_legacy_shape_migrates_and_nothing_outside_it_moves(
    tmp_path: Path,
) -> None:
    vault, runtime = _vault(tmp_path)

    _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
    )
    text = _learnings(vault)

    # The three shapes, each now carrying the machine comment.
    assert METADATA_MARKER + '{"aliases":[],"baseline":null,"id":' in text
    assert 'retries need a fresh token' in text
    assert 'A plain bullet with no structure at all.' in text
    assert "(x3) Airtable sort param returns 400" in text
    # Everything the migration does not own is byte-identical.
    assert text.startswith("---\ntags: [ciao, learnings]\n---\n# Learnings\n")
    assert "## Format\nEntries look like `- [key]" in text
    resolved = text.partition("## Promoted / Resolved")[2]
    # The whole Resolved section, byte for byte, machine comment included.
    assert resolved == (
        "\n- [pin-node] [2024-01-01 → 2024-02-01] (x4) Pin the Node version.\n"
    )


def test_unknown_history_stays_unknown_through_the_migration(
    tmp_path: Path,
) -> None:
    """A legacy bullet has no recurrence, so it does not acquire an invented x1."""
    vault, runtime = _vault(tmp_path)
    _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
    )

    plain = next(
        line
        for line in _learnings(vault).splitlines()
        if "A plain bullet with no structure" in line
    )
    assert "(?)" in plain
    assert "[unknown → unknown]" in plain
    # A date-only legacy entry has a date and no count.
    dated = next(
        line
        for line in _learnings(vault).splitlines()
        if "retries need a fresh token" in line
    )
    assert "[2024-05-01 → 2024-05-01]" in dated
    assert "(?)" in dated
    # The category and confidence are retained, not discarded.
    assert '"category":"debugging"' in dated
    assert '"confidence":"high"' in dated


def test_bom_and_crlf_survive_the_whole_round_trip(tmp_path: Path) -> None:
    body = (
        "﻿# Learnings\r\n"
        "\r\n"
        "## Active\r\n"
        "- [2024-05-01] debugging: retries need a fresh token\r\n"
    )
    vault, runtime = _vault(tmp_path, body)

    _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
    )
    raw = (vault / "Workspace" / "Learnings.md").read_bytes()

    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\r\n") == 4
    assert b"\n" not in raw.replace(b"\r\n", b"")

    receipt = _receipts(runtime)[0]
    _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--revert",
        str(receipt),
        "--apply",
    )
    # Byte-identical to what the owner had, CRLF pairs and BOM included.
    assert (vault / "Workspace" / "Learnings.md").read_bytes() == body.encode("utf-8")


# ---- idempotent ------------------------------------------------------------


def test_a_second_apply_changes_nothing_and_writes_no_second_receipt(
    tmp_path: Path,
) -> None:
    vault, runtime = _vault(tmp_path)
    args = ["learnings-migrate", "--vault-root", str(vault), "--runtime-root", str(runtime)]
    _run(*args, "--apply")
    migrated = _learnings(vault)
    first = _receipts(runtime)

    assert _run(*args, "--apply") == 0
    assert _learnings(vault) == migrated
    # The gate is the content itself: nothing left to rewrite means no run, and a
    # second receipt would be a reverse map for an edit that did not happen.
    assert _receipts(runtime) == first


def test_a_second_run_over_migrated_content_is_a_clean_no_op(
    tmp_path: Path, capsys
) -> None:
    vault, runtime = _vault(tmp_path)
    args = ["learnings-migrate", "--vault-root", str(vault), "--runtime-root", str(runtime)]
    _run(*args, "--apply")
    capsys.readouterr()

    assert _run(*args, "--apply") == 0
    out = capsys.readouterr().out
    assert "already canonical" in out
    assert METADATA_MARKER not in out


# ---- a run that did not write ----------------------------------------------
#
# The fourth property, and the one every receipt rests on: a receipt is the only
# way back, so it may only exist for a run whose bytes are on disk. The refusal
# to write is the case the summary has to survive — `failed` set, the count
# zero, the file untouched — and every reader of it has to draw the same
# conclusion. The concurrent-write refusal is the same shape and is covered at
# the module level in `test_a_concurrent_write_is_refused_rather_than_overwritten`.


def test_a_failed_apply_writes_no_receipt_and_claims_no_migration(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A receipt for a write that never happened reverses spans still in place.

    `entries_migrated` counted the plan, so a run whose write raised still
    reported three migrated entries, the CLI wrote a receipt off that count and
    printed how to reverse it — over a file that had not changed. Following the
    printed instruction would have restored spans against the very bytes they
    came from.
    """
    vault, runtime = _vault(tmp_path)
    before = _learnings(vault)

    def _refuse(target: Path, text: str, *, expect: str = "") -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(lm, "_write_locked", _refuse)

    code = _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
        "--json",
    )
    summary = json.loads(capsys.readouterr().out)

    assert code == 1
    assert summary["failed"] == [
        {"path": "Workspace/Learnings.md", "error": "no space left on device"}
    ]
    # Zero, not three: the count is what the receipt is gated on, and a number
    # that means "would have" cannot answer a question about what is on disk.
    assert summary["entries_migrated"] == 0
    # The spans stay — they are the plan, and the file is worth reading again
    # once there is room for it — but nothing claims a receipt for them.
    assert len(summary["rewrites"]) == 3
    assert "revision_after" not in summary, "a write that did not land has no after"
    assert "receipt_path" not in summary
    assert _receipts(runtime) == []
    assert _learnings(vault) == before


def test_a_failed_apply_says_it_wrote_nothing(tmp_path: Path, capsys, monkeypatch) -> None:
    """The human run must not read like a finished one either.

    The failure is already on stderr, and the count is zero because nothing was
    migrated — so the summary line falls through to the one that says the file is
    already canonical. On an untouched file full of legacy lines that is the
    opposite of true, and it is the only line a casual reader sees.
    """
    vault, runtime = _vault(tmp_path)

    def _refuse(target: Path, text: str, *, expect: str = "") -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(lm, "_write_locked", _refuse)
    _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
    )
    captured = capsys.readouterr()

    assert "Migrated" not in captured.out
    assert "already canonical" not in captured.out
    assert "Nothing was written: the file is as it was." in captured.out
    assert "no space left on device" in captured.err
    assert "Receipt:" not in captured.out
    assert _receipts(runtime) == []


# ---- reversible ------------------------------------------------------------


def test_the_receipt_reverses_to_the_exact_original_bytes(
    tmp_path: Path, capsys
) -> None:
    vault, runtime = _vault(tmp_path)
    original = _learnings(vault)
    _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
    )
    capsys.readouterr()
    assert _learnings(vault) != original

    receipt = _receipts(runtime)[0]
    assert _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--revert",
        str(receipt),
        "--apply",
    ) == 0

    assert _learnings(vault) == original
    assert "Reverted" in capsys.readouterr().out


def test_the_receipt_names_the_spans_it_can_put_back(tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
    )

    payload = json.loads(_receipts(runtime)[0].read_text(encoding="utf-8"))

    assert payload["schema_version"] == lm.RECEIPT_VERSION
    assert payload["path"] == "Workspace/Learnings.md"
    assert payload["entries_migrated"] == len(payload["rewrites"]) == 3
    for change in payload["rewrites"]:
        # Enough to reverse without re-deriving anything: the original bytes, the
        # bytes that replaced them, and where in the file both were.
        assert change["from"] and change["to"]
        assert isinstance(change["offset"], int)


def test_the_receipt_names_the_revisions_of_both_ends(tmp_path: Path) -> None:
    """`revision_before` is the file the run read, not the one it left behind.

    A receipt exists to put a file back, so the revision it names has to be the
    one its spans were computed against. Overwriting that with the
    post-migration hash recorded a revision of a file that no longer exists —
    and `revision_before` is the field a reader checks before trusting a
    receipt, so the one field that has to mean something was the one that
    could not.
    """
    vault, runtime = _vault(tmp_path)

    _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
    )
    payload = json.loads(_receipts(runtime)[0].read_text(encoding="utf-8"))

    assert payload["revision_before"] == content_revision(LEGACY_DOC)
    assert payload["revision_after"] == content_revision(_learnings(vault))
    assert payload["revision_before"] != payload["revision_after"]


def test_a_revert_dry_run_writes_nothing(tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    args = ["learnings-migrate", "--vault-root", str(vault), "--runtime-root", str(runtime)]
    _run(*args, "--apply")
    migrated = _learnings(vault)

    receipt = _receipts(runtime)[0]
    assert _run(*args, "--revert", str(receipt)) == 0
    assert _learnings(vault) == migrated


def test_a_file_edited_since_the_migration_is_left_entirely_alone(
    tmp_path: Path, capsys
) -> None:
    """Half a revert is worse than none: the offset check disqualifies the file."""
    vault, runtime = _vault(tmp_path)
    args = ["learnings-migrate", "--vault-root", str(vault), "--runtime-root", str(runtime)]
    _run(*args, "--apply")
    receipt = _receipts(runtime)[0]

    edited = _learnings(vault).replace("## Format", "## Format (edited)")
    (vault / "Workspace" / "Learnings.md").write_text(edited, encoding="utf-8", newline="")
    capsys.readouterr()

    assert _run(*args, "--revert", str(receipt), "--apply") == 1
    assert _learnings(vault) == edited
    assert "the file changed since the migration" in capsys.readouterr().err


def test_a_receipt_from_another_vault_is_refused(tmp_path: Path, capsys) -> None:
    """The receipt's offsets address one specific file, so reversing from
    another root would be checking the wrong document."""
    vault, runtime = _vault(tmp_path)
    args = ["learnings-migrate", "--vault-root", str(vault), "--runtime-root", str(runtime)]
    _run(*args, "--apply")
    receipt = _receipts(runtime)[0]
    migrated = _learnings(vault)
    capsys.readouterr()

    other, _ = _vault(tmp_path / "other")
    assert _run(
        "learnings-migrate",
        "--vault-root",
        str(other),
        "--runtime-root",
        str(runtime),
        "--revert",
        str(receipt),
        "--apply",
    ) == 1

    assert "not" in capsys.readouterr().err
    assert _learnings(vault) == migrated


def test_an_unreadable_receipt_is_refused_rather_than_half_trusted(
    tmp_path: Path, capsys
) -> None:
    vault, runtime = _vault(tmp_path)
    bogus = tmp_path / "not-a-receipt.json"
    bogus.write_text("{ not json", encoding="utf-8", newline="")

    assert _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--revert",
        str(bogus),
        "--apply",
    ) == 1
    assert "Not a readable learnings-migrate receipt" in capsys.readouterr().err


# ---- findings --------------------------------------------------------------


def test_a_malformed_line_is_reported_kept_and_reflected_in_the_exit_code(
    tmp_path: Path, capsys
) -> None:
    """A line this code cannot read is a line nothing downstream can count.

    So it is reported, kept byte-exact, and the run does not exit 0: a status of
    zero would tell a script the file needs no attention, which is exactly the
    condition that leaves the line unreadable.
    """
    vault, runtime = _vault(
        tmp_path,
        "# Learnings\n\n## Active\n"
        "- [2024-13-45] debugging: an impossible date\n"
        "- A bullet that does migrate.\n",
    )
    before = _learnings(vault)

    code = _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
    )
    captured = capsys.readouterr()
    text = _learnings(vault)

    assert code == 1
    assert "kept as written" in captured.err
    assert "2024-13-45" in captured.err
    # The unreadable line, byte for byte. The other one still migrated: a finding
    # is not a reason to stop, and stopping would leave the file half-converted
    # for no benefit.
    assert "- [2024-13-45] debugging: an impossible date" in text
    assert "A bullet that does migrate." in text
    assert METADATA_MARKER in text
    # Exactly one span rewritten, and it is the readable one.
    assert len(_spans(captured.out)) == 1


def test_a_clean_run_over_clean_content_exits_zero(tmp_path: Path) -> None:
    vault, runtime = _vault(
        tmp_path, "# Learnings\n\n## Active\n- Nothing here to migrate.\n"
    )

    assert _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
    ) == 0


# ---- json ------------------------------------------------------------------


def test_json_output_carries_the_summary_and_the_receipt_path(
    tmp_path: Path, capsys
) -> None:
    vault, runtime = _vault(tmp_path)

    _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--apply",
        "--json",
    )
    summary = json.loads(capsys.readouterr().out)

    assert summary["entries_migrated"] == 3
    assert summary["applied"] is True
    assert lm.read_receipt(Path(summary["receipt_path"])) is not None


def test_a_json_dry_run_claims_no_receipt(tmp_path: Path, capsys) -> None:
    """A receipt for a dry run reverses spans that never moved."""
    vault, runtime = _vault(tmp_path)

    _run(
        "learnings-migrate",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(runtime),
        "--json",
    )
    summary = json.loads(capsys.readouterr().out)

    assert summary["applied"] is False
    assert "receipt_path" not in summary
    assert _receipts(runtime) == []


# ---- the module API, without the CLI ---------------------------------------


def test_migrate_learnings_file_reports_rather_than_writes_on_a_read_error(
    tmp_path: Path,
) -> None:
    vault, runtime = _vault(tmp_path)
    path = vault / "Workspace" / "Learnings.md"
    path.write_bytes(b"# Learnings\n\n## Active\n- \xff\xfe not utf-8\n")

    summary = lm.migrate_learnings_file(vault, workspace="personal", apply=True)

    assert summary["failed"]
    assert summary["rewrites"] == []
    assert path.read_bytes() == b"# Learnings\n\n## Active\n- \xff\xfe not utf-8\n"


def test_a_concurrent_write_is_refused_rather_than_overwritten(
    tmp_path: Path, monkeypatch
) -> None:
    """The spans were recorded against one revision; another landed, so stop."""
    vault, runtime = _vault(tmp_path)
    path = vault / "Workspace" / "Learnings.md"

    from ciao import learnings_migrate

    real = learnings_migrate._write_locked

    def _raced(target: Path, text: str, *, expect: str = "") -> None:
        # An accept landing between this run's read and its write.
        path.write_text(text + "\n- raced in\n", encoding="utf-8", newline="")
        real(target, text, expect=expect)

    monkeypatch.setattr(learnings_migrate, "_write_locked", _raced)
    summary = learnings_migrate.migrate_learnings_file(
        vault, workspace="personal", apply=True
    )

    assert summary["failed"]
    assert "changed while the migration was running" in summary["failed"][0]["error"]
    # The spans were computed, and none of them landed: the count says so, or
    # this run reports a migration over a file it refused to touch.
    assert summary["entries_migrated"] == 0
    assert "- raced in" in path.read_text(encoding="utf-8")


def test_a_new_receipt_path_never_overwrites_an_existing_one(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    first = lm.new_receipt_path(runtime)
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("{}", encoding="utf-8", newline="")

    assert lm.new_receipt_path(runtime) != first
