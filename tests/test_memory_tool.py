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


def test_migrate_region_caps_restamps_former_default(tmp_path: Path) -> None:
    """A guide stamped by a pre-3000 release must advertise the new default.

    ``ensure_regions`` never rewrites existing markers, so without this
    migration an upgraded install loads a guide that says cap=2200 while the
    runtime enforces 3000 — two different answers to the same question.
    """
    guide = tmp_path / "CLAUDE.md"
    guide.write_text(
        "# Guide\n\n"
        "<!-- ciao:memory:start cap=2200 -->\n"
        "a durable fact\n"
        "<!-- ciao:memory:end -->\n\n"
        "<!-- ciao:profile:start cap=1375 -->\n"
        "<!-- ciao:profile:end -->\n",
        encoding="utf-8",
    )

    assert mt.migrate_region_caps(guide) == ["memory"]

    text = guide.read_text(encoding="utf-8")
    assert "<!-- ciao:memory:start cap=3000 -->" in text
    assert "cap=2200" not in text
    # The profile region is untouched: its default never changed.
    assert "<!-- ciao:profile:start cap=1375 -->" in text
    # Entries survive byte-identical.
    entries, diags = mt.read_region(guide, "memory")
    assert diags == []
    assert entries == ["a durable fact"]


def test_migrate_region_caps_preserves_custom_caps(tmp_path: Path) -> None:
    """Any stamp other than the former default is an intentional custom cap."""
    guide = tmp_path / "CLAUDE.md"
    original = (
        "# Guide\n\n"
        "<!-- ciao:memory:start cap=5000 -->\n"
        "<!-- ciao:memory:end -->\n\n"
        "<!-- ciao:profile:start cap=800 -->\n"
        "<!-- ciao:profile:end -->\n"
    )
    guide.write_text(original, encoding="utf-8")

    assert mt.migrate_region_caps(guide) == []
    assert guide.read_text(encoding="utf-8") == original


