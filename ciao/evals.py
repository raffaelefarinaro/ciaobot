"""Strict declarative schemas and deterministic assertions for live agent evals."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, TypeAlias, cast

from ciao.eval_runner import (
    ChatObservation,
    ChatRunSpec,
    IsolatedChatServer,
    run_chat_turn,
)
from ciao import provider_registry
from ciao.sync_skills import _install_codex_agents, sync_workspace_skills

TargetKind: TypeAlias = Literal["skill", "subagent"]
# Validated at runtime against ciao.provider_registry; the alias stays a plain
# str so adding a provider does not require editing a Literal.
ProviderName: TypeAlias = str
SurfaceName: TypeAlias = Literal["legacy", "mcp"]
EvalStatus: TypeAlias = Literal["pending", "passed", "failed", "interrupted"]
AssertionKind: TypeAlias = Literal[
    "output_contains",
    "output_not_contains",
    "output_regex",
    "output_not_regex",
    "required_tools",
    "forbidden_tools",
]

_TARGET_KINDS = ("skill", "subagent")
_PROVIDERS = provider_registry.provider_ids()
_SURFACES = ("legacy", "mcp")
_ASSERTION_FIELDS = (
    "output_contains",
    "output_not_contains",
    "output_regex",
    "output_not_regex",
    "required_tools",
    "forbidden_tools",
)


class EvalSchemaError(ValueError):
    """Raised when an eval suite does not conform to schema version 1."""


class EvalTargetError(ValueError):
    """Raised when an eval target cannot be staged unambiguously."""


class EvalReportError(RuntimeError):
    """Raised when completed eval state cannot be published."""


@dataclass(frozen=True, slots=True)
class EvalTarget:
    """A skill or subagent selected for isolated evaluation."""

    kind: TargetKind
    name: str


@dataclass(frozen=True, slots=True)
class EvalAssertions:
    """Deterministic assertions applied to one chat observation."""

    output_contains: tuple[str, ...] = field(default_factory=tuple)
    output_not_contains: tuple[str, ...] = field(default_factory=tuple)
    output_regex: tuple[str, ...] = field(default_factory=tuple)
    output_not_regex: tuple[str, ...] = field(default_factory=tuple)
    required_tools: tuple[str, ...] = field(default_factory=tuple)
    forbidden_tools: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class EvalDefaults:
    """Provider routing defaults for every scenario in a suite."""

    provider: ProviderName
    model: str
    surface: SurfaceName


@dataclass(frozen=True, slots=True)
class EvalScenario:
    """One target, prompt, routing override, and assertion set."""

    name: str
    description: str
    target: EvalTarget
    prompt: str
    provider: ProviderName | None
    model: str | None
    surface: SurfaceName | None
    assertions: EvalAssertions


@dataclass(frozen=True, slots=True)
class EvalSuite:
    """A parsed schema-version-1 eval suite."""

    schema_version: Literal[1]
    name: str
    defaults: EvalDefaults
    scenarios: tuple[EvalScenario, ...]


@dataclass(frozen=True, slots=True)
class EvalAssertionResult:
    """The deterministic outcome of one configured assertion."""

    kind: AssertionKind
    expected: str
    passed: bool


@dataclass(frozen=True, slots=True)
class EvalResult:
    """Assertion results for one completed scenario."""

    scenario_name: str
    passed: bool
    duration_s: float
    assertion_results: tuple[EvalAssertionResult, ...]
    normalized_tools: tuple[str, ...]
    output: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EvalRunOverrides:
    """Optional command-line routing values applied above suite configuration."""

    provider: ProviderName | None = None
    model: str | None = None
    surface: SurfaceName | None = None


@dataclass(frozen=True, slots=True)
class EvalScenarioRecord:
    """Serializable execution and assertion record for one selected scenario."""

    name: str
    description: str
    target: EvalTarget
    status: EvalStatus
    provider: ProviderName
    model: str
    surface: SurfaceName
    assertions: tuple[EvalAssertionResult, ...] = ()
    output: str = ""
    error: str | None = None
    selected_model: str | None = None
    effective_model: str | None = None
    normalized_tools: tuple[str, ...] = ()
    raw_usage: dict[str, str] = field(default_factory=dict)
    token_total: int | None = None
    elapsed_ms: int | None = None
    provider_duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class EvalSuiteRun:
    """Completed declarative suite run and its process exit code."""

    suite: EvalSuite
    records: tuple[EvalScenarioRecord, ...]
    exit_code: Literal[0, 1]


@dataclass(frozen=True, slots=True)
class StagedEvalTarget:
    """A target copied into one isolated evaluation workspace."""

    target: EvalTarget
    definition_path: Path
    companion_path: Path | None = None


class _ServerFactory(Protocol):
    def __call__(
        self,
        *,
        root: Path,
        surface: SurfaceName,
        provider: ProviderName,
        workspace_name: str,
        startup_timeout: float,
        install_packaged_assets: bool = True,
        require_subagent_synthesis: bool = False,
        subagent_discovery_polls: int = 3,
    ) -> IsolatedChatServer: ...


class _RunTurn(Protocol):
    def __call__(
        self,
        server: IsolatedChatServer,
        spec: ChatRunSpec,
    ) -> ChatObservation: ...


def _schema_error(path: str, message: str) -> EvalSchemaError:
    return EvalSchemaError(f"{path}: {message}")


def _expect_object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _schema_error(path, "expected object")
    for key in value:
        if not isinstance(key, str):
            raise _schema_error(path, "object keys must be strings")
    return cast(dict[str, object], value)


def _expect_array(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise _schema_error(path, "expected array")
    return value


def _check_fields(
    value: dict[str, object],
    *,
    path: str,
    allowed: frozenset[str],
    required: frozenset[str],
) -> None:
    unknown = sorted(value.keys() - allowed)
    if unknown:
        raise _schema_error(f"{path}.{unknown[0]}", "unknown field")
    missing = sorted(required - value.keys())
    if missing:
        raise _schema_error(f"{path}.{missing[0]}", "missing required field")


def _expect_string(
    value: object,
    path: str,
    *,
    nonempty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise _schema_error(path, "expected string")
    if nonempty and not value.strip():
        raise _schema_error(path, "must not be empty")
    return value


def _expect_enum(
    value: object,
    path: str,
    choices: tuple[str, ...],
) -> str:
    parsed = _expect_string(value, path)
    if parsed not in choices:
        raise _schema_error(path, f"expected one of: {', '.join(choices)}")
    return parsed


def _parse_string_array(value: object, path: str) -> tuple[str, ...]:
    items = _expect_array(value, path)
    parsed: list[str] = []
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]"
        parsed.append(_expect_string(item, item_path, nonempty=True))
    return tuple(parsed)


def _parse_target(value: object, path: str) -> EvalTarget:
    data = _expect_object(value, path)
    _check_fields(
        data,
        path=path,
        allowed=frozenset({"kind", "name"}),
        required=frozenset({"kind", "name"}),
    )
    kind = cast(TargetKind, _expect_enum(data["kind"], f"{path}.kind", _TARGET_KINDS))
    name = _expect_string(data["name"], f"{path}.name", nonempty=True)
    return EvalTarget(kind=kind, name=name)


def _parse_assertions(value: object, path: str) -> EvalAssertions:
    data = _expect_object(value, path)
    fields = frozenset(_ASSERTION_FIELDS)
    _check_fields(data, path=path, allowed=fields, required=fields)

    parsed = {
        field_name: _parse_string_array(data[field_name], f"{path}.{field_name}")
        for field_name in _ASSERTION_FIELDS
    }
    if not any(parsed.values()):
        raise _schema_error(path, "must contain at least one assertion")

    for field_name in ("output_regex", "output_not_regex"):
        for index, pattern in enumerate(parsed[field_name]):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise _schema_error(
                    f"{path}.{field_name}[{index}]",
                    f"invalid regex: {exc}",
                ) from exc

    return EvalAssertions(
        output_contains=parsed["output_contains"],
        output_not_contains=parsed["output_not_contains"],
        output_regex=parsed["output_regex"],
        output_not_regex=parsed["output_not_regex"],
        required_tools=parsed["required_tools"],
        forbidden_tools=parsed["forbidden_tools"],
    )


def _parse_defaults(value: object, path: str) -> EvalDefaults:
    data = _expect_object(value, path)
    fields = frozenset({"provider", "model", "surface"})
    _check_fields(data, path=path, allowed=fields, required=fields)
    provider = cast(
        ProviderName,
        _expect_enum(data["provider"], f"{path}.provider", _PROVIDERS),
    )
    model = _expect_string(data["model"], f"{path}.model", nonempty=True)
    surface = cast(
        SurfaceName,
        _expect_enum(data["surface"], f"{path}.surface", _SURFACES),
    )
    return EvalDefaults(provider=provider, model=model, surface=surface)


def _parse_optional_routing(
    data: dict[str, object],
    *,
    path: str,
) -> tuple[ProviderName | None, str | None, SurfaceName | None]:
    provider: ProviderName | None = None
    if "provider" in data:
        provider = cast(
            ProviderName,
            _expect_enum(data["provider"], f"{path}.provider", _PROVIDERS),
        )

    model: str | None = None
    if "model" in data:
        model = _expect_string(data["model"], f"{path}.model", nonempty=True)

    surface: SurfaceName | None = None
    if "surface" in data:
        surface = cast(
            SurfaceName,
            _expect_enum(data["surface"], f"{path}.surface", _SURFACES),
        )
    return provider, model, surface


def _parse_scenario(value: object, path: str) -> EvalScenario:
    data = _expect_object(value, path)
    allowed = frozenset(
        {
            "name",
            "description",
            "target",
            "prompt",
            "provider",
            "model",
            "surface",
            "assertions",
        }
    )
    required = frozenset(
        {"name", "description", "target", "prompt", "assertions"}
    )
    _check_fields(data, path=path, allowed=allowed, required=required)

    name = _expect_string(data["name"], f"{path}.name", nonempty=True)
    description = _expect_string(data["description"], f"{path}.description")
    target = _parse_target(data["target"], f"{path}.target")
    prompt = _expect_string(data["prompt"], f"{path}.prompt", nonempty=True)
    provider, model, surface = _parse_optional_routing(data, path=path)
    assertions = _parse_assertions(data["assertions"], f"{path}.assertions")
    return EvalScenario(
        name=name,
        description=description,
        target=target,
        prompt=prompt,
        provider=provider,
        model=model,
        surface=surface,
        assertions=assertions,
    )


def parse_eval_suite(value: object) -> EvalSuite:
    """Parse an in-memory JSON value as a strict schema-version-1 suite."""
    data = _expect_object(value, "$")
    allowed = frozenset({"schema_version", "name", "defaults", "scenarios"})
    _check_fields(data, path="$", allowed=allowed, required=allowed)

    version = data["schema_version"]
    if type(version) is not int:
        raise _schema_error("$.schema_version", "expected integer")
    if version != 1:
        raise _schema_error(
            "$.schema_version",
            f"unsupported schema version {version}",
        )

    name = _expect_string(data["name"], "$.name", nonempty=True)
    defaults = _parse_defaults(data["defaults"], "$.defaults")
    raw_scenarios = _expect_array(data["scenarios"], "$.scenarios")
    if not raw_scenarios:
        raise _schema_error("$.scenarios", "must contain at least one scenario")

    scenarios: list[EvalScenario] = []
    seen_names: set[str] = set()
    for index, raw_scenario in enumerate(raw_scenarios):
        path = f"$.scenarios[{index}]"
        scenario = _parse_scenario(raw_scenario, path)
        if scenario.name in seen_names:
            raise _schema_error(f"{path}.name", "duplicate scenario name")
        seen_names.add(scenario.name)
        scenarios.append(scenario)

    return EvalSuite(
        schema_version=1,
        name=name,
        defaults=defaults,
        scenarios=tuple(scenarios),
    )


def load_eval_suite(path: Path) -> EvalSuite:
    """Load and strictly parse one JSON eval suite."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _schema_error("$", f"invalid JSON: {exc.msg}") from exc
    return parse_eval_suite(value)


