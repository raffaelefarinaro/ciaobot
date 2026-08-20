"""Moving one note between agent roots, links and all.

The bulk re-home pass moves only tag-obvious notes; every queued row on a real
install is a judgement, so the bulk mover would move none of them. This is the
per-row counterpart, and it is a link rewriter more than a file move: three ref
dialects, in both directions, across two vault directories.

Everything works in install-relative space (`personal/memory-vault/People/Mo.md`),
which is what makes the arithmetic real. An earlier attempt used the rendered
identity space and produced `../../personal/People/Alba.md` for a path three
levels up inside another vault — plus, because `rewrite_references` keyed its
table with one fixed `VAULT_PREFIX`, it rewrote nothing at all in the other
direction while reporting success.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ciao.vault_rehome import move_note_between_roots

MD_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")


def _note(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _install(tmp_path: Path, *, git: bool = True) -> tuple[Path, list]:
    root = tmp_path
    if git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for ws in ("personal", "work"):
        (root / ws / "memory-vault").mkdir(parents=True)
    _note(
        root / "personal" / "memory-vault" / "People" / "Mo.md",
        '---\ntype: person\nrelated:\n  - "People/Alba"\n---\n# Mo\n\n'
        "Sees [[Alba]] and [Alba](./Alba.md).\n",
    )
    _note(
        root / "personal" / "memory-vault" / "People" / "Alba.md",
        '---\ntype: person\nrelated:\n  - "People/Mo"\n---\n# Alba\n\n'
        "With [[Mo]] and [Mo](./Mo.md).\n",
    )
    _note(
        root / "work" / "memory-vault" / "People" / "Zed.md",
        '---\ntype: person\nrelated:\n  - "personal/People/Mo"\n---\n# Zed\n\n'
        "Mentions [[personal/People/Mo]].\n",
    )
    if git:
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "seed"],
            cwd=root, check=True,
        )
    targets = [
        (root / ws / "memory-vault", ws, Path(f"{ws}/memory-vault"))
        for ws in ("personal", "work")
    ]
    return root, targets


def _move(root: Path, targets: list, source: str, to: str, **kw):
    return move_note_between_roots(
        root, source, to, targets=targets, workspaces=["personal", "work"], **kw
    )


def _broken(root: Path) -> list[str]:
    bad = []
    for f in root.rglob("*.md"):
        if ".git" in f.parts:
            continue
        for m in MD_LINK.finditer(f.read_text(encoding="utf-8")):
            ref = m.group(1).split("#")[0]
            if not (f.parent / ref).exists():
                bad.append(f"{f.relative_to(root)} -> {ref}")
    return bad


def test_a_move_leaves_no_broken_markdown_link(tmp_path: Path) -> None:
    """The property that matters. Measured on the real vault too: moving its
    most-linked note broke nothing and repaired four pre-existing links."""
    root, targets = _install(tmp_path)

    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    assert result["refusals"] == []
    assert _broken(root) == []


def test_refs_to_the_moved_note_are_re_spelled_per_root(tmp_path: Path) -> None:
    """Root-relative for its new neighbours, workspace-qualified for everyone
    else. `Zed` already referred to it across a root and now sits beside it."""
    root, targets = _install(tmp_path)

    _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    alba = (root / "personal/memory-vault/People/Alba.md").read_text()
    zed = (root / "work/memory-vault/People/Zed.md").read_text()
    assert '"work/People/Mo"' in alba and "[[work/People/Mo]]" in alba
    assert '"People/Mo"' in zed and "[[People/Mo]]" in zed
    assert "personal/People/Mo" not in zed


def test_the_moved_notes_own_refs_gain_the_root_it_left(tmp_path: Path) -> None:
    """Its `related: People/Alba` was written in personal and is read from work."""
    root, targets = _install(tmp_path)

    _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    moved = (root / "work/memory-vault/People/Mo.md").read_text()
    assert '"personal/People/Alba"' in moved
    assert "[[personal/People/Alba]]" in moved
    assert "../../../personal/memory-vault/People/Alba.md" in moved


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    root, targets = _install(tmp_path)
    before = {p: p.read_text() for p in root.rglob("*.md") if ".git" not in p.parts}

    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work")

    assert result["applied"] is False
    assert result["rewrites"], "a dry run still reports what it would change"
    assert (root / "personal/memory-vault/People/Mo.md").is_file()
    assert {p: p.read_text() for p in root.rglob("*.md") if ".git" not in p.parts} == before


def test_git_records_it_as_a_rename(tmp_path: Path) -> None:
    """A `git mv`, not a copy-and-delete, so history can follow the note.

    Asserted on the INDEX rather than on `git log --follow`: follow relies on
    content similarity, and this rewriter deliberately changes the note's refs, so
    on a small note similarity can fall below the detection threshold. Whether
    git later chooses to follow it is not something the mover controls; staging a
    rename is.
    """
    root, targets = _install(tmp_path)

    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    assert result["git_mv"] == "ok"
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "-M"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout
    renames = [line for line in staged.splitlines() if line.startswith("R")]
    assert any("work/memory-vault/People/Mo.md" in line for line in renames), staged


def test_a_move_outside_a_repository_still_moves(tmp_path: Path) -> None:
    """No repo is not a reason to refuse a per-row move; it only means history
    cannot follow."""
    root, targets = _install(tmp_path, git=False)

    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    assert result["applied"] is True
    assert result["git_mv"] != "ok"
    assert (root / "work/memory-vault/People/Mo.md").is_file()


def test_an_occupied_destination_refuses(tmp_path: Path) -> None:
    """Merging two people's notes is a content decision, never a move."""
    root, targets = _install(tmp_path)
    _note(root / "work" / "memory-vault" / "People" / "Mo.md", "# A different Mo\n")

    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    assert any("already exists" in r for r in result["refusals"])
    assert (root / "personal/memory-vault/People/Mo.md").is_file()


