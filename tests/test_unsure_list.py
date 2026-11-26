"""Direct appends to Workspace/Unsure.md."""

from __future__ import annotations

from pathlib import Path

from ciao import unsure_list as ul


def test_first_append_creates_the_list_with_a_heading(tmp_path: Path) -> None:
    added = ul.append_unsure(
        tmp_path,
        [ul.UnsureItem("Anna prefers email", reason="one chat", source="chat-1")],
        today="2026-11-26",
    )
    assert added == ["- [ ] Anna prefers email — unsure because one chat; source: chat-1; 2026-11-26"]
    text = (tmp_path / "Workspace" / "Unsure.md").read_text(encoding="utf-8")
    assert text.startswith("# Unsure\n\n- [ ] Anna prefers email")


def test_a_repeat_or_a_ticked_line_is_not_added_again(tmp_path: Path) -> None:
    item = ul.UnsureItem("The deploy runs on Fridays", kind="repo /r")
    assert len(ul.append_unsure(tmp_path, [item], today="2026-11-26")) == 1
    assert ul.append_unsure(tmp_path, [item], today="2026-11-27") == []
    path = tmp_path / "Workspace" / "Unsure.md"
    path.write_text(path.read_text(encoding="utf-8").replace("- [ ]", "- [x]"), encoding="utf-8")
    assert ul.append_unsure(tmp_path, [item], today="2026-11-28") == []
    assert path.read_text(encoding="utf-8").count("deploy runs") == 1


def test_the_same_text_with_a_different_kind_is_a_different_item(tmp_path: Path) -> None:
    ul.append_unsure(tmp_path, [ul.UnsureItem("x y", kind="observation")])
    assert len(ul.append_unsure(tmp_path, [ul.UnsureItem("x y")])) == 1


def test_credential_shaped_and_empty_items_are_skipped(tmp_path: Path) -> None:
    added = ul.append_unsure(
        tmp_path,
        [ul.UnsureItem("api_key: sk-live-abcdef0123456789"), ul.UnsureItem("   "), ul.UnsureItem("fine")],
    )
    assert len(added) == 1 and "fine" in added[0]


def test_text_is_flattened_and_bounded(tmp_path: Path) -> None:
    added = ul.append_unsure(tmp_path, [ul.UnsureItem("one\ntwo " + "w" * 900)], today="2026-11-26")
    assert "\n" not in added[0] and len(added[0]) < 700


def test_existing_content_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "Workspace" / "Unsure.md"
    path.parent.mkdir()
    path.write_text("# Unsure\n\n- [ ] older item — 2026-01-01\n", encoding="utf-8")
    ul.append_unsure(tmp_path, [ul.UnsureItem("newer item")], today="2026-11-26")
    text = path.read_text(encoding="utf-8")
    assert "older item" in text and text.index("older") < text.index("newer")
    assert ul.read_keys(tmp_path) == {"older item", "newer item"}