def resolve_routing(
    defaults: EvalDefaults,
    scenario: EvalScenario,
    overrides: EvalRunOverrides | None = None,
) -> EvalDefaults:
    """Resolve provider routing with CLI, scenario, then suite precedence."""
    cli = overrides or EvalRunOverrides()
    if cli.model is not None and not cli.model.strip():
        raise EvalSchemaError("model override must not be empty")
    return EvalDefaults(
        provider=cli.provider or scenario.provider or defaults.provider,
        model=(
            cli.model
            if cli.model is not None
            else scenario.model or defaults.model
        ),
        surface=cli.surface or scenario.surface or defaults.surface,
    )


def _validate_target_name(target: EvalTarget) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", target.name):
        raise EvalTargetError(
            f"Invalid {target.kind} target name {target.name!r}; "
            "expected letters, numbers, hyphens, or underscores"
        )


def _is_within(path: Path, boundary: Path) -> bool:
    return path == boundary or boundary in path.parents


def _reject_workspace_overlap(source_root: Path, destination_root: Path) -> None:
    if _is_within(destination_root, source_root) or _is_within(
        source_root, destination_root
    ):
        raise EvalTargetError(
            "Source and isolated workspaces overlap: "
            f"{source_root} and {destination_root}"
        )


def _resolved_path(path: Path, *, label: str) -> Path:
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise EvalTargetError(f"{label} cannot be resolved safely: {path}") from exc