def test_moving_into_its_own_workspace_refuses(tmp_path: Path) -> None:
    root, targets = _install(tmp_path)

    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "personal")

    assert any("already in personal" in r for r in result["refusals"])


def test_an_unregistered_destination_refuses(tmp_path: Path) -> None:
    root, targets = _install(tmp_path)

    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "clientA")

    assert any("not a registered workspace" in r for r in result["refusals"])


def test_a_link_that_cannot_be_resolved_is_left_alone(tmp_path: Path) -> None:
    """Measured on the real vault: the moved note held links in the
    workspace-qualified dialect, which `resolve_vault_link` reads as a relative
    path. Re-spelling those from the new location turned an already-broken link
    into a differently-broken one."""
    root, targets = _install(tmp_path)
    path = root / "personal" / "memory-vault" / "People" / "Mo.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\nAlso [X](work/People/Nobody.md).\n",
        encoding="utf-8",
    )

    _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    moved = (root / "work/memory-vault/People/Mo.md").read_text()
    assert "[X](work/People/Nobody.md)" in moved


def test_the_root_notes_are_swept_too(tmp_path: Path) -> None:
    """MEMORY.md is skipped by the scan as "generated", but only INDEX.md and
    VOCABULARY.md are actually regenerated — so nothing else fixes a link in it.
    This was the single broken link left on the real-vault check."""
    root, targets = _install(tmp_path)
    _note(
        root / "personal" / "memory-vault" / "MEMORY.md",
        "# Memory\n\nSee [Mo](./People/Mo.md).\n",
    )

    _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    memory = (root / "personal/memory-vault/MEMORY.md").read_text()
    assert "./People/Mo.md" not in memory
    assert _broken(root) == []


def test_both_roots_indexes_are_rebuilt(tmp_path: Path) -> None:
    """Derived files naming a note that moved are rebuilt, not rewritten."""
    root, targets = _install(tmp_path)

    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    assert {row["workspace"] for row in result["indexes"]["rebuilt"]} == {"personal", "work"}
    assert (root / "work/memory-vault/INDEX.md").is_file()
    assert "People/Mo" in (root / "work/memory-vault/INDEX.md").read_text()
