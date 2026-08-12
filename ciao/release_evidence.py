"""Release-oriented evals, telemetry normalization, and public evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence, cast

from ciao.eval_runner import (
    ChatObservation,
    ChatRunSpec,
    IsolatedChatServer,
    PreparedChat,
    run_chat_turn,
)
from ciao.evals import (
    EvalAssertions,
    EvalDefaults,
    EvalScenario,
    EvalSchemaError,
    EvalTarget,
    EvalTargetError,
    evaluate_output,
    normalize_tool_identifier,
    stage_eval_target,
)


ReleaseMode = Literal["cold", "warm", "restart"]
_MODES: tuple[ReleaseMode, ...] = ("cold", "warm", "restart")
_PROVIDERS = ("claude", "codex")
_INVENTORY_CATEGORIES = ("skills", "agents", "commands", "mcp_tools", "memory")


class ReleaseEvidenceError(RuntimeError):
    """Raised when release evidence is malformed or incomplete."""


@dataclass(frozen=True, slots=True)
class ReleaseTurn:
    prompt: str
    assertions: EvalAssertions
    required_sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReleaseScenario:
    name: str
    description: str
    target: EvalTarget
    turns: tuple[ReleaseTurn, ...]
    providers: tuple[str, ...]
    model: str
    surface: str
    vault_files: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ReleaseSuite:
    schema_version: Literal[2]
    name: str
    providers: tuple[str, ...]
    model: str
    surface: str
    scenarios: tuple[ReleaseScenario, ...]


@dataclass(frozen=True, slots=True)
class ReleaseEvidenceResult:
    summary: dict[str, Any]
    changes: dict[str, Any]
    report: str
    rationale: str
    files: tuple[Path, ...]
    advisory_flags: tuple[str, ...]
    complete: bool


def _expect_object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise EvalSchemaError(f"{path}: expected object")
    if any(not isinstance(key, str) for key in value):
        raise EvalSchemaError(f"{path}: object keys must be strings")
    return cast(dict[str, object], value)


def _expect_array(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise EvalSchemaError(f"{path}: expected array")
    return value


def _expect_string(value: object, path: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str):
        raise EvalSchemaError(f"{path}: expected string")
    if nonempty and not value.strip():
        raise EvalSchemaError(f"{path}: must not be empty")
    return value


def _check_fields(
    data: Mapping[str, object],
    *,
    path: str,
    allowed: set[str],
    required: set[str],
) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise EvalSchemaError(f"{path}.{unknown[0]}: unknown field")
    missing = sorted(required - set(data))
    if missing:
        raise EvalSchemaError(f"{path}.{missing[0]}: missing required field")


def _enum(value: object, path: str, choices: Sequence[str]) -> str:
    parsed = _expect_string(value, path, nonempty=True)
    if parsed not in choices:
        raise EvalSchemaError(f"{path}: expected one of: {', '.join(choices)}")
    return parsed


def _strings(value: object, path: str) -> tuple[str, ...]:
    return tuple(
        _expect_string(item, f"{path}[{index}]", nonempty=True)
        for index, item in enumerate(_expect_array(value, path))
    )


def _safe_relative_path(value: object, path: str) -> str:
    parsed = _expect_string(value, path, nonempty=True)
    candidate = Path(parsed)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise EvalSchemaError(f"{path}: path must stay relative to the fixture root")
    return candidate.as_posix()


def _parse_providers(value: object, path: str) -> tuple[str, ...]:
    providers = _strings(value, path)
    if not providers:
        raise EvalSchemaError(f"{path}: must contain at least one provider")
    invalid = [item for item in providers if item not in _PROVIDERS]
    if invalid:
        raise EvalSchemaError(
            f"{path}: unsupported provider {invalid[0]!r}; expected claude or codex"
        )
    if len(set(providers)) != len(providers):
        raise EvalSchemaError(f"{path}: duplicate provider")
    return providers


def _parse_vault_files(value: object, path: str) -> tuple[tuple[str, str], ...]:
    data = _expect_object(value, path)
    _check_fields(data, path=path, allowed={"files"}, required={"files"})
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(_expect_array(data["files"], f"{path}.files")):
        item_path = f"{path}.files[{index}]"
        item = _expect_object(raw, item_path)
        _check_fields(
            item,
            path=item_path,
            allowed={"path", "content"},
            required={"path", "content"},
        )
        file_path = _safe_relative_path(item["path"], f"{item_path}.path")
        if file_path in seen:
            raise EvalSchemaError(f"{item_path}.path: duplicate fixture path")
        seen.add(file_path)
        result.append(
            (file_path, _expect_string(item["content"], f"{item_path}.content"))
        )
    return tuple(result)


def _parse_turn(value: object, path: str) -> ReleaseTurn:
    data = _expect_object(value, path)
    _check_fields(
        data,
        path=path,
        allowed={"prompt", "assertions", "required_sources"},
        required={"prompt", "assertions"},
    )
    prompt = _expect_string(data["prompt"], f"{path}.prompt", nonempty=True)
    from ciao.evals import _parse_assertions  # local to keep v1 imports stable

    assertions = _parse_assertions(data["assertions"], f"{path}.assertions")
    sources = (
        _strings(data["required_sources"], f"{path}.required_sources")
        if "required_sources" in data
        else ()
    )
    for source in sources:
        _safe_relative_path(source, f"{path}.required_sources")
    return ReleaseTurn(prompt=prompt, assertions=assertions, required_sources=sources)


def parse_release_suite(value: object) -> ReleaseSuite:
    """Strictly parse a schema-version-2 release evaluation suite."""
    data = _expect_object(value, "$")
    _check_fields(
        data,
        path="$",
        allowed={"schema_version", "name", "defaults", "scenarios"},
        required={"schema_version", "name", "defaults", "scenarios"},
    )
    if data["schema_version"] != 2 or type(data["schema_version"]) is not int:
        raise EvalSchemaError("$.schema_version: expected schema version 2")
    name = _expect_string(data["name"], "$.name", nonempty=True)
    defaults = _expect_object(data["defaults"], "$.defaults")
    _check_fields(
        defaults,
        path="$.defaults",
        allowed={"providers", "model", "surface"},
        required={"providers", "model", "surface"},
    )
    providers = _parse_providers(defaults["providers"], "$.defaults.providers")
    model = _expect_string(defaults["model"], "$.defaults.model", nonempty=True)
    surface = _enum(defaults["surface"], "$.defaults.surface", ("legacy", "mcp"))

    scenarios: list[ReleaseScenario] = []
    seen: set[str] = set()
    for index, raw in enumerate(_expect_array(data["scenarios"], "$.scenarios")):
        path = f"$.scenarios[{index}]"
        scenario = _expect_object(raw, path)
        _check_fields(
            scenario,
            path=path,
            allowed={
                "name",
                "description",
                "target",
                "turns",
                "providers",
                "model",
                "surface",
                "vault_fixture",
            },
            required={"name", "description", "target", "turns"},
        )
        name_value = _expect_string(scenario["name"], f"{path}.name", nonempty=True)
        if name_value in seen:
            raise EvalSchemaError(f"{path}.name: duplicate scenario name")
        seen.add(name_value)
        description = _expect_string(scenario["description"], f"{path}.description")
        from ciao.evals import _parse_target  # local to keep v1 imports stable

        target = _parse_target(scenario["target"], f"{path}.target")
        turns_raw = _expect_array(scenario["turns"], f"{path}.turns")
        if not turns_raw:
            raise EvalSchemaError(f"{path}.turns: must contain at least one turn")
        turns = tuple(
            _parse_turn(item, f"{path}.turns[{turn_index}]")
            for turn_index, item in enumerate(turns_raw)
        )
        scenario_providers = (
            _parse_providers(scenario["providers"], f"{path}.providers")
            if "providers" in scenario
            else providers
        )
        scenario_model = (
            _expect_string(scenario["model"], f"{path}.model", nonempty=True)
            if "model" in scenario
            else model
        )
        scenario_surface = (
            _enum(scenario["surface"], f"{path}.surface", ("legacy", "mcp"))
            if "surface" in scenario
            else surface
        )
        vault_files = (
            _parse_vault_files(scenario["vault_fixture"], f"{path}.vault_fixture")
            if "vault_fixture" in scenario
            else ()
        )
        scenarios.append(
            ReleaseScenario(
                name=name_value,
                description=description,
                target=target,
                turns=turns,
                providers=scenario_providers,
                model=scenario_model,
                surface=scenario_surface,
                vault_files=vault_files,
            )
        )
    if not scenarios:
        raise EvalSchemaError("$.scenarios: must contain at least one scenario")
    return ReleaseSuite(
        schema_version=2,
        name=name,
        providers=providers,
        model=model,
        surface=surface,
        scenarios=tuple(scenarios),
    )


def load_release_suite(path: Path) -> ReleaseSuite:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalSchemaError(f"Could not load release suite {path}: {exc}") from exc
    return parse_release_suite(value)


def _number(value: object) -> float | None:
    try:
        parsed = float(str(value).rstrip("%"))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def normalize_usage_metrics(provider: str, usage: Mapping[str, object]) -> dict[str, float]:
    """Normalize provider usage without conflating provider-specific semantics."""
    result: dict[str, float] = {}
    aliases = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "reasoning_output_tokens": "reasoning_output_tokens",
        "cache_creation_input_tokens": "cache_creation_tokens",
        "cache_read_input_tokens": "cache_read_tokens",
        "cached_input_tokens": "cached_input_tokens",
        "context_window": "context_window_tokens",
        "context_pct": "context_pct",
    }
    for source, target in aliases.items():
        value = _number(usage.get(source))
        if value is not None:
            result[target] = value
    if provider == "claude":
        creation = result.get("cache_creation_tokens", 0.0)
        read = result.get("cache_read_tokens", 0.0)
        if creation + read:
            result["cache_read_share"] = read / (creation + read)
    else:
        input_tokens = result.get("input_tokens", 0.0)
        cached = result.get("cached_input_tokens", 0.0)
        if input_tokens:
            result["cached_input_share"] = min(1.0, cached / input_tokens)
    return result


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100) * len(ordered)))
    return ordered[rank - 1]


def summarize_values(values: Iterable[float]) -> dict[str, float | int | None]:
    parsed = [float(value) for value in values if math.isfinite(float(value))]
    if not parsed:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    ordered = sorted(parsed)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": median,
        "p95": _percentile(ordered, 95),
        "max": ordered[-1],
    }


def _pass_rate(group: Mapping[str, Any]) -> float | None:
    """Return ``passed / repetitions`` for a comparison group, or None.

    Comparing raw ``passed`` across scorecards produced with different
    ``--repeats`` is misleading (3/3 vs 2/2 reads as a regression; 2/2 vs
    2/3 reads as a pass). A missing or zero denominator is treated as
    undefined so it is never compared.
    """
    repetitions = group.get("repetitions")
    if not isinstance(repetitions, int) or repetitions <= 0:
        return None
    passed = group.get("passed", 0)
    try:
        return float(passed) / repetitions
    except (TypeError, ValueError):
        return None


def _cache_metric(group: Mapping[str, Any]) -> float | None:
    """Return the provider-appropriate cache-hit median for a group, or None.

    Claude populates ``cache_read_share`` and Codex populates
    ``cached_input_share``; the opposite field is null and a naive
    ``cache_read_share`` lookup silently lets Codex cache regressions
    pass. The provider tag is on each group, but if it is missing or
    unknown, fall back to whichever field carries a real number.
    """
    provider = str(group.get("provider") or "").lower()
    if provider == "claude":
        value = group.get("cache_read_share", {}).get("median")
        if isinstance(value, (int, float)):
            return float(value)
        value = group.get("cached_input_share", {}).get("median")
        return float(value) if isinstance(value, (int, float)) else None
    if provider == "codex":
        value = group.get("cached_input_share", {}).get("median")
        if isinstance(value, (int, float)):
            return float(value)
        value = group.get("cache_read_share", {}).get("median")
        return float(value) if isinstance(value, (int, float)) else None
    for field_name in ("cached_input_share", "cache_read_share"):
        value = group.get(field_name, {}).get("median")
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_text(root: Path, ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _git_paths(root: Path, ref: str | None) -> set[str]:
    if ref is None:
        result: set[str] = set()
        for path in root.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            result.add(path.relative_to(root).as_posix())
        return result
    process = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return set(process.stdout.splitlines()) if process.returncode == 0 else set()


def _inventory_file(path: str) -> bool:
    return (
        path.startswith("ciao/stock/skills/") and path.endswith("/SKILL.md")
        or path.startswith("ciao/stock/agents/") and path.endswith(".md")
        or path.startswith("ciao/stock/commands/") and path.endswith(".md")
    )


def _memory_file(path: str) -> bool:
    return (
        path.startswith("ciao/memory")
        or path.startswith("ciao/context/")
        or path in {"ciao/fts_search.py", "ciao/vault_index.py"}
        or path in {"ciao/stock/agents/memory.md", "ciao/stock/commands/remember.md"}
        or path == "evals/release.json"
    )


def _mcp_inventory(text: str) -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    matches = list(
        re.finditer(
            r'@tool\(name="([^"]+)"\s*,\s*annotations=(_[A-Z]+)',
            text,
        )
    )
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start() : end]
        name = match.group(1)
        entries[name] = {
            "name": name,
            "annotations": match.group(2),
            "schema_sha256": _sha256_bytes(block.encode("utf-8")),
        }
    return entries


def collect_inventory(root: Path, ref: str | None = None) -> dict[str, Any]:
    """Collect a public structural inventory from the working tree or a Git ref."""
    paths = _git_paths(root, ref)
    entries: dict[str, dict[str, dict[str, str]]] = {
        category: {} for category in _INVENTORY_CATEGORIES
    }
    for path in sorted(paths):
        if _inventory_file(path):
            category = (
                "skills"
                if path.startswith("ciao/stock/skills/")
                else "agents"
                if path.startswith("ciao/stock/agents/")
                else "commands"
            )
            content = (
                root.joinpath(path).read_bytes()
                if ref is None
                else (_git_text(root, ref, path) or "").encode("utf-8")
            )
            entries[category][path] = {
                "path": path,
                "sha256": _sha256_bytes(content),
            }
        if _memory_file(path):
            content = (
                root.joinpath(path).read_bytes()
                if ref is None
                else (_git_text(root, ref, path) or "").encode("utf-8")
            )
            entries["memory"][path] = {
                "path": path,
                "sha256": _sha256_bytes(content),
            }
    mcp_path = "ciao/mcp_server.py"
    mcp_text = (
        root.joinpath(mcp_path).read_text(encoding="utf-8")
        if ref is None and root.joinpath(mcp_path).is_file()
        else _git_text(root, ref, mcp_path) if ref else ""
    ) or ""
    entries["mcp_tools"] = _mcp_inventory(mcp_text)
    return {"categories": entries}


def diff_inventories(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {"categories": {}}
    baseline_categories = baseline.get("categories", {})
    current_categories = current.get("categories", {})
    for category in _INVENTORY_CATEGORIES:
        before = baseline_categories.get(category, {})
        after = current_categories.get(category, {})
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(
            key
            for key in set(before) & set(after)
            if before[key] != after[key]
        )
        result["categories"][category] = {
            "added": [after[key] for key in added],
            "removed": [before[key] for key in removed],
            "changed": [
                {"before": before[key], "after": after[key]} for key in changed
            ],
        }
    return result


def _sample_metrics(provider: str, observation: ChatObservation) -> dict[str, Any]:
    metrics: dict[str, Any] = normalize_usage_metrics(provider, observation.usage)
    metrics.update(
        {
            "elapsed_ms": observation.elapsed_ms,
            "provider_duration_ms": observation.provider_duration_ms,
            "mcp_errors": observation.mcp_errors,
            "provider_tool_count": len(observation.provider_tools),
            "mcp_tool_count": len(observation.mcp_tools),
            "tool_count": len(observation.provider_tools) + len(observation.mcp_tools),
            "mcp_tool_durations_ms": list(observation.mcp_tool_durations_ms),
        }
    )
    return metrics


def _public_assertions(
    result: Any,
    required_sources: Sequence[str],
    paths: Sequence[str],
    *,
    expose_paths: bool = True,
) -> tuple[bool, dict[str, Any]]:
    expected = set(required_sources)
    observed = set(paths)
    source_hits = sorted(expected & observed)
    source_recall = len(source_hits) / len(expected) if expected else None
    source_ok = not expected or expected <= observed
    public_paths = sorted(observed)[:50] if expose_paths else [
        f"source-{_sha256_bytes(path.encode('utf-8'))[:12]}" for path in sorted(observed)[:50]
    ]
    negative_assertions = [
        item
        for item in result.assertion_results
        if item.kind in {"output_not_contains", "output_not_regex"}
    ]
    return bool(result.passed and source_ok), {
        "assertion_count": len(result.assertion_results),
        "assertions_passed": sum(item.passed for item in result.assertion_results),
        "source_expected": len(expected),
        "source_hits": len(source_hits),
        "source_recall": source_recall,
        "memory_retrieval": source_recall,
        "memory_source_accuracy": 1.0 if expected and source_ok else (None if not expected else 0.0),
        "memory_leakage": (
            (0.0 if all(item.passed for item in negative_assertions) else 1.0)
            if negative_assertions
            else None
        ),
        "source_paths": public_paths,
    }


def summarize_samples(samples: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for sample in samples:
        key = (
            str(sample.get("scenario", "")),
            str(sample.get("provider", "")),
            str(sample.get("mode", "")),
        )
        groups.setdefault(key, []).append(sample)
    summaries: list[dict[str, Any]] = []
    numeric_fields = (
        "elapsed_ms",
        "provider_duration_ms",
        "input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "context_pct",
        "context_growth_pct",
        "context_peak_pct",
        "cache_read_share",
        "cached_input_share",
        "memory_retrieval",
        "memory_source_accuracy",
        "memory_leakage",
        "memory_persistence",
        "provider_tool_count",
        "mcp_tool_count",
        "tool_count",
        "mcp_errors",
    )
    for (scenario, provider, mode), rows in sorted(groups.items()):
        summary: dict[str, Any] = {
            "scenario": scenario,
            "provider": provider,
            "mode": mode,
            "repetitions": len(rows),
            "passed": sum(row.get("status") == "passed" for row in rows),
            "failed": sum(row.get("status") != "passed" for row in rows),
        }
        for field_name in numeric_fields:
            summary[field_name] = summarize_values(
                float(row[field_name])
                for row in rows
                if isinstance(row.get(field_name), (int, float))
            )
        summary["source_recall"] = summarize_values(
            float(row["source_recall"])
            for row in rows
            if isinstance(row.get("source_recall"), (int, float))
        )
        summary["source_paths"] = sorted(
            {
                path
                for row in rows
                for path in row.get("source_paths", [])
                if isinstance(path, str)
            }
        )[:50]
        summary["assertion_count"] = sum(
            int(row.get("assertion_count", 0))
            for row in rows
            if isinstance(row.get("assertion_count", 0), int)
        )
        summary["assertions_passed"] = sum(
            int(row.get("assertions_passed", 0))
            for row in rows
            if isinstance(row.get("assertions_passed", 0), int)
        )
        assertion_count = summary["assertion_count"]
        summary["assertion_rate"] = (
            summary["assertions_passed"] / assertion_count
            if assertion_count
            else None
        )
        summary["mcp_tool_duration_ms"] = summarize_values(
            float(duration)
            for row in rows
            for duration in row.get("mcp_tool_durations_ms", [])
            if isinstance(duration, (int, float))
        )
        summaries.append(summary)
    return summaries


def compare_summaries(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare public summaries; performance findings are advisory."""
    before = {
        (row.get("scenario"), row.get("provider"), row.get("mode")): row
        for row in baseline.get("groups", [])
        if isinstance(row, dict)
    }
    after = {
        (row.get("scenario"), row.get("provider"), row.get("mode")): row
        for row in current.get("groups", [])
        if isinstance(row, dict)
    }
    flags: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after), key=str):
        if key not in before:
            flags.append({"kind": "coverage_added", "key": list(key)})
            continue
        if key not in after:
            flags.append({"kind": "coverage_removed", "key": list(key)})
            continue
        old = before[key]
        new = after[key]
        old_pass_rate = _pass_rate(old)
        new_pass_rate = _pass_rate(new)
        if (
            old_pass_rate is not None
            and new_pass_rate is not None
            and new_pass_rate < old_pass_rate
        ):
            flags.append({"kind": "quality_regression", "key": list(key)})
        for field_name in ("elapsed_ms", "provider_duration_ms", "input_tokens", "output_tokens", "context_pct"):
            old_value = old.get(field_name, {}).get("median")
            new_value = new.get(field_name, {}).get("median")
            if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)) and old_value > 0:
                delta = (new_value - old_value) / old_value
                if delta > 0.10:
                    flags.append({
                        "kind": "advisory_regression",
                        "metric": field_name,
                        "delta": delta,
                        "key": list(key),
                    })
        old_recall = old.get("memory_retrieval", {}).get("median")
        new_recall = new.get("memory_retrieval", {}).get("median")
        if (
            isinstance(old_recall, (int, float))
            and isinstance(new_recall, (int, float))
            and new_recall < old_recall
        ):
            flags.append({"kind": "memory_quality_regression", "key": list(key)})
        old_leakage = old.get("memory_leakage", {}).get("median")
        new_leakage = new.get("memory_leakage", {}).get("median")
        if (
            isinstance(old_leakage, (int, float))
            and isinstance(new_leakage, (int, float))
            and new_leakage > old_leakage
        ):
            flags.append({"kind": "memory_leakage_regression", "key": list(key)})
        old_cache = _cache_metric(old)
        new_cache = _cache_metric(new)
        if isinstance(old_cache, (int, float)) and isinstance(new_cache, (int, float)):
            if old_cache - new_cache > 0.10:
                flags.append({"kind": "cache_regression", "key": list(key)})
    return {"flags": flags, "advisory": any(item["kind"] != "quality_regression" for item in flags)}


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _render_report(
    *,
    summary: Mapping[str, Any],
    changes: Mapping[str, Any],
    rationale: str,
) -> str:
    lines = [
        f"# Release evidence: {summary.get('version', 'unversioned')}",
        "",
        "This is a sanitized public scorecard. It contains aggregate behavior and structural changes, not prompts, answers, vault contents, or raw tool payloads.",
        "",
        "## Why",
        "",
        rationale.strip() or "No additional rationale supplied; see CHANGELOG.md.",
        "",
        "## What changed",
        "",
    ]
    for category, diff in changes.get("categories", {}).items():
        lines.append(
            f"- **{category}:** +{len(diff.get('added', []))}, "
            f"-{len(diff.get('removed', []))}, ~{len(diff.get('changed', []))}"
        )
    lines.extend(["", "## Measured behavior", "", "| Scenario | Provider | Mode | Pass | Median ms | Median context % |", "| --- | --- | --- | ---: | ---: | ---: |"])
    for row in summary.get("groups", []):
        latency = row.get("elapsed_ms", {}).get("median")
        context = row.get("context_pct", {}).get("median")
        lines.append(
            f"| {row.get('scenario')} | {row.get('provider')} | {row.get('mode')} | "
            f"{row.get('passed')}/{row.get('repetitions')} | "
            f"{latency if latency is not None else 'n/a'} | "
            f"{context if context is not None else 'n/a'} |"
        )
    comparison = summary.get("comparison")
    if comparison and comparison.get("flags"):
        lines.extend(["", "## Release comparison flags", ""])
        for flag in comparison["flags"]:
            lines.append(f"- `{flag.get('kind')}`: {flag.get('key', '')}")
    lines.append("")
    return "\n".join(lines)


