from __future__ import annotations

import json
import os
import subprocess
import sys
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
    assert 'export CIAO_ENGINE_PATH="$root/bin/ciao"' in script
    assert "unset CIAO_RUNTIME_ROOT" in script
    assert 'CIAO_RUNTIME_ROOT="${CIAO_RUNTIME_ROOT:-$root}"' not in script


def test_bundled_launcher_does_not_export_pythonpath_to_children() -> None:
    """PYTHONPATH is inherited by every descendant, including a child `ciao`
    running a different CPython against the bundle's 3.12 extension modules -
    which fails with `No module named 'pydantic_core._pydantic_core'`. The
    interpreter attaches its own tree instead, so the launcher must not export
    PYTHONPATH nor pass an inherited one through."""
    script = (REPO_ROOT / "scripts" / "build-bundled-runtime.sh").read_text(
        encoding="utf-8"
    )
    launcher = script.split("<<'LAUNCHER'", 1)[1].split("LAUNCHER", 1)[0]

    assert "export PYTHONPATH" not in launcher
    assert "unset PYTHONPATH" in launcher


def test_runtime_builder_installs_the_bundled_site_hook() -> None:
    script = (REPO_ROOT / "scripts" / "build-bundled-runtime.sh").read_text(
        encoding="utf-8"
    )

    assert "runtime_bundled_site.py" in script
    assert '>"$purelib/ciao_bundled_site.py"' in script
    assert "echo 'import ciao_bundled_site' >\"$purelib/zz-ciao-bundled-site.pth\"" in script
    # A silently unsubstituted placeholder would ship a runtime that cannot
    # import anything, so the build has to fail on it.
    assert "kept its unsubstituted placeholder" in script


def test_runtime_builder_probes_pydantic_core_for_each_architecture() -> None:
    script = (REPO_ROOT / "scripts" / "build-bundled-runtime.sh").read_text(
        encoding="utf-8"
    )

    assert "import pydantic_core" in script
    assert "Bundled ${arch} runtime cannot import pydantic-core" in script
    # The probe has to run without PYTHONPATH, because that is how the
    # launcher invokes the interpreter.
    assert "unset PYTHONPATH;" in script


def test_runtime_builder_installs_from_the_frozen_hashed_lock_export() -> None:
    script = (REPO_ROOT / "scripts" / "build-bundled-runtime.sh").read_text(
        encoding="utf-8"
    )

    assert "uv export" in script
    assert "--frozen" in script
    assert "--require-hashes" in script
    assert "-r \"$requirements\"" in script


