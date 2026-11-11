"""Catalog of Claude Code harness features Ciaobot trims from the model's context.

The Claude CLI ships built-in tools, bundled skills and feature switches that
Ciaobot does not want in a chat. Two reasons:

* The PWA owns scheduling, notifications and plan approval. Some CLI surfaces
  (cloud routines, harness-local loops, plan mode, push notifications) would
  bypass those, so they are denied for every workspace.
* Every tool definition and skill listing costs context on every turn, so
  features the workspace never uses are hidden to shrink each request.

Each option has a default. A workspace stores only the options it changed, as a
sparse ``claude_trim`` map of option key -> trimmed. Scheduling options are
locked: Ciaobot's own surfaces replace them, so they are always trimmed.

Two levers apply to bundled skills. ``skillOverrides`` in the CLI settings layer
hides a skill from the model's listing and slash-command picker. A
``Skill(<name>)`` deny rule blocks execution if one is re-enabled downstream;
deny alone is not enough, because a denied skill still appears in the listing
and the model burns a turn on an approval card for a tool it cannot run.
Plugin skills are not affected by ``skillOverrides``: uninstall the plugin or
shadow it with a same-named workspace skill.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

TrimKind = Literal["tool", "skill", "setting"]


@dataclass(frozen=True)
class TrimOption:
    key: str  # "<kind>:<name>", e.g. "tool:NotebookEdit"
    kind: TrimKind
    name: str  # tool name, bundled skill name, or CLI settings key
    label: str  # short UI label
    reason: str  # one sentence: what it is and why trim it
    group: str
    default_trimmed: bool
    locked: bool = False  # always trimmed; cannot be un-trimmed
    covers: tuple[str, ...] = ()  # setting options only: tool/skill names the setting removes


def _tool(
    name: str,
    label: str,
    reason: str,
    group: str,
    default_trimmed: bool = True,
    locked: bool = False,
) -> TrimOption:
    return TrimOption(f"tool:{name}", "tool", name, label, reason, group, default_trimmed, locked)


def _skill(
    name: str,
    label: str,
    reason: str,
    group: str,
    default_trimmed: bool = True,
    locked: bool = False,
) -> TrimOption:
    return TrimOption(f"skill:{name}", "skill", name, label, reason, group, default_trimmed, locked)


def _setting(
    name: str,
    label: str,
    reason: str,
    group: str,
    default_trimmed: bool,
    covers: tuple[str, ...],
) -> TrimOption:
    return TrimOption(
        f"setting:{name}", "setting", name, label, reason, group, default_trimmed, False, covers
    )


TRIM_OPTIONS: tuple[TrimOption, ...] = (
    _tool(
        "CronCreate",
        "Create cloud routines",
        "Creates Claude cloud routines that bypass Ciaobot's schedules.",
        "scheduling",
        locked=True,
    ),
    _tool(
        "CronDelete",
        "Delete cloud routines",
        "Deletes Claude cloud routines; Ciaobot's schedules replace them.",
        "scheduling",
        locked=True,
    ),
    _tool(
        "CronList",
        "List cloud routines",
        "Lists Claude cloud routines; Ciaobot's schedules replace them.",
        "scheduling",
        locked=True,
    ),
    _tool(
        "ScheduleWakeup",
        "Interval wakeups",
        "Harness-local interval loop that bypasses Ciaobot's schedules.",
        "scheduling",
        locked=True,
    ),
    _tool(
        "RemoteTrigger",
        "Remote triggers",
        "Fires claude.ai routines outside Ciaobot's schedules.",
        "scheduling",
        locked=True,
    ),
    _skill(
        "schedule",
        "/schedule skill",
        "Creates cloud routines; Ciaobot's schedules replace it.",
        "scheduling",
        locked=True,
    ),
    _skill(
        "loop",
        "/loop skill",
        "Harness-local interval loop; Ciaobot's schedules replace it.",
        "scheduling",
        locked=True,
    ),
    _tool(
        "EnterPlanMode",
        "Enter plan mode",
        "Built-in plan mode; the PWA owns plan approval.",
        "plan",
    ),
    _tool(
        "ExitPlanMode",
        "Exit plan mode",
        "Built-in plan approval; the PWA owns plan approval.",
        "plan",
    ),
    _tool(
        "PushNotification",
        "Push notifications",
        "Desktop and phone pushes; the PWA owns notifications.",
        "notifications",
    ),
    _tool(
        "DesignSync",
        "Design sync",
        "Syncs a design system; Ciaobot's design surfaces replace it.",
        "design",
    ),
    _skill(
        "design-sync",
        "/design-sync skill",
        "Design-system sync; its DesignSync tool is trimmed with it.",
        "design",
    ),
    _tool(
        "NotebookEdit",
        "Notebook editing",
        "Edits Jupyter notebooks; rarely needed here and its definition costs context every turn.",
        "features",
    ),
    _tool(
        "ReportFindings",
        "Code review findings",
        "Structured output for /code-review; hide it if you never run reviews.",
        "review",
        default_trimmed=False,
    ),
    _skill(
        "update-config",
        "/update-config skill",
        "Writes settings.json; the PWA Settings screen is the surface.",
        "skills",
    ),
    _skill(
        "fewer-permission-prompts",
        "/fewer-permission-prompts skill",
        "Edits permissions from local transcripts; Ciaobot manages approvals.",
        "skills",
    ),
    _skill(
        "doctor",
        "/doctor skill",
        "Duplicates the PWA diagnostics.",
        "skills",
    ),
    _skill(
        "run",
        "/run skill",
        "Bundled run stub; Ciaobot uses per-project run skills.",
        "skills",
    ),
    _skill(
        "run-skill-generator",
        "/run-skill-generator skill",
        "Creates per-project run skills that Ciaobot does not use.",
        "skills",
    ),
    _skill(
        "dataviz",
        "/dataviz skill",
        "Bundled Artifacts guidance; Ciaobot has its own dataviz skill.",
        "skills",
    ),
    _skill(
        "design",
        "/design skill",
        "Design guidance for UI work; hide it if you never design interfaces here.",
        "skills",
        default_trimmed=False,
    ),
    _skill(
        "verify",
        "/verify skill",
        "Verification workflow; hide it if you never verify changes here.",
        "skills",
        default_trimmed=False,
    ),
    _skill(
        "debug",
        "/debug skill",
        "Debugging workflow; hide it if you never debug here.",
        "skills",
        default_trimmed=False,
    ),
    _skill(
        "code-review",
        "/code-review skill",
        "Code review workflow; hide it if you never review code here.",
        "skills",
        default_trimmed=False,
    ),
    _skill(
        "simplify",
        "/simplify skill",
        "Cleanup and simplification workflow; hide it if you never refactor here.",
        "skills",
        default_trimmed=False,
    ),
    _skill(
        "batch",
        "/batch skill",
        "Applies one change across many files; hide it if you never bulk-edit here.",
        "skills",
        default_trimmed=False,
    ),
    _skill(
        "claude-api",
        "/claude-api skill",
        "Anthropic API and SDK reference; hide it if you never build on Claude here.",
        "skills",
        default_trimmed=False,
    ),
    _skill(
        "plugin-authoring",
        "/plugin-authoring skill",
        "Claude Code plugin authoring guide; hide it if you never write plugins here.",
        "skills",
        default_trimmed=False,
    ),
    _setting(
        "disableArtifact",
        "Artifacts",
        "Claude Artifacts; Ciaobot renders HTML artifacts in its own panel.",
        "features",
        default_trimmed=True,
        covers=("Artifact", "ArtifactCheck", "ArtifactComments", "ArtifactData"),
    ),
    _setting(
        "disableWorkflows",
        "Workflows",
        "Multi-agent workflows plus the deep-research and workflow-authoring skills.",
        "features",
        default_trimmed=False,
        covers=("Workflow", "deep-research", "workflow-authoring"),
    ),
)

_BY_KEY: dict[str, TrimOption] = {option.key: option for option in TRIM_OPTIONS}

# Built-in CLI tools Ciaobot keeps and does not offer as toggles. The first group
# is core file, shell and agent tools the PWA relies on; AskUserQuestion renders
# as a PWA question card, and Monitor and TaskStop cover background work.
KEPT_CLAUDE_TOOLS: frozenset[str] = frozenset(
    {
        "Task",
        "Agent",
        "Bash",
        "Read",
        "Write",
        "Edit",
        "MultiEdit",
        "Glob",
        "Grep",
        "LS",
        "TodoWrite",
        "WebFetch",
        "WebSearch",
        "Skill",
        "ToolSearch",
        "Monitor",
        "TaskStop",
        "TaskOutput",
        "SendMessage",
        "ListAgents",
        "EnterWorktree",
        "ExitWorktree",
        "AskUserQuestion",
        "BashOutput",
        "KillShell",
        "SlashCommand",
    }
)


def option(key: str) -> TrimOption:
    """The catalog entry for ``key``; KeyError if unknown."""
    return _BY_KEY[key]


def coerce_claude_trim(raw: object) -> dict[str, bool]:
    """Strictly validate a ``claude_trim`` object and return it sparse, in catalog order.

    Entries equal to the catalog default are dropped. Raises ValueError on a bad
    shape, an unknown key, a non-bool value, or an attempt to un-trim a locked option.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("claude_trim must be an object of option key -> true/false")
    picked: dict[str, bool] = {}
    for key, value in raw.items():
        entry = _BY_KEY.get(key)
        if entry is None:
            raise ValueError(f"unknown claude_trim option: {key}")
        if not isinstance(value, bool):
            raise ValueError(f"claude_trim {key} must be true or false")
        if entry.locked and not value:
            raise ValueError(f"{key} is always trimmed: Ciaobot's own schedules replace it")
        if value != entry.default_trimmed:
            picked[entry.key] = value
    return {o.key: picked[o.key] for o in TRIM_OPTIONS if o.key in picked}


