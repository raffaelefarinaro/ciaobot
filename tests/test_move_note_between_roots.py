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

import os
import re
import subprocess
import threading
from pathlib import Path

import pytest

from ciao.vault_rehome import move_note_between_roots

MD_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")


def _note(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # As written, not line-translated: the note has the same bytes on every OS.
    path.write_text(body, encoding="utf-8", newline="")


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


def _bytes(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*.md"))
        if ".git" not in p.parts
    }


@pytest.mark.parametrize("method", ["tracked", "hardlink", "copy"])
def test_cooperating_destination_writer_waits_for_publication(tmp_path: Path, monkeypatch, method: str) -> None:
    import errno
    from ciao import vault_rehome
    from ciao.memory_receipts import queue_lock

    root, targets = _install(tmp_path, git=method == "tracked")
    destination = root / "work/memory-vault/People/Mo.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    acquired = threading.Event()
    threads: list[threading.Thread] = []
    saw_early: list[bool] = []
    def writer():
        with queue_lock(destination):
            acquired.set()
            with destination.open("a", encoding="utf-8") as handle:
                handle.write("\nForeign writer.\n")
    def start_writer():
        thread = threading.Thread(target=writer)
        threads.append(thread)
        thread.start()
        saw_early.append(acquired.wait(0.25))
    real_git, real_link = vault_rehome.run_git, vault_rehome.os.link
    def git(root_path, *args):
        if args and args[0] == "mv":
            if method == "tracked":
                start_writer()
                return real_git(root_path, *args)
            return 1, "not tracked"
        return real_git(root_path, *args)
    def link(src, dst, **kwargs):
        start_writer()
        if method == "copy":
            raise OSError(errno.EXDEV, "cross-device")
        return real_link(src, dst, **kwargs)
    monkeypatch.setattr(vault_rehome, "run_git", git)
    monkeypatch.setattr(vault_rehome.os, "link", link)
    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert saw_early == [False]
    assert result["applied"] and acquired.is_set()
    assert "Foreign writer." in destination.read_text(encoding="utf-8")
    assert not (root / "personal/memory-vault/People/Mo.md").exists()


def test_destination_created_during_move_is_never_overwritten(tmp_path: Path, monkeypatch) -> None:
    from ciao import vault_rehome

    root, targets = _install(tmp_path, git=False)
    source = root / "personal/memory-vault/People/Mo.md"
    destination = root / "work/memory-vault/People/Mo.md"
    before = source.read_bytes()

    def concurrent(*args):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"foreign destination\n")
        return 1, "no git"

    monkeypatch.setattr(vault_rehome, "run_git", concurrent)
    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)
    assert not result["applied"]
    assert source.read_bytes() == before
    assert destination.read_bytes() == b"foreign destination\n"


def test_concurrent_link_edit_is_not_overwritten(tmp_path: Path, monkeypatch) -> None:
    from ciao import vault_rehome

    root, targets = _install(tmp_path, git=False)
    source = root / "personal/memory-vault/People/Mo.md"
    source_before = source.read_bytes()
    backlink = root / "personal/memory-vault/People/Alba.md"
    original = vault_rehome.tempfile.NamedTemporaryFile
    changed = False

    def race(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            backlink.write_text("foreign edit\n", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(vault_rehome.tempfile, "NamedTemporaryFile", race)
    monkeypatch.setattr(vault_rehome, "run_git", lambda *args: (1, "no git"))
    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)
    assert not result["applied"]
    assert backlink.read_text(encoding="utf-8") == "foreign edit\n"
    assert source.read_bytes() == source_before


def test_untracked_move_preserves_links_without_replace(tmp_path: Path, monkeypatch) -> None:
    from ciao import vault_rehome

    root, targets = _install(tmp_path, git=False)
    monkeypatch.setattr(vault_rehome, "run_git", lambda *args: (1, "untracked"))
    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)
    assert result["applied"], result["refusals"]
    assert not (root / "personal/memory-vault/People/Mo.md").exists()
    assert (root / "work/memory-vault/People/Mo.md").is_file()
    assert not _broken(root)


