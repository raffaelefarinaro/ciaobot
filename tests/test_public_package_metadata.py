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

    assert "PWA_AUTH_TOKEN=" in env_example
    for marker in PRIVATE_ENV_MARKERS | WORKSPACE_SPECIFIC_ENV_MARKERS:
        assert marker not in env_example


def test_push_subject_is_accepted_by_apple() -> None:
    from ciao.main import PUSH_SUBJECT

    # Apple's push service answers 403 BadJwtToken for a localhost subject.
    assert PUSH_SUBJECT.startswith("mailto:")
    assert "localhost" not in PUSH_SUBJECT
