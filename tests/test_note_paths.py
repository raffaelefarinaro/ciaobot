"""Confining a note path to its vault (`ciao.note_paths`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import note_paths as np_

NOTE = "notes/inside.md"


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    return vault


def _confined_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A vault next to an outside folder, wired with every trap worth trying."""
    vault = _vault(tmp_path)
    _write(vault, NOTE, "inside\n")
    _write(vault, "notes/plain.txt", "not markdown\n")
    (vault / "folder.md").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = _write(outside, "secret.md", "secret\n")
    (vault / "link-dir").symlink_to(outside, target_is_directory=True)
    (vault / "link-file.md").symlink_to(secret)
    return vault, secret


@pytest.mark.parametrize(
    "relative_path, why",
    [
        ("", "an empty path names nothing"),
        ("../outside/secret.md", "traversal out of the vault"),
        ("notes/../../outside/secret.md", "traversal in the middle"),
        ("/etc/hosts", "an absolute path"),
        ("link-dir/secret.md", "a symlinked ancestor"),
        ("link-file.md", "a symlinked file"),
        ("notes/missing.md", "a note that does not exist"),
        ("notes/plain.txt", "a file that is not Markdown"),
        ("folder.md", "a directory named like a note"),
    ],
)
def test_note_target_confinement(tmp_path, relative_path, why):
    vault, secret = _confined_vault(tmp_path)
    with pytest.raises(np_.NoteTargetRefused):
        np_.resolve_note_path(vault, relative_path)
    assert secret.read_bytes() == b"secret\n", why


def test_a_note_inside_the_vault_resolves(tmp_path):
    vault, _secret = _confined_vault(tmp_path)
    assert np_.resolve_note_path(vault, NOTE) == np_.canonical_vault(vault) / NOTE


def test_a_vault_root_that_is_not_a_directory_is_refused(tmp_path):
    with pytest.raises(np_.NoteTargetRefused):
        np_.canonical_vault(tmp_path / "missing")


def _case_insensitive_filesystem(tmp_path: Path) -> bool:
    """Probed rather than assumed: CI also runs on a case-sensitive filesystem."""
    probe = tmp_path / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    return (tmp_path / "caseprobe").exists()


def test_both_spellings_of_a_name_are_one_file(tmp_path):
    if not _case_insensitive_filesystem(tmp_path):
        pytest.skip("a case-sensitive filesystem has two genuinely different names")
    vault = _vault(tmp_path)
    _write(vault, "Topics/Alpha.md", "base\n")
    root = np_.canonical_vault(vault)

    canonical = np_.resolve_note_path(vault, "Topics/Alpha.md")
    other = np_.resolve_note_path(vault, "topics/alpha.md")

    assert canonical == root / "Topics" / "Alpha.md"
    assert other == canonical, "both spellings must name one file"
    assert other.relative_to(root).as_posix() == "Topics/Alpha.md"


def test_a_name_two_files_claim_is_refused(tmp_path):
    if _case_insensitive_filesystem(tmp_path):
        pytest.skip("a case-insensitive filesystem cannot hold both spellings")
    vault = _vault(tmp_path)
    upper = _write(vault, "Topics/Alpha.md", "the upper one\n")
    lower = _write(vault, "Topics/alpha.md", "the lower one\n")

    with pytest.raises(np_.NoteTargetRefused, match="no such note"):
        np_.resolve_note_path(vault, "Topics/ALPHA.md")

    assert np_.resolve_note_path(vault, "Topics/Alpha.md") == upper
    assert np_.resolve_note_path(vault, "Topics/alpha.md") == lower


def test_a_spelling_the_filesystem_does_not_resolve_is_refused(tmp_path):
    if _case_insensitive_filesystem(tmp_path):
        pytest.skip("a case-insensitive filesystem resolves both spellings to one file")
    vault = _vault(tmp_path)
    path = _write(vault, "Notes/Alpha.md", "the note as it stands\n")

    with pytest.raises(np_.NoteTargetRefused, match="no such note"):
        np_.resolve_note_path(vault, "notes/alpha.md")
    with pytest.raises(np_.NoteTargetRefused, match="no such note"):
        np_.resolve_note_path(vault, "Notes/ALPHA.md")
    assert np_.resolve_note_path(vault, "Notes/Alpha.md") == path


def test_the_tie_between_two_case_variants_on_a_resolving_filesystem_is_refused(tmp_path, monkeypatch):
    """Two entries differing only in case, one of which the filesystem resolved."""
    vault = _vault(tmp_path)
    _write(vault, "Topics/Alpha.md", "x\n")
    parent = np_.canonical_vault(vault) / "Topics"
    real = np_.os.scandir

    class _Entry:
        def __init__(self, name):
            self.name = name

    class _Scan:
        def __init__(self, names):
            self._names = names

        def __enter__(self):
            return [_Entry(n) for n in self._names]

        def __exit__(self, *exc):
            return False

    def fake(path):
        if Path(path) == parent:
            return _Scan(["Alpha.md", "ALPHA.md"])
        return real(path)

    monkeypatch.setattr(np_.os, "scandir", fake)
    # The caller's spelling exists on disk (so the filesystem resolved it) but is
    # not either listed name exactly: the tie must be refused, not guessed.
    if (parent / "alpha.md").exists():
        with pytest.raises(np_.NoteTargetRefused, match="more than one entry"):
            np_.on_disk_name(parent, "alpha.md")
    else:
        pytest.skip("a case-sensitive filesystem never resolves the third spelling")


@pytest.mark.parametrize(
    "relative_path",
    [
        "Workspace/Memory-Proposals.md",
        "Workspace/Curation-Log.md",
        "workspace/memory-consolidations.md",
        "Workspace/Unsure.md",
    ],
)
def test_vault_bookkeeping_is_refused(tmp_path, relative_path):
    vault = _vault(tmp_path)
    _write(vault, relative_path, "- a line the pipeline owns\n")
    _write(vault, "projects/acme/workspace/Curation-Log.md", "a real note\n")
    _write(vault, "Top-Level.md", "a real note at the root\n")

    with pytest.raises(np_.NoteTargetRefused):
        np_.resolve_note_path(vault, relative_path)

    # A note that merely shares a name outside Workspace/, and a root note, stay writable.
    root = np_.canonical_vault(vault)
    assert np_.resolve_note_path(vault, "projects/acme/workspace/Curation-Log.md").is_relative_to(root)
    assert np_.resolve_note_path(vault, "Top-Level.md") == root / "Top-Level.md"