def _write_public_files(
    output: Path,
    *,
    summary: dict[str, Any],
    changes: dict[str, Any],
    rationale: str,
) -> tuple[Path, ...]:
    report = _render_report(summary=summary, changes=changes, rationale=rationale)
    files = {
        "summary.json": json.dumps(summary, indent=2, sort_keys=True) + "\n",
        "changes.json": json.dumps(changes, indent=2, sort_keys=True) + "\n",
        "REPORT.md": report,
        "rationale.md": rationale.rstrip() + "\n",
    }
    for name, content in files.items():
        _write_atomic(output / name, content)
    return tuple(output / name for name in files)


def _seed_vault(root: Path, workspace_name: str, files: Sequence[tuple[str, str]]) -> None:
    vault = (root / "memory-vault" / workspace_name).resolve()
    vault.mkdir(parents=True, exist_ok=True)
    for relative, content in files:
        destination = (vault / relative).resolve()
        if vault not in destination.parents:
            raise EvalTargetError(f"Vault fixture escapes its root: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")


def _copy_external_vault(root: Path, workspace_name: str, source: Path) -> None:
    source_root = source.expanduser().resolve()
    if not source_root.is_dir():
        raise EvalTargetError(f"External vault is not a directory: {source_root}")
    for entry in source_root.rglob("*"):
        if entry.is_symlink():
            resolved = entry.resolve()
            if source_root not in resolved.parents and resolved != source_root:
                raise EvalTargetError(
                    f"External vault symlink escapes its source boundary: {entry}"
                )
    destination = root / "memory-vault" / workspace_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, destination, symlinks=False, dirs_exist_ok=True)