def load_claude_trim(raw: object) -> dict[str, bool]:
    """Leniently read a stored ``claude_trim`` map: invalid entries are skipped, never raises."""
    if not isinstance(raw, dict):
        return {}
    valid: dict[str, bool] = {}
    for key, value in raw.items():
        try:
            valid.update(coerce_claude_trim({key: value}))
        except ValueError:
            continue
    return {o.key: valid[o.key] for o in TRIM_OPTIONS if o.key in valid}


def effective_trim(overrides: Mapping[str, bool]) -> dict[str, bool]:
    """Every catalog key mapped to trimmed or not. Locked options are always trimmed."""
    return {
        o.key: True if o.locked else overrides.get(o.key, o.default_trimmed)
        for o in TRIM_OPTIONS
    }


def trim_tool_denies(overrides: Mapping[str, bool]) -> list[str]:
    """Denied tool names: tools by name, bundled skills as ``Skill(<name>)``, catalog order."""
    effective = effective_trim(overrides)
    denies: list[str] = []
    for o in TRIM_OPTIONS:
        if not effective[o.key]:
            continue
        if o.kind == "tool":
            denies.append(o.name)
        elif o.kind == "skill":
            denies.append(f"Skill({o.name})")
    return denies


def claude_settings_layer(overrides: Mapping[str, bool]) -> dict[str, Any]:
    """The inline CLI settings layer: ``skillOverrides`` plus ``disable*`` keys set to True.

    Untrimmed settings are omitted rather than written as False.
    """
    effective = effective_trim(overrides)
    layer: dict[str, Any] = {"skillOverrides": {}}
    for o in TRIM_OPTIONS:
        if not effective[o.key]:
            continue
        if o.kind == "skill":
            layer["skillOverrides"][o.name] = "off"
        elif o.kind == "setting":
            layer[o.name] = True
    return layer


def catalog_payload() -> list[dict[str, Any]]:
    """The catalog as JSON-ready dicts, in catalog order, for the Settings trim card."""
    return [
        {
            "key": o.key,
            "kind": o.kind,
            "name": o.name,
            "label": o.label,
            "reason": o.reason,
            "group": o.group,
            "default_trimmed": o.default_trimmed,
            "locked": o.locked,
        }
        for o in TRIM_OPTIONS
    ]
