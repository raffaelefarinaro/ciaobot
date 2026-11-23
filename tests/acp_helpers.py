"""Helpers for tests that drive the scripted ACP agent in ``tests/fixtures/acp``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ciao.providers.acp.spec import AgentSpec

FAKE_AGENT = Path(__file__).parent / "fixtures" / "acp" / "fake_agent.py"


def write_scenario(tmp_path: Path, **scenario: Any) -> tuple[AgentSpec, Path]:
    """Write ``scenario`` and return a spec that runs the fake agent on it, and the calls-log path.

    ``spec_*`` keys are not scenario data: they override ``AgentSpec`` fields
    (``spec_initialize_timeout_s``, ``spec_client_meta``, ``spec_env``).
    """
    overrides = {k[len("spec_"):]: scenario.pop(k) for k in list(scenario) if k.startswith("spec_")}
    provider_id = str(scenario.pop("provider_id", "fake"))
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    spec = AgentSpec(
        id=provider_id,
        binary=sys.executable,
        args=(str(FAKE_AGENT), str(scenario_path)),
        **overrides,
    )
    return spec, Path(str(scenario_path) + ".calls.jsonl")


def read_calls(path: Path) -> list[dict[str, Any]]:
    """Every call the fake agent logged, in order; empty when none arrived."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
