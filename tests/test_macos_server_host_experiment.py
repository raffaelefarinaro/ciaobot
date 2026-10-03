"""Offline checks for the Ciaobot Server Host experiment, runnable everywhere.

These import the builder and the child probe as plain modules and exercise pure
logic (bundle metadata, refusal rules, layout, argv, receipt validation and the
timeout/exit/permission classifiers). They never invoke swiftc or codesign and
never touch macOS permissions.

The native host's own Swift behavior (AX prompting/lifetime, Process spawning,
pipe draining and timeout reaping) cannot be proven by scanning Python source.
It is validated by the compile/sign step and, when the coordinator approves, a
recorded live run per the README evidence matrix. Those are deliberately not
asserted here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments/macos-server-host"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = _load("macos_server_host_build", EXPERIMENT / "build.py")
CHILD = _load("macos_server_host_child", EXPERIMENT / "child_probe.py")


# --- identity -------------------------------------------------------------


def test_identity_is_separate_from_pwa_and_previous_experiment() -> None:
    info = BUILD.bundle_info("7")
    assert info["CFBundleIdentifier"] == "local.ciaobot.server-host-experiment"
    assert info["CFBundleDisplayName"] == "Ciaobot Server Host (Experiment)"
    assert info["CFBundleExecutable"] == "CiaobotServerHost"
    assert info["CFBundleVersion"] == "7"
    assert info["LSUIElement"] is True
    # A distinct identity from the PWA and from PR #119's earlier probe, so a
    # grant can never be inherited from either.
    assert info["CFBundleIdentifier"] not in {
        "local.ciaobot.permission-experiment",
        "app.ciaobot.pwa",
    }


# --- builder refusals -----------------------------------------------------


def _fail_runner(cmd: list[str], **_kwargs: Any) -> None:
    raise AssertionError(f"compiler/signer must not run: {cmd}")


def _fail_icon() -> bytes:
    raise AssertionError("icon extraction must not run")


@pytest.mark.parametrize("kind", ["file", "dir"])
def test_builder_refuses_existing_output(tmp_path: Path, kind: str) -> None:
    output = tmp_path / "existing"
    if kind == "file":
        output.write_text("keep me")
    else:
        output.mkdir()
        (output / "marker").write_text("keep me")

    before = sorted(p.name for p in output.iterdir()) if output.is_dir() else None
    with pytest.raises(BUILD.BuildError):
        BUILD.build(
            output,
            platform_name="darwin",
            runner=_fail_runner,
            icon_loader=_fail_icon,
        )

    # Untouched, and the compiler/signer were never reached.
    if kind == "file":
        assert output.read_text() == "keep me"
    else:
        assert sorted(p.name for p in output.iterdir()) == before
        assert (output / "marker").read_text() == "keep me"


def test_builder_refuses_existing_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:  # symlink creation may need privilege on Windows
        pytest.skip("symlinks unavailable on this platform")

    with pytest.raises(BUILD.BuildError):
        BUILD.build(
            link,
            platform_name="darwin",
            runner=_fail_runner,
            icon_loader=_fail_icon,
        )
    # The symlink is neither followed nor replaced, and its target is untouched.
    assert link.is_symlink()
    assert list(target.iterdir()) == []


def test_non_macos_build_fails_before_writes(tmp_path: Path) -> None:
    output = tmp_path / "never-created"
    with pytest.raises(BUILD.BuildError):
        BUILD.build(
            output,
            platform_name="win32",
            runner=_fail_runner,
            icon_loader=_fail_icon,
        )
    assert not output.exists()


# --- layout: mutable sidecar outside the signed bundle --------------------


def test_build_error_leaves_no_partial_output(tmp_path: Path) -> None:
    output = tmp_path / "partial"

    def boom(cmd: list[str], **_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, cmd)

    with pytest.raises(subprocess.CalledProcessError):
        BUILD.build(
            output,
            platform_name="darwin",
            runner=boom,
            icon_loader=lambda: b"icon",
        )
    assert not output.exists()


def test_child_script_is_outside_signed_bundle(tmp_path: Path) -> None:
    output = tmp_path / "host"
    commands: list[list[str]] = []

    def fake_runner(cmd: list[str], **_kwargs: Any) -> None:
        commands.append(list(cmd))
        if cmd[0] == "xcrun":
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"native-binary")

    result = BUILD.build(
        output,
        revision="3",
        platform_name="darwin",
        runner=fake_runner,
        icon_loader=lambda: b"icon-bytes",
    )
    paths = result["paths"]

    # Layout itself keeps the mutable child outside the app bundle.
    app = paths["app"]
    assert app not in paths["sidecar_script"].parents
    assert paths["sidecar_script"] == output / "child" / "child_probe.py"
    assert paths["sidecar_script"].is_file()

    # Nothing mutable lives under Resources: only the historical icon and plist.
    assert not (app / "Contents" / "Resources" / "child_probe.py").exists()
    assert (app / "Contents" / "Resources" / BUILD.ICON_NAME).read_bytes() == b"icon-bytes"

    # Ad-hoc signing of the bundle happened after compilation.
    assert BUILD.compile_command(BUILD.HOST_SOURCE, paths["executable"]) == commands[0]
    assert any(c[0] == "codesign" and "--verify" in c for c in commands)
    assert len(str(result["digest"])) == 64
    assert result["revision"] == "3"


# --- child probe logic ----------------------------------------------------


def test_osascript_argv_is_fixed_and_read_only() -> None:
    argv = CHILD.osascript_argv()
    assert argv == [CHILD.OSASCRIPT_BIN, "-e", CHILD.OSASCRIPT_SCRIPT]
    # No shell and no interpreter indirection.
    assert not any(part in {"/bin/sh", "/bin/bash", "-c"} for part in argv)
    script = argv[2]
    # The only query is the frontmost process's role; no names/titles/text/actions.
    assert script == CHILD.OSASCRIPT_SCRIPT
    assert "role of" in script
    assert "frontmost" in script
    for forbidden in (
        "name of",
        "title of",
        "value of",
        "keystroke",
        "click",
        "perform action",
        "set ",
        "delete",
    ):
        assert forbidden not in script


def test_child_receipt_validation() -> None:
    valid = {
        "mode": "python",
        "pid": 4242,
        "ppid": 4000,
        "platform": "Darwin",
        "accessibility_trusted": False,
    }
    assert CHILD.parse_child_receipt(valid)["pid"] == 4242
    # JSON text and bytes are accepted too.
    import json

    assert CHILD.parse_child_receipt(json.dumps(valid))["ppid"] == 4000
    assert CHILD.parse_child_receipt(json.dumps(valid).encode())["mode"] == "python"

    # Non-object JSON.
    with pytest.raises(CHILD.ChildReceiptError):
        CHILD.parse_child_receipt([1, 2, 3])
    with pytest.raises(CHILD.ChildReceiptError):
        CHILD.parse_child_receipt("not json")

    # Missing and wrong-typed fields.
    for broken in (
        {k: v for k, v in valid.items() if k != "pid"},
        {k: v for k, v in valid.items() if k != "mode"},
        {**valid, "pid": "4242"},
        {**valid, "platform": 3},
        {**valid, "mode": "shell"},
        {**valid, "pid": 0},
        {**valid, "ppid": -1},
    ):
        with pytest.raises(CHILD.ChildReceiptError):
            CHILD.parse_child_receipt(broken)

    # Oversized evidence is rejected before it is parsed.
    oversized = json.dumps({**valid, "blob": "x" * (CHILD.MAX_RECEIPT_BYTES + 1)})
    with pytest.raises(CHILD.ChildReceiptError):
        CHILD.parse_child_receipt(oversized)


def test_python_ax_classification_distinguishes_missing_element_from_no_grant() -> None:
    assert CHILD.classify_python_ax(False, 0, True) == "no_grant"
    assert CHILD.classify_python_ax(True, CHILD.AX_ERROR_API_DISABLED, False) == "no_grant"
    # Trusted API but nothing focused is its own state, not a denial.
    assert CHILD.classify_python_ax(True, 0, False) == "no_focused_element"
    assert CHILD.classify_python_ax(True, CHILD.AX_ERROR_NO_VALUE, False) == "no_focused_element"
    assert CHILD.classify_python_ax(True, 0, True) == "ok"
    assert CHILD.classify_python_ax(True, -25204, True) == "error"


def test_osascript_classification_splits_automation_denial_from_exit() -> None:
    denial = "execution error: Not authorized to send Apple events to System Events. (-1743)"
    assert CHILD.classify_osascript(None, True, "") == "timeout"
    assert CHILD.classify_osascript(1, False, denial) == "automation_denied"
    assert CHILD.classify_osascript(1, False, "some other error") == "exit"
    assert CHILD.classify_osascript(0, False, "") == "ok"


def test_child_main_rejects_unknown_mode_without_probing(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        CHILD.main(["definitely-not-a-mode"])
    captured = capsys.readouterr()
    assert captured.out == ""
