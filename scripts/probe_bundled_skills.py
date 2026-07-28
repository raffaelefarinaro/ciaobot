#!/usr/bin/env python3
"""Probe the bundled Claude CLI and assert the visible bundled skills match the
expected keep-list for Ciaobot.

This is the integration-style drift detector proposed in:
  memory-vault/personal/Workspace/bundled-skills-evaluation.md

Run from the ciaobot repo root:

  python3 scripts/probe_bundled_skills.py

The script runs the bundled `claude` binary with the same `skillOverrides` layer
Ciaobot injects (via `ciao.execution_modes.harness_skill_overrides()`), reads the
init payload, and checks that the visible `skills` array is exactly the kept
set. If a future CLI upgrade adds a new bundled skill, this fails and we decide
to keep or hide it. If `HARNESS_DISABLED_SKILLS` drifts, it also fails.

Plugin skills (e.g. `skill-creator:skill-creator`) are intentionally excluded
from the assertion: `skillOverrides` does not affect plugins, so they will
appear regardless. See the note in `ciao/execution_modes.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# The bundled skills Ciaobot intentionally keeps visible. Must stay in sync
# with HARNESS_DISABLED_SKILLS and the rationale in bundled-skills-evaluation.md.
EXPECTED_VISIBLE_BUNDLED_SKILLS = {
    "batch",
    "claude-api",
    "code-review",
    "debug",
    "deep-research",
    "simplify",
    "verify",
}


def get_bundled_claude_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    candidate = (
        repo_root
        / ".venv"
        / "lib"
        / "python3.13"
        / "site-packages"
        / "claude_agent_sdk"
        / "_bundled"
        / "claude"
    )
    if not candidate.exists():
        raise FileNotFoundError(f"bundled Claude CLI not found at {candidate}")
    return candidate


def main() -> int:
    cli = get_bundled_claude_path()
    repo_root = cli.parents[4].parents[3]

    # Import the same override map Ciaobot uses so this cannot drift from it.
    sys.path.insert(0, str(repo_root))
    from ciao.execution_modes import harness_skill_overrides

    overrides = {"skillOverrides": harness_skill_overrides()}
    settings_json = json.dumps(overrides, separators=(",", ":"))

    # Run from a neutral cwd so the bundled CLI does not pick up the ciaobot
    # repo's own .claude/skills/ (which would appear as additional skills).
    import tempfile

    with tempfile.TemporaryDirectory() as neutral_cwd:
        proc = subprocess.run(
            [
                str(cli),
                "-p",
                "--settings",
                settings_json,
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                "haiku",
                "hi",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=neutral_cwd,
        )
    if proc.returncode != 0:
        print(f"bundled CLI exited {proc.returncode}", file=sys.stderr)
        print(proc.stderr[-2000:], file=sys.stderr)
        return 1

    if not proc.stdout:
        print("bundled CLI produced no stdout", file=sys.stderr)
        return 1

    init_line = proc.stdout.splitlines()[0]
    init = json.loads(init_line)
    visible = set(init.get("skills", []))

    # Plugin skills are not affected by skillOverrides; do not assert on them.
    plugin_skills = {"skill-creator:skill-creator"}
    visible_without_plugins = visible - plugin_skills

    missing = EXPECTED_VISIBLE_BUNDLED_SKILLS - visible_without_plugins
    extra = visible_without_plugins - EXPECTED_VISIBLE_BUNDLED_SKILLS

    if missing or extra:
        print("FAIL: visible bundled skills do not match expected set")
        print(f"  expected (no plugins): {sorted(EXPECTED_VISIBLE_BUNDLED_SKILLS)}")
        print(f"  actual:                {sorted(visible_without_plugins)}")
        if missing:
            print(f"  missing: {sorted(missing)}")
        if extra:
            print(f"  extra:   {sorted(extra)}")
        print(
            "Update HARNESS_DISABLED_SKILLS in ciao/execution_modes.py and/or "
            "the expected set in this script, then refresh bundled-skills-evaluation.md."
        )
        return 1

    print("OK: visible bundled skills match expected set")
    print(f"  kept:   {sorted(EXPECTED_VISIBLE_BUNDLED_SKILLS)}")
    print(f"  hidden: {sorted(harness_skill_overrides().keys())}")
    print(f"  plugins (unaffected): {sorted(visible & plugin_skills)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
