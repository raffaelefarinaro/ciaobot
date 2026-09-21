"""Rescore stored behavioral-eval reports with the current scoring code.

Usage: PYTHONPATH=. python scripts/surface-compare/rescore.py REPORT.json ...

Writes ``<name>.rescored.json`` beside each input, keeping provenance, budget
and the raw outcomes (tools, writes, answer, deferred) and recomputing
violations, per-outcome scores, the aggregate dimensions and the
zero-tolerance list. Used by the MCP-versus-CLI comparison so a scoring fix
(for example, mapping ``ciao vault search`` to ``vault_search``) applies to
every arm without re-spending model calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ciao import behavioral_eval as be


def rescore(path: Path) -> Path:
    report = json.loads(path.read_text(encoding="utf-8"))
    scenarios = {s.id: s for s in be.load_scenarios().scenarios}
    outcomes = []
    for raw in report["outcomes"]:
        if not raw.get("ok"):
            outcomes.append(be.RunOutcome(**{k: raw[k] for k in raw if k in be.RunOutcome.__dataclass_fields__}))
            continue
        scenario = scenarios[raw["scenario_id"]]
        record = be.BehaviorRecord(
            tools=tuple(raw.get("tools") or ()),
            writes=tuple(raw.get("writes") or ()),
            answer=str(raw.get("answer") or ""),
            deferred=tuple(raw.get("deferred") or ()),
        )
        outcomes.append(
            be.RunOutcome(
                scenario_id=scenario.id,
                category=scenario.category,
                repeat=int(raw.get("repeat", 0)),
                ok=True,
                error="",
                answer=record.answer,
                tools=record.tools,
                writes=record.writes,
                deferred=record.deferred,
                violations=be.detect_violations(scenario, record),
                scores=be.score_record(scenario, record),
            )
        )
    dims = be._aggregate(tuple(outcomes))
    report["dimensions"] = {k: v.to_dict() for k, v in sorted(dims.items())}
    report["zero_tolerance_failures"] = [
        {"scenario_id": o.scenario_id, "repeat": o.repeat, "violations": list(o.violations)}
        for o in outcomes
        if o.violations
    ]
    report["outcomes"] = [o.to_dict() for o in outcomes]
    report["label"] = f"{report['label']}-rescored"
    out = path.with_suffix(".rescored.json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    ra = dims.get("routing_accuracy")
    print(f"{path.name}: routing_accuracy {ra.mean:.3f} (n={ra.n}); violations {len(report['zero_tolerance_failures'])}")
    return out


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        rescore(Path(arg))