def _require_within(path: Path, boundary: Path, *, label: str) -> Path:
    resolved = _resolved_path(path, label=label)
    if not _is_within(resolved, boundary):
        raise EvalTargetError(
            f"{label} escapes its canonical source boundary: {path} -> {resolved}"
        )
    return resolved


def _validate_materialized_tree(
    source: Path,
    *,
    boundary: Path,
    label: str,
) -> None:
    """Allow internal links, reject escapes, and let copytree materialize them."""
    _require_within(source, boundary, label=label)
    try:
        entries = source.rglob("*")
        for entry in entries:
            if entry.is_symlink():
                _require_within(entry, boundary, label=label)
    except OSError as exc:
        raise EvalTargetError(f"{label} cannot be inspected safely: {source}") from exc


def _distinct_existing_skill_sources(
    source_workspace: Path,
    name: str,
) -> list[Path]:
    candidates = (
        source_workspace / "skills" / name,
        source_workspace / ".claude" / "skills" / name,
        source_workspace / ".agents" / "skills" / name,
    )
    distinct: dict[Path, Path] = {}
    for candidate in candidates:
        if not (candidate / "SKILL.md").is_file():
            continue
        try:
            identity = candidate.resolve(strict=True)
        except OSError:
            identity = candidate.absolute()
        distinct.setdefault(identity, candidate)
    return list(distinct.values())