def test_bundled_site_hook_attaches_the_tree_for_its_own_architecture(
    tmp_path: Path,
) -> None:
    """Exercise the hook's path math against the real bundle layout.

    The relative path is what lets the signed app work from either
    ``/Applications`` or ``~/Applications``; getting it wrong ships a runtime
    that imports nothing.
    """
    runtime = tmp_path / "ciao-runtime"
    purelib = runtime / "python" / "arm64" / "lib" / "python3.12" / "site-packages"
    tree = runtime / "site-packages" / "arm64"
    other = runtime / "site-packages" / "x86_64"
    for directory in (purelib, tree, other):
        directory.mkdir(parents=True)
    (tree / "bundled_marker.py").write_text("ARCH = 'arm64'\n", encoding="utf-8")
    (other / "wrong_arch_marker.py").write_text("ARCH = 'x86_64'\n", encoding="utf-8")

    template = (REPO_ROOT / "scripts" / "runtime_bundled_site.py").read_text(
        encoding="utf-8"
    )
    assert "@BUNDLED_SITE_REL@" in template, "build-time placeholder is gone"
    rel = os.path.relpath(tree, purelib)
    (purelib / "ciao_bundled_site.py").write_text(
        template.replace("@BUNDLED_SITE_REL@", rel), encoding="utf-8"
    )

    probe = (
        "import sys;"
        f" sys.path.insert(0, {str(purelib)!r});"
        " import ciao_bundled_site;"
        " import bundled_marker;"
        " print(bundled_marker.ARCH)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "arm64"

    # The sibling architecture's tree must stay off sys.path.
    wrong = subprocess.run(
        [
            sys.executable,
            "-c",
            probe.replace("import bundled_marker;", "import wrong_arch_marker;").replace(
                "print(bundled_marker.ARCH)", "print('unexpected')"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert wrong.returncode != 0
    assert "ModuleNotFoundError" in wrong.stderr


def test_bundled_site_hook_is_inert_when_the_tree_is_missing(tmp_path: Path) -> None:
    """A partially assembled runtime must not crash every `import site`."""
    purelib = tmp_path / "lib" / "python3.12" / "site-packages"
    purelib.mkdir(parents=True)
    template = (REPO_ROOT / "scripts" / "runtime_bundled_site.py").read_text(
        encoding="utf-8"
    )
    (purelib / "ciao_bundled_site.py").write_text(
        template.replace("@BUNDLED_SITE_REL@", "../../../nope/arm64"),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(purelib)!r}); import ciao_bundled_site",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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


def test_installer_starts_and_persists_the_menu_bar_agent() -> None:
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert 'desktop_plist="$HOME/Library/LaunchAgents/Ciaobot.plist"' in script
    assert '<string>--background</string>' in script
    assert 'launchctl bootstrap "gui/$uid" "$desktop_plist"' in script
    assert 'launchctl kickstart "gui/$uid/Ciaobot"' in script


def test_installer_does_not_swallow_existing_workspace_setup_failure() -> None:
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    setup_block = script[script.index('if [ -n "$workspace" ]; then') :]
    assert '"$engine" setup' in setup_block
    assert "--load-launchd" in setup_block
    assert '"$engine" setup \\\n            --workspace "$workspace"' in setup_block
    assert '>/dev/null || true' not in setup_block


def test_installer_retries_until_the_menu_bar_agent_names_the_new_app() -> None:
    # bootout is asynchronous, so a bootstrap issued immediately after it can
    # fail outright, or appear to succeed while launchd still holds the stale
    # job — which names the bundle this install just moved aside. Swallowing
    # either outcome leaves an updated install with no menu-bar app, and that
    # is invisible until the user looks for the tray icon.
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    reload_block = script[script.index('if [ "$no_start" -eq 0 ]; then') :]
    assert 'grep -qF "$desktop_executable"' in reload_block
    assert "the menu-bar LaunchAgent did not load" in reload_block
    # Verify before acting: a job that already names the new executable must not
    # be torn down, or a mis-detection would boot a working agent out on every
    # one of the retries.
    assert reload_block.index('grep -qF "$desktop_executable"') < (
        reload_block.index('launchctl bootout "gui/$uid/Ciaobot"')
    )
    assert reload_block.index('launchctl bootout "gui/$uid/Ciaobot"') < (
        reload_block.index('launchctl bootstrap "gui/$uid" "$desktop_plist"')
    )
    assert "grep" in script[: script.index("[ \"$(uname -s)\" = \"Darwin\" ]")]


def test_bundled_launcher_keeps_bytecode_out_of_the_signed_bundle() -> None:
    # The runtime is sealed by the app's code signature. Bytecode written next
    # to the bundled sources adds unsealed files, so `codesign -v` fails after
    # the first run.
    script = (REPO_ROOT / "scripts" / "build-bundled-runtime.sh").read_text(
        encoding="utf-8"
    )

    assert 'PYTHONPYCACHEPREFIX="$cache_root/Ciaobot/pycache"' in script
    assert "export PYTHONPYCACHEPREFIX" in script
    assert "export PYTHONDONTWRITEBYTECODE=1" in script


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
    assert "mapfile" not in workflows


def test_release_smoke_only_runs_with_a_published_version() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release-smoke.yml").read_text(
        encoding="utf-8"
    )

    # Pull requests do not populate workflow_call/workflow_dispatch inputs, so
    # triggering this release-only smoke test there would normalize an empty
    # version and fail before the installer is exercised.
    assert "workflow_call:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert 'LaunchAgents/com.ciao.server.plist")' not in workflow


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
