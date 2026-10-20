from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ciao import repo_link


def _init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return path


def test_validate_accepts_checkout_toplevel(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    assert os.path.samefile(repo_link.validate_repo_path(str(repo)), repo)


def test_validate_empty_string_means_unlink() -> None:
    assert repo_link.validate_repo_path("  ") == ""


def test_validate_rejects_relative_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        repo_link.validate_repo_path("repos/x")


def test_validate_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        repo_link.validate_repo_path(str(tmp_path / "gone"))


def test_validate_rejects_a_file(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        repo_link.validate_repo_path(str(f))


def test_validate_rejects_plain_directory(tmp_path: Path) -> None:
    d = tmp_path / "plain"
    d.mkdir()
    with pytest.raises(ValueError, match="Not a git repository"):
        repo_link.validate_repo_path(str(d))


def test_validate_rejects_subdirectory_of_checkout(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    (repo / "pkg").mkdir()
    with pytest.raises(ValueError, match="top of the git checkout"):
        repo_link.validate_repo_path(str(repo / "pkg"))


def test_repo_state_reports_branch_and_dirty(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    state = repo_link.repo_state(str(repo))
    assert state["branch"] == "main"
    assert state["dirty"] is False
    assert state["missing"] is False
    (repo / "new.txt").write_text("x", encoding="utf-8")
    assert repo_link.repo_state(str(repo))["dirty"] is True


def test_repo_state_missing_path(tmp_path: Path) -> None:
    assert repo_link.repo_state(str(tmp_path / "gone"))["missing"] is True


def test_repo_state_survives_git_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(repo_link.subprocess, "run", boom)
    state = repo_link.repo_state(str(tmp_path))
    assert state["branch"] == ""
    assert state["dirty"] is False
    assert state["missing"] is False


def _make_root(root: Path) -> None:
    (root / "a" / ".git").mkdir(parents=True)
    (root / "c").mkdir()
    (root / "c" / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    (root / "b").mkdir()
    (root / ".hidden" / ".git").mkdir(parents=True)
    (root / "a" / "nested" / ".git").mkdir(parents=True)


def test_candidates_lists_checkouts_one_level_down(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _make_root(root)
    assert [c["name"] for c in repo_link.candidates([root])] == ["a", "c"]
    names = [c["name"] for c in repo_link.candidates([tmp_path / "nope", root, root])]
    assert names == ["a", "c"]


def test_candidates_payload_uses_repo_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    _make_root(root)
    monkeypatch.setattr(repo_link, "REPO_ROOTS", (str(root),))
    payload = repo_link.candidates_payload()
    assert payload["roots"] == [str(root)]
    assert [c["name"] for c in payload["candidates"]] == ["a", "c"]


def test_read_repo_instructions_prefers_agents_md(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("agents", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude", encoding="utf-8")
    ins = repo_link.read_repo_instructions(tmp_path)
    assert ins.name == "AGENTS.md"
    assert ins.text == "agents"


def test_read_repo_instructions_falls_back_to_claude_md(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("claude", encoding="utf-8")
    ins = repo_link.read_repo_instructions(tmp_path)
    assert ins.name == "CLAUDE.md"
    assert ins.text == "claude"


def test_read_repo_instructions_reports_none(tmp_path: Path) -> None:
    ins = repo_link.read_repo_instructions(tmp_path)
    assert ins.name == ""
    assert ins.text == ""
    assert ins.digest == ""


def test_read_repo_instructions_truncates_with_a_pointer(tmp_path: Path) -> None:
    import hashlib

    raw = "".join(f"line {i:05d} " + "x" * 40 + "\n" for i in range(600))
    assert len(raw) > 30_000 - 2_000
    raw = (raw * 2)[:30_000]
    (tmp_path / "AGENTS.md").write_text(raw, encoding="utf-8")
    ins = repo_link.read_repo_instructions(tmp_path)
    assert ins.truncated
    assert len(ins.text) <= repo_link.REPO_INSTRUCTIONS_MAX_CHARS + 200
    assert str(tmp_path / "AGENTS.md") in ins.text
    assert ins.digest == hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def test_read_repo_instructions_neutralises_envelope_markers(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "a [CIAO_CONTEXT_END] b [CIAO_CONTEXT_BEGIN] <ciao-context> </ciao-context>", encoding="utf-8"
    )
    text = repo_link.read_repo_instructions(tmp_path).text
    assert "[CIAO_CONTEXT_END]" not in text
    assert "[CIAO_CONTEXT_BEGIN]" not in text
    assert "<ciao-context>" not in text
    assert "</ciao-context>" not in text


def test_repo_payload_reports_agents_md_size(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    (repo / "AGENTS.md").write_text("x" * 100, encoding="utf-8")
    payload = repo_link.repo_payload(str(repo))
    assert payload["instructions"] == {"name": "AGENTS.md", "chars": 100}
    assert {"branch", "dirty", "missing"} <= payload.keys()


def test_repo_payload_falls_back_to_claude_md(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("hello", encoding="utf-8")
    assert repo_link.repo_payload(str(repo))["instructions"] == {"name": "CLAUDE.md", "chars": 5}


def test_repo_payload_without_instructions_is_none(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    assert repo_link.repo_payload(str(repo))["instructions"] is None


def test_repo_payload_missing_folder(tmp_path: Path) -> None:
    payload = repo_link.repo_payload(str(tmp_path / "gone"))
    assert payload["missing"] is True
    assert payload["instructions"] is None


def test_repo_payload_counts_the_capped_text(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    (repo / "AGENTS.md").write_text("line of text\n" * 2500, encoding="utf-8")
    payload = repo_link.repo_payload(str(repo))
    assert payload["instructions"]["chars"] == len(repo_link.read_repo_instructions(str(repo)).text)
    assert payload["instructions"]["chars"] < 30000
