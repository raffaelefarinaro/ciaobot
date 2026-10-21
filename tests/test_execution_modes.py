"""The credential-adjacency predicate that guards vault-persisted file reads (#1746)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ciao import agent_cli
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
        # Case-insensitive on every platform: Windows and macOS volumes
        # resolve these to the same store (#1746 review round 1).
        ((".ENV",), True),
        ((".Env.Local",), True),
        (("Secrets", "d", "k"), True),
        ((".RUNTIME", "a", "b"), True),
        # `.env` variants beyond `.env.*`.
        ((".envrc",), True),
        ((".env-local",), True),
        ((".env_prod",), True),
        ((".envelope",), False),
    ],
)
def test_is_credential_adjacent(parts: tuple[str, ...], expected: bool) -> None:
    assert is_credential_adjacent(_ROOT.joinpath(*parts)) is expected


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_task_body_refuses_a_symlink_into_the_runtime_directory(tmp_path: Path) -> None:
    """A benign-looking name that points into `.runtime/` is refused, because the
    check runs on the resolved target (#1746 review round 1)."""
    target = tmp_path / ".runtime" / "a" / "b" / "f"
    target.parent.mkdir(parents=True)
    target.write_text("token", encoding="utf-8")
    link = tmp_path / "notes.txt"
    link.symlink_to(target)
    with pytest.raises(agent_cli.UsageError, match="credential"):
        agent_cli._task_body(f"@{link}")