def test_untracked_move_copies_exclusively_when_hardlinks_unavailable(tmp_path: Path, monkeypatch) -> None:
    import errno
    from ciao import vault_rehome

    root, targets = _install(tmp_path, git=False)
    source = root / "personal/memory-vault/People/Mo.md"
    source.chmod(0o640)
    source_mode = source.stat().st_mode
    destination = root / "work/memory-vault/People/Mo.md"
    monkeypatch.setattr(vault_rehome, "run_git", lambda *args: (1, "untracked"))
    monkeypatch.setattr(vault_rehome.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross device")))
    timestamp_fds: list[int] = []

    from ciao.os_support import files
    real_set_file_times = files.set_file_times

    def set_file_times(fd, *, atime_ns, mtime_ns):
        timestamp_fds.append(fd)
        return real_set_file_times(fd, atime_ns=atime_ns, mtime_ns=mtime_ns)

    monkeypatch.setattr(files, "set_file_times", set_file_times)
    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)
    assert result["applied"], result["refusals"]
    assert not source.exists()
    assert destination.is_file()
    assert len(timestamp_fds) == 1
    if os.name == "nt":
        import stat
        assert destination.stat().st_mode & stat.S_IWRITE == source_mode & stat.S_IWRITE
    else:
        assert destination.stat().st_mode & 0o777 == 0o640
    assert not _broken(root)


def test_copy_fallback_refuses_racing_destination(tmp_path: Path, monkeypatch) -> None:
    import errno
    from ciao import vault_rehome

    root, targets = _install(tmp_path, git=False)
    source = root / "personal/memory-vault/People/Mo.md"
    before = source.read_bytes()
    dest = root / "work/memory-vault/People/Mo.md"
    original_open = os.open

    def racing_open(path, flags, *args, **kwargs):
        if Path(path) == dest:
            dest.write_bytes(b"foreign\n")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(vault_rehome, "run_git", lambda *args: (1, "untracked"))
    monkeypatch.setattr(vault_rehome.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross device")))
    monkeypatch.setattr(vault_rehome.os, "open", racing_open)
    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)
    assert not result["applied"]
    assert source.read_bytes() == before
    assert dest.read_bytes() == b"foreign\n"


def test_a_late_rewrite_failure_leaves_every_note_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    """A backlink write that fails mid-commit used to strand the earlier ones:
    notes already pointing at the destination while the source still sat at its
    old path, and the move reported as refused. The rewrites are one transaction
    now — a failure anywhere restores every note byte for byte."""
    root, targets = _install(tmp_path)
    before = _bytes(root)

    from ciao import vault_rehome

    real_replace = os.replace
    swaps = {"n": 0}

    def flaky_replace(src, dst, *args, **kwargs):
        if str(dst).endswith(".md"):
            swaps["n"] += 1
            if swaps["n"] == 3:
                raise OSError("disk full")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(vault_rehome.os, "replace", flaky_replace)

    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    assert result["applied"] is False
    assert any("could not rewrite" in r for r in result["refusals"])
    assert result["files_rewritten"] == 3
    assert _bytes(root) == before, "every touched note is byte-identical afterwards"
    assert (root / "personal/memory-vault/People/Mo.md").is_file()
    assert not (root / "work/memory-vault/People/Mo.md").exists()
    assert not list(root.rglob(".*.tmp")), "no temp files left behind"


