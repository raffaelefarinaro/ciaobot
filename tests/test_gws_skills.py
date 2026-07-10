"""Tests for ``ciao.gws_skills``: regeneration of the packaged gws-* skills.

The real generation shells out to ``gws generate-skills``; here we inject a
fake generator so the decision/curation logic is exercised without the CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import gws_skills


def _write_skill(root: Path, name: str, body: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def _fake_generator(payload: dict[str, str]):
    """Return a generator that writes ``payload`` into ``<dest>/skills``."""

    def _gen(dest: Path) -> None:
        skills = dest / "skills"
        for name, body in payload.items():
            _write_skill(skills, name, body)

    return _gen


def test_shipped_gws_skills_discovers_prefixed_dirs(tmp_path: Path) -> None:
    _write_skill(tmp_path, "gws-gmail", "x")
    _write_skill(tmp_path, "gws-shared", "y")
    _write_skill(tmp_path, "web-research", "z")  # not a gws skill
    (tmp_path / "gws-empty").mkdir()  # no SKILL.md -> excluded
    assert gws_skills.shipped_gws_skills(tmp_path) == ["gws-gmail", "gws-shared"]


def test_strip_community_etiquette_removes_section() -> None:
    text = (
        "# gws — Shared Reference\n\n"
        "## Shell Tips\n\n- tip\n\n"
        "## Community & Feedback Etiquette\n\n- star the repo\n- open issues\n"
    )
    out = gws_skills.strip_community_etiquette(text)
    assert "Community & Feedback Etiquette" not in out
    assert "star the repo" not in out
    assert out.endswith("- tip\n")


def test_strip_community_etiquette_is_idempotent() -> None:
    text = "# Title\n\n## Shell Tips\n\n- tip\n"
    assert gws_skills.strip_community_etiquette(text) == text


def test_strip_community_etiquette_keeps_following_section() -> None:
    text = (
        "## Community & Feedback Etiquette\n\n- star\n\n"
        "## Upstream docs\n\n- link\n"
    )
    out = gws_skills.strip_community_etiquette(text)
    assert "Community & Feedback Etiquette" not in out
    assert "## Upstream docs" in out
    assert "- link" in out


def test_regenerate_updates_changed_and_strips_bloat(tmp_path: Path) -> None:
    # Currently shipped (stale) copies.
    _write_skill(tmp_path, "gws-gmail", "old gmail\n")
    _write_skill(tmp_path, "gws-shared", "shared v1\n")

    generated = {
        "gws-gmail": "new gmail\n",
        "gws-shared": (
            "shared v2\n\n## Community & Feedback Etiquette\n\n- star the repo\n"
        ),
        # Extra upstream skills we do not ship are ignored.
        "gws-keep": "keep\n",
        "persona-researcher": "persona\n",
    }
    result = gws_skills.regenerate_stock_gws_skills(
        tmp_path, generator=_fake_generator(generated)
    )

    assert set(result.updated) == {"gws-gmail", "gws-shared"}
    assert result.missing == []
    assert (tmp_path / "gws-gmail" / "SKILL.md").read_text() == "new gmail\n"
    shared = (tmp_path / "gws-shared" / "SKILL.md").read_text()
    assert shared == "shared v2\n"
    # Non-shipped upstream skills are not added to the packaged set.
    assert not (tmp_path / "gws-keep").exists()
    assert not (tmp_path / "persona-researcher").exists()


def test_regenerate_reports_unchanged(tmp_path: Path) -> None:
    _write_skill(tmp_path, "gws-gmail", "same\n")
    result = gws_skills.regenerate_stock_gws_skills(
        tmp_path, generator=_fake_generator({"gws-gmail": "same\n"})
    )
    assert result.updated == []
    assert result.unchanged == ["gws-gmail"]
    assert result.changed is False


def test_regenerate_flags_missing_from_generator(tmp_path: Path) -> None:
    _write_skill(tmp_path, "gws-gmail", "g\n")
    _write_skill(tmp_path, "gws-dropped", "d\n")
    result = gws_skills.regenerate_stock_gws_skills(
        tmp_path, generator=_fake_generator({"gws-gmail": "g2\n"})
    )
    assert result.updated == ["gws-gmail"]
    assert result.missing == ["gws-dropped"]
    # A skill the generator no longer produces is left untouched.
    assert (tmp_path / "gws-dropped" / "SKILL.md").read_text() == "d\n"


def test_regenerate_dry_run_does_not_write(tmp_path: Path) -> None:
    _write_skill(tmp_path, "gws-gmail", "old\n")
    result = gws_skills.regenerate_stock_gws_skills(
        tmp_path, generator=_fake_generator({"gws-gmail": "new\n"}), write=False
    )
    assert result.updated == ["gws-gmail"]
    assert (tmp_path / "gws-gmail" / "SKILL.md").read_text() == "old\n"


def test_regenerate_raises_without_shipped_skills(tmp_path: Path) -> None:
    with pytest.raises(gws_skills.GwsSkillsError):
        gws_skills.regenerate_stock_gws_skills(
            tmp_path, generator=_fake_generator({"gws-gmail": "x\n"})
        )


def test_strip_openclaw_metadata_removes_block() -> None:
    text = (
        "---\nname: gws-gmail\nmetadata:\n  version: 0.22.5\n  openclaw:\n"
        "    category: productivity\n    requires:\n      bins:\n        - gws\n---\n\n# body\n"
    )
    out = gws_skills.strip_openclaw_metadata(text)
    assert "openclaw" not in out
    assert "version: 0.22.5" in out


def test_replace_prerequisite_swaps_upstream_wording() -> None:
    text = (
        "> **PREREQUISITE:** Read `../gws-shared/SKILL.md` for auth, global flags, "
        "and security rules. If missing, run `gws generate-skills` to create it.\n\n"
        "Body\n"
    )
    out = gws_skills.replace_prerequisite(text)
    assert "gws generate-skills" not in out
    assert "gws-shared" in out


def test_rewrite_gws_commands_only_in_code_blocks() -> None:
    text = "# gws — Shared Reference\n\n```bash\ngws gmail +triage\n```\n"
    out = gws_skills.rewrite_gws_commands(text)
    assert out.startswith("# gws — Shared Reference")
    assert "scripts/gws-profile.sh <personal|work> gmail +triage" in out


def test_curate_gws_shared_replaces_auth_section() -> None:
    text = (
        "# gws — Shared Reference\n\n## Installation\n\nold\n\n## Authentication\n\n"
        "gws auth login\n\n## Global Flags\n\n| x |\n"
    )
    out = gws_skills.curate_gws_shared(text)
    assert "Authentication (Ciaobot)" in out
    assert "gws auth login" not in out
    assert "## Global Flags" in out


def test_pinned_gws_version_reads_metadata(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "gws-shared",
        "---\nname: gws-shared\nmetadata:\n  version: 0.22.5\n---\n# body\n",
    )
    assert gws_skills.pinned_gws_version(tmp_path) == "0.22.5"


def test_pinned_gws_version_missing_returns_none(tmp_path: Path) -> None:
    assert gws_skills.pinned_gws_version(tmp_path) is None


def test_installed_gws_version_parses_output() -> None:
    class _Result:
        returncode = 0
        stdout = "gws 0.22.5\nThis is not an officially supported Google product.\n"

    def _runner(*_args, **_kwargs):
        return _Result()

    assert gws_skills.installed_gws_version(runner=_runner) == "0.22.5"


def test_installed_gws_version_handles_missing_binary() -> None:
    def _runner(*_args, **_kwargs):
        raise OSError("not found")

    assert gws_skills.installed_gws_version(runner=_runner) is None
