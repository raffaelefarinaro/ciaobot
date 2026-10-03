"""Pin the Windows docs to the installer and the service module they describe (#696, C10)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ONE_LINER = (
    "irm https://github.com/raffaelefarinaro/ciaobot"
    "/releases/latest/download/install.ps1 | iex"
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_windows_doc_exists_and_is_linked_from_the_hub_and_readme() -> None:
    assert (ROOT / "docs" / "WINDOWS.md").is_file()
    assert "(WINDOWS.md)" in _read("docs/README.md")
    assert "(docs/WINDOWS.md)" in _read("README.md")


def test_every_surface_names_the_same_windows_one_liner() -> None:
    for relative in ("README.md", "docs/WINDOWS.md", "INTEGRATIONS.md"):
        assert ONE_LINER in _read(relative), relative
    assert ONE_LINER in _read("ciao/stock/skills/ciao-capabilities/SKILL.md")
    assert ONE_LINER in _read("skills/ciao-release/SKILL.md")


def test_the_readme_labels_windows_a_preview() -> None:
    readme = _read("README.md")
    assert "**Windows 11 (x64 or ARM64), preview:**" in readme
    assert "preview" in _read("docs/WINDOWS.md").splitlines()[0].lower()


def test_documented_installer_options_are_real_install_ps1_parameters() -> None:
    script = _read("scripts/install.ps1")
    param_block = script[script.index("param("): script.index(")", script.index("param("))]
    real = set(re.findall(r"\$([A-Za-z]+)", param_block))
    doc = _read("docs/WINDOWS.md")
    documented = set(re.findall(r"`-([A-Za-z]+)(?: [^`]*)?`", doc))
    options = {"Workspace", "NoStart", "Uninstall", "Version", "DryRun", "ReleaseDir"}
    assert options <= real, options - real
    assert options <= documented, options - documented
    # An option the doc names that install.ps1 does not define is a dead instruction.
    assert {name for name in documented if name[0].isupper()} - {"ExecutionPolicy", "NoProfile", "File", "Force"} <= real


def test_the_doc_names_the_task_and_files_the_code_writes() -> None:
    from ciao import windows_service

    doc = _read("docs/WINDOWS.md")
    assert windows_service.TASK_NAME in doc  # \Ciaobot\Engine
    assert windows_service.TASK_FILE_NAME in doc  # Ciaobot-Engine.xml
    assert "ciao.stdout.log" in doc and "ciao.stderr.log" in doc
    assert "pythonw.exe -m ciao.cli supervise" in doc
    assert "install-receipt.json" in doc and "install-state.json" in doc


def test_the_update_section_names_the_tasks_and_commands_the_updater_uses() -> None:
    from ciao import windows_service

    doc = _read("docs/WINDOWS.md")
    section = doc[doc.index("## Update and rollback"): doc.index("## Uninstall")]
    assert "land with #857" not in section
    assert windows_service.UPDATER_TASK_NAME in section  # \Ciaobot\Updater
    assert windows_service.RECOVER_TASK_NAME in section  # \Ciaobot\Recover
    for command in ("ciao update stage", "ciao update apply", "ciao update status"):
        assert command in section, command
    assert "#857" not in _read("ciao/stock/skills/ciao-capabilities/SKILL.md")


def test_architecture_describes_the_windows_lifecycle() -> None:
    architecture = _read("docs/ARCHITECTURE.md")
    assert "**Windows lifecycle (#696).**" in architecture
    assert "no service file uses it yet" not in architecture


def test_the_release_skill_has_a_windows_smoke_step() -> None:
    skill = _read("skills/ciao-release/SKILL.md")
    assert "### Windows smoke" in skill
    assert "-Uninstall" in skill and "install.ps1" in skill