"""Tests for workspace-owned skill sources (issue #675).

Every fixture is a throwaway install under ``tmp_path``. The feature exists so
a pass edits the user's real canonical source, which is exactly why no test
here may read or write the developer's own install: the configs below build
their own registry, and for the per-root layout their own migration receipt,
because ``CiaoConfig.agent_root`` answers per-root only once one exists.
"""

from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.os_support.links import is_link
from ciao.skills_inventory import (
    STOCK_SKILL_MARKER,
    eligible_owned_skills,
    resolve_owned_skill,
)
from ciao.workspace_reroot import mark_born_per_root


def _config(tmp_path: Path, *names: str, rerooted: bool = False) -> CiaoConfig:
    """A config over ``tmp_path`` registered with ``names``.

    Without ``rerooted`` the install answers every workspace with the install
    root, which is the pre-re-rooting layout; with it, one agent root each.
    """
    runtime = tmp_path / ".runtime"
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={name: WorkspaceConfig(name=name, vault_root=name) for name in names},
    )
    if rerooted:
        mark_born_per_root(tmp_path, runtime, list(names))
    return config


def _write_skill_file(skill_dir: Path, name: str, description: str = "Owned skill") -> Path:
    """Create ``<skill_dir>/SKILL.md``; return the file it wrote."""
    skill_md = skill_dir / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8", newline=""
    )
    return skill_md


def _write_skill(root: Path, name: str, description: str = "Owned skill") -> Path:
    """Create the canonical owned source ``<root>/skills/<name>/SKILL.md``."""
    return _write_skill_file(root / "skills" / name, name, description)


def _tree(root: Path) -> list[tuple[str, int, bytes]]:
    """Every entry below ``root`` with its mtime and bytes, to prove no writes."""
    return sorted(
        (
            str(path.relative_to(root)),
            path.lstat().st_mtime_ns,
            path.read_bytes() if path.is_file() else b"",
        )
        for path in root.rglob("*")
    )


def test_workspace_local_source_and_revision(tmp_path: Path) -> None:
    config = _config(tmp_path, "personal", rerooted=True)
    root = config.agent_root("personal")
    skill_md = _write_skill(root, "demo", "Owned source")

    owned = resolve_owned_skill(config, "personal", "demo")

    assert owned.workspace == "personal"
    assert owned.name == "demo"
    assert owned.path == skill_md
    assert owned.path.is_absolute()
    assert owned.content == skill_md.read_text(encoding="utf-8")
    assert owned.revision == hashlib.sha256(skill_md.read_bytes()).hexdigest()
    assert eligible_owned_skills(config, "personal") == [owned]

    skill_md.write_text("# demo, rewritten\n", encoding="utf-8", newline="")

    assert resolve_owned_skill(config, "personal", "demo").revision != owned.revision


def test_custom_shadow_is_eligible(tmp_path: Path) -> None:
    packaged = resources.files("ciao.stock").joinpath("skills")
    shadow = "convert-documents-to-markdown"
    assert shadow in {e.name for e in packaged.iterdir() if e.is_dir()}, (
        "the shadow has to shadow a name that actually ships"
    )

    config = _config(tmp_path, "personal", rerooted=True)
    root = config.agent_root("personal")
    # What sync leaves behind when a workspace skill shadows the packaged one.
    installed = _write_skill_file(
        root / ".claude" / "skills" / shadow, shadow, "Packaged copy"
    )
    (installed.parent / STOCK_SKILL_MARKER).touch()
    local = _write_skill(root, shadow, "Mine, not theirs")
    # Unreadable, so a resolver that consulted the installed copy to decide
    # anything would raise rather than quietly return the local source. Root
    # reads a 0o000 file anyway; the rest of the assertion still holds there.
    installed.chmod(0o000)

    owned = resolve_owned_skill(config, "personal", shadow)

    assert owned.path == local
    assert owned.content == local.read_text(encoding="utf-8")
    assert "Packaged copy" not in owned.content
    assert eligible_owned_skills(config, "personal") == [owned]


def test_stock_and_provider_mirrors_are_ineligible(tmp_path: Path) -> None:
    config = _config(tmp_path, "personal", rerooted=True)
    root = config.agent_root("personal")
    (root / "skills").mkdir(parents=True)

    # Installed only: the packaged skill, refreshed on every sync.
    installed = _write_skill_file(
        root / ".claude" / "skills" / "ciao-capabilities", "ciao-capabilities", "Packaged"
    )
    (installed.parent / STOCK_SKILL_MARKER).touch()

    assert eligible_owned_skills(config, "personal") == []
    with pytest.raises(ValueError, match="owns no canonical source"):
        resolve_owned_skill(config, "personal", "ciao-capabilities")

    # The marker travelled with a copy of a stock skill into the source tree.
    marked = _write_skill(root, "copied-stock")
    (marked.parent / STOCK_SKILL_MARKER).touch()

    assert eligible_owned_skills(config, "personal") == []
    with pytest.raises(ValueError, match="sync refreshes on every run"):
        resolve_owned_skill(config, "personal", "copied-stock")


