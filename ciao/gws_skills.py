"""Regenerate the packaged ``gws-*`` stock skills from the ``gws`` CLI.

The Google Workspace CLI ships a hidden ``gws generate-skills`` command that
writes one ``SKILL.md`` per service (plus helper sub-skills, personas, and
recipes) into ``./skills/``. Ciaobot ships a **curated subset** of those under
``ciao/stock/skills/`` so they are installed into every workspace without the
CLI needing to be present at install time.

Historically the packaged ``gws-shared`` skill was hand-edited to carry
Ciaobot integration notes (profiles, the ``gws-profile.sh`` wrapper, PWA OAuth
setup). Those notes now live in the system prompt (see
``ciao.memory_injector``), so the packaged skills can stay byte-for-byte
upstream and be regenerated deterministically on release.

The only non-verbatim step is stripping the generator's "Community & Feedback
Etiquette" section (a "star the repo / open issues" block) from ``gws-shared``;
it is boilerplate that does not belong in a shipped reference. The strip is
idempotent, so regenerating an already-clean tree is a no-op.

This module is import-safe without the ``gws`` binary: the actual generation
is an injectable callable so the decision logic stays unit-testable.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Prefix that marks the skills we regenerate from the CLI. Any directory under
# the stock skills folder starting with this is treated as a generated gws
# skill; the curated set is discovered from disk so adding a new gws-* skill
# dir is enough to have it refreshed on the next release.
GWS_SKILL_PREFIX = "gws-"

# A generator populates ``<dest>/skills/<name>/SKILL.md`` for every skill the
# CLI knows about. The default shells out to ``gws generate-skills``.
Generator = Callable[[Path], None]


class GwsSkillsError(RuntimeError):
    """Raised when the gws skills cannot be regenerated."""


@dataclass(frozen=True, slots=True)
class RegenResult:
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.updated)


def shipped_gws_skills(stock_skills_dir: Path) -> list[str]:
    """Return the sorted curated ``gws-*`` skill names currently shipped."""
    if not stock_skills_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in stock_skills_dir.iterdir()
        if entry.is_dir()
        and entry.name.startswith(GWS_SKILL_PREFIX)
        and (entry / "SKILL.md").is_file()
    )


def strip_community_etiquette(text: str) -> str:
    """Drop the generator's "Community & Feedback Etiquette" section.

    Removes the ``## Community & Feedback Etiquette`` heading through the end of
    the section (the next ``## `` heading or end of file). Idempotent: text
    without the section is returned unchanged apart from trailing-newline
    normalization.
    """
    pattern = re.compile(
        r"(?:\n|^)##[ \t]+Community & Feedback Etiquette\b.*?(?=\n## |\Z)",
        re.DOTALL,
    )
    stripped = pattern.sub("", text)
    return stripped.rstrip() + "\n"


def _normalize(text: str) -> str:
    return text.rstrip() + "\n"


def _gws_generate(dest: Path, *, gws_bin: str = "gws") -> None:
    """Default generator: run ``gws generate-skills`` in ``dest``."""
    binary = shutil.which(gws_bin) or gws_bin
    try:
        result = subprocess.run(
            [binary, "generate-skills"],
            cwd=str(dest),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise GwsSkillsError(f"could not run {gws_bin!r}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise GwsSkillsError(
            f"`{gws_bin} generate-skills` failed ({result.returncode})"
            + (f": {detail}" if detail else "")
        )


def installed_gws_version(*, gws_bin: str = "gws", runner=subprocess.run) -> str | None:
    """Return the installed ``gws`` CLI version (e.g. ``0.22.5``) or None."""
    binary = shutil.which(gws_bin) or gws_bin
    try:
        result = runner(
            [binary, "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    match = re.search(r"([0-9]+\.[0-9]+\.[0-9]+)", result.stdout or "")
    return match.group(1) if match else None


def pinned_gws_version(stock_skills_dir: Path) -> str | None:
    """Return the ``metadata.version`` pinned in the packaged ``gws-shared``."""
    shared = stock_skills_dir / "gws-shared" / "SKILL.md"
    try:
        text = shared.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^\s*version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def _transform(name: str, text: str) -> str:
    """Apply deterministic curation to a generated skill's content."""
    text = _normalize(text)
    if name == "gws-shared":
        text = strip_community_etiquette(text)
    return text


def regenerate_stock_gws_skills(
    stock_skills_dir: Path | str,
    *,
    generator: Generator | None = None,
    gws_bin: str = "gws",
    write: bool = True,
) -> RegenResult:
    """Regenerate the curated ``gws-*`` stock skills in place.

    Generates the full upstream skill set into a temp dir, then for each
    currently-shipped ``gws-*`` skill copies the freshly generated (and
    curated) ``SKILL.md`` back into ``stock_skills_dir`` when its content
    changed. Skills that the generator no longer produces are reported in
    ``missing`` and left untouched. Set ``write=False`` for a dry run.
    """
    stock_dir = Path(stock_skills_dir)
    curated = shipped_gws_skills(stock_dir)
    if not curated:
        raise GwsSkillsError(f"no shipped gws-* skills found under {stock_dir}")

    gen = generator or (lambda dest: _gws_generate(dest, gws_bin=gws_bin))

    updated: list[str] = []
    unchanged: list[str] = []
    missing: list[str] = []

    with tempfile.TemporaryDirectory(prefix="ciao-gws-skills-") as tmp:
        tmp_root = Path(tmp)
        gen(tmp_root)
        generated_root = tmp_root / "skills"

        for name in curated:
            generated = generated_root / name / "SKILL.md"
            if not generated.is_file():
                missing.append(name)
                continue
            new_text = _transform(name, generated.read_text(encoding="utf-8"))
            target = stock_dir / name / "SKILL.md"
            current = target.read_text(encoding="utf-8") if target.is_file() else ""
            if _normalize(current) == new_text:
                unchanged.append(name)
                continue
            updated.append(name)
            if write:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_text, encoding="utf-8")

    return RegenResult(updated=updated, unchanged=unchanged, missing=missing)
