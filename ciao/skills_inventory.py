"""Skill inventory helpers for the PWA Settings page.

Two questions live here and they are not the same one. :func:`build_skill_inventory`
answers "what is installed", as labels for the Settings list.
:func:`resolve_owned_skill` and :func:`eligible_owned_skills` answer "which of
those may a pass edit", which no label can carry: see the workspace-owned
sources section at the foot of the module.

:data:`MAX_SKILL_BYTES` lives here for the same reason: a budget for one
``SKILL.md`` is a fact about skills, and this is the module that owns them.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ciao.config import CiaoConfig

# Marker dropped into skills copied from ``ciao.stock`` (see
# ``ciao.sync_skills._install_stock_skills``), so a stale copy is pruned when it
# disappears from the package and an unshadowed one is refreshed on every sync.
# The same marker is what tells an owned source from a generated copy.
STOCK_SKILL_MARKER = ".ciao-stock-skill"

# The context budget for one ``SKILL.md``. A skill loads in full every time it
# triggers, so this is what the engine pays for it, not a validity rule: an
# import warns past it (``ciao.skill_import``) and ``os_audit`` reports a skill
# as over budget, but neither refuses the file. It used to live in the retired
# weekly skill-evolution pass; the memory pass replaced that pass as the sole
# producer, and nothing but the import reads this name, so it lives here with
# the other skill constants rather than in a module that has no callers.
MAX_SKILL_BYTES = 15 * 1024


def build_skill_inventory(
    workspace_root: Path | str,
    *,
    include_content: bool = True,
) -> dict[str, Any]:
    """Return installed/known skills labelled by source.

    Source labels stay coarse for the Settings UI:
    ``custom`` means the skill is maintained under ``skills/``;
    ``stock`` means it ships with the app and is installed into ``.claude/skills``.
    GitHub/package skills are no longer a separate surface.
    """

    root = Path(workspace_root)
    custom_names = _skill_names(root / "skills")
    stock_names = _stock_skill_names(root)
    names = sorted(custom_names | stock_names)

    skills: list[dict[str, Any]] = []
    counts = {"custom": 0, "stock": 0}
    for name in names:
        is_custom = name in custom_names
        if is_custom:
            label = "custom"
        else:
            label = "stock"
        counts[label] += 1
        if is_custom:
            source = "skills/"
            source_type = "custom"
        else:
            source = "ciao.stock/skills"
            source_type = "stock"
        skill = {
            "name": name,
            "label": label,
            "source": source,
            "source_type": source_type,
            "description": _description_for(root, name, prefer_custom=is_custom),
            "path": _path_for(root, name, prefer_custom=is_custom),
            "installed_targets": _installed_targets(root, name),
        }
        if include_content:
            skill["content"] = _content_for(root, name, prefer_custom=is_custom)
        skills.append(skill)

    return {"counts": counts, "skills": skills}


def _stock_skill_names(root: Path) -> set[str]:
    """Names of packaged stock skills installed into ``.claude/skills``.

    Stock copies carry a ``.ciao-stock-skill`` marker (see
    ``ciao.sync_skills._install_stock_skills``). A workspace ``skills/<name>``
    shadows the packaged skill, so a name is only reported as stock when the
    installed copy is still marked.
    """
    claude_skills = root / ".claude" / "skills"
    if not claude_skills.is_dir():
        return set()
    return {
        path.parent.name
        for path in claude_skills.glob(f"*/{STOCK_SKILL_MARKER}")
        if path.parent.name and not path.parent.name.startswith(".")
    }


def _skill_names(skills_root: Path) -> set[str]:
    if not skills_root.exists():
        return set()
    return {
        path.parent.name
        for path in skills_root.glob("*/SKILL.md")
        if path.parent.name and not path.parent.name.startswith(".")
    }





def _skill_candidates(root: Path, name: str, *, prefer_custom: bool) -> list[Path]:
    """Where a skill's SKILL.md may live, in read-priority order.

    Custom skills live under ``skills/``; stock and GitHub skills are read
    from their installed copy under ``.claude/skills`` or ``.agents/skills``.
    """
    candidates = []
    if prefer_custom:
        candidates.append(root / "skills" / name / "SKILL.md")
    candidates.extend(
        [
            root / ".claude" / "skills" / name / "SKILL.md",
            root / ".agents" / "skills" / name / "SKILL.md",
        ]
    )
    return candidates


def _description_for(root: Path, name: str, *, prefer_custom: bool) -> str:
    for path in _skill_candidates(root, name, prefer_custom=prefer_custom):
        description = _read_frontmatter_description(path)
        if description:
            return description
    return ""


def _read_frontmatter_description(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return ""
    lines = parts[1].splitlines()
    collecting = False
    values: list[str] = []
    for line in lines:
        if line.startswith("description:"):
            raw = line.split(":", 1)[1].strip()
            if raw and raw not in {"|", ">", "|-", ">-"}:
                return raw.strip('"\'')
            collecting = True
            continue
        if collecting:
            if line and not line[0].isspace() and ":" in line:
                break
            stripped = line.strip()
            if stripped:
                values.append(stripped.strip('"\''))
    return " ".join(values).strip()


def _path_for(root: Path, name: str, *, prefer_custom: bool) -> str:
    """Canonical SKILL.md path for a skill, relative to the workspace root."""
    for path in _skill_candidates(root, name, prefer_custom=prefer_custom):
        if path.exists():
            return _relative_or_absolute(path, root)
    return ""


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _installed_targets(root: Path, name: str) -> list[str]:
    """Providers that can see this skill, from where its SKILL.md landed.

    opencode discovers both catalogs natively, so it needs no projection of
    its own and is credited whenever either path exists.
    """
    targets: list[str] = []
    claude_path = root / ".claude" / "skills" / name / "SKILL.md"
    agents_path = root / ".agents" / "skills" / name / "SKILL.md"
    if claude_path.exists():
        targets.append("claude")
    if claude_path.exists() or agents_path.exists():
        targets.append("opencode")
    return targets


def _content_for(root: Path, name: str, *, prefer_custom: bool) -> str:
    for path in _skill_candidates(root, name, prefer_custom=prefer_custom):
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                pass
    return ""


# -- Workspace-owned sources ------------------------------------------------
#
# The inventory above answers "what is installed". These two answer "what may a
# pass edit", which is a different question and cannot be read off a label: a
# skill improvement writes a file, so its target has to be provably the
# workspace's own canonical source. Everything the app generates is refused —
# the installed stock copies sync refreshes, the provider mirrors under
# ``.claude/``, the install-wide ``skills-src/`` mirror source — and so is a
# catalog a second registered workspace can equally edit. A refusal names the
# reason rather than resolving a path the caller would then write to.


@dataclass(frozen=True, slots=True)
class OwnedSkill:
    """One workspace-owned skill source, resolved to the bytes on disk.

    ``revision`` is the sha256 of the bytes ``content`` was decoded from, so a
    caller can hold the source it read and refuse to write a file that changed
    underneath it — the same revision-before-write contract the memory
    receipts hold for the guide's bounded regions.
    """

    workspace: str
    name: str
    path: Path
    revision: str
    content: str


def resolve_owned_skill(config: CiaoConfig, workspace: str, name: str) -> OwnedSkill:
    """Return the one source an improvement pass may edit for ``name``.

    That source is ``config.agent_root(workspace)/skills/<name>/SKILL.md``,
    derived from a registered workspace and a validated directory name, and it
    is the whole resolution: there is no path argument, so a caller holding a
    proposed edit target cannot aim it at an installed copy, at a mirror, or at
    a file in another workspace, and a name that is not one plain directory
    never reaches the filesystem.

    A real local skill shadowing a packaged one is owned like any other: what
    disqualifies a source is being generated or shared, not sharing a name with
    the stock catalog.

    Raises ``ValueError`` saying which of those it is: an unknown workspace, an
    agent root a second workspace also owns, a name that is not a directory, a
    symlink or a missing directory anywhere on the way down, a path that leaves
    the root, a stock-marked copy, or a file that is missing, empty or not
    UTF-8.
    """
    _check_skill_name(name)
    root = _owned_agent_root(config, workspace)
    skill_dir = root / "skills" / name
    skill_md = skill_dir / "SKILL.md"
    _require_directory(
        root / "skills",
        f"the skills/ catalog of workspace {workspace!r} must be a real directory",
    )
    _require_directory(
        skill_dir,
        f"workspace {workspace!r} owns no canonical source for {name!r}: a copy "
        f"under .claude/skills, or a link into skills-src, is generated and is "
        f"not editable",
    )
    if skill_md.is_symlink() or not skill_md.is_file():
        raise ValueError(
            f"{skill_md} is not a regular file: the canonical source of {name!r} "
            f"is a real SKILL.md, not a link into an installed or shared copy"
        )
    # Unreachable while every component above is a real directory and the name
    # is one segment, which is the point: confinement is asserted, not assumed
    # from the construction above it.
    if not skill_md.resolve().is_relative_to(root.resolve()):
        raise ValueError(
            f"{skill_md} resolves outside the agent root of workspace {workspace!r}"
        )
    if os.path.lexists(skill_dir / STOCK_SKILL_MARKER):
        raise ValueError(
            f"{skill_dir} carries {STOCK_SKILL_MARKER}: it is a copy of a "
            f"packaged skill that sync refreshes on every run, so an edit to it "
            f"is discarded"
        )
    try:
        raw = skill_md.read_bytes()
    except OSError as exc:
        raise ValueError(f"{skill_md} cannot be read: {exc}") from exc
    if not raw:
        raise ValueError(f"{skill_md} is empty: there is no source to edit")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{skill_md} is not valid UTF-8") from exc
    return OwnedSkill(
        workspace=workspace,
        name=name,
        path=skill_md,
        revision=hashlib.sha256(raw).hexdigest(),
        content=content,
    )


def eligible_owned_skills(config: CiaoConfig, workspace: str) -> list[OwnedSkill]:
    """Every source in ``workspace`` a pass may edit, sorted by name.

    Enumerated through the same ``skills/`` scan the Settings inventory uses, so
    there is one definition of which names exist, and every candidate then goes
    through :func:`resolve_owned_skill`. One malformed entry is skipped rather
    than failing the catalog: a half-written SKILL.md is exactly the condition
    under which a user needs to see the rest of their skills.

    A name that exists only under ``.claude/`` — an installed stock copy, a
    provider mirror, a link into the shared ``skills-src/`` — is absent here
    rather than resolved to its mirror, which is also why no installed stock
    body is ever read. Nothing is written, so calling this is safe to put in a
    status path.
    """
    root = _owned_agent_root(config, workspace)
    owned: list[OwnedSkill] = []
    for name in sorted(_skill_names(root / "skills")):
        try:
            owned.append(resolve_owned_skill(config, workspace, name))
        except ValueError:
            continue
    return owned


def _owned_agent_root(config: CiaoConfig, workspace: str) -> Path:
    """The agent root this workspace owns alone, or refuse.

    An install that has not re-rooted answers ``agent_root`` with the install
    root for every registered workspace, so one ``skills/`` catalog is visible
    to all of them and an edit made on behalf of one rewrites what the others
    read. The single-workspace case is the same layout and is fine, so the test
    is not "rerooted or not" but "one owner or several".
    """
    names = config.workspace_names()
    if workspace not in names:
        raise ValueError(
            f"unknown workspace {workspace!r}: the registry holds "
            f"{', '.join(sorted(names)) or 'nothing'}"
        )
    root = config.agent_root(workspace)
    owners = sorted(name for name in names if config.agent_root(name) == root)
    if len(owners) > 1:
        raise ValueError(
            f"workspace {workspace!r} shares its agent root with "
            f"{', '.join(owners)}: one catalog serves them all, so an edit to it "
            f"would rewrite another workspace's skills too"
        )
    return root


def _check_skill_name(name: str) -> None:
    """One plain directory name, or refuse before it reaches the filesystem.

    The same rule as ``ciao.skill_import._is_valid_skill_dir_name``, kept here
    so the Settings inventory does not pull the import and evolution chain in to
    check a string. A name that could address a second directory — a separator,
    ``.``/``..``, a leading dot — is refused rather than normalized, because a
    normalized name is not a name the catalog ever listed.
    """
    if not name or name in {".", ".."} or name.startswith("."):
        raise ValueError(f"{name!r} is not a skill directory name")
    if "/" in name or "\\" in name:
        raise ValueError(f"{name!r} is not one directory under skills/")


def _require_directory(path: Path, reason: str) -> None:
    """A real directory, never a link to one.

    A symlinked ``skills/``, skill directory or catalog root is the shape the
    shared mirror source and generated provider copies take, so it is refused
    at the link rather than after following it to wherever it leads.
    """
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink: {reason}")
    if not path.is_dir():
        raise ValueError(f"{path} is missing: {reason}")