def _copy_skill_target(
    source_workspace: Path,
    isolated_root: Path,
    target: EvalTarget,
) -> Path:
    candidates = _distinct_existing_skill_sources(source_workspace, target.name)
    if len(candidates) > 1:
        rendered = ", ".join(str(path) for path in candidates)
        raise EvalTargetError(
            f"Skill target {target.name!r} is ambiguous in the source workspace: "
            f"{rendered}"
        )

    destination = isolated_root / "skills" / target.name
    if destination.exists() or destination.is_symlink():
        raise EvalTargetError(f"Isolated skill destination already exists: {destination}")
    if candidates:
        source = candidates[0]
        resolved_source = _require_within(
            source,
            source_workspace,
            label=f"Skill target {target.name!r}",
        )
        _validate_materialized_tree(
            source,
            boundary=resolved_source,
            label=f"Skill target {target.name!r}",
        )
        shutil.copytree(source, destination)
        return destination / "SKILL.md"

    stock = resources.files("ciao.stock").joinpath("skills", target.name)
    try:
        is_stock_skill = stock.joinpath("SKILL.md").is_file()
    except OSError:
        is_stock_skill = False
    if not is_stock_skill:
        raise EvalTargetError(f"Skill target {target.name!r} was not found")
    with resources.as_file(stock) as source:
        shutil.copytree(source, destination)
    return destination / "SKILL.md"


def _copy_subagent_target(
    source_workspace: Path,
    isolated_root: Path,
    target: EvalTarget,
) -> tuple[Path, Path | None]:
    source_definition = source_workspace / "subagents" / f"{target.name}.md"
    destination_root = isolated_root / "subagents"
    destination_definition = destination_root / f"{target.name}.md"
    destination_root.mkdir(parents=True, exist_ok=True)
    if destination_definition.exists() or destination_definition.is_symlink():
        raise EvalTargetError(
            f"Isolated subagent destination already exists: {destination_definition}"
        )

    if source_definition.is_file():
        subagents_boundary = source_workspace / "subagents"
        _require_within(
            source_definition,
            subagents_boundary,
            label=f"Subagent definition {target.name!r}",
        )
        shutil.copy2(source_definition, destination_definition)
        source_companion = source_workspace / "subagents" / target.name
        destination_companion: Path | None = None
        if source_companion.is_dir():
            resolved_companion = _require_within(
                source_companion,
                subagents_boundary,
                label=f"Subagent companion {target.name!r}",
            )
            for asset_dir in ("scripts", "resources"):
                source_assets = source_companion / asset_dir
                if source_assets.is_dir():
                    _validate_materialized_tree(
                        source_assets,
                        boundary=resolved_companion,
                        label=(
                            f"Subagent companion {target.name!r}/{asset_dir}"
                        ),
                    )
                    destination_assets = (
                        destination_root / target.name / asset_dir
                    )
                    shutil.copytree(source_assets, destination_assets)
                    destination_companion = destination_root / target.name
        return destination_definition, destination_companion

    stock = resources.files("ciao.stock").joinpath("agents", f"{target.name}.md")
    try:
        is_stock_subagent = stock.is_file()
    except OSError:
        is_stock_subagent = False
    if not is_stock_subagent:
        raise EvalTargetError(f"Subagent target {target.name!r} was not found")
    with resources.as_file(stock) as source:
        shutil.copy2(source, destination_definition)
    return destination_definition, None


def _remove_unselected_entries(directory: Path, allowed: frozenset[str]) -> None:
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.name in allowed:
            continue
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry)


