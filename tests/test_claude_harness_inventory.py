"""Claude harness inventory: every built-in tool and bundled skill of the bundled CLI is classified.

A new built-in tool or bundled skill in a bundled Claude CLI costs context in every
chat, or it is a surface that bypasses Ciaobot (a new cron or routine tool). So it
must be classified in ``ciao/claude_trim.py`` before it ships: a ``TrimOption``
with a default, or a ``KEPT_CLAUDE_TOOLS`` entry with a reason.

The CLI prints its ``system/init`` message before any API call. The probe runs it
with an invalid API key and throwaway ``HOME`` and ``cwd``, reads that one line,
and kills the process, so the inventory works offline and the developer's own
skills and credentials stay out of it.

When a test fails, it names each unclassified tool or skill. Add a ``TrimOption``
or a ``KEPT_CLAUDE_TOOLS`` entry; see /ciao-upkeep section 1, "Claude harness trim
catalog".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ciao.claude_trim import (
    KEPT_CLAUDE_TOOLS,
    TRIM_OPTIONS,
    claude_settings_layer,
)
from ciao.providers.claude import get_bundled_claude_path

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="the inventory does not depend on the platform; the macOS and Linux runs cover it",
    ),
]


@pytest.fixture(scope="module")
def cli() -> str:
    path = get_bundled_claude_path()
    if path is None:
        pytest.skip("no bundled Claude CLI")
    return path


def _init_payload(cli: str, tmp_path: Path, settings: dict | None = None) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "ANTHROPIC_API_KEY": "sk-ant-invalid-inventory-probe",
        "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    argv = [cli, "-p", "hi", "--output-format", "stream-json", "--verbose", "--max-turns", "1"]
    if settings is not None:
        argv += ["--settings", json.dumps(settings)]
    proc = subprocess.Popen(
        argv,
        cwd=work,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "system" and msg.get("subtype") == "init":
                return msg
            if time.monotonic() > deadline:
                break
        pytest.fail("bundled Claude CLI produced no init message")
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_every_builtin_tool_is_classified(cli: str, tmp_path: Path) -> None:
    init = _init_payload(cli, tmp_path)
    classified = (
        {o.name for o in TRIM_OPTIONS if o.kind == "tool"}
        | {c for o in TRIM_OPTIONS for c in o.covers}
        | KEPT_CLAUDE_TOOLS
    )
    unknown = sorted(t for t in init["tools"] if not t.startswith("mcp__") and t not in classified)
    assert unknown == [], (
        f"Claude CLI {init.get('claude_code_version')} has unclassified built-in tools {unknown}: "
        "add each to ciao/claude_trim.py (TRIM_OPTIONS or KEPT_CLAUDE_TOOLS)"
    )


def test_every_bundled_skill_is_classified(cli: str, tmp_path: Path) -> None:
    init = _init_payload(cli, tmp_path)
    classified = {o.name for o in TRIM_OPTIONS if o.kind == "skill"} | {
        c for o in TRIM_OPTIONS for c in o.covers
    }
    unknown = sorted(s for s in init["skills"] if s not in classified)
    assert unknown == [], (
        f"Claude CLI {init.get('claude_code_version')} has unclassified bundled skills {unknown}: "
        "add each to ciao/claude_trim.py (a skill TrimOption, or a covers entry of a setting option)"
    )


def test_setting_options_remove_what_they_cover(cli: str, tmp_path: Path) -> None:
    base = _init_payload(cli, tmp_path)
    trimmed = _init_payload(
        cli, tmp_path, {o.name: True for o in TRIM_OPTIONS if o.kind == "setting"}
    )
    base_names = set(base["tools"]) | set(base["skills"])
    trimmed_names = set(trimmed["tools"]) | set(trimmed["skills"])
    present_before: list[str] = []
    for option in TRIM_OPTIONS:
        if option.kind != "setting":
            continue
        for name in option.covers:
            if name not in base_names:
                continue
            present_before.append(name)
            assert name not in trimmed_names, (
                f"{option.key} is set but the CLI still lists {name}; the setting does not remove it"
            )
    # Guard against a CLI that lists none of the covered names: the loop above would pass vacuously.
    assert present_before, "no setting-covered tool or skill is listed by the CLI; the check proves nothing"


def test_trim_layer_is_accepted_by_the_cli(cli: str, tmp_path: Path) -> None:
    layer: dict[str, Any] = claude_settings_layer({})
    off_skills = set(layer["skillOverrides"])
    init = _init_payload(cli, tmp_path, layer)
    still_listed = sorted(off_skills & set(init["skills"]))
    assert still_listed == [], (
        f"the default trim layer is not applied: skills {still_listed} are still listed"
    )
