"""Offline checks for the Ciaobot Server Host experiment, runnable everywhere.

These import the builder and the child probe as plain modules and exercise pure
logic (bundle metadata, refusal rules, layout, argv, receipt validation and the
timeout/exit/permission classifiers). They never invoke swiftc or codesign and
never touch macOS permissions, except in the small macOS-only Swift checks below,
which compile and run a throwaway snippet.

The parent-proof and output-path checks live in the host's Swift `runChild` /
`parseInvocation`, so Python cannot prove them. On macOS we typecheck the real
`HostProbe.swift` (proving the NSNumber->CFNumber bridging compiles) and run the
reviewer-requested Swift snippet that shows JSON `true` and `4242.0` are rejected
as integers. Those checks are skipped off macOS; the README records that a live
run remains a coordinator gate.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
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

HAS_XCRUN = sys.platform == "darwin" and shutil.which("xcrun") is not None


# --- identity -------------------------------------------------------------


def test_identity_is_separate_from_pwa_and_previous_experiment() -> None:
    info = BUILD.bundle_info("7")
    assert info["CFBundleIdentifier"] == "local.ciaobot.server-host-experiment"
    assert info["CFBundleDisplayName"] == "Ciaobot Server Host (Experiment)"
    assert info["CFBundleExecutable"] == "CiaobotServerHost"
    assert info["CFBundleVersion"] == "7"
    assert info["LSUIElement"] is True
    # A distinct identity from the real Ciaobot bundles, so a grant can never be
    # inherited from the PWA, the retired app, a launcher, or the menu bar agent.
    # These are the actual constants, imported rather than copied. The menu bar ID
    # stays a literal because ciao/cli.py:392 spells it inline with no constant to
    # import.
    from ciao import cli, macos_service

    reserved = {macos_service.DESKTOP_BUNDLE_ID, "local.ciaobot.menubar", *cli._OUR_BUNDLE_IDS}
    assert BUILD.BUNDLE_ID not in reserved
    # The earlier direct AX probe lives in a separate, uncommitted checkout, so its
    # bundle ID is not a constant here and is deliberately not asserted.


# --- README run-command arguments (regression) ----------------------------


def _fenced_sh_commands(text: str) -> list[str]:
    """Every shell command inside a ```sh fence, comments stripped.

    A command continues only across a trailing backslash; a new line without one
    starts a new command, so a `PY=...` assignment and the `open` that follows it
    stay separate.
    """
    commands: list[str] = []
    in_fence = False
    buffer: list[str] = []
    continuing = False
    for line in text.splitlines():
        if line.strip() == "```sh":
            in_fence = True
            buffer = []
            continuing = False
            continue
        if in_fence and line.strip() == "```":
            in_fence = False
            if buffer:
                commands.append(" ".join(buffer))
            buffer = []
            continue
        if not in_fence:
            continue
        # Drop an inline comment (the README uses ` # ...`).
        stripped = re.sub(r"\s+#.*$", "", line).strip()
        if not stripped and not continuing:
            continue
        continuing = stripped.endswith("\\")
        if continuing:
            stripped = stripped[:-1].rstrip()
        if stripped:
            buffer.append(stripped)
        if not continuing and buffer:
            commands.append(" ".join(buffer))
            buffer = []
    if buffer:
        commands.append(" ".join(buffer))
    return commands


def test_readme_open_invocations_place_options_before_app_and_args() -> None:
    text = (EXPERIMENT / "README.md").read_text()
    commands = _fenced_sh_commands(text)
    opens = [c for c in commands if c.startswith("open ")]
    # All four documented modes are present, no more.
    assert len(opens) == 4
    for command in opens:
        tokens = command.split()
        app_index = tokens.index("--args")
        stdout_index = tokens.index("--stdout")
        stderr_index = tokens.index("--stderr")
        # --stdout/--stderr are open's own options and must precede the app path
        # and --args; after --args they become the host's argv and exit 2.
        assert stdout_index < app_index
        assert stderr_index < app_index
        # The app argument is the token immediately before --args.
        assert tokens[app_index - 1] == '"$APP"'
        # --output is a host argument, so it lives after --args.
        assert tokens.index("--output") > app_index


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

    def fake_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(list(cmd))
        if cmd[0] == "xcrun":
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"native-binary")
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:2] == ["codesign", "-dv"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="CDHash=deadbeef\n")
        return subprocess.CompletedProcess(cmd, 0)

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

    # Order is compile -> ad-hoc sign -> strict verify -> CDHash read.
    assert commands[0] == BUILD.compile_command(BUILD.HOST_SOURCE, paths["executable"])
    assert commands[1][:4] == ["codesign", "--force", "--sign", "-"]
    assert commands[1][-1] == str(app)
    assert commands[2] == ["codesign", "--verify", "--strict", str(app)]
    assert commands[3][:2] == ["codesign", "-dv"]
    assert result["cdhash"] == "deadbeef"
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
        # The Swift mirror rejects these; the Python reference must too.
        {**valid, "pid": True},
        {**valid, "pid": 4242.0},
    ):
        with pytest.raises(CHILD.ChildReceiptError):
            CHILD.parse_child_receipt(broken)

    # Oversized evidence is rejected before it is parsed.
    oversized = json.dumps({**valid, "blob": "x" * (CHILD.MAX_RECEIPT_BYTES + 1)})
    with pytest.raises(CHILD.ChildReceiptError):
        CHILD.parse_child_receipt(oversized)


