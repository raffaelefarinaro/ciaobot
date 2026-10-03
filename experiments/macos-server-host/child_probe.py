"""Fixed-mode child probe for the Ciaobot Server Host experiment.

This script is deliberately mutable and lives OUTSIDE the signed host bundle, at
``<app-parent>/child/child_probe.py``. It is a developer spike, not an exposed
production API: it accepts exactly one fixed mode, writes one JSON receipt on
stdout, and performs only read-only probes. It never runs a shell, never reads
text/value/window titles/screenshots, and never acts on the desktop.

Modes
-----
``python``
    Ask the Accessibility API, through ctypes, whether this process is trusted
    and what the focused UI element's role is. Records raw AX result codes.
``osascript``
    Run one fixed, read-only AppleScript that asks System Events for the
    frontmost process's role. Never asks for process names, window titles, text,
    or actions, and never attempts to grant anything.

Import-safe on non-macOS: no framework is loaded and no subprocess runs at
import time.

The host validates the same receipt fields; ``parse_child_receipt`` exposes that
contract so offline tests can exercise it without a Mac.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

MODES = ("python", "osascript")

# A child receipt is small by design; larger evidence is rejected rather than
# truncated silently.
MAX_RECEIPT_BYTES = 65536
MAX_OUTPUT_BYTES = 65536

OSASCRIPT_BIN = "/usr/bin/osascript"
OSASCRIPT_TIMEOUT_SECONDS = 15.0

# Pure, read-only AppleScript: the frontmost process's role only.
OSASCRIPT_SCRIPT = (
    'tell application "System Events" to get role of '
    "first application process whose frontmost is true"
)

# macOS Automation denial: "Not authorized to send Apple events to System Events."
AUTOMATION_DENIED_CODE = -1743

# Accessibility API result codes used by the classifier.
AX_ERROR_API_DISABLED = -25211
AX_ERROR_NO_VALUE = -25212

# Fields every child receipt must carry, with their required types.
REQUIRED_FIELDS: dict[str, type] = {
    "mode": str,
    "pid": int,
    "ppid": int,
    "platform": str,
}


class ChildReceiptError(ValueError):
    """Raised when a child receipt is malformed or missing expected fields."""


def osascript_argv() -> list[str]:
    """Return the exact, fixed, read-only osascript invocation.

    No variable payload, action, or content query can enter this argv.
    """
    return [OSASCRIPT_BIN, "-e", OSASCRIPT_SCRIPT]


def parse_child_receipt(raw: Any) -> dict[str, Any]:
    """Validate a child receipt and return a normalized copy.

    Accepts JSON text/bytes or an already-decoded mapping. Rejects oversized
    evidence, non-object JSON, a bad/missing ``mode``, and missing or
    wrong-typed required fields.
    """
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_RECEIPT_BYTES:
            raise ChildReceiptError("receipt exceeds the bounded size")
        try:
            decoded = json.loads(bytes(raw))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChildReceiptError(f"receipt is not valid JSON: {exc}") from exc
    elif isinstance(raw, str):
        encoded = raw.encode("utf-8")
        if len(encoded) > MAX_RECEIPT_BYTES:
            raise ChildReceiptError("receipt exceeds the bounded size")
        try:
            decoded = json.loads(raw)
        except ValueError as exc:
            raise ChildReceiptError(f"receipt is not valid JSON: {exc}") from exc
    else:
        decoded = raw

    if not isinstance(decoded, dict):
        raise ChildReceiptError("receipt is not a JSON object")

    normalized: dict[str, Any] = {}
    for field, expected in REQUIRED_FIELDS.items():
        if field not in decoded:
            raise ChildReceiptError(f"receipt is missing required field {field!r}")
        value = decoded[field]
        if expected is int:
            # bool is an int subclass in Python; a bool pid is not a pid.
            if not isinstance(value, int) or isinstance(value, bool):
                raise ChildReceiptError(f"receipt field {field!r} must be an integer")
        elif not isinstance(value, expected):
            raise ChildReceiptError(
                f"receipt field {field!r} must be {expected.__name__}"
            )
        normalized[field] = value

    if normalized["mode"] not in MODES:
        raise ChildReceiptError(f"receipt mode {normalized['mode']!r} is not a fixed mode")
    if normalized["pid"] <= 0:
        raise ChildReceiptError("receipt pid must be positive")
    if normalized["ppid"] < 0:
        raise ChildReceiptError("receipt ppid must not be negative")
    return normalized


def classify_python_ax(
    trusted: bool, focused_result: int, focused_present: bool
) -> str:
    """Classify a Python AX probe.

    A missing focused element is distinct from a missing grant: ``no_grant``
    means the API itself is disabled, ``no_focused_element`` means the API is
    available but nothing is focused.
    """
    if not trusted or focused_result == AX_ERROR_API_DISABLED:
        return "no_grant"
    if focused_result == AX_ERROR_NO_VALUE or (
        focused_result == 0 and not focused_present
    ):
        return "no_focused_element"
    if focused_result == 0:
        return "ok"
    return "error"


def classify_osascript(returncode: int | None, timed_out: bool, stderr: str) -> str:
    """Classify an osascript probe: timeout, automation denial, exit, or ok."""
    if timed_out:
        return "timeout"
    if returncode == 0:
        return "ok"
    if str(AUTOMATION_DENIED_CODE) in stderr:
        return "automation_denied"
    return "exit"


def _truncate(data: bytes) -> str:
    return data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")


def _load_ax_frameworks() -> tuple[Any, Any, Any]:
    """Load CoreFoundation and ApplicationServices with explicit ctypes types."""
    import ctypes
    from ctypes import POINTER, c_char_p, c_int32, c_size_t, c_uint8, c_uint32, c_void_p

    core = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",
        use_errno=False,
    )
    app_services = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",
        use_errno=False,
    )

    core.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
    core.CFStringCreateWithCString.restype = c_void_p
    core.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_size_t, c_uint32]
    core.CFStringGetCString.restype = c_uint8
    core.CFRelease.argtypes = [c_void_p]
    core.CFRelease.restype = None

    app_services.AXIsProcessTrusted.argtypes = []
    app_services.AXIsProcessTrusted.restype = c_uint8
    app_services.AXUIElementCreateSystemWide.argtypes = []
    app_services.AXUIElementCreateSystemWide.restype = c_void_p
    app_services.AXUIElementCopyAttributeValue.argtypes = [
        c_void_p,
        c_void_p,
        POINTER(c_void_p),
    ]
    app_services.AXUIElementCopyAttributeValue.restype = c_int32

    return ctypes, core, app_services


def run_python_ax() -> dict[str, Any]:
    """Read trust and the focused element's role through the AX API via ctypes."""
    if platform.system() != "Darwin":
        return {"ax_supported": False, "ax_error": "python AX probe requires macOS"}

    ctypes, core, app_services = _load_ax_frameworks()
    from ctypes import c_void_p, byref

    outcome: dict[str, Any] = {"ax_supported": True}
    trusted = bool(app_services.AXIsProcessTrusted())
    outcome["accessibility_trusted"] = trusted

    core_foundation_utf8 = 0x08000100
    focused_attr = core.CFStringCreateWithCString(
        None, b"AXFocusedUIElement", core_foundation_utf8
    )
    role_attr = core.CFStringCreateWithCString(None, b"AXRole", core_foundation_utf8)
    system_wide = app_services.AXUIElementCreateSystemWide()

    focused_result = -1
    focused_present = False
    owned_refs: list[Any] = []
    try:
        if not system_wide:
            outcome["focused_element_result"] = None
            outcome["error"] = "AXUIElementCreateSystemWide returned null"
            return outcome
        owned_refs.append(system_wide)
        focused = c_void_p()
        focused_result = int(
            app_services.AXUIElementCopyAttributeValue(
                system_wide, focused_attr, byref(focused)
            )
        )
        focused_present = bool(focused_result == 0 and focused.value)
        outcome["focused_element_result"] = focused_result
        outcome["focused_element_present"] = focused_present

        if focused_result == 0 and focused.value:
            owned_refs.append(focused)
            role = c_void_p()
            role_result = int(
                app_services.AXUIElementCopyAttributeValue(
                    focused, role_attr, byref(role)
                )
            )
            outcome["role_result"] = role_result
            if role_result == 0 and role.value:
                owned_refs.append(role)
                buffer = ctypes.create_string_buffer(256)
                if core.CFStringGetCString(
                    role, ctypes.cast(buffer, ctypes.c_char_p), len(buffer), core_foundation_utf8
                ):
                    outcome["focused_role"] = buffer.value.decode(
                        "utf-8", errors="replace"
                    )
    finally:
        for ref in owned_refs:
            core.CFRelease(ref)
        core.CFRelease(focused_attr)
        core.CFRelease(role_attr)

    outcome["ax_classification"] = classify_python_ax(
        trusted, focused_result, focused_present
    )
    return outcome


