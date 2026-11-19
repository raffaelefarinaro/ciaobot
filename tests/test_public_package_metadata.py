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


def test_runtime_security_pins_are_published_and_locked() -> None:
    # A wheel install resolves the published Requires-Dist, not uv.lock, so the
    # patched PyJWT/urllib3 floors have to be declared as exact project
    # dependencies AND present in the lock (#953). The two must agree.
    repo = Path(__file__).parents[1]
    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = project["dependencies"]

    def matches(prefix: str, specifier: str) -> bool:
        return any(
            dep.split("==", 1)[0].lower() == prefix.lower() and dep.endswith(f"=={specifier}")
            for dep in declared
        )

    assert matches("PyJWT[crypto]", "2.15.1"), declared
    assert matches("urllib3", "2.8.0"), declared

    lock = tomllib.loads((repo / "uv.lock").read_text(encoding="utf-8"))
    versions = {pkg["name"].lower(): pkg["version"] for pkg in lock["package"]}
    assert versions["pyjwt"] == "2.15.1"
    assert versions["urllib3"] == "2.8.0"


def test_third_party_notices_carry_the_sdk_license_and_ship_in_the_wheel() -> None:
    # claude_sessions.py ports code from claude-agent-sdk (MIT). The MIT terms need the
    # copyright line and the permission notice with substantial copies; the SDK is no
    # longer installed, so the notices file itself carries both.
    import ciao.claude_sessions as claude_sessions

    repo = Path(__file__).parents[1]
    notices = (repo / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "Copyright (c) 2025 Anthropic, PBC" in notices
    assert "Permission is hereby granted, free of charge" in notices

    assert "THIRD_PARTY_NOTICES.md" in (claude_sessions.__doc__ or "")
    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "THIRD_PARTY_NOTICES.md" in project["license-files"]