def _sequence(
    server: IsolatedChatServer,
    scenario: ReleaseScenario,
    provider: str,
    *,
    prepared: PreparedChat | None = None,
) -> tuple[PreparedChat, list[ChatObservation]]:
    if not server.is_running:
        server.start()
    first = ChatRunSpec(
        scenario=f"{scenario.name}:0",
        prompt=scenario.turns[0].prompt,
        provider=cast(Any, provider),
        model=scenario.model,
        surface=cast(Any, scenario.surface),
        turn_timeout_s=300.0,
    )
    chat = prepared or server.prepare_chat(first)
    observations: list[ChatObservation] = []
    for index, turn in enumerate(scenario.turns):
        spec = ChatRunSpec(
            scenario=f"{scenario.name}:{index}",
            prompt=turn.prompt,
            provider=cast(Any, provider),
            model=scenario.model,
            surface=cast(Any, scenario.surface),
            turn_timeout_s=300.0,
        )
        observation = run_chat_turn(
            server,
            spec,
            prepared_chat=replace(chat, prepared_at=time.perf_counter()),
        )
        observations.append(observation)
        if observation.error:
            break
    return chat, observations


def _run_mode(
    scenario: ReleaseScenario,
    provider: str,
    mode: ReleaseMode,
    *,
    workspace: Path,
    workspace_name: str,
    startup_timeout_s: float,
    external_vault: Path | None = None,
) -> list[ChatObservation]:
    with tempfile.TemporaryDirectory(prefix="ciao-release-eval-") as raw_root:
        root = Path(raw_root)
        stage_eval_target(workspace, root, scenario.target)
        if external_vault is not None:
            _copy_external_vault(root, workspace_name, external_vault)
        else:
            _seed_vault(root, workspace_name, scenario.vault_files)
        kwargs = {
            "root": root,
            "surface": cast(Any, scenario.surface),
            "provider": cast(Any, provider),
            "workspace_name": workspace_name,
            "startup_timeout": startup_timeout_s,
            "install_packaged_assets": False,
            "require_subagent_synthesis": scenario.target.kind == "subagent",
        }
        server = IsolatedChatServer(**kwargs)
        try:
            if mode == "cold":
                _, observations = _sequence(server, scenario, provider)
                return observations
            if mode == "warm":
                server.start()
                chat, _ = _sequence(server, scenario, provider)
                _, measured = _sequence(server, scenario, provider, prepared=chat)
                return measured
            _, _ = _sequence(server, scenario, provider)
        finally:
            server.stop()

        restarted = IsolatedChatServer(**kwargs)
        try:
            _, measured = _sequence(restarted, scenario, provider)
            return measured
        finally:
            restarted.stop()


