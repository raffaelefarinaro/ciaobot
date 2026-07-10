"""Regenerate the packaged ``gws-*`` stock skills from the ``gws`` CLI.

The Google Workspace CLI ships a hidden ``gws generate-skills`` command that
writes one ``SKILL.md`` per service (plus helper sub-skills, personas, and
recipes) into ``./skills/``. Ciaobot ships a **curated subset** of those under
``ciao/stock/skills/`` so they are installed into every workspace without the
CLI needing to be present at install time.

Upstream skills are regenerated on release, then passed through
:func:`curate_gws_skill` to apply Ciaobot-specific edits (profile-wrapper
commands, auth notes, boilerplate trimming). The curated files on disk are the
source of truth installed by ``ciao sync-skills``.

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

_PREREQUISITE_OLD = re.compile(
    r"> \*\*PREREQUISITE:\*\* Read `\.\./gws-shared/SKILL\.md` for auth, global flags, "
    r"and security rules\. If missing, run `gws generate-skills` to create it\.\n+",
)
_PREREQUISITE_NEW = (
    "> **PREREQUISITE:** Read `gws-shared` for Ciaobot auth (profile wrapper), "
    "global flags, and security rules.\n\n"
)

_CIAOBOT_SHARED_HEAD = """## Installation

Install `gws` from Settings → Integrations (or see the Ciaobot README). The binary must be on `$PATH`.

## Authentication (Ciaobot)

Run every Google API call through the profile wrapper — never bare `gws`:

```bash
scripts/gws-profile.sh <personal|work> <service> <subcommand> [flags]
```

Use the chat's `GWS_PROFILE` unless the user asks otherwise. The wrapper routes credentials and execs `gws`. Do not `source` it and do not repeat the `gws` binary after the profile name.

OAuth setup: Settings → Integrations. Config dirs: `secrets/gws-personal/` (personal), `secrets/gws/` (work).

"""


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


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---\n", 4)
    if end == -1:
        return "", text
    return text[: end + 5], text[end + 5 :]


def strip_openclaw_metadata(text: str) -> str:
    """Drop the upstream ``metadata.openclaw`` block from YAML frontmatter."""
    frontmatter, body = _split_frontmatter(text)
    if not frontmatter:
        return text
    cleaned = re.sub(
        r"(?m)^  openclaw:\n(?:    .*\n)*",
        "",
        frontmatter,
    )
    return cleaned + body


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


def replace_prerequisite(text: str) -> str:
    """Swap the upstream prerequisite block for the Ciaobot pointer."""
    return _PREREQUISITE_OLD.sub(_PREREQUISITE_NEW, text)


def strip_see_also(text: str) -> str:
    """Drop redundant See Also footers from helper skills."""
    pattern = re.compile(r"\n## See Also\n.*\Z", re.DOTALL)
    return pattern.sub("\n", text).rstrip() + "\n"


def rewrite_gws_commands(text: str) -> str:
    """Prefix bare ``gws `` CLI examples with the Ciaobot profile wrapper."""
    frontmatter, body = _split_frontmatter(text)

    def _rewrite_codeblock(match: re.Match[str]) -> str:
        lang = match.group(1)
        code = match.group(2)
        if "scripts/gws-profile.sh" in code:
            code = re.sub(
                r"scripts/gws-profile\.sh <profile> ",
                "scripts/gws-profile.sh <personal|work> ",
                code,
            )
        code = re.sub(
            r"(?m)^(\s*)gws ",
            r"\1scripts/gws-profile.sh <personal|work> ",
            code,
        )
        return f"```{lang}\n{code}```"

    body = re.sub(
        r"```([^\n]*)\n(.*?)```",
        _rewrite_codeblock,
        body,
        flags=re.DOTALL,
    )
    return frontmatter + body


def curate_gws_shared(text: str) -> str:
    """Replace upstream install/auth with Ciaobot integration notes."""
    text = strip_community_etiquette(text)
    pattern = re.compile(r"## Installation\b.*?## Global Flags\b", re.DOTALL)
    if not pattern.search(text):
        return text
    return pattern.sub(_CIAOBOT_SHARED_HEAD + "## Global Flags", text, count=1)


def curate_gws_skill(name: str, text: str) -> str:
    """Apply deterministic Ciaobot curation to a generated skill's content."""
    text = _normalize(text)
    text = strip_openclaw_metadata(text)
    if name == "gws-shared":
        text = curate_gws_shared(text)
    else:
        text = replace_prerequisite(text)
        text = strip_see_also(text)
    text = rewrite_gws_commands(text)
    return _normalize(text)


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
    return curate_gws_skill(name, text)


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