def test_a_failed_move_rolls_the_committed_rewrites_back(
    tmp_path: Path, monkeypatch
) -> None:
    """The source move participates in the same transaction: if it fails after
    the link commits are in, the link commits come back out."""
    root, targets = _install(tmp_path)
    before = _bytes(root)
    dest = root / "work" / "memory-vault" / "People" / "Mo.md"

    from ciao import vault_rehome

    def blocked_link(src, dst, *args, **kwargs):
        assert Path(dst) == dest
        raise OSError("move refused")

    monkeypatch.setattr(vault_rehome, "run_git", lambda root_, *a: (1, "fatal: nope"))
    monkeypatch.setattr(vault_rehome.os, "link", blocked_link)

    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    assert result["applied"] is False
    assert any("could not move the note" in r for r in result["refusals"])
    assert _bytes(root) == before, "the committed link edits came back out"
    assert (root / "personal/memory-vault/People/Mo.md").is_file()
    assert not dest.exists()
    assert not list(root.rglob(".*.tmp")), "no temp files left behind"


def test_an_applied_multi_backlink_move_writes_exactly_the_predicted_rewrites(
    tmp_path: Path,
) -> None:
    """The transactional commit writes what the dry run predicted, nothing else.

    Guarding the refactor from per-note `write_text` to staged-and-swapped: a
    four-note move (the moved note itself, two backlinks, and MEMORY.md) must
    land byte-for-byte on the dry run's plan with no broken links.
    """
    root, targets = _install(tmp_path)
    _note(
        root / "personal" / "memory-vault" / "MEMORY.md",
        "# Memory\n\nSee [Mo](./People/Mo.md).\n",
    )

    predicted = _move(root, targets, "personal/memory-vault/People/Mo.md", "work")
    applied = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    assert applied["applied"] is True
    assert applied["refusals"] == []
    assert applied["rewrites"] == predicted["rewrites"]
    assert applied["files_rewritten"] == predicted["files_rewritten"] == 4
    for row in applied["rewrites"]:
        text = (root / row["path"]).read_text(encoding="utf-8")
        assert row["to"] in text, row
    assert _broken(root) == []


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
        newline="",
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


def test_a_note_already_at_the_destination_is_not_a_failure(tmp_path: Path) -> None:
    """The state a cancelled handler leaves behind.

    The sweep was 2s of synchronous work inside the event loop on the real vault,
    so a request timed out after the `git mv` and before its queue row was
    dropped. Reporting "no note at ..." then left the row permanently unclickable:
    the operator could neither move it (gone) nor see that it had already moved.
    """
    root, targets = _install(tmp_path)
    _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    again = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    assert again["refusals"] == []
    assert again["already_moved"] is True
    assert again["applied"] is True


def test_a_missing_note_with_no_copy_anywhere_still_refuses(tmp_path: Path) -> None:
    """Idempotence must not become "shrug at anything missing"."""
    root, targets = _install(tmp_path)

    result = _move(root, targets, "personal/memory-vault/People/Ghost.md", "work", apply=True)

    assert any("no note at" in r for r in result["refusals"])


def test_files_that_cannot_mention_the_note_are_not_parsed(
    tmp_path: Path, monkeypatch
) -> None:
    """Any ref carries the note's stem, whatever dialect it uses, so a file
    without the stem cannot mention it.

    Asserted on the PARSES, not on the result: skipping them changes no output, so
    `files_rewritten` is identical either way. It is the work that matters — this
    took the real-vault sweep from 2.03s to 0.35s, and that sweep running
    synchronously inside the event loop is what let a request time out mid-move.
    """
    root, targets = _install(tmp_path)
    for i in range(30):
        _note(
            root / "work" / "memory-vault" / "People" / f"Unrelated{i}.md",
            f"---\ntype: person\n---\n# Unrelated{i}\n\nNothing to do with anyone.\n",
        )

    from ciao import vault_rehome

    parsed: list[str] = []
    real = vault_rehome.rewrite_references

    def counting(text, before, after, *args, **kwargs):
        parsed.append(before)
        return real(text, before, after, *args, **kwargs)

    monkeypatch.setattr(vault_rehome, "rewrite_references", counting)
    result = _move(root, targets, "personal/memory-vault/People/Mo.md", "work", apply=True)

    # Mo itself, plus the two notes that name it. Not the 30 that cannot.
    assert len(parsed) == 3, parsed
    assert result["files_rewritten"] == 3
    assert _broken(root) == []