@pytest.mark.parametrize(
    ("workspace", "name"),
    [
        ("personal", ""),
        ("personal", "."),
        ("personal", ".."),
        ("personal", ".hidden"),
        ("personal", "a/b"),
        ("personal", "a\\b"),
        ("personal", "/abs"),
        ("personal", "sub/"),
        ("personal", "demo/../demo"),
        ("nope", "demo"),
        ("", "demo"),
    ],
)
def test_traversal_and_unknown_workspace_are_rejected(
    tmp_path: Path, workspace: str, name: str
) -> None:
    config = _config(tmp_path, "personal", rerooted=True)
    _write_skill(config.agent_root("personal"), "demo")

    with pytest.raises(ValueError):
        resolve_owned_skill(config, workspace, name)

    # Whatever the reason, a name the catalog never listed cannot become one.
    assert [owned.name for owned in eligible_owned_skills(config, "personal")] == ["demo"]


def _link_catalog(tmp_path: Path, root: Path, target: str) -> Path:
    """A real skills-shaped directory for a symlink to point at, or a dead path.

    Stock is the generated copy under ``.claude/``, shared is the install-wide
    ``skills-src/`` mirror source, outside is somewhere the workspace has no
    claim on at all, dangling never exists.
    """
    if target == "stock":
        catalog = root / ".claude" / "skills"
    elif target == "shared":
        catalog = tmp_path / "skills-src"
    elif target == "outside":
        catalog = tmp_path / "outside-catalog"
    else:
        return tmp_path / "absent-catalog"
    _write_skill_file(catalog / "demo", "demo", "Somewhere else")
    return catalog


@pytest.mark.parametrize("component", ["skills", "skill-dir", "skill-md"])
@pytest.mark.parametrize("target", ["stock", "shared", "outside", "dangling"])
def test_symlink_components_are_rejected(
    tmp_path: Path, component: str, target: str
) -> None:
    config = _config(tmp_path, "personal", rerooted=True)
    root = config.agent_root("personal")
    root.mkdir(parents=True, exist_ok=True)
    catalog = _link_catalog(tmp_path, root, target)

    if component == "skills":
        link, points_at = root / "skills", catalog
    elif component == "skill-dir":
        (root / "skills").mkdir(parents=True, exist_ok=True)
        link, points_at = root / "skills" / "demo", catalog / "demo"
    else:
        (root / "skills" / "demo").mkdir(parents=True, exist_ok=True)
        link = root / "skills" / "demo" / "SKILL.md"
        points_at = catalog / "demo" / "SKILL.md"
    link.symlink_to(points_at, target_is_directory=points_at.is_dir())

    with pytest.raises(ValueError, match="symlink"):
        resolve_owned_skill(config, "personal", "demo")

    assert eligible_owned_skills(config, "personal") == []


def test_shared_sources_are_ineligible(tmp_path: Path) -> None:
    from ciao.sync_skills import mirror_shared_skill_sources

    config = _config(tmp_path, "personal", rerooted=True)
    root = config.agent_root("personal")
    (root / "skills").mkdir(parents=True)
    shared = tmp_path / "skills-src"
    _write_skill_file(shared / "shared-demo", "shared-demo", "Applies everywhere")

    # The real mirror: one edit in skills-src, N roots, a link in each catalog.
    linked, pruned = mirror_shared_skill_sources(root, shared)

    assert (linked, pruned) == (1, 0)
    # A symlink on POSIX, a junction on Windows: the link module answers for both.
    assert is_link(root / ".claude" / "skills" / "shared-demo")
    assert eligible_owned_skills(config, "personal") == []
    with pytest.raises(ValueError, match="owns no canonical source"):
        resolve_owned_skill(config, "personal", "shared-demo")

    # And a link from the workspace's own catalog into that shared source.
    (root / "skills" / "linked-demo").symlink_to(
        shared / "shared-demo", target_is_directory=True
    )

    with pytest.raises(ValueError, match="symlink"):
        resolve_owned_skill(config, "personal", "linked-demo")
    assert eligible_owned_skills(config, "personal") == []


