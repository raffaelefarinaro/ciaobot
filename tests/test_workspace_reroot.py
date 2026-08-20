"""V2 gate: fixture assertions for the per-workspace re-rooting plan.

The plan half is read only, so these prove classification and refusal. The
apply half is gated on this file plus the real-data rehearsal, because a
migration that can stop halfway is worse than one that refuses outright.

Every shape the census reports on the real vault needs a fixture counterpart
here; one that does not is a coverage hole. The reference vault reports: notes
under two registered workspaces, a Logs/ tree holding most of them, a
Templates/ folder, an .obsidian/ directory, three generated notes loose at the
vault root, and a .DS_Store beside them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ciao.workspace_reroot import plan, read_receipt, rehearse, write_receipt


def _vault(tmp_path: Path, *, workspaces: tuple[str, ...] = ("personal", "work")) -> Path:
    """A vault shaped like the real one, small enough to assert over."""
    vault = tmp_path / "memory-vault"
    for name in workspaces:
        (vault / name / "People").mkdir(parents=True)
        (vault / name / "People" / "Peter.md").write_text(
            "---\ntype: person\n---\n# Peter\n", encoding="utf-8"
        )
    (vault / "Logs" / "Chats" / "chat-1").mkdir(parents=True)
    (vault / "Logs" / "Chats" / "chat-1" / "session.md").write_text("log\n", encoding="utf-8")
    (vault / "Templates").mkdir()
    (vault / "Templates" / "person.md").write_text("tpl\n", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "app.json").write_text("{}\n", encoding="utf-8")
    for generated in ("INDEX.md", "MEMORY.md", "VOCABULARY.md"):
        (vault / generated).write_text("generated\n", encoding="utf-8")
    (vault / ".DS_Store").write_bytes(b"\x00")
    return vault


def _tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_plan_classifies_every_path_and_does_not_refuse(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    result = plan(tmp_path, vault, ["personal", "work"])

    assert result.refused is False
    assert result.unclassified == []
    assert result.refusals == []

    moves = {(m.source, m.destination) for m in result.moves}
    assert moves == {
        ("memory-vault/Logs", "Logs"),
        ("memory-vault/Templates", "templates-src"),
        ("memory-vault/personal", "personal/memory-vault"),
        ("memory-vault/work", "work/memory-vault"),
    }
    assert result.global_keeps == ["memory-vault/.obsidian"]
    assert sorted(result.regenerated) == [
        "memory-vault/INDEX.md",
        "memory-vault/MEMORY.md",
        "memory-vault/VOCABULARY.md",
    ]
    assert result.ignored == ["memory-vault/.DS_Store"]


def test_plan_is_read_only(tmp_path: Path) -> None:
    """The load-bearing guarantee: planning cannot change the vault.

    Hashes every file before and after, and asserts no path appeared or vanished.
    """
    vault = _vault(tmp_path)
    before = _tree_hashes(vault)

    plan(tmp_path, vault, ["personal", "work"])

    after = _tree_hashes(vault)
    assert before == after


def test_every_path_lands_in_exactly_one_bucket(tmp_path: Path) -> None:
    """Conservation at the top level: no path is dropped and none is counted twice."""
    vault = _vault(tmp_path)
    result = plan(tmp_path, vault, ["personal", "work"])

    classified = (
        [m.source for m in result.moves]
        + result.global_keeps
        + result.regenerated
        + result.ignored
    )
    assert len(classified) == len(set(classified)), "a path was classified twice"

    on_disk = {f"memory-vault/{p.name}" for p in vault.iterdir()}
    assert set(classified) == on_disk


def test_an_unregistered_directory_refuses(tmp_path: Path) -> None:
    """A directory nobody registered has no destination, so the plan refuses.

    This is the gap the census exists to find: it reported .obsidian, Logs and
    Templates as unregistered, and each needed an explicit decision before the
    migration could be trusted.
    """
    vault = _vault(tmp_path)
    (vault / "research").mkdir()

    result = plan(tmp_path, vault, ["personal", "work"])

    assert result.refused is True
    assert any("research" in item for item in result.unclassified)


def test_an_unrecognised_loose_file_refuses(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "scratch.md").write_text("notes\n", encoding="utf-8")

    result = plan(tmp_path, vault, ["personal", "work"])

    assert result.refused is True
    assert any("scratch.md" in item for item in result.unclassified)


def test_a_symlink_refuses_rather_than_being_moved(tmp_path: Path) -> None:
    """A symlink can point outside the vault, so moving it is not conservative."""
    vault = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "linked").symlink_to(outside)

    result = plan(tmp_path, vault, ["personal", "work"])

    assert result.refused is True
    assert any("symlink" in item for item in result.unclassified)


def test_a_registered_workspace_with_no_vault_is_reported_not_guessed(tmp_path: Path) -> None:
    vault = _vault(tmp_path, workspaces=("personal",))

    result = plan(tmp_path, vault, ["personal", "work"])

    assert result.refused is True
    assert any("work" in r and "no vault directory" in r for r in result.refusals)


def test_a_non_empty_destination_refuses(tmp_path: Path) -> None:
    """Never write into a root that already has content."""
    vault = _vault(tmp_path)
    (tmp_path / "personal").mkdir()
    (tmp_path / "personal" / "leftover.md").write_text("x\n", encoding="utf-8")

    result = plan(tmp_path, vault, ["personal", "work"])

    assert result.refused is True
    assert any("already exists and is not empty" in r for r in result.refusals)


def test_no_registered_workspace_refuses(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    result = plan(tmp_path, vault, [])

    assert result.refused is True
    assert any("no registered workspace" in r for r in result.refusals)


def test_a_missing_vault_refuses(tmp_path: Path) -> None:
    result = plan(tmp_path, tmp_path / "absent", ["personal"])

    assert result.refused is True
    assert any("not a directory" in r for r in result.refusals)


def test_rehearse_never_records_migrated(tmp_path: Path) -> None:
    """A rehearsal must not make the real migration look already done.

    This is the D4 defect that bit the earlier migrations: read_receipt gated on
    the receipt existing, so a survey receipt permanently blocked the real run.
    """
    vault = _vault(tmp_path)
    runtime = tmp_path / ".runtime"
    runtime.mkdir()

    payload = rehearse(tmp_path, vault, ["personal", "work"], runtime)

    assert payload["status"] == "surveyed"
    assert read_receipt(runtime) is None


def test_rehearse_records_a_refusal_as_refused(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "research").mkdir()
    runtime = tmp_path / ".runtime"
    runtime.mkdir()

    payload = rehearse(tmp_path, vault, ["personal", "work"], runtime)

    assert payload["status"] == "refused"
    assert read_receipt(runtime) is None


def test_read_receipt_only_accepts_migrated(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()

    write_receipt(runtime, {"status": "surveyed"})
    assert read_receipt(runtime) is None

    write_receipt(runtime, {"status": "migrated", "moves": []})
    assert read_receipt(runtime) is not None


def test_write_receipt_keeps_the_earlier_one(tmp_path: Path) -> None:
    """An earlier receipt holds the reverse map, so it is never overwritten away."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir()

    write_receipt(runtime, {"status": "migrated", "marker": "first"})
    write_receipt(runtime, {"status": "surveyed", "marker": "second"})

    archived = list((runtime / "migration").glob("workspace-rooting.*.json"))
    assert archived, "the earlier receipt was lost"
    kept = json.loads(archived[0].read_text(encoding="utf-8"))
    assert kept["marker"] == "first"