def run_release_evidence(
    *,
    suite_path: Path,
    workspace: Path,
    output: Path,
    version: str,
    baseline_summary: Path | None = None,
    from_ref: str | None = None,
    to_ref: str = "HEAD",
    rationale: str = "",
    modes: Sequence[ReleaseMode] = _MODES,
    repeats: int = 3,
    startup_timeout_s: float = 30.0,
    require_complete: bool = True,
    mode_runner: Callable[..., list[ChatObservation]] | None = None,
    external_vault: Path | None = None,
) -> ReleaseEvidenceResult:
    if repeats <= 0:
        raise ReleaseEvidenceError("repeats must be greater than zero")
    selected_modes = tuple(dict.fromkeys(modes))
    if not selected_modes or any(mode not in _MODES for mode in selected_modes):
        raise ReleaseEvidenceError("modes must be cold, warm, or restart")
    suite = load_release_suite(suite_path.resolve())
    current_inventory = collect_inventory(workspace.resolve(), to_ref)
    baseline_inventory = collect_inventory(workspace.resolve(), from_ref) if from_ref else {"categories": {}}
    changes = diff_inventories(baseline_inventory, current_inventory)
    changes["from_ref"] = from_ref
    changes["to_ref"] = to_ref
    samples: list[dict[str, Any]] = []
    complete = True
    runner = mode_runner or _run_mode
    for scenario in suite.scenarios:
        for provider in scenario.providers:
            for mode in selected_modes:
                for repetition in range(1, repeats + 1):
                    try:
                        observations = runner(
                            scenario,
                            provider,
                            mode,
                            workspace=workspace,
                            workspace_name="personal",
                            startup_timeout_s=startup_timeout_s,
                            external_vault=external_vault,
                        )
                    except Exception as exc:  # preserve public evidence for failures
                        complete = False
                        samples.append({
                            "scenario": scenario.name,
                            "provider": provider,
                            "mode": mode,
                            "repetition": repetition,
                            "status": "failed",
                            "error_kind": type(exc).__name__,
                            "assertion_count": 0,
                            "assertions_passed": 0,
                        })
                        continue
                    passed = True
                    turn_metrics = [
                        _sample_metrics(provider, item) for item in observations
                    ]
                    context_values = [
                        metric["context_pct"]
                        for metric in turn_metrics
                        if isinstance(metric.get("context_pct"), (int, float))
                    ]
                    assertion_count = 0
                    assertions_passed = 0
                    final_memory: dict[str, Any] = {}
                    persistence_checks: list[bool] = []
                    for index, observation in enumerate(observations):
                        turn = scenario.turns[index]
                        eval_scenario = EvalScenario(
                            name=f"{scenario.name}:{index}",
                            description=scenario.description,
                            target=scenario.target,
                            prompt=turn.prompt,
                            provider=cast(Any, provider),
                            model=scenario.model,
                            surface=cast(Any, scenario.surface),
                            assertions=turn.assertions,
                        )
                        result = evaluate_output(
                            eval_scenario,
                            observation.final_text,
                            (*observation.provider_tools, *observation.mcp_tools),
                            duration_s=observation.elapsed_ms / 1000,
                            error=observation.error or None,
                        )
                        turn_passed, memory = _public_assertions(
                            result,
                                turn.required_sources,
                                observation.mcp_result_paths,
                            expose_paths=external_vault is None,
                        )
                        assertion_count += int(memory["assertion_count"])
                        assertions_passed += int(memory["assertions_passed"])
                        if turn.required_sources:
                            persistence_checks.append(
                                memory["memory_source_accuracy"] == 1.0
                            )
                        final_memory = memory
                        passed = passed and turn_passed
                        if not turn_passed:
                            complete = False
                        if index == len(observations) - 1:
                            metrics = dict(turn_metrics[index])
                            if context_values:
                                metrics["context_peak_pct"] = max(context_values)
                                metrics["context_growth_pct"] = (
                                    context_values[-1] - context_values[0]
                                )
                            if persistence_checks:
                                metrics["memory_persistence"] = sum(
                                    persistence_checks
                                ) / len(persistence_checks)
                            row = {
                                "scenario": scenario.name,
                                "provider": provider,
                                "mode": mode,
                                "repetition": repetition,
                                "status": "passed" if passed else "failed",
                                "metrics": metrics,
                                "assertion_count": assertion_count,
                                "assertions_passed": assertions_passed,
                                "tools": sorted({
                                    normalize_tool_identifier(tool)
                                    for tool in (*observation.provider_tools, *observation.mcp_tools)
                                    if tool
                                }),
                                **final_memory,
                            }
                            samples.append(row)
                    if len(observations) != len(scenario.turns):
                        complete = False
                        if not observations:
                            samples.append({
                                "scenario": scenario.name,
                                "provider": provider,
                                "mode": mode,
                                "repetition": repetition,
                                "status": "failed",
                                "assertion_count": 0,
                                "assertions_passed": 0,
                            })

    flattened: list[dict[str, Any]] = []
    for sample in samples:
        row = dict(sample)
        metrics = row.pop("metrics", {})
        row.update(metrics)
        flattened.append(row)
    summary: dict[str, Any] = {
        "version": version,
        "generated_at": datetime.now(UTC).isoformat(),
        "revision": _git_revision(workspace),
        "suite": suite.name,
        "providers": list(suite.providers),
        "modes": list(selected_modes),
        "repetitions": repeats,
        "complete": complete,
        "groups": summarize_samples(flattened),
    }
    if baseline_summary is not None and baseline_summary.is_file():
        try:
            baseline = json.loads(baseline_summary.read_text(encoding="utf-8"))
            summary["comparison"] = compare_summaries(baseline, summary)
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseEvidenceError(f"Could not read baseline summary: {exc}") from exc
    elif baseline_summary is not None:
        summary["comparison"] = {"flags": [{"kind": "baseline_missing"}], "advisory": True}
    flags = tuple(
        f"{item.get('kind')}:{','.join(map(str, item.get('key', [])))}"
        for item in summary.get("comparison", {}).get("flags", [])
    )
    report_rationale = rationale.strip() or "See CHANGELOG.md and the release PR for the release rationale."
    files = _write_public_files(
        output.resolve(),
        summary=summary,
        changes=changes,
        rationale=report_rationale,
    )
    if require_complete and not complete:
        raise ReleaseEvidenceError(
            "Release evaluation evidence is incomplete or has correctness failures; "
            f"public evidence was written to {output.resolve()}"
        )
    return ReleaseEvidenceResult(
        summary=summary,
        changes=changes,
        report=(output.resolve() / "REPORT.md").read_text(encoding="utf-8"),
        rationale=report_rationale,
        files=files,
        advisory_flags=flags,
        complete=complete,
    )


def _git_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def compare_summary_files(baseline: Path, current: Path) -> dict[str, Any]:
    try:
        before = json.loads(baseline.read_text(encoding="utf-8"))
        after = json.loads(current.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError(f"Could not load summary: {exc}") from exc
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ReleaseEvidenceError("summary files must contain JSON objects")
    return compare_summaries(before, after)
