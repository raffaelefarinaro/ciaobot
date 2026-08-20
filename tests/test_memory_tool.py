"""Unit tests for ``ciao.memory_tool`` region APIs."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import memory_tool as mt


def _guide_with_regions(path: Path, *, memory: str = "", profile: str = "") -> Path:
    body = (
        "# Guide\n\n"
        "Standing directive: always confirm before deleting.\n\n"
        "<!-- ciao:memory:start cap=2200 -->\n"
        "## Agent memory\n\n"
        f"{memory}"
        "<!-- ciao:memory:end -->\n\n"
        "<!-- ciao:profile:start cap=1375 -->\n"
        "## User profile\n\n"
        f"{profile}"
        "<!-- ciao:profile:end -->\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


def test_parse_and_serialize_roundtrip() -> None:
    raw = "alpha\n§\nbeta\n§\ngamma"
    entries = mt.parse_entries(raw)
    assert entries == ["alpha", "beta", "gamma"]
    assert mt.parse_entries(mt.serialize_entries(entries)) == entries


def test_parse_handles_missing_newlines_around_sep() -> None:
    raw = "alpha\n§\nbeta§gamma"
    assert mt.parse_entries(raw) == ["alpha", "beta", "gamma"]


def test_serialize_empty_returns_empty_string() -> None:
    assert mt.serialize_entries([]) == ""


def test_normalize_keeps_invisible_unicode() -> None:
    text = "hello\u200bworld"
    assert "\u200b" in mt._normalize(text)


def test_contains_invisible_unicode() -> None:
    assert mt.contains_invisible_unicode("x\u200by") is True
    assert mt.contains_invisible_unicode("plain") is False


def test_ensure_regions_appends_missing(tmp_path: Path) -> None:
    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Guide\n\nHello.\n", encoding="utf-8")
    added = mt.ensure_regions(guide)
    assert set(added) == {"memory", "profile"}
    text = guide.read_text(encoding="utf-8")
    assert "<!-- ciao:memory:start" in text
    assert "<!-- ciao:profile:start" in text
    assert mt.ensure_regions(guide) == []


def test_ensure_regions_creates_missing_guide(tmp_path: Path) -> None:
    guide = tmp_path / "CLAUDE.md"
    assert mt.ensure_regions(guide) == ["memory", "profile"]
    assert guide.is_file()


def test_read_and_write_region(tmp_path: Path) -> None:
    guide = _guide_with_regions(tmp_path / "CLAUDE.md")
    mt.write_region(guide, "memory", ["alpha", "beta"])
    entries, diags = mt.read_region(guide, "memory")
    assert diags == []
    assert entries == ["alpha", "beta"]
    usage = mt.region_usage(entries, 2200)
    assert usage["entry_count"] == 2
    assert usage["used_chars"] == mt.total_chars(entries)


def test_read_region_accepts_legacy_user_alias(tmp_path: Path) -> None:
    guide = _guide_with_regions(
        tmp_path / "CLAUDE.md",
        profile=mt.serialize_entries(["Raffa"]),
    )
    entries, diags = mt.read_region(guide, "user")
    assert diags == []
    assert entries == ["Raffa"]


def test_read_region_missing_markers_returns_empty(tmp_path: Path) -> None:
    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Guide\n", encoding="utf-8")
    entries, diags = mt.read_region(guide, "memory")
    assert entries == []
    assert any(d.code == "missing" for d in diags)


def test_diagnose_duplicated_and_inverted(tmp_path: Path) -> None:
    guide = tmp_path / "CLAUDE.md"
    guide.write_text(
        "<!-- ciao:memory:start -->\n"
        "<!-- ciao:memory:start -->\n"
        "<!-- ciao:memory:end -->\n",
        encoding="utf-8",
    )
    diags = mt.diagnose_region(guide.read_text(encoding="utf-8"), "memory")
    assert any(d.code == "duplicated" for d in diags)

    guide.write_text(
        "<!-- ciao:memory:end -->\n<!-- ciao:memory:start -->\n",
        encoding="utf-8",
    )
    diags = mt.diagnose_region(guide.read_text(encoding="utf-8"), "memory")
    assert any(d.code == "inverted" for d in diags)


def test_write_region_refuses_bad_markers(tmp_path: Path) -> None:
    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# no markers\n", encoding="utf-8")
    with pytest.raises(ValueError):
        mt.write_region(guide, "memory", ["x"])


def test_prune_expired_entries_keeps_native_guide_storage(tmp_path: Path) -> None:
    import datetime

    guide = _guide_with_regions(
        tmp_path / "CLAUDE.md",
        memory=mt.serialize_entries([
            "keep this",
            "remove this [expires: 2020-01-01]",
            "keep malformed [expires: tomorrow]",
        ]),
    )
    result = mt.prune_expired_entries(
        guide, today=datetime.date(2026, 8, 16)
    )
    assert result["ok"] is True
    assert result["removed"]["memory"] == 1
    entries, _ = mt.read_region(guide, "memory")
    assert entries == ["keep this", "keep malformed [expires: tomorrow]"]


def test_update_region_enforces_bound_and_reports_status(tmp_path: Path) -> None:
    guide = _guide_with_regions(tmp_path / "CLAUDE.md")
    result = mt.update_region(
        guide,
        "memory",
        action="add",
        entry="a durable fact",
        char_limit=100,
    )
    assert result["changed"] is True
    status = mt.memory_status(guide, memory_char_limit=100)
    assert status["regions"]["memory"]["entry_count"] == 1
    # The cap is ADVISORY, which is what it was always documented to be. Refusing
    # here turned a budget into a wall — and refusing does not shrink the region
    # either: the fact just stays queued and the region stays exactly as full.
    # Bounding it is consolidation's job, during memory curation.
    over = mt.update_region(
        guide,
        "memory",
        action="add",
        entry="x" * 100,
        char_limit=100,
    )
    assert over["changed"] is True
    assert over["over_cap"] is True
    assert over["char_limit"] == 100
    assert over["used_chars"] > 100
    # And the entry really is in the guide, not merely reported as written.
    assert "x" * 100 in guide.read_text(encoding="utf-8")


def test_migrate_legacy_files(tmp_path: Path) -> None:
    guide = _guide_with_regions(tmp_path / "CLAUDE.md")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "memory.md").write_text("prefers dark mode\n", encoding="utf-8")
    (legacy / "user.md").write_text("Name: Raffa\n", encoding="utf-8")
    result = mt.migrate_legacy_files(guide, memory_dir=legacy)
    assert result["ok"] is True
    assert (legacy / "memory.md.migrated").is_file()
    assert (legacy / "user.md.migrated").is_file()
    mem, _ = mt.read_region(guide, "memory")
    profile, _ = mt.read_region(guide, "profile")
    assert "prefers dark mode" in mem
    assert "Name: Raffa" in profile


def test_user_char_limit_defaults_agree() -> None:
    from ciao.config import CiaoConfig

    assert mt.DEFAULT_USER_CHAR_LIMIT == 1375
    assert CiaoConfig.__dataclass_fields__["user_char_limit"].default == 1375


def test_the_advisory_cap_never_blocks_a_write(tmp_path: Path) -> None:
    """The cap is a budget, not a gate.

    Enforcing it as a refusal made the accept button dead for 67 of 130 queued
    proposals on a real vault — and refusing does not reclaim a single character:
    the fact stays queued and the region stays exactly as full. Only consolidation
    shrinks it, which is memory curation's job.
    """
    guide = _guide_with_regions(tmp_path / "CLAUDE.md")

    for i in range(6):
        result = mt.update_region(
            guide, "memory", action="add", entry=f"fact {i} " + "y" * 40, char_limit=50
        )
        assert result["changed"] is True, i

    status = mt.memory_status(guide, memory_char_limit=50)
    assert status["regions"]["memory"]["entry_count"] == 6
    assert mt.update_region(
        guide, "memory", action="add", entry="one more", char_limit=50
    )["over_cap"] is True


def test_a_write_inside_the_cap_is_not_flagged(tmp_path: Path) -> None:
    guide = _guide_with_regions(tmp_path / "CLAUDE.md")

    result = mt.update_region(
        guide, "memory", action="add", entry="short", char_limit=1000
    )

    assert result["over_cap"] is False
    assert result["char_limit"] == 1000


def test_no_cap_means_no_over_cap_reporting(tmp_path: Path) -> None:
    """A caller that passes no limit gets no opinion about one."""
    guide = _guide_with_regions(tmp_path / "CLAUDE.md")

    result = mt.update_region(guide, "memory", action="add", entry="short")

    assert "over_cap" not in result
    assert "char_limit" not in result