def test_ambiguous_shared_root_is_refused(tmp_path: Path) -> None:
    # Two registered workspaces, one catalog: nobody owns it exclusively.
    config = _config(tmp_path, "personal", "work")
    assert config.agent_root("personal") == config.agent_root("work")
    _write_skill(config.workspace_root, "demo")

    for workspace in ("personal", "work"):
        with pytest.raises(ValueError, match="shares its agent root"):
            resolve_owned_skill(config, workspace, "demo")
        # The catalog refuses the whole workspace rather than quietly returning
        # nothing: an empty list reads as "no skills", which is the one answer
        # here that would be a lie.
        with pytest.raises(ValueError, match="shares its agent root"):
            eligible_owned_skills(config, workspace)


def test_single_workspace_legacy_source_is_eligible(tmp_path: Path) -> None:
    config = _config(tmp_path, "personal")
    skill_md = _write_skill(config.workspace_root, "demo", "Legacy source")

    owned = resolve_owned_skill(config, "personal", "demo")

    assert owned.path == skill_md
    assert owned.content == skill_md.read_text(encoding="utf-8")
    assert owned.revision == hashlib.sha256(skill_md.read_bytes()).hexdigest()
    assert eligible_owned_skills(config, "personal") == [owned]


def test_catalog_skips_invalid_sources_and_is_sorted(tmp_path: Path) -> None:
    config = _config(tmp_path, "personal", rerooted=True)
    root = config.agent_root("personal")
    _write_skill(root, "zeta", "Last")
    _write_skill(root, "alpha", "First")
    _write_skill(root, "mid", "Middle")

    (root / "skills" / "empty").mkdir(parents=True)
    (root / "skills" / "empty" / "SKILL.md").write_bytes(b"")
    binary = root / "skills" / "binary" / "SKILL.md"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"---\nname: binary\n---\n\n\xff\xfe not utf-8\n")
    marked = _write_skill(root, "marked")
    (marked.parent / STOCK_SKILL_MARKER).touch()
    (root / "skills" / "no-skill-md").mkdir(parents=True)
    (root / "skills" / "linked").mkdir(parents=True)
    (root / "skills" / "linked" / "SKILL.md").symlink_to(
        root / "skills" / "alpha" / "SKILL.md"
    )

    before = _tree(root)

    owned = eligible_owned_skills(config, "personal")

    assert [skill.name for skill in owned] == ["alpha", "mid", "zeta"]
    assert all(skill.path.parent.parent == root / "skills" for skill in owned)
    assert all(skill.workspace == "personal" for skill in owned)
    assert _tree(root) == before


# ── `create_owned_skill` (the new-skill route, #728-D) ─────────────────────


def _skill_body(name: str, description: str = "Owned skill", body: str = "# x\n") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


def test_a_creation_returns_the_source_read_back(tmp_path: Path) -> None:
    """The caller is handed the bytes on disk, not the text it submitted.

    Same contract an edit gets: the readback goes through `resolve_owned_skill`,
    so the revision here is a revision somebody could hold and re-check.
    """
    from ciao.skills_inventory import create_owned_skill

    config = _config(tmp_path, "personal", rerooted=True)
    content = _skill_body("invoice-recon", "Reconcile an invoice")

    created = create_owned_skill(config, "personal", "invoice-recon", content)

    root = config.agent_root("personal")
    written = root / "skills" / "invoice-recon" / "SKILL.md"
    assert written.read_text(encoding="utf-8") == content
    assert created.skill.path == written
    assert created.skill.revision == hashlib.sha256(written.read_bytes()).hexdigest()
    assert created.trigger == "invoice-recon"
    assert created.description == "Reconcile an invoice"
    assert created.over_budget is False
    # And the catalog now resolves it like any other owned source, which is what
    # makes the next improvement against it a normal edit.
    assert [owned.name for owned in eligible_owned_skills(config, "personal")] == [
        "invoice-recon"
    ]


def test_a_creation_refuses_a_name_that_already_exists(tmp_path: Path) -> None:
    """A creation that overwrote an existing skill would be an edit.

    The owned file is somebody's work; pointing the new-skill route at it would
    take the check every other write depends on away from the queue, so the
    collision is refused and the existing bytes are left alone.
    """
    from ciao.skills_inventory import create_owned_skill

    config = _config(tmp_path, "personal", rerooted=True)
    existing = _write_skill(config.agent_root("personal"), "notes", "Mine")

    with pytest.raises(ValueError, match="already has a skills/notes entry"):
        create_owned_skill(
            config, "personal", "notes", _skill_body("notes", "Something else")
        )

    assert "description: Mine" in existing.read_text(encoding="utf-8")