def test_migrate_region_caps_is_idempotent_and_tolerates_absence(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "CLAUDE.md"
    guide.write_text(
        "# Guide\n\n<!-- ciao:memory:start cap=2200 -->\n<!-- ciao:memory:end -->\n",
        encoding="utf-8",
    )

    assert mt.migrate_region_caps(guide) == ["memory"]
    # Second run: nothing left to restamp.
    assert mt.migrate_region_caps(guide) == []

    # A missing guide is not created and reports nothing.
    absent = tmp_path / "nested" / "CLAUDE.md"
    assert mt.migrate_region_caps(absent) == []
    assert not absent.exists()


def test_migrate_region_caps_respects_explicit_override(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """An explicit CIAO_MEMORY_CHAR_LIMIT=2200 is a choice, not a stale stamp.

    Restamping the marker to 3000 while the runtime enforces 2200 would
    recreate the guide/runtime disagreement this migration exists to remove,
    and every sync would do it again.
    """
    guide = tmp_path / "CLAUDE.md"
    original = (
        "# Guide\n\n"
        "<!-- ciao:memory:start cap=2200 -->\n"
        "<!-- ciao:memory:end -->\n"
    )
    guide.write_text(original, encoding="utf-8")
    monkeypatch.setenv("CIAO_MEMORY_CHAR_LIMIT", "2200")

    assert mt.migrate_region_caps(guide) == []
    assert guide.read_text(encoding="utf-8") == original


def test_migrate_region_caps_stamps_explicit_override_value(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """With an explicit non-default limit, the stamp follows that limit.

    The guide must advertise the cap the runtime actually enforces, whatever
    its source; a leftover former-default stamp is still drift.
    """
    guide = tmp_path / "CLAUDE.md"
    guide.write_text(
        "# Guide\n\n"
        "<!-- ciao:memory:start cap=2200 -->\n"
        "<!-- ciao:memory:end -->\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_MEMORY_CHAR_LIMIT", "5000")

    assert mt.migrate_region_caps(guide) == ["memory"]
    text = guide.read_text(encoding="utf-8")
    assert "<!-- ciao:memory:start cap=5000 -->" in text
    assert "cap=2200" not in text


def test_migrate_region_caps_ignores_nonnumeric_override(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """A garbage env value falls back to the shipped default, like config."""
    guide = tmp_path / "CLAUDE.md"
    guide.write_text(
        "# Guide\n\n"
        "<!-- ciao:memory:start cap=2200 -->\n"
        "<!-- ciao:memory:end -->\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_MEMORY_CHAR_LIMIT", "lots")

    assert mt.migrate_region_caps(guide) == ["memory"]
    assert "<!-- ciao:memory:start cap=3000 -->" in guide.read_text(
        encoding="utf-8"
    )


def test_migrate_region_caps_reconciles_current_default_stamp(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """A freshly seeded guide stamped with the shipped default follows too.

    Seeding stamps cap=3000; when an explicit override enforces 2200 the
    guide must say 2200, otherwise sync leaves a brand-new guide advertising
    a cap nothing enforces. Only known shipped defaults reconcile — custom
    caps stay.
    """
    guide = tmp_path / "CLAUDE.md"
    guide.write_text(
        "# Guide\n\n"
        "<!-- ciao:memory:start cap=3000 -->\n"
        "<!-- ciao:memory:end -->\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_MEMORY_CHAR_LIMIT", "2200")

    assert mt.migrate_region_caps(guide) == ["memory"]
    text = guide.read_text(encoding="utf-8")
    assert "<!-- ciao:memory:start cap=2200 -->" in text


def test_migrate_region_caps_caller_limit_beats_env(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """A caller-resolved limit (e.g. workspace .env) wins over the process env.

    The standalone `ciao sync-skills` path resolves the workspace dotenv
    itself because memory_tool cannot see it; the parameter must dominate so
    the stamp matches what a server start will enforce.
    """
    guide = tmp_path / "CLAUDE.md"
    guide.write_text(
        "# Guide\n\n"
        "<!-- ciao:memory:start cap=2200 -->\n"
        "<!-- ciao:memory:end -->\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_MEMORY_CHAR_LIMIT", "2200")

    assert mt.migrate_region_caps(guide, char_limit=5000) == ["memory"]
    assert "<!-- ciao:memory:start cap=5000 -->" in guide.read_text(
        encoding="utf-8"
    )


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


def test_entry_expires_after_its_stated_date() -> None:
    """An entry is valid through its stated date, expired the day after."""
    import datetime

    entry = "temporary note [expires: 2026-07-26]"
    assert mt.is_entry_expired(entry, datetime.date(2026, 7, 26)) is False
    assert mt.is_entry_expired(entry, datetime.date(2026, 7, 27)) is True


def test_expiry_filtering_keeps_storage_intact(tmp_path: Path) -> None:
    """Expired entries drop out of the active list but stay in storage.

    The injector once rendered this as an '[active N chars; stored M/limit]'
    header; that renderer is gone, but pruning and audit still need both
    numbers, so read_region must keep expired text in the stored list.
    """
    import datetime

    guide = _guide_with_regions(tmp_path / "CLAUDE.md")
    mt.write_region(
        guide,
        "memory",
        ["durable fact", "temporary note [expires: 2026-01-01]"],
    )

    stored, _diags = mt.read_region(guide, "memory")
    active = [
        e for e in stored if not mt.is_entry_expired(e, datetime.date(2026, 7, 26))
    ]

    assert active == ["durable fact"]
    assert len(stored) == 2
    assert mt.total_chars(stored) > mt.total_chars(active)


def test_noncanonical_expiration_tags_do_not_crash_or_drop(tmp_path: Path) -> None:
    """Malformed or doubled [expires: ...] tags keep the entry, unexpired.

    A wrong date format must not silently retire a fact someone meant to
    keep: only a parseable date past `today` expires an entry.
    """
    import datetime

    guide = _guide_with_regions(tmp_path / "CLAUDE.md")
    entries = [
        "compact [expires: 20260720]",
        "multiple [expires: 2026-07-20] [expires: someday]",
    ]
    mt.write_region(guide, "memory", entries)

    stored, _diags = mt.read_region(guide, "memory")

    assert stored == entries
    assert all(
        not mt.is_entry_expired(e, datetime.date(2026, 7, 26)) for e in stored
    )
