"""git prints a non-ASCII path as quoted octal escapes; the backup must name the real file."""

from __future__ import annotations

import pytest

from ciao.local_session import _porcelain_entries, _unquote_git_path


@pytest.mark.parametrize(
    ("quoted", "expected"),
    [
        (r"M\303\274ller.md", "M\u00fcller.md"),
        (r"a\"b\\c\td", 'a"b\\c\td'),
        (r"plain.md", "plain.md"),
    ],
)
def test_unquote_git_path(quoted: str, expected: str) -> None:
    assert _unquote_git_path(quoted) == expected


def test_porcelain_entries_decode_quoted_paths() -> None:
    porcelain = '?? "memory-vault/M\\303\\274ller.md"\n M memory-vault/a.md\n'
    assert _porcelain_entries(porcelain) == [
        ("??", "memory-vault/M\u00fcller.md"),
        (" M", "memory-vault/a.md"),
    ]
