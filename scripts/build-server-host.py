"""Build the universal, ad-hoc-signed Ciaobot Server host archive (#1009).

This is a build tool, not an installer. It compiles the small native host at
``native/server-host/ServerHost.swift`` for both macOS architectures, assembles
a ``Ciaobot Server.app`` bundle with the tracked PR119 icon, ad-hoc signs it once
after all resources are in place, verifies the signature and universal slices,
and writes ``caibot-server-host-macos-universal-v1.tar.gz``.

It writes only under the caller-provided new output directory. It never touches
``~/Applications``, launch agents, services, permissions, or a running engine,
and it never launches what it builds. Import-safe on Windows and Linux: the
macOS-only subprocesses are injected and never reached until ``build`` has
passed the platform check.

```sh
python3 scripts/build-server-host.py --output /tmp/ciaobot-server-host-build
```
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Callable

APP_NAME = "Ciaobot Server.app"
BUNDLE_ID = "local.ciaobot.server"
DISPLAY_NAME = "Ciaobot Server"
EXECUTABLE_NAME = "CiaobotServerHost"
HOST_REVISION = 1
MACOS_DEPLOYMENT_TARGET = "13.0"
ARM_TARGET = f"arm64-apple-macosx{MACOS_DEPLOYMENT_TARGET}"
INTEL_TARGET = f"x86_64-apple-macosx{MACOS_DEPLOYMENT_TARGET}"

ICON_NAME = "CiaobotServer.icns"
ARCHIVE_NAME = "caibot-server-host-macos-universal-v1.tar.gz"

# The custom plist key the host-aware service contract will read (#1008 child B).
HOST_PROTOCOL_KEY = "CiaobotServerHostProtocol"
HOST_PROTOCOL_REVISION = 1

# Honest descriptions: the host runs whatever tools the user invokes, not only
# read-only probes. They describe the capability, not a promise that every tool
# is harmless, and they never imply the host grants anything by itself.
APPLE_EVENTS_USAGE = (
    "Ciaobot Server runs the tools you ask for through your installed agent "
    "harnesses. Some of those tools automate other applications with Apple "
    "Events; Ciaobot asks on their behalf only when you invoke one."
)
ACCESSIBILITY_USAGE = (
    "Ciaobot Server asks for Accessibility so the tools you invoke can control "
    "applications on your behalf. Ciaobot itself never reads, clicks, types, or "
    "scripts your desktop; a tool you ran does, when you ask it to."
)

REPO = Path(__file__).resolve().parents[1]
HOST_SOURCE = REPO / "native" / "server-host" / "ServerHost.swift"
ICON_SOURCE = REPO / "ciao" / "stock" / "deploy" / ICON_NAME

ARCHITECTURES = ("arm64", "x86_64")


class BuildError(RuntimeError):
    """A refusal or failure that must not leave a partial build behind."""


def bundle_info() -> dict[str, object]:
    """The fixed bundle metadata, expressed as host revision rather than engine version."""
    return {
        "CFBundleName": DISPLAY_NAME,
        "CFBundleDisplayName": DISPLAY_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": ICON_NAME,
        "CFBundleVersion": str(HOST_REVISION),
        "CFBundleShortVersionString": str(HOST_REVISION),
        HOST_PROTOCOL_KEY: HOST_PROTOCOL_REVISION,
        "LSMinimumSystemVersion": MACOS_DEPLOYMENT_TARGET,
        # Accessory app: no Dock icon, no window, no desktop UI.
        "LSUIElement": True,
        "NSAppleEventsUsageDescription": APPLE_EVENTS_USAGE,
        "NSAccessibilityUsageDescription": ACCESSIBILITY_USAGE,
    }


def _swiftc_command(source: Path, output: Path, target: str) -> list[str]:
    return ["xcrun", "swiftc", str(source), "-target", target, "-O", "-o", str(output)]


def compile_commands(source: Path, arm_output: Path, intel_output: Path) -> list[list[str]]:
    """The exact both-architecture swiftc invocations, exposed for tests."""
    return [
        _swiftc_command(source, arm_output, ARM_TARGET),
        _swiftc_command(source, intel_output, INTEL_TARGET),
    ]


def lipo_create_command(arm_output: Path, intel_output: Path, universal_output: Path) -> list[str]:
    return ["lipo", "-create", "-output", str(universal_output), str(arm_output), str(intel_output)]


def lipo_archs_command(binary: Path) -> list[str]:
    return ["lipo", "-archs", str(binary)]


def lipo_thin_command(binary: Path, arch: str, output: Path) -> list[str]:
    return ["lipo", "-thin", arch, "-output", str(output), str(binary)]


def codesign_cdhash(path: Path, runner: Callable[..., Any]) -> str | None:
    """The ``CDHash=`` a signature reports for ``path``, or None when absent.

    The executable SHA-256 alone is not signature evidence; this is the signed
    code identity, printed on stderr by current macOS.
    """
    completed = runner(["codesign", "-dv", "--verbose=4", str(path)], capture_output=True, text=True)
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


def _layout(output: Path) -> dict[str, Path]:
    app = output / APP_NAME
    return {
        "output": output,
        "app": app,
        "contents": app / "Contents",
        "macos": app / "Contents" / "MacOS",
        "resources": app / "Contents" / "Resources",
        "executable": app / "Contents" / "MacOS" / EXECUTABLE_NAME,
        "info_plist": app / "Contents" / "Info.plist",
        "icon": app / "Contents" / "Resources" / ICON_NAME,
        "staging": output / ".host-build",
        "archive": output / ARCHIVE_NAME,
    }


def _verify_universal(executable: Path, runner: Callable[..., Any]) -> None:
    completed = runner(lipo_archs_command(executable), capture_output=True, text=True)
    archs = set(str(completed.stdout or "").split())
    missing = [arch for arch in ARCHITECTURES if arch not in archs]
    if missing:
        raise BuildError(f"universal binary is missing architectures: {', '.join(missing)}")


def _per_arch_cdhashes(
    executable: Path, staging: Path, runner: Callable[..., Any]
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for arch in ARCHITECTURES:
        thin = staging / f"{EXECUTABLE_NAME}.{arch}"
        runner(lipo_thin_command(executable, arch, thin), check=True)
        cdhash = codesign_cdhash(thin, runner)
        if not cdhash:
            raise BuildError(f"no CDHash reported for the {arch} slice")
        hashes[arch] = cdhash
    return hashes


def _normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
    # No ownership state and no local identity: uid/gid zeroed and user/group
    # names cleared, while the ordinary mode bits are preserved as required.
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _archive_app(app: Path, archive: Path) -> None:
    """Write ``archive`` holding exactly the app subtree, ordinary files/dirs only."""
    with tarfile.open(archive, "w:gz") as tar:

        def add(entry: Path) -> None:
            if entry.is_symlink():
                raise BuildError(f"refusing to archive a symlink: {entry}")
            arcname = str(entry.relative_to(app.parent))
            info = _normalize(tar.gettarinfo(str(entry), arcname=arcname))
            if info.isdir():
                tar.addfile(info)
            else:
                with entry.open("rb") as handle:
                    tar.addfile(info, handle)

        for root, dirs, files in os.walk(app, followlinks=False):
            root_path = Path(root)
            dirs.sort()
            files.sort()
            add(root_path)
            for name in files:
                add(root_path / name)


def _re_extract_and_verify(archive: Path, runner: Callable[..., Any]) -> None:
    """Unpack the archive into a fresh scratch dir and strict-verify the app."""
    scratch = Path(tempfile.mkdtemp(prefix="ciaobot-server-host-verify-"))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                name = member.name
                if name.startswith("/") or ".." in Path(name).parts:
                    raise BuildError(f"archive member has an unsafe path: {name}")
                if member.issym() or member.islnk():
                    raise BuildError(f"archive member is a link: {name}")
            tar.extractall(scratch, filter="fully_trusted")
        extracted = scratch / APP_NAME
        runner(["codesign", "--verify", "--strict", str(extracted)], check=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def build(
    output: Path,
    *,
    platform_name: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    icon_loader: Callable[[], bytes] | None = None,
) -> dict[str, Any]:
    """Compile, sign, archive and re-verify the host under a new ``output`` dir.

    Platform is validated before any filesystem write, and an existing output
    (file, directory, or dangling symlink) is refused untouched. The compiler,
    signer and archiver are injected so offline tests can prove ordering and
    failure behaviour without launching anything.
    """
    effective_platform = sys.platform if platform_name is None else platform_name
    if effective_platform != "darwin":
        raise BuildError("the Ciaobot Server host requires macOS; Windows and Linux are unchanged")

    output = output.expanduser()
    _refuse_existing(output)
    output = output.resolve()

    paths = _layout(output)
    paths["output"].mkdir(parents=True, exist_ok=False)
    try:
        if icon_loader is None:
            icon = ICON_SOURCE.read_bytes()
        else:
            icon = icon_loader()
        if not icon:
            raise BuildError(f"tracked icon is empty: {ICON_SOURCE}")

        # Universal assembly FIRST: thin binaries live in a staging directory
        # outside the .app, never inside it.
        paths["staging"].mkdir()
        arm_path = paths["staging"] / f"{EXECUTABLE_NAME}.arm64"
        intel_path = paths["staging"] / f"{EXECUTABLE_NAME}.x86_64"
        commands = compile_commands(HOST_SOURCE, arm_path, intel_path)
        runner(commands[0], check=True)
        runner(commands[1], check=True)
        paths["macos"].mkdir(parents=True)
        runner(lipo_create_command(arm_path, intel_path, paths["executable"]), check=True)

        # Bundle metadata and resources are all in place before the one signature.
        paths["resources"].mkdir()
        paths["icon"].write_bytes(icon)
        with paths["info_plist"].open("wb") as handle:
            plistlib.dump(bundle_info(), handle)

        runner(["codesign", "--force", "--sign", "-", str(paths["app"])], check=True)
        runner(["codesign", "--verify", "--strict", str(paths["app"])], check=True)
        _verify_universal(paths["executable"], runner)
        per_arch = _per_arch_cdhashes(paths["executable"], paths["staging"], runner)

        # Nothing changes the sealed bundle after this point.
        shutil.rmtree(paths["staging"])
        _archive_app(paths["app"], paths["archive"])
        _re_extract_and_verify(paths["archive"], runner)

        executable_digest = hashlib.sha256(paths["executable"].read_bytes()).hexdigest()
        archive_bytes = paths["archive"].read_bytes()
    except BaseException:
        # Remove only the directory this invocation just created.
        shutil.rmtree(paths["output"], ignore_errors=True)
        raise

    return {
        "paths": {
            "app": paths["app"],
            "executable": paths["executable"],
            "archive": paths["archive"],
        },
        "metadata": {
            "bundle_id": BUNDLE_ID,
            "host_revision": HOST_REVISION,
            "host_protocol": HOST_PROTOCOL_REVISION,
            "minimum_system_version": MACOS_DEPLOYMENT_TARGET,
            "architectures": list(ARCHITECTURES),
            "executable_sha256": executable_digest,
            "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "archive_size": len(archive_bytes),
            "per_arch_cdhashes": per_arch,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New build directory (must not exist)",
    )
    args = parser.parse_args(argv)

    try:
        result = build(args.output)
    except BuildError as exc:
        parser.error(str(exc))

    paths: dict[str, Path] = result["paths"]
    metadata: dict[str, Any] = result["metadata"]
    import json

    print(f"App: {paths['app']}")
    print(f"Archive: {paths['archive']}")
    print(json.dumps(metadata, indent=2, sort_keys=True))
    print("Built only. No launch agents, installed bundles, or permission requests were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
