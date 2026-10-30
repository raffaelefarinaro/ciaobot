"""Build the universal Ciaobot Server host archive (#1009, #2328).

This is a build tool, not an installer. It compiles the small native host at
``native/server-host/ServerHost.swift`` for both macOS architectures, assembles
a ``Ciaobot Server.app`` bundle with the tracked PR119 icon, signs it once after
all resources are in place, verifies the signature and universal slices, and
writes ``ciaobot-server-host-macos-universal-v1.tar.gz``.

The default build is ad-hoc signed and unchanged. An explicit release build
(``--release`` with a Developer ID identity, team ID and notary keychain
profile) signs with hardened runtime, a secure timestamp and the tracked
entitlements, submits the app for notarization, staples the accepted ticket and
re-verifies the extracted app. Nothing is auto-detected: the caller names every
release input, and no environment variable changes the mode.

It writes only under the caller-provided new output directory. It never touches
``~/Applications``, launch agents, services, permissions, or a running engine,
and it never launches what it builds. Import-safe on Windows and Linux: the
macOS-only subprocesses are injected and never reached until ``build`` has
passed the platform check.

```sh
python scripts/build-server-host.py --output /tmp/ciaobot-server-host-build
python scripts/build-server-host.py --output /tmp/ciaobot-server-host-release \\
    --release --signing-identity "Developer ID Application: ..." \\
    --team-id ABCDE12345 --notary-profile ciaobot-notary
```
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
from typing import Any, Callable, NamedTuple

APP_NAME = "Ciaobot Server.app"
BUNDLE_ID = "local.ciaobot.server"
DISPLAY_NAME = "Ciaobot Server"
EXECUTABLE_NAME = "Ciaobot Server"
HOST_REVISION = 4
# An explicitly selected Developer ID/notarized build ships different signed
# bytes, so it gets its own host revision. The protocol does not change.
SIGNED_HOST_REVISION = 5
MACOS_DEPLOYMENT_TARGET = "13.0"
ARM_TARGET = f"arm64-apple-macosx{MACOS_DEPLOYMENT_TARGET}"
INTEL_TARGET = f"x86_64-apple-macosx{MACOS_DEPLOYMENT_TARGET}"

ICON_NAME = "CiaobotServer.icns"
ARCHIVE_NAME = "ciaobot-server-host-macos-universal-v1.tar.gz"

# The custom plist key the host-aware service contract will read (#1008 child B).
HOST_PROTOCOL_KEY = "CiaobotServerHostProtocol"
HOST_PROTOCOL_REVISION = 2

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

# The three folders macOS can gate behind a prompt (#1659). macOS shows the
# explanation line it is given: with no key for a folder, the prompt appears
# with an empty line. Each string says who asks, names the folder, and says
# the access is scoped to a workspace the user chose - it is not a blanket
# claim about the folder.
DESKTOP_FOLDER_USAGE = (
    "Ciaobot Server needs access to your Desktop folder only when you ask it "
    "to keep a Ciaobot workspace there."
)
DOCUMENTS_FOLDER_USAGE = (
    "Ciaobot Server needs access to your Documents folder only when you ask "
    "it to keep a Ciaobot workspace there."
)
DOWNLOADS_FOLDER_USAGE = (
    "Ciaobot Server needs access to your Downloads folder only when you ask "
    "it to keep a Ciaobot workspace there."
)

REPO = Path(__file__).resolve().parents[1]
HOST_SOURCE = REPO / "native" / "server-host" / "ServerHost.swift"
ENTITLEMENTS_SOURCE = REPO / "native" / "server-host" / "ServerHost.entitlements.plist"
ICON_SOURCE = REPO / "ciao" / "stock" / "deploy" / ICON_NAME

ARCHITECTURES = ("arm64", "x86_64")

# The release build applies exactly this entitlement set. Ad-hoc builds apply
# none, so the ad-hoc path keeps its existing signature.
RELEASE_ENTITLEMENTS: dict[str, object] = {
    "com.apple.security.automation.apple-events": True,
}

# A Developer ID designated requirement is stable across rebuilds and is the
# same on both slices: the bundle identifier, the Apple generic anchor, the
# Developer ID CA and leaf certificate extensions, and the expected team. The
# signing certificate's fingerprint may select the identity but never defines
# this logical identity.
DEVELOPER_ID_CA_EXTENSION = "1.2.840.113635.100.6.2.6"
DEVELOPER_ID_APPLICATION_EXTENSION = "1.2.840.113635.100.6.1.13"
TEAM_ID_PATTERN = re.compile(r"[A-Z0-9]{10}")

# The CodeDirectory option flag that marks a hardened-runtime signature
# (CS_RUNTIME). ``Runtime Version`` alone is not proof: the flag is.
CODE_DIRECTORY_RUNTIME_FLAG = 0x10000

# Notary statuses worth naming in an error. Anything else is reported as
# unrecognized, so a tool's arbitrary output text can never reach the user.
NOTARY_REPORTED_STATUSES = frozenset({"Invalid", "Rejected", "In Progress"})

# Bounded signed subprocesses. The notary wait is a network operation and gets
# its own, longer deadline.
SIGNED_COMMAND_TIMEOUT_SECONDS = 300
NOTARY_WAIT_TIMEOUT = "30m"
NOTARY_PROCESS_TIMEOUT_SECONDS = 1950


class BuildError(RuntimeError):
    """A refusal or failure that must not leave a partial build behind."""


class ReleaseSigning(NamedTuple):
    """Every explicit input of a Developer ID/notarized build.

    Frozen on purpose: nothing here is auto-detected or defaulted, and the
    caller names the signing identity, the expected team and the notarytool
    keychain profile.
    """

    identity: str
    team_id: str
    keychain_profile: str


def _validate_release_signing(signing: ReleaseSigning) -> None:
    """Refuse an incomplete or ad-hoc release configuration before any write.

    The messages never repeat the caller's identity or profile: those are
    sensitive inputs and must not reach an error or the CLI output.
    """
    identity = signing.identity
    if not identity.strip() or identity.startswith("-"):
        raise BuildError(
            "a release build needs a Developer ID identity; '-' is an ad-hoc signature"
        )
    if identity != identity.strip() or any(char in identity for char in "\r\n\t"):
        raise BuildError("refusing a malformed signing identity")
    if not TEAM_ID_PATTERN.fullmatch(signing.team_id):
        raise BuildError("the team ID must be ten uppercase letters or digits")
    profile = signing.keychain_profile
    if not profile.strip() or profile.startswith("-") or any(
        char in profile for char in "\r\n\t"
    ):
        raise BuildError("the notary keychain profile must be a plain profile name")
    if not ENTITLEMENTS_SOURCE.is_file():
        raise BuildError(f"the release entitlements are missing: {ENTITLEMENTS_SOURCE}")
    with ENTITLEMENTS_SOURCE.open("rb") as handle:
        entitlements = plistlib.load(handle)
    if entitlements != RELEASE_ENTITLEMENTS:
        raise BuildError(
            "the release entitlements must be exactly "
            f"{sorted(RELEASE_ENTITLEMENTS)}: {ENTITLEMENTS_SOURCE}"
        )


def designated_requirement(team_id: str) -> str:
    """The stable Developer ID designated requirement for this bundle and team."""
    if not TEAM_ID_PATTERN.fullmatch(team_id):
        raise BuildError("the team ID must be ten uppercase letters or digits")
    return (
        f'identifier "{BUNDLE_ID}"'
        " and anchor apple generic"
        f" and certificate 1[field.{DEVELOPER_ID_CA_EXTENSION}] /* exists */"
        f" and certificate leaf[field.{DEVELOPER_ID_APPLICATION_EXTENSION}] /* exists */"
        f' and certificate leaf[subject.OU] = "{team_id}"'
    )


def release_requirement(signing: ReleaseSigning) -> str:
    """The designated requirement set ``--requirements`` embeds when signing."""
    return f"=designated => {designated_requirement(signing.team_id)}"


def expected_requirement(signing: ReleaseSigning) -> str:
    """The single requirement ``--test-requirement`` enforces during verification.

    Verification takes a code requirement, not the ``=designated =>`` set that
    ``--requirements`` embeds at signing time. A leading ``=`` marks literal
    requirement source text (``codesign(1)``, SPECIFYING REQUIREMENTS).
    """
    return f"={designated_requirement(signing.team_id)}"


def verify_release_command(app: Path, signing: ReleaseSigning) -> list[str]:
    """Check ``app`` against the external expected requirement on every slice."""
    return [
        "codesign",
        "--verify",
        "--strict",
        # -R/--test-requirement is the verification seam; -r/--requirements is
        # the signing (embed) and display option and must not be used here.
        "--test-requirement",
        expected_requirement(signing),
        "--all-architectures",
        str(app),
    ]


def bundle_info(host_revision: int = HOST_REVISION) -> dict[str, object]:
    """The fixed bundle metadata, expressed as host revision rather than engine version."""
    return {
        "CFBundleName": DISPLAY_NAME,
        "CFBundleDisplayName": DISPLAY_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": ICON_NAME,
        "CFBundleVersion": str(host_revision),
        "CFBundleShortVersionString": str(host_revision),
        HOST_PROTOCOL_KEY: HOST_PROTOCOL_REVISION,
        "LSMinimumSystemVersion": MACOS_DEPLOYMENT_TARGET,
        # Accessory app: no Dock icon, no window, no desktop UI.
        "LSUIElement": True,
        "NSAppleEventsUsageDescription": APPLE_EVENTS_USAGE,
        "NSAccessibilityUsageDescription": ACCESSIBILITY_USAGE,
        "NSDesktopFolderUsageDescription": DESKTOP_FOLDER_USAGE,
        "NSDocumentsFolderUsageDescription": DOCUMENTS_FOLDER_USAGE,
        "NSDownloadsFolderUsageDescription": DOWNLOADS_FOLDER_USAGE,
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


def codesign_cdhash(path: Path, arch: str, runner: Callable[..., Any]) -> str | None:
    """The ``CDHash=`` the ``arch`` slice of ``path`` reports, or None when absent.

    The executable SHA-256 alone is not signature evidence; this is the signed
    code identity, printed on stderr by current macOS.
    """
    completed = runner(
        ["codesign", "-dv", "--verbose=4", "--arch", arch, str(path)],
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
    if os.path.lexists(output):
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
        "verify": output / ".host-verify",
        "notarize_zip": output / ".host-notarize.zip",
        "archive": output / ARCHIVE_NAME,
    }


def _verify_universal(executable: Path, runner: Callable[..., Any]) -> None:
    completed = runner(lipo_archs_command(executable), capture_output=True, text=True)
    archs = set(str(completed.stdout or "").split())
    missing = [arch for arch in ARCHITECTURES if arch not in archs]
    if missing:
        raise BuildError(f"universal binary is missing architectures: {', '.join(missing)}")


def _per_arch_cdhashes(executable: Path, runner: Callable[..., Any]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for arch in ARCHITECTURES:
        cdhash = codesign_cdhash(executable, arch, runner)
        if not cdhash:
            raise BuildError(f"no CDHash reported for the {arch} slice")
        hashes[arch] = cdhash
    return hashes


# --- release signing (Developer ID, hardened runtime, notarization) ---------


def _run_release(
    runner: Callable[..., Any],
    cmd: list[str],
    *,
    failure: str,
    timeout: int,
    check: bool = True,
) -> Any:
    """Run one bounded release tool; every failure becomes a sanitized error.

    The tool argv, its captured output and the original exception stay out of
    the message and out of the exception chain: the signing identity and the
    notary profile are caller-supplied values that must never reach an error or
    the CLI output.
    """
    try:
        return runner(cmd, check=check, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BuildError(
            f"{failure}: the tool did not finish within {timeout} seconds"
        ) from None
    except subprocess.CalledProcessError:
        raise BuildError(failure) from None
    except OSError:
        raise BuildError(failure) from None


def sign_release_command(app: Path, signing: ReleaseSigning) -> list[str]:
    """The one release signature: runtime, timestamp, entitlements, stable DR."""
    return [
        "codesign",
        "--force",
        "--sign",
        signing.identity,
        "--options",
        "runtime",
        "--timestamp",
        "--entitlements",
        str(ENTITLEMENTS_SOURCE),
        "--requirements",
        release_requirement(signing),
        str(app),
    ]


def notary_submit_command(zip_path: Path, signing: ReleaseSigning) -> list[str]:
    """Submit with the named keychain profile only - no credentials on the argv."""
    return [
        "xcrun",
        "notarytool",
        "submit",
        str(zip_path),
        "--keychain-profile",
        signing.keychain_profile,
        "--wait",
        "--timeout",
        NOTARY_WAIT_TIMEOUT,
        "--output-format",
        "json",
    ]


def staple_command(app: Path) -> list[str]:
    return ["xcrun", "stapler", "staple", str(app)]


def staple_validate_command(app: Path) -> list[str]:
    return ["xcrun", "stapler", "validate", str(app)]


def gatekeeper_assess_command(app: Path) -> list[str]:
    return ["spctl", "--assess", "--type", "execute", "--verbose=2", str(app)]


def _codesign_display_fields(
    path: Path, arch: str, runner: Callable[..., Any]
) -> dict[str, str]:
    """The ``codesign -dv`` fields of one slice, keyed by their printed name."""
    completed = _run_release(
        runner,
        ["codesign", "-dv", "--verbose=4", "--arch", arch, str(path)],
        failure=f"the {arch} slice's signature details could not be read",
        timeout=SIGNED_COMMAND_TIMEOUT_SECONDS,
    )
    fields: dict[str, str] = {}
    for line in f"{completed.stdout or ''}\n{completed.stderr or ''}".splitlines():
        # "CodeDirectory v=20500 size=400 flags=0x10000(runtime) ..." has no
        # single "Key=Value" shape, so keep the whole line under its name.
        if line.startswith("CodeDirectory"):
            fields.setdefault("CodeDirectory", line)
            continue
        key, separator, value = line.partition("=")
        if separator and key and key not in fields:
            fields[key] = value.strip()
    return fields


def _entitlements_of_slice(
    path: Path, arch: str, runner: Callable[..., Any]
) -> dict[str, object]:
    completed = _run_release(
        runner,
        ["codesign", "-d", "--entitlements", ":-", "--xml", "--arch", arch, str(path)],
        failure=f"the {arch} slice's entitlements could not be read",
        timeout=SIGNED_COMMAND_TIMEOUT_SECONDS,
    )
    text = completed.stdout or ""
    start = text.find("<?xml")
    if start < 0:
        start = text.find("<plist")
    if start < 0:
        raise BuildError(f"the {arch} slice reports no readable entitlements")
    try:
        document = plistlib.loads(text[start:].encode())
    except (plistlib.InvalidFileException, ValueError):
        raise BuildError(f"the {arch} slice's entitlements are not a plist") from None
    if not isinstance(document, dict):
        raise BuildError(f"the {arch} slice's entitlements are not a dictionary")
    return document


def _verify_exact_architectures(executable: Path, runner: Callable[..., Any]) -> None:
    completed = _run_release(
        runner,
        lipo_archs_command(executable),
        failure="the release architecture check failed",
        timeout=SIGNED_COMMAND_TIMEOUT_SECONDS,
    )
    archs = set(str(completed.stdout or "").split())
    if archs != set(ARCHITECTURES):
        # Fixed text only: the tool's architecture list is not echoed.
        raise BuildError(
            f"the release binary must carry exactly {' '.join(ARCHITECTURES)}"
        )


def _verify_release_bundle(
    app: Path,
    executable: Path,
    signing: ReleaseSigning,
    runner: Callable[..., Any],
    *,
    label: str,
) -> dict[str, str]:
    """Strict integrity, the external expected requirement, and both slices' facts."""
    _run_release(
        runner,
        ["codesign", "--verify", "--strict", str(app)],
        failure=f"{label} failed strict signature verification",
        timeout=SIGNED_COMMAND_TIMEOUT_SECONDS,
    )
    _run_release(
        runner,
        verify_release_command(app, signing),
        failure=f"{label} does not satisfy the expected requirement",
        timeout=SIGNED_COMMAND_TIMEOUT_SECONDS,
    )
    _verify_exact_architectures(executable, runner)

    hashes: dict[str, str] = {}
    for arch in ARCHITECTURES:
        fields = _codesign_display_fields(executable, arch, runner)
        # Every message below is fixed text naming only our own expectation: the
        # native tool's identifier, team, flags and entitlement keys are never
        # echoed, so no raw field (or arbitrary output) reaches an error.
        if fields.get("Identifier") != BUNDLE_ID:
            raise BuildError(
                f"the {arch} slice's bundle identifier is not {BUNDLE_ID}"
            )
        if fields.get("Signature", "").lower() == "adhoc":
            raise BuildError(f"the {arch} slice is ad-hoc signed, not Developer ID signed")
        if fields.get("TeamIdentifier") != signing.team_id:
            raise BuildError(
                f"the {arch} slice's signing team is not the expected team"
            )
        # Hardened runtime is a CodeDirectory option flag, not a version string.
        code_directory = fields.get("CodeDirectory")
        if not code_directory:
            raise BuildError(f"the {arch} slice reports no CodeDirectory")
        flags_match = re.search(r"flags=0x([0-9a-fA-F]+)", code_directory)
        if flags_match is None:
            raise BuildError(f"the {arch} slice's CodeDirectory reports no option flags")
        if not int(flags_match.group(1), 16) & CODE_DIRECTORY_RUNTIME_FLAG:
            raise BuildError(
                f"the {arch} slice's CodeDirectory does not carry the hardened-runtime "
                f"option flag (0x{CODE_DIRECTORY_RUNTIME_FLAG:x})"
            )
        if not fields.get("Runtime Version"):
            raise BuildError(f"the {arch} slice carries no hardened runtime version")
        if not fields.get("Timestamp"):
            raise BuildError(f"the {arch} slice carries no secure timestamp")
        cdhash = fields.get("CDHash")
        if not cdhash:
            raise BuildError(f"no CDHash reported for the {arch} slice")
        entitlements = _entitlements_of_slice(executable, arch, runner)
        if entitlements != RELEASE_ENTITLEMENTS:
            raise BuildError(f"the {arch} slice's entitlements are not the release set")
        hashes[arch] = cdhash
    return hashes