def _prune_provider_targets(isolated_root: Path, target: EvalTarget) -> None:
    """Remove stock projections added by sync that are outside the eval target."""
    for instruction_name in ("CLAUDE.md", "AGENTS.md"):
        (isolated_root / instruction_name).unlink(missing_ok=True)
    _remove_unselected_entries(isolated_root / "commands", frozenset())
    _remove_unselected_entries(isolated_root / ".claude" / "commands", frozenset())
    _remove_unselected_entries(isolated_root / ".opencode" / "commands", frozenset())
    if target.kind == "skill":
        _remove_unselected_entries(
            isolated_root / ".claude" / "skills",
            frozenset({target.name}),
        )
        _remove_unselected_entries(
            isolated_root / ".agents" / "skills",
            frozenset({target.name}),
        )
        _remove_unselected_entries(
            isolated_root / ".claude" / "agents",
            frozenset(),
        )
        _remove_unselected_entries(
            isolated_root / ".codex" / "agents",
            frozenset(),
        )
        _remove_unselected_entries(
            isolated_root / ".opencode" / "agents",
            frozenset(),
        )
        _install_codex_agents(isolated_root)
        return

    _remove_unselected_entries(
        isolated_root / ".claude" / "skills",
        frozenset(),
    )
    _remove_unselected_entries(
        isolated_root / ".agents" / "skills",
        frozenset({f"ciao-agent-{target.name}"}),
    )
    _remove_unselected_entries(
        isolated_root / ".claude" / "agents",
        frozenset({f"{target.name}.md"}),
    )
    _remove_unselected_entries(
        isolated_root / ".codex" / "agents",
        frozenset({f"{target.name}.toml"}),
    )
    _remove_unselected_entries(
        isolated_root / ".opencode" / "agents",
        frozenset({f"{target.name}.md"}),
    )
    # Recompile the managed config block after stock agent projections are
    # removed, otherwise Codex would retain registrations for absent files.
    _install_codex_agents(isolated_root)


def stage_eval_target(
    source_workspace: Path,
    isolated_root: Path,
    target: EvalTarget,
    *,
    sync: Any = sync_workspace_skills,
) -> StagedEvalTarget:
    """Copy exactly one target into a new workspace and build provider projections."""
    _validate_target_name(target)
    source_root = source_workspace.expanduser().resolve()
    destination_root = isolated_root.expanduser().resolve()
    _reject_workspace_overlap(source_root, destination_root)
    if destination_root.exists() and any(destination_root.iterdir()):
        raise EvalTargetError(
            f"Isolated workspace must be new or empty: {destination_root}"
        )
    destination_root.mkdir(parents=True, exist_ok=True)

    if target.kind == "skill":
        definition = _copy_skill_target(source_root, destination_root, target)
        companion = None
    else:
        definition, companion = _copy_subagent_target(
            source_root, destination_root, target
        )

    sync(destination_root, refresh_upstream=False)
    _prune_provider_targets(destination_root, target)
    return StagedEvalTarget(
        target=target,
        definition_path=definition,
        companion_path=companion,
    )


def _target_prompt(scenario: EvalScenario) -> str:
    if scenario.target.kind == "skill":
        prefix = f"Use the {scenario.target.name} skill for this task."
    else:
        prefix = (
            f"Delegate this task to the {scenario.target.name} subagent "
            "and wait for its final result."
        )
    return f"{prefix}\n\n{scenario.prompt}"


def run_eval_scenario(
    scenario: EvalScenario,
    *,
    defaults: EvalDefaults,
    source_workspace: Path,
    isolated_root: Path,
    overrides: EvalRunOverrides | None = None,
    workspace_name: str = "personal",
    turn_timeout_s: float = 300.0,
    startup_timeout_s: float = 30.0,
    sync: Any = sync_workspace_skills,
    server_factory: _ServerFactory = IsolatedChatServer,
    run_turn: _RunTurn = run_chat_turn,
) -> ChatObservation:
    """Stage and execute one scenario through a fresh isolated full chat."""
    # Resolution and staging intentionally happen before server construction.
    stage_eval_target(
        source_workspace,
        isolated_root,
        scenario.target,
        sync=sync,
    )
    routing = resolve_routing(defaults, scenario, overrides)
    spec = ChatRunSpec(
        scenario=scenario.name,
        prompt=_target_prompt(scenario),
        provider=routing.provider,
        model=routing.model,
        surface=routing.surface,
        turn_timeout_s=turn_timeout_s,
    )
    server = server_factory(
        root=isolated_root.resolve(),
        surface=routing.surface,
        provider=routing.provider,
        workspace_name=workspace_name,
        startup_timeout=startup_timeout_s,
        install_packaged_assets=False,
        require_subagent_synthesis=scenario.target.kind == "subagent",
        subagent_discovery_polls=3,
    )
    return run_turn(server, spec)