def test_a_creation_refuses_a_name_an_installed_copy_takes(tmp_path: Path) -> None:
    """A local skill shadowing an installed one is owned — but not *creatable*.

    `resolve_owned_skill` deliberately treats a real local skill that shadows a
    packaged one as owned, because it is somebody's file. A creation is the case
    that has to refuse: the person writing it did not know the copy underneath,
    and the next sync would keep the two in tension.
    """
    from ciao.skills_inventory import create_owned_skill

    config = _config(tmp_path, "personal", rerooted=True)
    root = config.agent_root("personal")
    installed = _write_skill_file(
        root / ".claude" / "skills" / "web-research", "web-research", "Packaged"
    )
    (installed.parent / STOCK_SKILL_MARKER).touch()

    with pytest.raises(ValueError, match="already exists"):
        create_owned_skill(
            config, "personal", "web-research", _skill_body("web-research", "Mine")
        )
    assert not (root / "skills" / "web-research").exists()


def test_a_creation_refuses_a_symlinked_catalog(tmp_path: Path) -> None:
    """The shared-mirror shape is refused at the link, not after following it."""
    from ciao.skills_inventory import create_owned_skill

    config = _config(tmp_path, "personal", rerooted=True)
    root = config.agent_root("personal")
    root.mkdir(parents=True, exist_ok=True)
    shared = tmp_path / "shared"
    shared.mkdir()
    (root / "skills").symlink_to(shared, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        create_owned_skill(config, "personal", "invoice-recon", _skill_body("invoice-recon"))

    assert list(shared.iterdir()) == []


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("no frontmatter at all", "frontmatter"),
        ("---\nname: other\ndescription: d\n---\n\nbody\n", "frontmatter name"),
        ("---\nname: invoice-recon\n---\n\nbody\n", "description"),
        ("---\nname: invoice-recon\ndescription: d\n---\n\n   \n", "no body"),
        ("   ", "empty content"),
    ],
)
def test_a_creation_refuses_content_that_would_not_load(
    tmp_path: Path, content: str, expected: str
) -> None:
    """A skill with no trigger never fires and one with no description is never
    chosen, so a creation that allowed either would write a file nothing reads.

    The name check is the load-bearing one: a skill is loaded under its
    frontmatter name, so a mismatch creates one nobody can address. Refused
    rather than normalized, for the reason a bad directory name is.
    """
    from ciao.skills_inventory import create_owned_skill

    config = _config(tmp_path, "personal", rerooted=True)
    with pytest.raises(ValueError, match=expected):
        create_owned_skill(config, "personal", "invoice-recon", content)
    assert not (config.agent_root("personal") / "skills" / "invoice-recon").exists()


def test_an_over_budget_creation_is_written_and_flagged(tmp_path: Path) -> None:
    """The budget warns everywhere else, so it warns here too.

    `MAX_SKILL_BYTES` is a context cost the engine pays on every load, not a
    validity rule: refusing here would make the first skill of a large workspace
    impossible to create, and the audit already reports an over-budget skill.
    """
    from ciao.skills_inventory import MAX_SKILL_BYTES, create_owned_skill

    config = _config(tmp_path, "personal", rerooted=True)
    body = "\n".join(f"line {n}" for n in range(MAX_SKILL_BYTES // 4))
    content = _skill_body("invoice-recon", "Reconcile an invoice", body=body)

    created = create_owned_skill(config, "personal", "invoice-recon", content)

    assert created.over_budget is True
    assert (config.agent_root("personal") / "skills" / "invoice-recon" / "SKILL.md").is_file()


def test_a_creation_still_refuses_a_shared_agent_root(tmp_path: Path) -> None:
    """One catalog serving two workspaces means a "new" skill is visible to both."""
    from ciao.skills_inventory import create_owned_skill

    # Not rerooted: every workspace answers with the install root.
    config = _config(tmp_path, "personal", "work")
    with pytest.raises(ValueError, match="shares its agent root"):
        create_owned_skill(config, "personal", "invoice-recon", _skill_body("invoice-recon"))
    with pytest.raises(ValueError, match="unknown workspace"):
        create_owned_skill(config, "elsewhere", "invoice-recon", _skill_body("invoice-recon"))


def test_a_creation_refuses_a_name_that_is_not_one_directory(tmp_path: Path) -> None:
    """The same rule `resolve_owned_skill` enforces, for the same reason."""
    from ciao.skills_inventory import create_owned_skill

    config = _config(tmp_path, "personal", rerooted=True)
    for name in ("../escape", "a/b", "", ".", "..", ".hidden"):
        with pytest.raises(ValueError, match="not a skill directory name|not one directory"):
            create_owned_skill(config, "personal", name, _skill_body("x"))