def test_child_main_self_checks_before_emitting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A real run must exercise parse_child_receipt: an invalid assembled receipt
    # is refused before anything reaches stdout.
    monkeypatch.setattr(
        CHILD,
        "build_receipt",
        lambda mode: {"mode": mode, "pid": 0, "ppid": 1, "platform": "Darwin"},
    )
    assert CHILD.main(["python"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid receipt" in captured.err


def test_child_main_accepts_a_valid_receipt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        CHILD,
        "build_receipt",
        lambda mode: {"mode": mode, "pid": 42, "ppid": 1, "platform": "Darwin"},
    )
    assert CHILD.main(["osascript"]) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["pid"] == 42


def test_truncation_bookkeeping() -> None:
    text, truncated = CHILD._truncate(b"x" * (CHILD.OSASCRIPT_OUTPUT_BYTES + 10))
    assert truncated is True
    assert len(text) == CHILD.OSASCRIPT_OUTPUT_BYTES

    text, truncated = CHILD._truncate(b"AXApplication\n")
    assert truncated is False
    assert text == "AXApplication\n"

    # Invalid bytes are replaced, not erased (a cut at the cap must keep evidence).
    text, _ = CHILD._truncate(b"\xff\xfe")
    assert text != ""


def test_python_ax_classification_distinguishes_missing_element_from_no_grant() -> None:
    assert CHILD.classify_python_ax(False, 0, True) == "no_grant"
    assert CHILD.classify_python_ax(True, CHILD.AX_ERROR_API_DISABLED, False) == "no_grant"
    # Trusted API but nothing focused is its own state, not a denial.
    assert CHILD.classify_python_ax(True, 0, False) == "no_focused_element"
    assert CHILD.classify_python_ax(True, CHILD.AX_ERROR_NO_VALUE, False) == "no_focused_element"
    assert CHILD.classify_python_ax(True, 0, True) == "ok"
    assert CHILD.classify_python_ax(True, -25204, True) == "error"


def test_osascript_classification_splits_denials_from_exit() -> None:
    automation = "execution error: Not authorized to send Apple events to System Events. (-1743)"
    ax_1719 = (
        "execution error: System Events got an error: osascript is not allowed "
        "assistive access. (-1719)"
    )
    ax_25211 = "AX API disabled (-25211)"
    # The exact trailing code is matched, so -17430 is not -1743.
    not_a_match = "some unrelated failure (-17430)"

    assert CHILD.classify_osascript(None, True, "") == "timeout"
    assert CHILD.classify_osascript(1, False, automation) == "automation_denied"
    assert CHILD.classify_osascript(1, False, ax_1719) == "accessibility_denied"
    assert CHILD.classify_osascript(1, False, ax_25211) == "accessibility_denied"
    assert CHILD.classify_osascript(1, False, not_a_match) == "exit"
    assert CHILD.classify_osascript(1, False, "some other error") == "exit"
    assert CHILD.classify_osascript(0, False, "") == "ok"

    assert CHILD._parse_osascript_error_code(automation) == -1743
    assert CHILD._parse_osascript_error_code(ax_1719) == -1719
    assert CHILD._parse_osascript_error_code(not_a_match) == -17430
    assert CHILD._parse_osascript_error_code("no code here") is None


def test_child_main_rejects_unknown_mode_without_probing(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        CHILD.main(["definitely-not-a-mode"])
    captured = capsys.readouterr()
    assert captured.out == ""


# --- executable Swift evidence (macOS only) -------------------------------

_BRIDGE_SNIPPET = r"""
import Foundation
import CoreFoundation

func integralNumber(_ value: Any) -> Int? {
    guard let number = value as? NSNumber else { return nil }
    if CFGetTypeID(number) == CFBooleanGetTypeID() { return nil }
    if CFNumberIsFloatType(number) { return nil }
    return number.intValue
}

func result(_ json: String) -> String {
    let data = json.data(using: .utf8)!
    let object = try! JSONSerialization.jsonObject(with: data) as! [String: Any]
    return integralNumber(object["pid"]!).map(String.init) ?? "reject"
}
print(result("{\"pid\":4242}"))
print(result("{\"pid\":true}"))
print(result("{\"pid\":4242.0}"))
print(result("{\"pid\":\"4242\"}"))
"""


@pytest.mark.skipif(not HAS_XCRUN, reason="swift typecheck requires macOS Command Line Tools")
def test_native_host_source_typechecks() -> None:
    # Proves the real NSNumber->CFNumber bridging and the whole host compile.
    completed = subprocess.run(
        ["xcrun", "swiftc", "-typecheck", str(EXPERIMENT / "HostProbe.swift")],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(not HAS_XCRUN, reason="swift snippet requires macOS Command Line Tools")
def test_swift_integral_number_rejects_bool_and_float(tmp_path: Path) -> None:
    # The exact rejection semantics the host's parse branch relies on, executed.
    source = tmp_path / "bridge.swift"
    source.write_text(_BRIDGE_SNIPPET)
    binary = tmp_path / "bridge"
    compile_completed = subprocess.run(
        ["xcrun", "swiftc", str(source), "-o", str(binary)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert compile_completed.returncode == 0, compile_completed.stderr
    run_completed = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
    assert run_completed.returncode == 0, run_completed.stderr
    assert run_completed.stdout.split() == ["4242", "reject", "reject", "reject"]


# Extracts the exact path-validation functions from the real HostProbe.swift and
# compiles them with an injected bundle root, so the test runs the shipped logic
# rather than a copy. AX/AppKit are not imported by these functions and nothing
# is launched.
_PATH_EXTRACTOR_SNIPPET = r"""
import Foundation
import Darwin

__FUNCTIONS__

let bundleRoot = CommandLine.arguments[1]
let output = CommandLine.arguments[2]
print(outputPathRejection(output, bundleRoot: bundleRoot) ?? "ACCEPT")
"""


def _extract_path_validation_source() -> str:
    host = (EXPERIMENT / "HostProbe.swift").read_text()
    wanted = ("func resolveExistingPath(", "func outputPathRejection(")
    lines = host.splitlines()
    chunks: list[str] = []
    for start, line in enumerate(lines):
        if any(line.startswith(name) for name in wanted):
            depth = 0
            started = False
            for candidate in lines[start:]:
                depth += candidate.count("{") - candidate.count("}")
                chunks.append(candidate)
                if "{" in candidate:
                    started = True
                if started and depth == 0:
                    break
    assert len(chunks) > 10, "could not extract the path-validation functions"
    return "\n".join(chunks)


@pytest.mark.skipif(not HAS_XCRUN, reason="swift snippet requires macOS Command Line Tools")
def test_swift_output_path_validation(tmp_path: Path) -> None:
    # Build a fake "bundle root" out of real directories, without touching any
    # signed app. `alias` is a symlink to `bin`, and `.` / `..` name segments.
    fake_root = tmp_path / "h"
    (fake_root / "bin").mkdir(parents=True)
    (fake_root / "child").mkdir()
    (fake_root / "bin2").mkdir()
    (fake_root / "out").mkdir()
    alias = fake_root / "alias"
    alias.symlink_to(fake_root / "bin")

    bundle_root = str(fake_root / "bin")
    source = tmp_path / "pathval.swift"
    source.write_text(
        _PATH_EXTRACTOR_SNIPPET.replace("__FUNCTIONS__", _extract_path_validation_source())
    )
    binary = tmp_path / "pathval"
    compiled = subprocess.run(
        ["xcrun", "swiftc", str(source), "-o", str(binary)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert compiled.returncode == 0, compiled.stderr

    def rejection(output: str, root: str = bundle_root) -> str:
        completed = subprocess.run(
            [str(binary), root, output], capture_output=True, text=True, timeout=60
        )
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.strip()

    inside = "output is inside the signed bundle"
    assert rejection(f"{bundle_root}/receipt.json") == inside
    assert rejection(f"{fake_root}/child/../bin/dot.json") == inside
    assert rejection(f"{alias}/sym.json") == inside
    assert rejection(f"{bundle_root}/.") != "ACCEPT"
    assert rejection(f"{bundle_root}/..") != "ACCEPT"
    # A sibling whose name shares a prefix is not inside (path-boundary compare).
    assert rejection(f"{fake_root}/bin2/sib.json") == "ACCEPT"
    # A valid external path is accepted.
    assert rejection(f"{fake_root}/out/ok.json") == "ACCEPT"
    # A missing parent and a relative path are refused.
    assert rejection(f"{fake_root}/nope/x.json") == "output directory does not exist"
    assert rejection("rel/x.json") == "output must be an absolute path"
    # A canonical-alias bundle root (e.g. /tmp vs /private/tmp) is matched too.
    if Path("/tmp").resolve() != Path("/tmp"):
        assert rejection("/tmp/never-there/x.json", root="/tmp") != "ACCEPT"