def _submit_for_notarization(
    zip_path: Path, signing: ReleaseSigning, runner: Callable[..., Any]
) -> None:
    """Require exactly ``Accepted``; never echo submission arguments or output."""
    completed = _run_release(
        runner,
        notary_submit_command(zip_path, signing),
        failure="notarytool could not be run",
        timeout=NOTARY_PROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise BuildError(f"notarytool failed with exit status {completed.returncode}")
    try:
        payload = json.loads(str(completed.stdout or ""))
    except ValueError:
        raise BuildError("notarytool did not return a JSON status") from None
    if not isinstance(payload, dict):
        raise BuildError("notarytool returned a JSON value that is not a status object")
    status = payload.get("status")
    if status != "Accepted":
        # Type-check before membership: a JSON list or object is valid JSON but
        # unhashable, and must become a fixed sanitized error, never a TypeError.
        if isinstance(status, str) and status in NOTARY_REPORTED_STATUSES:
            reported = status
        else:
            reported = "an unrecognized status"
        raise BuildError(f"notarization was not accepted: {reported}")


def _sign_release_app(
    paths: dict[str, Path], signing: ReleaseSigning, runner: Callable[..., Any]
) -> dict[str, str]:
    """Sign once, verify, notarize, staple and validate. Never sign after stapling."""
    app = paths["app"]
    executable = paths["executable"]

    _run_release(
        runner,
        sign_release_command(app, signing),
        failure="the release signature could not be applied",
        timeout=SIGNED_COMMAND_TIMEOUT_SECONDS,
    )
    per_arch = _verify_release_bundle(
        app, executable, signing, runner, label="the signed app"
    )

    zip_path = paths["notarize_zip"]
    try:
        _run_release(
            runner,
            ["ditto", "-c", "-k", "--keepParent", str(app), str(zip_path)],
            failure="the app could not be packaged for notarization",
            timeout=SIGNED_COMMAND_TIMEOUT_SECONDS,
        )
        _submit_for_notarization(zip_path, signing, runner)
    finally:
        zip_path.unlink(missing_ok=True)

    _run_release(
        runner,
        staple_command(app),
        failure="the notarization ticket could not be stapled",
        timeout=SIGNED_COMMAND_TIMEOUT_SECONDS,
    )
    _run_release(
        runner,
        staple_validate_command(app),
        failure="the stapled ticket could not be validated",
        timeout=SIGNED_COMMAND_TIMEOUT_SECONDS,
    )
    return per_arch


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
            for name in dirs:
                # os.walk lists a symlinked directory here without descending
                # into it, so refuse it explicitly instead of dropping it.
                if (root_path / name).is_symlink():
                    raise BuildError(f"refusing to archive a symlink: {root_path / name}")
            for name in files:
                add(root_path / name)


def _re_extract_and_verify(
    archive: Path,
    scratch: Path,
    runner: Callable[..., Any],
    release_signing: ReleaseSigning | None = None,
) -> None:
    """Unpack the archive into the fresh ``scratch`` dir and verify the app there.

    The ``data`` filter refuses absolute paths, ``..`` traversal and special
    files; links are refused outright because the archive holds none. A release
    build repeats the signature, expected-requirement, runtime/entitlement,
    staple and Gatekeeper checks on the extracted app, independently of the one
    it just built.
    """
    scratch.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if member.issym() or member.islnk():
                    raise BuildError(f"archive member is a link: {member.name}")
            try:
                tar.extractall(scratch, filter="data")
            except tarfile.FilterError as exc:
                raise BuildError(f"archive member is unsafe: {exc}") from exc
        extracted = scratch / APP_NAME
        if release_signing is None:
            runner(["codesign", "--verify", "--strict", str(extracted)], check=True)
        else:
            executable = extracted / "Contents" / "MacOS" / EXECUTABLE_NAME
            _verify_release_bundle(
                extracted, executable, release_signing, runner, label="the extracted app"
            )
            _run_release(
                runner,
                staple_validate_command(extracted),
                failure="the extracted app's stapled ticket could not be validated",
                timeout=SIGNED_COMMAND_TIMEOUT_SECONDS,
            )
            _run_release(
                runner,
                gatekeeper_assess_command(extracted),
                failure="Gatekeeper rejected the extracted app",
                timeout=SIGNED_COMMAND_TIMEOUT_SECONDS,
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def build(
    output: Path,
    *,
    platform_name: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    release_signing: ReleaseSigning | None = None,
) -> dict[str, Any]:
    """Compile, sign, archive and re-verify the host under a new ``output`` dir.

    Platform is validated before any filesystem write, and an existing output
    (file, directory, or dangling symlink) is refused untouched. The compiler,
    signer and archiver are injected so offline tests can prove ordering and
    failure behaviour without launching anything.

    With ``release_signing`` the build is an explicit Developer ID/notarized
    one: hardened runtime, secure timestamp, the tracked entitlements, a stable
    designated requirement, a notary submission that must return ``Accepted``,
    a stapled and validated ticket, and a re-verified extracted app. Without
    it, the default ad-hoc build is unchanged.
    """
    effective_platform = sys.platform if platform_name is None else platform_name
    if effective_platform != "darwin":
        raise BuildError("the Ciaobot Server host requires macOS; Windows and Linux are unchanged")
    if sys.version_info < (3, 12):
        # The archive re-verification needs tarfile's ``filter="data"``, which
        # the macOS Command Line Tools' python3 (3.9) does not have.
        raise BuildError("the builder requires Python 3.12+; run it with the Ciaobot venv's python")
    if release_signing is not None:
        # Every release input is validated before the output directory exists.
        _validate_release_signing(release_signing)

    output = output.expanduser()
    _refuse_existing(output)
    output = output.resolve()

    paths = _layout(output)
    paths["output"].mkdir(parents=True, exist_ok=False)
    try:
        icon = ICON_SOURCE.read_bytes()
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
        shutil.rmtree(paths["staging"])

        # Bundle metadata and resources are all in place before the one signature.
        paths["resources"].mkdir()
        paths["icon"].write_bytes(icon)
        revision = HOST_REVISION if release_signing is None else SIGNED_HOST_REVISION
        with paths["info_plist"].open("wb") as handle:
            plistlib.dump(bundle_info(revision), handle)

        if release_signing is None:
            runner(["codesign", "--force", "--sign", "-", str(paths["app"])], check=True)
            runner(["codesign", "--verify", "--strict", str(paths["app"])], check=True)
            _verify_universal(paths["executable"], runner)
            per_arch = _per_arch_cdhashes(paths["executable"], runner)
        else:
            per_arch = _sign_release_app(paths, release_signing, runner)

        # Nothing changes the sealed bundle after this point.
        _archive_app(paths["app"], paths["archive"])
        _re_extract_and_verify(
            paths["archive"], paths["verify"], runner, release_signing=release_signing
        )

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
            "host_revision": revision,
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
    parser.add_argument(
        "--release",
        action="store_true",
        help="Explicit Developer ID, hardened-runtime and notarized build",
    )
    parser.add_argument(
        "--signing-identity",
        metavar="VALUE",
        help="Developer ID signing identity (never '-': ad-hoc is the default build)",
    )
    parser.add_argument(
        "--team-id",
        metavar="VALUE",
        help="Expected ten-character Apple team ID of the signing certificate",
    )
    parser.add_argument(
        "--notary-profile",
        metavar="NAME",
        help="notarytool keychain profile name holding the notary credentials",
    )
    args = parser.parse_args(argv)

    release_values = (args.signing_identity, args.team_id, args.notary_profile)
    release_signing: ReleaseSigning | None = None
    if not args.release:
        if any(value is not None for value in release_values):
            parser.error(
                "--signing-identity, --team-id and --notary-profile require --release"
            )
    else:
        if any(value is None for value in release_values):
            parser.error(
                "--release requires --signing-identity, --team-id and --notary-profile"
            )
        release_signing = ReleaseSigning(
            identity=str(args.signing_identity),
            team_id=str(args.team_id),
            keychain_profile=str(args.notary_profile),
        )

    try:
        result = build(args.output, release_signing=release_signing)
    except BuildError as exc:
        parser.error(str(exc))

    paths: dict[str, Path] = result["paths"]
    metadata: dict[str, Any] = result["metadata"]
    print(f"App: {paths['app']}")
    print(f"Archive: {paths['archive']}")
    print(json.dumps(metadata, indent=2, sort_keys=True))
    print("Built only. No launch agents, installed bundles, or permission requests were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
