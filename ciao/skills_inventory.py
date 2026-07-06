"""Skill inventory helpers for the PWA Settings page."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_skill_inventory(workspace_root: Path | str) -> dict[str, Any]:
    """Return installed/known skills labelled by source.

    Source labels intentionally stay coarse for the Settings UI:
    ``custom`` means the skill is maintained under ``skills/``;
    ``github`` means it comes from ``skills-lock.json``.
    """

    root = Path(workspace_root)
    custom_names = _skill_names(root / "skills")
    lock_entries = _read_lock_entries(root / "skills-lock.json")
    names = sorted(custom_names | set(lock_entries))

    skills: list[dict[str, Any]] = []
    counts = {"custom": 0, "github": 0}
    for name in names:
        is_custom = name in custom_names
        lock = lock_entries.get(name, {})
        label = "custom" if is_custom else "github"
        counts[label] += 1
        source = "skills/" if is_custom else str(lock.get("source") or "skills-lock.json")
        source_type = "custom" if is_custom else str(lock.get("sourceType") or "github")
        skills.append(
            {
                "name": name,
                "label": label,
                "source": source,
                "source_type": source_type,
                "description": _description_for(root, name, prefer_custom=is_custom),
                "content": _content_for(root, name, prefer_custom=is_custom),
                "installed_targets": _installed_targets(root, name),
            }
        )

    return {"counts": counts, "skills": skills}


def _skill_names(skills_root: Path) -> set[str]:
    if not skills_root.exists():
        return set()
    return {
        path.parent.name
        for path in skills_root.glob("*/SKILL.md")
        if path.parent.name and not path.parent.name.startswith(".")
    }


def _read_lock_entries(lock_path: Path) -> dict[str, dict[str, Any]]:
    if not lock_path.exists():
        return {}
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, dict):
        return {}
    return {str(name): value for name, value in skills.items() if isinstance(value, dict)}


def _description_for(root: Path, name: str, *, prefer_custom: bool) -> str:
    candidates = []
    if prefer_custom:
        candidates.append(root / "skills" / name / "SKILL.md")
    candidates.extend(
        [
            root / ".claude" / "skills" / name / "SKILL.md",
        ]
    )
    for path in candidates:
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


def _installed_targets(root: Path, name: str) -> list[str]:
    targets: list[str] = []
    path = root / ".claude" / "skills" / name / "SKILL.md"
    if path.exists():
        targets.append("claude")
    return targets


def _content_for(root: Path, name: str, *, prefer_custom: bool) -> str:
    candidates = []
    if prefer_custom:
        candidates.append(root / "skills" / name / "SKILL.md")
    candidates.extend(
        [
            root / ".claude" / "skills" / name / "SKILL.md",
        ]
    )
    for path in candidates:
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                pass
    return ""

