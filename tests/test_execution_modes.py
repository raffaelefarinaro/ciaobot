"""The credential-adjacency predicate that guards vault-persisted file reads (#1746)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.execution_modes import is_credential_adjacent

_ROOT = Path("/a")


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        ((".runtime", "x"), True),
        ((".runtime", "x", "y", "z"), True),
        (("secrets", "d", "k"), True),
        ((".env",), True),
        ((".env.local",), True),
        (("notes", "env.md"), False),
        (("notes", ".envelope"), False),
        (("my-secrets-notes", "x.md"), False),
    ],
)
def test_is_credential_adjacent(parts: tuple[str, ...], expected: bool) -> None:
    assert is_credential_adjacent(_ROOT.joinpath(*parts)) is expected
