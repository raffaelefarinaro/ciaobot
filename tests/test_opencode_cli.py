"""The OpenCode CLI helpers that outlive the HTTP driver: the version floor and the binary.

The chat runtime is `opencode acp` (see test_acp_agents.py). These guards cover what
Settings, the releaser, the conversation importer and the ACP start gate all share.
"""

from __future__ import annotations

import pytest

from ciao import opencode_cli


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("opencode v2.3.2", True),
        ("2.3.2", True),
        ("2.10.0", True),
        ("2.3.1", False),
        ("2.0.16", False),
        ("1.14.0", False),
        ("3.0.0", False),
        ("unknown", False),
        ("", False),
    ],
)
def test_the_floor_is_2_3_2_within_major_2(version: str, supported: bool) -> None:
    # Mutation check: lowering the floor back to (2, 0, 16) makes "2.3.1" and "2.0.16" pass.
    assert opencode_cli.opencode_start_supported(version) is supported
    assert (opencode_cli.cli_version_error({"version": version}) is None) is supported


def test_a_payload_that_is_not_a_mapping_is_refused_with_the_floor_message() -> None:
    error = opencode_cli.cli_version_error("2.3.2")
    assert error == opencode_cli.OPENCODE_V2_REQUIRED


def test_a_refused_version_names_the_installed_version() -> None:
    error = opencode_cli.cli_version_error({"version": "2.0.16"})
    assert error is not None
    assert error.endswith("Installed server version: 2.0.16.")


def test_version_number_reads_the_dotted_prefix_and_ignores_suffixes() -> None:
    assert opencode_cli.version_number("opencode v2.3.2") == (2, 3, 2)
    assert opencode_cli.version_number("2.0.16-beta.3") == (2, 0, 16)
    assert opencode_cli.version_number("unknown") is None


def test_default_model_reads_the_operator_override_and_defaults_to_empty() -> None:
    from types import SimpleNamespace

    assert opencode_cli.opencode_default_model(SimpleNamespace()) == ""
    config = SimpleNamespace(opencode=opencode_cli.OpencodeSettings(default_model="x/y"))
    assert opencode_cli.opencode_default_model(config) == "x/y"


def test_the_binary_override_must_point_at_a_file(tmp_path, monkeypatch) -> None:
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("CIAO_OPENCODE_BIN", str(binary))
    assert opencode_cli.resolve_opencode_binary() == str(binary.resolve())
    monkeypatch.setenv("CIAO_OPENCODE_BIN", str(tmp_path / "missing"))
    assert opencode_cli.resolve_opencode_binary() is None
