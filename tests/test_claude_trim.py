"""Claude harness trim catalog: defaults, sparse overrides, and the denies/settings they produce."""

from __future__ import annotations

import pytest

from ciao.claude_trim import (
    TRIM_OPTIONS,
    claude_settings_layer,
    coerce_claude_trim,
    load_claude_trim,
    trim_tool_denies,
)

# The nine bundled skills Ciaobot hid before the catalog existed.
_OLD_HIDDEN_SKILLS = (
    "schedule",
    "loop",
    "design-sync",
    "update-config",
    "fewer-permission-prompts",
    "doctor",
    "run",
    "run-skill-generator",
    "dataviz",
)


def test_catalog_keys_unique_and_well_formed() -> None:
    keys = [option.key for option in TRIM_OPTIONS]
    assert len(keys) == len(set(keys))
    assert len({option.label for option in TRIM_OPTIONS}) == len(TRIM_OPTIONS)
    for option in TRIM_OPTIONS:
        assert option.key == f"{option.kind}:{option.name}"
        if option.locked:
            assert option.default_trimmed is True
        if option.covers:
            assert option.kind == "setting"


def test_default_denies_match_previous_harness_set() -> None:
    assert trim_tool_denies({}) == [
        "CronCreate",
        "CronDelete",
        "CronList",
        "ScheduleWakeup",
        "RemoteTrigger",
        "Skill(schedule)",
        "Skill(loop)",
        "EnterPlanMode",
        "ExitPlanMode",
        "PushNotification",
        "DesignSync",
        "Skill(design-sync)",
        "NotebookEdit",
        "Skill(update-config)",
        "Skill(fewer-permission-prompts)",
        "Skill(doctor)",
        "Skill(run)",
        "Skill(run-skill-generator)",
        "Skill(dataviz)",
    ]


def test_default_settings_layer() -> None:
    layer = claude_settings_layer({})
    assert layer["skillOverrides"] == {name: "off" for name in _OLD_HIDDEN_SKILLS}
    assert layer["disableArtifact"] is True
    assert "disableWorkflows" not in layer


def test_override_untrims_and_trims() -> None:
    overrides = {
        "tool:NotebookEdit": False,
        "tool:ReportFindings": True,
        "setting:disableWorkflows": True,
    }
    denies = trim_tool_denies(overrides)
    assert "NotebookEdit" not in denies
    assert "ReportFindings" in denies
    assert claude_settings_layer(overrides)["disableWorkflows"] is True


def test_coerce_rejects_locked_unknown_and_non_bool() -> None:
    for raw in (
        {"tool:CronCreate": False},
        {"tool:Nope": True},
        {"tool:NotebookEdit": "no"},
        ["x"],
    ):
        with pytest.raises(ValueError):
            coerce_claude_trim(raw)


def test_coerce_is_sparse() -> None:
    assert coerce_claude_trim(
        {"tool:NotebookEdit": True, "tool:ReportFindings": True}
    ) == {"tool:ReportFindings": True}


def test_load_is_lenient() -> None:
    assert load_claude_trim(
        {
            "tool:Nope": True,
            "tool:CronCreate": False,
            "tool:NotebookEdit": False,
        }
    ) == {"tool:NotebookEdit": False}
    assert load_claude_trim("junk") == {}
