"""Build an isolated, ad-hoc-signed native host experiment; never install or start.

This builder creates a throwaway ``.app`` under an explicit new directory, plus a
mutable Python child sidecar OUTSIDE that bundle. It does not touch the running
engine, the PWA, existing bundles, launch agents, services, or permissions, and
it never launches what it builds. Import-safe on every platform: the macOS-only
subprocesses run only from ``build`` after the platform check.

```sh
python3 experiments/macos-server-host/build.py --output "$HOME/Ciaobot Server Host Experiment"
```
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from typing import Any, Callable

# The distinct indigo Ciaobot Server icon introduced by PR #119.
HISTORY_COMMIT = "40747b8955eb0b246a717cd79568cd144d4c997f"
HISTORY_ICON_PATH = "ciao/stock/deploy/CiaobotServer.icns"

BUNDLE_ID = "local.ciaobot.server-host-experiment"
DISPLAY_NAME = "Ciaobot Server Host (Experiment)"
EXECUTABLE_NAME = "CiaobotServerHost"
ICON_NAME = "CiaobotServer.icns"
APP_NAME = f"{DISPLAY_NAME}.app"

CHILD_DIR_NAME = "child"
CHILD_SCRIPT_NAME = "child_probe.py"

SOURCE = Path(__file__).resolve().parent
HOST_SOURCE = SOURCE / "HostProbe.swift"
CHILD_SOURCE = SOURCE / CHILD_SCRIPT_NAME
REPO = SOURCE.parent.parent


class BuildError(RuntimeError):
    """A refusal or failure that must not leave a partial build behind."""


def bundle_info(revision: str) -> dict[str, object]:
    """The fixed bundle metadata, deliberately separate from PWA and prior probes."""
    return {
        "CFBundleName": DISPLAY_NAME,
        "CFBundleDisplayName": DISPLAY_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": ICON_NAME,
        "CFBundleVersion": revision,
        "CFBundleShortVersionString": "0.0.1",
        # Accessory app: no Dock icon, no window, no desktop UI.
        "LSUIElement": True,
        "NSAccessibilityUsageDescription": (
            "Test whether a Ciaobot Server host can identify the focused desktop "
            "element. No typing or clicking."
        ),
        # Required before the child probe may ask System Events for a role.
        "NSAppleEventsUsageDescription": (
            "Test whether a Ciaobot Server host's child probe may read the "
            "frontmost process role. No names, titles, text or actions."
        ),
    }


def layout(output: Path) -> dict[str, Path]:
    """Every path this builder creates, so the sidecar's location is checkable."""
    app = output / APP_NAME
    macos = app / "Contents" / "MacOS"
    return {
        "output": output,
        "app": app,
        "macos": macos,
        "resources": app / "Contents" / "Resources",
        "executable": macos / EXECUTABLE_NAME,
        "sidecar_dir": output / CHILD_DIR_NAME,
        "sidecar_script": output / CHILD_DIR_NAME / CHILD_SCRIPT_NAME,
    }


def load_historical_icon(repo: Path = REPO) -> bytes:
    """Read the historical Ciaobot Server icon from Git, not the working tree."""
    return subprocess.check_output(
        ["git", "show", f"{HISTORY_COMMIT}:{HISTORY_ICON_PATH}"], cwd=repo
    )


def compile_command(source: Path, executable: Path) -> list[str]:
    """The exact swiftc invocation, exposed so tests can assert its shape."""
    return ["xcrun", "swiftc", str(source), "-o", str(executable)]


def codesign_cdhash(app: Path, runner: Callable[..., Any] = subprocess.run) -> str | None:
    """The signed bundle's CDHash, or None if `codesign -dv` did not report one.

    The executable SHA-256 alone is not signature evidence; this is what
    `codesign -dv --verbose=4` prints as ``CDHash=`` (on stderr in current
    macOS). The sidecar-update row in the README asks for it.
    """
    completed = runner(
        ["codesign", "-dv", "--verbose=4", str(app)],
        capture_output=True,
        text=True,
    )
    combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    for line in combined.splitlines():
        if line.startswith("CDHash="):
            return line.split("=", 1)[1].strip()
    return None


def _refuse_existing(output: Path) -> None:
    # Called before Path.resolve(), which would dereference a symlink and could
    # then create the target. lexists() also catches a dangling symlink.
    if output.is_symlink() or os.path.lexists(output):
        raise BuildError(f"refusing to overwrite existing output: {output}")
    if not output.is_absolute():
        raise BuildError(f"refusing non-absolute output path: {output}")


def build(
    output: Path,
    revision: str = "1",
    *,
    platform_name: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    icon_loader: Callable[[], bytes] = load_historical_icon,
) -> dict[str, object]:
    """Create the app + sidecar under ``output``. Returns the layout and digest.

    Platform is validated before any filesystem write, and an existing output
    (file, directory, or symlink) is refused untouched. The compiler and signer
    are injected so offline tests can prove they are never reached.
    """
    effective_platform = sys.platform if platform_name is None else platform_name
    if effective_platform != "darwin":
        raise BuildError("This isolated experiment requires macOS; Windows is unchanged")

    _refuse_existing(output.expanduser())
    output = output.expanduser().resolve()

    paths = layout(output)
    paths["output"].mkdir(parents=True, exist_ok=False)
    try:
        paths["macos"].mkdir(parents=True)
        paths["resources"].mkdir()
        paths["sidecar_dir"].mkdir()

        # Historical icon lives in the signed bundle resources; the mutable child
        # script deliberately does not.
        (paths["resources"] / ICON_NAME).write_bytes(icon_loader())

        with (paths["app"] / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump(bundle_info(revision), handle)

        runner(compile_command(HOST_SOURCE, paths["executable"]), check=True)
        runner(["codesign", "--force", "--sign", "-", str(paths["app"])], check=True)
        runner(["codesign", "--verify", "--strict", str(paths["app"])], check=True)
        cdhash = codesign_cdhash(paths["app"], runner)

        # The host resolves only <app-parent>/child/child_probe.py; updating this
        # copy cannot change the host executable or its signature.
        (paths["sidecar_script"]).write_bytes(CHILD_SOURCE.read_bytes())
    except BaseException:
        # Leave no half-built app/sidecar behind on failure.
        import shutil

        shutil.rmtree(paths["output"], ignore_errors=True)
        raise

    digest = hashlib.sha256(paths["executable"].read_bytes()).hexdigest()
    return {"paths": paths, "digest": digest, "revision": revision, "cdhash": cdhash}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New experiment directory (must not exist)",
    )
    parser.add_argument(
        "--revision", default="1", help="Bundle revision for helper-update comparisons"
    )
    args = parser.parse_args(argv)

    try:
        result = build(args.output, args.revision)
    except BuildError as exc:
        parser.error(str(exc))

    paths: dict[str, Path] = result["paths"]  # type: ignore[assignment]
    print(f"App: {paths['app']}")
    print(f"Child sidecar (outside signed bundle): {paths['sidecar_script']}")
    print(f"Host executable SHA256: {result['digest']}")
    print(f"Signed bundle CDHash: {result['cdhash']}")
    print("Built only. No launch agents, engine changes, or permission requests made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