def run_osascript() -> dict[str, Any]:
    """Run the fixed, read-only osascript probe without a shell."""
    argv = osascript_argv()
    outcome: dict[str, Any] = {"osascript_argv": argv}
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=OSASCRIPT_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = _truncate(exc.stderr or b"")
        outcome["osascript_timed_out"] = True
        outcome["osascript_returncode"] = None
        outcome["osascript_stdout"] = _truncate(exc.stdout or b"")
        outcome["osascript_stderr"] = stderr
        outcome["osascript_classification"] = classify_osascript(None, True, stderr)
        return outcome

    stdout = _truncate(completed.stdout)
    stderr = _truncate(completed.stderr)
    classification = classify_osascript(completed.returncode, False, stderr)
    outcome["osascript_timed_out"] = False
    outcome["osascript_returncode"] = completed.returncode
    outcome["osascript_stdout"] = stdout
    outcome["osascript_stderr"] = stderr
    outcome["osascript_classification"] = classification
    if classification == "ok":
        outcome["frontmost_role"] = stdout.strip()
    return outcome


def build_receipt(mode: str) -> dict[str, Any]:
    """Assemble the JSON receipt for one fixed mode."""
    receipt: dict[str, Any] = {
        "mode": mode,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "platform": platform.system(),
        "platform_release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if mode == "python":
        receipt.update(run_python_ax())
    elif mode == "osascript":
        receipt.update(run_osascript())
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fixed-mode Ciaobot host child probe")
    parser.add_argument("mode", choices=MODES)
    args = parser.parse_args(argv)
    receipt = build_receipt(args.mode)
    json.dump(receipt, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
