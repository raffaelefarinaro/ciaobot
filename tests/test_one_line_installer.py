from __future__ import annotations

import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_installer_scripts_are_posix_shell() -> None:
    for name in ("install.sh", "build-bundled-runtime.sh"):
        result = subprocess.run(
            ["sh", "-n", str(REPO_ROOT / "scripts" / name)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_bundled_launcher_does_not_import_from_calling_directory() -> None:
    script = (REPO_ROOT / "scripts" / "build-bundled-runtime.sh").read_text(
        encoding="utf-8"
    )

    assert 'cd "$root"' in script


def test_bundled_launcher_keeps_child_ciao_commands_on_matching_runtime() -> None:
    script = (REPO_ROOT / "scripts" / "build-bundled-runtime.sh").read_text(
        encoding="utf-8"
    )

    assert 'export PATH="$root/bin${PATH:+:$PATH}"' in script
    assert 'export PYTHONPATH="$site${PYTHONPATH:+:$PYTHONPATH}"' in script


def test_runtime_builder_probes_pydantic_core_for_each_architecture() -> None:
    script = (REPO_ROOT / "scripts" / "build-bundled-runtime.sh").read_text(
        encoding="utf-8"
    )

    assert "import pydantic_core" in script
    assert "Bundled ${arch} runtime cannot import pydantic-core" in script


def test_installer_requires_native_verification_before_extraction() -> None:
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "__VERIFIER_SHA256__" in script
    assert 'placeholder=__VERIFIER_SHA"256__"' in script
    assert '"$verifier" "$archive" "$signature"' in script
    assert script.index('"$verifier" "$archive" "$signature"') < script.index(
        'tar -xzf "$archive"'
    )
    assert "ciao-runtime/bin/ciao" in script
    assert 'ciao-runtime/bin/ciao" --help' in script
    assert 'CIAO_RELEASE_BASE_URL' in script
    assert 'if [ -e "$backup" ]; then' in script


def test_installer_starts_and_persists_the_menu_bar_agent() -> None:
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert 'desktop_plist="$HOME/Library/LaunchAgents/Ciaobot.plist"' in script
    assert '<string>--background</string>' in script
    assert 'launchctl bootstrap "gui/$uid" "$desktop_plist"' in script
    assert 'launchctl kickstart "gui/$uid/Ciaobot"' in script


def test_fresh_install_defers_workspace_creation_to_onboarding() -> None:
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    # A configured workspace is recovered from the existing LaunchAgent. With
    # no valid workspace, the app must start in its built-in bootstrap mode;
    # setup must not invent a password in a new ~/Ciaobot directory first.
    assert 'workspace="$HOME/Ciaobot"' not in script
    assert '[ -f "$existing_workspace/.env" ]' in script
    assert 'if [ -n "$workspace" ]; then' in script
    assert 'first-run onboarding will ask where to create or adopt one' in script


def test_release_workflows_do_not_publish_removed_install_channels() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml")
    )

    assert "update-homebrew-tap" not in workflows
    assert "pypi:" not in workflows
    assert ".dmg" not in workflows.lower()
    assert "brew install" not in workflows


def test_ci_prepares_cross_arch_toolchains_before_runtime_build() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert ci.index("name: Set up Rust") < ci.index(
        "name: Build embedded Ciaobot runtime"
    )
    assert ci.index("name: Select newest Xcode") < ci.index(
        "name: Build embedded Ciaobot runtime"
    )


def test_desktop_bundle_configuration_embeds_runtime_without_dmg() -> None:
    config = json.loads(
        (REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )
    bundle = config["bundle"]

    assert "mainBinaryName" not in config
    assert bundle["targets"] == ["app"]
    assert bundle["resources"]["../runtime"] == "ciao-runtime"


def test_installer_verifier_is_outside_the_tauri_package() -> None:
    verifier = REPO_ROOT / "desktop" / "installer-verify"
    tauri_bins = REPO_ROOT / "desktop" / "src-tauri" / "src" / "bin"

    assert (verifier / "Cargo.toml").is_file()
    assert (verifier / "src" / "main.rs").is_file()
    assert not (tauri_bins / "ciaobot-installer-verify.rs").exists()
