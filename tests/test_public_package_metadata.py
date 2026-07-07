from __future__ import annotations

import tomllib
from pathlib import Path


PRIVATE_ENV_MARKERS = {
    "private-person",
    "private.example.com",
    "PrivateCo",
}

WORKSPACE_SPECIFIC_ENV_MARKERS = {
    "AIRTABLE_",
    "ZENDESK_",
    "BIGQUERY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GWS_PERSONAL",
    "GWS_WORK",
    "N8N_",
}


def test_pyproject_declares_public_license_and_python_floor() -> None:
    repo = Path(__file__).parents[1]
    data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))

    project = data["project"]
    assert project["requires-python"] == ">=3.12"
    assert project["license"] == "Apache-2.0"
    assert data["project"]["scripts"]["ciao"] == "ciao.cli:main"


def test_public_metadata_files_exist() -> None:
    repo = Path(__file__).parents[1]

    assert (repo / "LICENSE").is_file()
    assert "Apache License" in (repo / "LICENSE").read_text(encoding="utf-8")
    assert (repo / "SECURITY.md").is_file()


def test_env_example_is_generic_public_app_config() -> None:
    env_example = (Path(__file__).parents[1] / ".env.example").read_text(encoding="utf-8")

    assert "CIAO_PUSH_CONTACT=mailto:you@example.com" in env_example
    for marker in PRIVATE_ENV_MARKERS | WORKSPACE_SPECIFIC_ENV_MARKERS:
        assert marker not in env_example


def test_push_contact_is_optional_without_private_default() -> None:
    from types import SimpleNamespace

    from ciao.main import _push_subject_for_config, _push_subject_from_env

    assert _push_subject_from_env({"CIAO_PUSH_CONTACT": "mailto:admin@example.com"}) == "mailto:admin@example.com"
    assert (
        _push_subject_for_config(SimpleNamespace(bootstrap_mode=True))
        == "mailto:bootstrap@localhost"
    )

    # Missing contact yields an empty subject (Web Push disabled), never a
    # private fallback and never an error.
    assert _push_subject_from_env({}) == ""
    assert _push_subject_from_env({"CIAO_PUSH_CONTACT": "  "}) == ""
