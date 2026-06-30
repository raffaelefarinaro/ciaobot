#!/usr/bin/env python3
"""Add an upstream skill from GitHub via the `skills` CLI.

Wrapper around `npx skills add` (vercel-labs/skills) that accepts a plain
GitHub URL (or `owner/repo`) and resolves the `--skill <name>` automatically
when the URL deep-links a skill folder, so the user can paste a link instead
of remembering the `owner/repo --skill name` incantation.

The added skill lands in `skills-lock.json` (managed by `npx skills`) and
installs into `.claude/skills/<name>/`. It is then auto-updated on every ciao
startup by `scripts/install-custom-skills.sh` + `scripts/skills_sync.py`
(change-detected via `git ls-remote`), so no further action is needed to keep
it current.

Usage:
  scripts/skills_add.py <github-url-or-owner-repo> [--skill <name>] [--agent <agent>]
  scripts/skills_add.py https://github.com/owner/repo/tree/main/skills/foo
  scripts/skills_add.py owner/repo --skill foo

`--skill` is inferred from a `/skills/<name>` path segment in the URL when
present; otherwise it must be passed explicitly (a repo can hold many skills,
so there is no safe default from the repo name alone).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# owner/repo, optionally followed by /tree/<branch>[/<subpath>].
_URL_RE = re.compile(
    r"^(?:https?://github\.com/)?"
    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"(?:/tree/(?P<branch>[^/]+)(?:/(?P<subpath>.+))?)?"
    r"/?$"
)


def parse_source(raw: str) -> tuple[str, str | None]:
    """Resolve a user input into ``(owner/repo, skill_name_or_None)``.

    Accepts ``owner/repo`` or a full github URL, with or without a
    ``/tree/<branch>/<subpath>`` suffix. The skill name is inferred from a
    ``skills/<name>`` segment in the subpath; otherwise ``None`` (caller must
    pass ``--skill``).
    """
    m = _URL_RE.match(raw.strip())
    if not m:
        raise ValueError(f"Could not parse a GitHub source from: {raw!r}")
    repo = m.group("repo")
    subpath = (m.group("subpath") or "").strip("/")
    skill: str | None = None
    # Match a ".../skills/<name>" segment anywhere in the subpath.
    sm = re.search(r"(?:^|/)skills/([A-Za-z0-9_.-]+)/?$", subpath) or re.search(
        r"skills/([A-Za-z0-9_.-]+)", subpath
    )
    if sm:
        skill = sm.group(1)
    return repo, skill


def add_skill(raw_source: str, skill: str | None, agent: str, *, runner=subprocess.run) -> int:
    """Run `npx skills add <repo> --skill <skill> --agent <agent> -y`.

    Returns the subprocess exit code. ``runner`` is injectable for tests.
    """
    repo, inferred = parse_source(raw_source)
    name = skill or inferred
    if not name:
        print(
            f"Could not infer a skill name from {raw_source!r}. "
            "Pass --skill <name> explicitly.",
            file=sys.stderr,
        )
        return 2
    cmd = ["npx", "-y", "skills", "add", repo, "--skill", name, "--agent", agent, "-y"]
    print(f"Adding skill {name} from {repo} ...")
    result = runner(cmd, cwd=REPO_ROOT)
    if result.returncode == 0:
        print(
            f"Added {name}. It will auto-update on ciao startup "
            "(scripts/install-custom-skills.sh + skills_sync.py)."
        )
    return result.returncode


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Add an upstream skill from GitHub.",
        epilog="Accepts owner/repo or a github URL; infers --skill from a "
        "/skills/<name> URL segment when present.",
    )
    p.add_argument("source", help="owner/repo or https://github.com/owner/repo[/tree/branch/...]")
    p.add_argument("--skill", default=None, help="skill name (inferred from URL if omitted)")
    p.add_argument("--agent", default="claude-code", help="install agent (default: claude-code)")
    args = p.parse_args(argv)
    return add_skill(args.source, args.skill, args.agent)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))