def normalize_tool_identifier(identifier: str) -> str:
    """Return a provider-neutral identifier used for exact tool matching.

    Identifiers are stripped and case-folded. MCP telemetry names of the form
    ``mcp__<server>__<tool>`` become ``<server>.<tool>``. No suffix or
    substring matching is performed after normalization.
    """
    normalized = identifier.strip().casefold()
    if normalized.startswith("mcp__"):
        parts = normalized.split("__", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            return f"{parts[1]}.{parts[2]}"
    return normalized


def _normalized_tools(used_tools: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for identifier in used_tools:
        tool = normalize_tool_identifier(identifier)
        if tool not in seen:
            seen.add(tool)
            normalized.append(tool)
    return tuple(normalized)


def evaluate_output(
    scenario: EvalScenario,
    output: str,
    used_tools: list[str] | tuple[str, ...] | None = None,
    duration_s: float = 0.0,
    error: str | None = None,
) -> EvalResult:
    """Evaluate one output without a model grader or fuzzy tool matching."""
    tools = _normalized_tools(used_tools or ())
    if error:
        return EvalResult(
            scenario_name=scenario.name,
            passed=False,
            duration_s=duration_s,
            assertion_results=(),
            normalized_tools=tools,
            output=output,
            error=error,
        )

    assertions = scenario.assertions
    assertion_groups = (
        assertions.output_contains,
        assertions.output_not_contains,
        assertions.output_regex,
        assertions.output_not_regex,
        assertions.required_tools,
        assertions.forbidden_tools,
    )
    if not any(assertion_groups):
        return EvalResult(
            scenario_name=scenario.name,
            passed=False,
            duration_s=duration_s,
            assertion_results=(),
            normalized_tools=tools,
            output=output,
            error="Scenario has no effective assertions",
        )

    for field_name, patterns in (
        ("output_regex", assertions.output_regex),
        ("output_not_regex", assertions.output_not_regex),
    ):
        for index, pattern in enumerate(patterns):
            try:
                re.compile(pattern)
            except re.error as exc:
                return EvalResult(
                    scenario_name=scenario.name,
                    passed=False,
                    duration_s=duration_s,
                    assertion_results=(),
                    normalized_tools=tools,
                    output=output,
                    error=(
                        f"Invalid regex at assertions.{field_name}[{index}]: {exc}"
                    ),
                )

    results: list[EvalAssertionResult] = []
    for expected in assertions.output_contains:
        results.append(
            EvalAssertionResult(
                kind="output_contains",
                expected=expected,
                passed=expected in output,
            )
        )
    for expected in assertions.output_not_contains:
        results.append(
            EvalAssertionResult(
                kind="output_not_contains",
                expected=expected,
                passed=expected not in output,
            )
        )
    for expected in assertions.output_regex:
        results.append(
            EvalAssertionResult(
                kind="output_regex",
                expected=expected,
                passed=re.search(expected, output) is not None,
            )
        )
    for expected in assertions.output_not_regex:
        results.append(
            EvalAssertionResult(
                kind="output_not_regex",
                expected=expected,
                passed=re.search(expected, output) is None,
            )
        )

    tool_set = set(tools)
    for expected in assertions.required_tools:
        normalized_expected = normalize_tool_identifier(expected)
        results.append(
            EvalAssertionResult(
                kind="required_tools",
                expected=normalized_expected,
                passed=normalized_expected in tool_set,
            )
        )
    for expected in assertions.forbidden_tools:
        normalized_expected = normalize_tool_identifier(expected)
        results.append(
            EvalAssertionResult(
                kind="forbidden_tools",
                expected=normalized_expected,
                passed=normalized_expected not in tool_set,
            )
        )

    assertion_results = tuple(results)
    return EvalResult(
        scenario_name=scenario.name,
        passed=all(result.passed for result in assertion_results),
        duration_s=duration_s,
        assertion_results=assertion_results,
        normalized_tools=tools,
        output=output,
    )


def _pending_record(
    scenario: EvalScenario,
    defaults: EvalDefaults,
    overrides: EvalRunOverrides | None,
) -> EvalScenarioRecord:
    routing = resolve_routing(defaults, scenario, overrides)
    return EvalScenarioRecord(
        name=scenario.name,
        description=scenario.description,
        target=scenario.target,
        status="pending",
        provider=routing.provider,
        model=routing.model,
        surface=routing.surface,
    )


def _record_from_observation(
    pending: EvalScenarioRecord,
    scenario: EvalScenario,
    observation: ChatObservation,
) -> EvalScenarioRecord:
    execution_error = observation.error or None
    tools = (*observation.provider_tools, *observation.mcp_tools)
    result = evaluate_output(
        scenario,
        observation.final_text,
        tools,
        duration_s=observation.elapsed_ms / 1000,
        error=execution_error,
    )
    return EvalScenarioRecord(
        name=pending.name,
        description=pending.description,
        target=pending.target,
        status="passed" if result.passed else "failed",
        provider=pending.provider,
        model=pending.model,
        surface=pending.surface,
        assertions=result.assertion_results,
        output=result.output,
        error=result.error,
        selected_model=observation.selected_model,
        effective_model=observation.effective_model,
        normalized_tools=result.normalized_tools,
        raw_usage=dict(observation.usage),
        token_total=observation.tokens,
        elapsed_ms=observation.elapsed_ms,
        provider_duration_ms=observation.provider_duration_ms,
    )


def _failed_record(
    pending: EvalScenarioRecord,
    error: BaseException,
    *,
    status: EvalStatus = "failed",
) -> EvalScenarioRecord:
    message = str(error)
    if not message and isinstance(error, SystemExit):
        message = f"SystemExit({error.code})"
    if not message:
        message = type(error).__name__
    return EvalScenarioRecord(
        name=pending.name,
        description=pending.description,
        target=pending.target,
        status=status,
        provider=pending.provider,
        model=pending.model,
        surface=pending.surface,
        error=message,
    )


def _record_data(record: EvalScenarioRecord) -> dict[str, object]:
    return {
        "name": record.name,
        "description": record.description,
        "target": {
            "kind": record.target.kind,
            "name": record.target.name,
        },
        "status": record.status,
        "provider": record.provider,
        "model": record.model,
        "surface": record.surface,
        "assertions": [
            {
                "kind": assertion.kind,
                "expected": assertion.expected,
                "passed": assertion.passed,
            }
            for assertion in record.assertions
        ],
        "output": record.output,
        "error": record.error,
        "selected_model": record.selected_model,
        "effective_model": record.effective_model,
        "normalized_tools": list(record.normalized_tools),
        "raw_usage": record.raw_usage,
        "token_total": record.token_total,
        "elapsed_ms": record.elapsed_ms,
        "provider_duration_ms": record.provider_duration_ms,
    }


def _render_eval_report(suite: EvalSuite, records: list[EvalScenarioRecord]) -> str:
    suite_name = _markdown_inline(suite.name)
    completed = [record for record in records if record.status != "pending"]
    passed = sum(record.status == "passed" for record in completed)
    lines = [
        f"# Eval report: {suite_name}",
        "",
        f"- Schema version: {suite.schema_version}",
        f"- Selected scenarios: {len(records)}",
        f"- Completed: {len(completed)}",
        f"- Passed: {passed}",
        "",
        "## Scenarios",
        "",
        "| Scenario | Status | Provider | Model | Surface |",
        "| --- | --- | --- | --- | --- |",
    ]
    for record in records:
        lines.append(
            f"| {_markdown_inline(record.name)} | {record.status.upper()} | "
            f"{_markdown_inline(record.provider)} | "
            f"{_markdown_inline(record.model)} | "
            f"{_markdown_inline(record.surface)} |"
        )
        if record.error:
            lines.extend(
                [
                    "",
                    f"- **{_markdown_inline(record.name)} error:** "
                    f"{_markdown_inline(record.error)}",
                ]
            )
    lines.append("")
    return "\n".join(lines)


_MARKDOWN_ENTITIES = {
    "&": "&amp;",
    "\\": "&#92;",
    "`": "&#96;",
    "*": "&#42;",
    "_": "&#95;",
    "{": "&#123;",
    "}": "&#125;",
    "[": "&#91;",
    "]": "&#93;",
    "<": "&lt;",
    ">": "&gt;",
    "#": "&#35;",
    "+": "&#43;",
    "-": "&#45;",
    "!": "&#33;",
    "|": "&#124;",
}


def _markdown_inline(value: object) -> str:
    """Render untrusted schema/runtime text as one inert Markdown line."""
    normalized = str(value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\n", " / ")
    return "".join(_MARKDOWN_ENTITIES.get(char, char) for char in normalized)


def _replace_atomic_target(temporary: Path, target: Path) -> None:
    temporary.replace(target)


def _write_atomic_temporary(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(raw_temporary)
    try:
        _write_atomic_temporary(temporary, text)
        _replace_atomic_target(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_eval_reports(
    output: Path,
    suite_path: Path,
    suite: EvalSuite,
    records: list[EvalScenarioRecord],
) -> None:
    """Atomically replace the machine-readable and Markdown suite reports."""
    payload = {
        "suite": {
            "name": suite.name,
            "schema_version": suite.schema_version,
            "source": str(suite_path.resolve()),
        },
        "scenarios": [_record_data(record) for record in records],
    }
    _atomic_write(
        output / "results.json",
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(output / "REPORT.md", _render_eval_report(suite, records))


ScenarioRunner: TypeAlias = Callable[..., ChatObservation]
ReportWriter: TypeAlias = Callable[
    [Path, Path, EvalSuite, list[EvalScenarioRecord]],
    None,
]


def _publish_eval_report(
    writer: ReportWriter,
    output: Path,
    suite_path: Path,
    suite: EvalSuite,
    records: list[EvalScenarioRecord],
) -> None:
    try:
        writer(output, suite_path, suite, records)
    except Exception as exc:
        raise EvalReportError(f"Could not write eval report: {exc}") from exc


def run_eval_suite(
    suite_path: Path,
    source_workspace: Path,
    output: Path,
    *,
    name_filter: str | None = None,
    overrides: EvalRunOverrides | None = None,
    workspace_name: str = "personal",
    turn_timeout_s: float = 300.0,
    startup_timeout_s: float = 30.0,
    scenario_runner: ScenarioRunner | None = None,
    report_writer: ReportWriter = write_eval_reports,
) -> EvalSuiteRun:
    """Execute selected scenarios sequentially in disposable workspaces.

    Filtering is a case-sensitive substring match. Selected scenarios retain
    their declaration order from the suite.
    """
    suite_source = suite_path.expanduser().resolve()
    suite = load_eval_suite(suite_source)
    workspace = source_workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise EvalTargetError(f"Source workspace is not a directory: {workspace}")

    selected = [
        scenario
        for scenario in suite.scenarios
        if name_filter is None or name_filter in scenario.name
    ]
    if not selected:
        raise EvalSchemaError("No scenarios selected")

    cli = overrides or EvalRunOverrides()
    records = [
        _pending_record(scenario, suite.defaults, cli)
        for scenario in selected
    ]
    runner = scenario_runner or run_eval_scenario
    output_path = output.expanduser().resolve()

    for index, scenario in enumerate(selected):
        pending = records[index]
        try:
            with tempfile.TemporaryDirectory(prefix="ciao-eval-") as temporary:
                observation = runner(
                    scenario,
                    defaults=suite.defaults,
                    source_workspace=workspace,
                    isolated_root=Path(temporary),
                    overrides=cli,
                    workspace_name=workspace_name,
                    turn_timeout_s=turn_timeout_s,
                    startup_timeout_s=startup_timeout_s,
                )
            records[index] = _record_from_observation(
                pending,
                scenario,
                observation,
            )
        except Exception as exc:
            records[index] = _failed_record(pending, exc)
        except BaseException as exc:
            records[index] = _failed_record(
                pending,
                exc,
                status="interrupted",
            )
            try:
                report_writer(output_path, suite_source, suite, records)
            except BaseException:
                pass
            raise
        _publish_eval_report(
            report_writer,
            output_path,
            suite_source,
            suite,
            records,
        )

    # Flush once more during orderly shutdown so the final report reflects
    # the complete selected scenario set.
    _publish_eval_report(
        report_writer,
        output_path,
        suite_source,
        suite,
        records,
    )

    exit_code: Literal[0, 1] = (
        0 if all(record.status == "passed" for record in records) else 1
    )
    return EvalSuiteRun(suite=suite, records=tuple(records), exit_code=exit_code)


def scaffold_eval(workspace: Path, name: str) -> Path:
    """Create one schema-version-1 suite without overwriting existing data."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(
            f"Invalid eval name {name!r}: expected letters, numbers, "
            "hyphens, or underscores"
        )
    workspace_root = workspace.expanduser().resolve()
    if not workspace_root.is_dir():
        raise ValueError(f"Workspace is not a directory: {workspace_root}")
    evals_root = workspace_root / "evals"
    evals_root.mkdir(parents=True, exist_ok=True)
    canonical_evals_root = evals_root.resolve(strict=True)
    if not _is_within(canonical_evals_root, workspace_root):
        raise ValueError(
            f"Eval scaffold directory escapes workspace: {evals_root} -> "
            f"{canonical_evals_root}"
        )
    target = canonical_evals_root / f"{name}.json"
    content = {
        "schema_version": 1,
        "name": name,
        "defaults": {
            "provider": "claude",
            "model": "sonnet",
            "surface": "mcp",
        },
        "scenarios": [
            {
                "name": f"{name}-smoke",
                "description": f"Smoke evaluation for {name}.",
                "target": {"kind": "skill", "name": name},
                "prompt": "Complete the requested task.",
                "assertions": {
                    "output_contains": [],
                    "output_not_contains": ["Traceback"],
                    "output_regex": [],
                    "output_not_regex": [],
                    "required_tools": [],
                    "forbidden_tools": [],
                },
            }
        ],
    }
    serialized = json.dumps(content, indent=2) + "\n"
    _publish_scaffold_exclusive(target, serialized)
    return target


def _write_scaffold_temporary(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _publish_scaffold_exclusive(target: Path, text: str) -> None:
    """Publish a complete scaffold atomically without replacing an existing path."""
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(raw_temporary)
    try:
        _write_scaffold_temporary(temporary, text)
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
