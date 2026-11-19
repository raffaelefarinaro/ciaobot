"""Claude harness trim catalog: defaults, sparse overrides, and the denies/settings they produce."""

from __future__ import annotations

import pytest

from ciao.claude_trim import (
    CONNECTORS_TRIM_KEY,
    TRIM_OPTIONS,
    catalog_payload,
    claude_settings_layer,
    coerce_claude_trim,
    effective_trim,
    load_claude_trim,
    option,
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
    # Connectors stay on by default, and trimming them is an env var, not a CLI key.
    assert "claude_ai_connectors" not in layer
    trimmed = claude_settings_layer({CONNECTORS_TRIM_KEY: True})
    assert "claude_ai_connectors" not in trimmed
    assert effective_trim({})[CONNECTORS_TRIM_KEY] is False
    assert effective_trim({CONNECTORS_TRIM_KEY: True})[CONNECTORS_TRIM_KEY] is True


def test_connectors_option_is_an_editable_setting() -> None:
    entry = option(CONNECTORS_TRIM_KEY)
    assert entry.kind == "setting"
    assert entry.name == "claude_ai_connectors"
    assert entry.label == "claude.ai connectors"
    assert entry.group == "Connectors"
    assert entry.default_trimmed is False
    assert coerce_claude_trim({CONNECTORS_TRIM_KEY: True}) == {CONNECTORS_TRIM_KEY: True}
    assert coerce_claude_trim({CONNECTORS_TRIM_KEY: False}) == {}
    assert any(row["key"] == CONNECTORS_TRIM_KEY for row in catalog_payload())


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


def test_coerce_rejects_unknown_and_non_bool() -> None:
    for raw in (
        {"tool:Nope": True},
        {"tool:NotebookEdit": "no"},
        ["x"],
    ):
        with pytest.raises(ValueError):
            coerce_claude_trim(raw)


SCHEDULING_KEYS = [o.key for o in TRIM_OPTIONS if o.group == "scheduling"]


def test_scheduling_options_are_trimmed_by_default_and_not_special() -> None:
    assert SCHEDULING_KEYS
    assert not hasattr(TRIM_OPTIONS[0], "locked")
    assert all(option(key).default_trimmed for key in SCHEDULING_KEYS)
    # A map that never mentions them keeps them trimmed.
    assert all(effective_trim({})[key] for key in SCHEDULING_KEYS)
    assert all(effective_trim({"tool:NotebookEdit": False})[key] for key in SCHEDULING_KEYS)


def test_untrimming_a_scheduling_option_is_accepted_and_sparse() -> None:
    assert coerce_claude_trim({"tool:CronCreate": False}) == {"tool:CronCreate": False}
    assert coerce_claude_trim({"tool:CronCreate": True}) == {}
    assert load_claude_trim({"skill:loop": False}) == {"skill:loop": False}


def test_scheduling_option_reaches_denies_only_while_trimmed() -> None:
    assert "CronCreate" in trim_tool_denies({})
    assert "CronCreate" not in trim_tool_denies({"tool:CronCreate": False})
    assert "CronDelete" in trim_tool_denies({"tool:CronCreate": False})
    assert "Skill(loop)" in trim_tool_denies({})
    assert "Skill(loop)" not in trim_tool_denies({"skill:loop": False})
    assert claude_settings_layer({})["skillOverrides"]["loop"] == "off"
    assert "loop" not in claude_settings_layer({"skill:loop": False})["skillOverrides"]


def test_catalog_payload_has_no_locked_field() -> None:
    assert all("locked" not in entry for entry in catalog_payload())


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
    ) == {"tool:CronCreate": False, "tool:NotebookEdit": False}
    assert load_claude_trim("junk") == {}
