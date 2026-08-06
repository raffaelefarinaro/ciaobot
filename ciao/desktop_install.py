"""First install of the Tauri desktop app straight from a GitHub release.

The Homebrew cask this replaces downloads a DMG, and Homebrew — like a browser —
tags what it downloads with ``com.apple.quarantine``. Gatekeeper assesses
quarantined bundles on first launch, and because ``Ciaobot.app`` is ad-hoc signed
(``signingIdentity: "-"`` in ``desktop/src-tauri/tauri.conf.json``, no Developer
ID, no notarization) that assessment fails and macOS sends the user to System
Settings -> Privacy & Security -> "Open Anyway".

Downloading the same bundle from here avoids that entirely: quarantine is applied
by the downloading *application*, and ``urllib`` is not one of the applications
that sets it. No quarantine attribute means Gatekeeper never assesses, so the app
launches straight away. This is the same reason unsigned Homebrew *formulae*
install without a prompt while casks prompt.

That trade is deliberate but it moves a real security boundary. Gatekeeper's
notarization check is what would otherwise stand between a tampered release and
``/Applications``, so the minisign signature verified here replaces it and every
failure path below must refuse to install. There is no "warn and continue".

Only first install lives here. Updates go through the Tauri updater already wired
up in ``desktop/src-tauri/src/lib.rs``, which verifies the same minisign key and,
because an app is allowed to rewrite its own bundle, does not trip the macOS 14+
App Management prompt that a CLI overwriting somebody else's app would.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from ciao import desktop_build
from ciao.package_version import (
    _github_repo,
    _release_page_request,
    package_status,
)

APP_BUNDLE_NAME = desktop_build.APP_BUNDLE_NAME

# The updater public key, byte-for-byte as it appears under
# ``plugins.updater.pubkey`` in desktop/src-tauri/tauri.conf.json. It is
# duplicated here because tauri.conf.json is not packaged into the wheel;
# ``tests/test_desktop_install.py`` fails if the two ever drift apart, because a
# stale copy here would reject every genuine release and a wrong one would
# accept a foreign signer.
MINISIGN_PUBKEY_B64 = (
    "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDlBRTkzOTQwMUU3MjUyODMKUldTRFVuSWVRRG5w"
    "bW5OSmlUakxtTjZYT1ZGcWduMUEwRVh2VFZHN0FKSVpYSnhoeUZOOW9zeG0K"
)

# Minisign wire format: a 2-byte algorithm tag, an 8-byte key id, then the key
# or signature itself. ``Ed`` signs the payload directly; ``ED`` signs a
# BLAKE2b-512 hash of it. Tauri emits ``ED``, but both are accepted so a future
# signer change does not silently break installs.
_ALG_PURE = b"Ed"
_ALG_PREHASHED = b"ED"
_KEY_ID = slice(2, 10)
_BODY = slice(10, None)

# Large enough for a ~13 MB universal bundle on a slow connection, small enough
# that a hung mirror fails the command instead of the user's patience.
DOWNLOAD_TIMEOUT_S = 300.0
EXTRACT_TIMEOUT_S = 300.0


class SignatureError(Exception):
    """The downloaded bundle is not the one this key signed."""


class InstallError(Exception):
    """The install could not proceed. Nothing was written to the app directory."""


def _decode_signed_file(text: str, *, what: str) -> list[str]:
    """Return the non-empty lines of a base64-wrapped minisign file.

    Tauri stores both the public key and the ``.sig`` as base64 of the whole
    minisign *file*, not of its payload line, so each has to be unwrapped once
    before the usual comment/base64 line structure appears.
    """

    try:
        decoded = base64.b64decode(text.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise SignatureError(f"{what} is not valid base64: {exc}") from exc
    lines = [line for line in decoded.splitlines() if line.strip()]
    if not lines:
        raise SignatureError(f"{what} is empty")
    return lines


def _decode_body(line: str, *, what: str, expected: int) -> bytes:
    try:
        raw = base64.b64decode(line.strip(), validate=True)
    except ValueError as exc:
        raise SignatureError(f"{what} is not valid base64: {exc}") from exc
    if len(raw) != expected:
        raise SignatureError(f"{what} is {len(raw)} bytes, expected {expected}")
    return raw


def verify_minisign(
    payload: bytes,
    signature: str,
    *,
    pubkey: str = MINISIGN_PUBKEY_B64,
) -> str:
    """Verify ``payload`` against a Tauri-style minisign ``signature``.

    Returns the signature's trusted comment. Raises ``SignatureError`` for every
    failure, including a malformed key or signature file, so a caller cannot
    mistake "could not check" for "checked and fine".
    """

    # Imported lazily: cryptography pulls in a compiled extension, and the
    # module-level import would cost every `ciao` invocation for a code path
    # only the install command reaches.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key_lines = _decode_signed_file(pubkey, what="the updater public key")
    # The key file is a comment line followed by the key; take the last line so
    # a multi-line or comment-free file both work.
    key_raw = _decode_body(key_lines[-1], what="the updater public key", expected=42)

    sig_lines = _decode_signed_file(signature, what="the bundle signature")
    if len(sig_lines) < 2:
        raise SignatureError("the bundle signature has no signature line")
    sig_raw = _decode_body(sig_lines[1], what="the bundle signature", expected=74)

    if sig_raw[_KEY_ID] != key_raw[_KEY_ID]:
        raise SignatureError(
            "the bundle was signed by a different key "
            f"({sig_raw[_KEY_ID].hex()}, expected {key_raw[_KEY_ID].hex()})"
        )

    algorithm = bytes(sig_raw[:2])
    if algorithm == _ALG_PREHASHED:
        signed = hashlib.blake2b(payload, digest_size=64).digest()
    elif algorithm == _ALG_PURE:
        signed = payload
    else:
        raise SignatureError(f"unsupported signature algorithm {algorithm!r}")

    verifier = Ed25519PublicKey.from_public_bytes(key_raw[_BODY])
    try:
        verifier.verify(sig_raw[_BODY], signed)
    except InvalidSignature as exc:
        raise SignatureError("the bundle does not match its signature") from exc

    # Minisign's line order is fixed: untrusted comment, signature, trusted
    # comment, global signature. The trusted comment carries the timestamp and
    # filename and is covered by that global signature over the payload
    # signature concatenated with it. Checking it costs nothing and closes the
    # gap where those fields could be rewritten after the fact.
    if len(sig_lines) < 3 or not sig_lines[2].startswith("trusted comment:"):
        return ""
    trusted = sig_lines[2].removeprefix("trusted comment:").lstrip()
    if len(sig_lines) < 4:
        raise SignatureError("the bundle signature has a trusted comment but no global signature")
    global_sig = _decode_body(sig_lines[3], what="the global signature", expected=64)
    try:
        verifier.verify(global_sig, sig_raw[_BODY] + trusted.encode("utf-8"))
    except InvalidSignature as exc:
        raise SignatureError("the signature's trusted comment has been altered") from exc
    return trusted


def asset_names(version: str) -> tuple[str, str]:
    """Bundle and signature asset names for ``version`` (no leading ``v``)."""

    bundle = f"Ciaobot_{version}_universal.app.tar.gz"
    return bundle, f"{bundle}.sig"


def asset_url(version: str, name: str, *, repo: str | None = None) -> str:
    repo = (repo or _github_repo()).strip("/")
    return f"https://github.com/{repo}/releases/download/v{version}/{name}"


def resolve_latest_version(
    *,
    repo: str | None = None,
    opener: Callable[..., AbstractContextManager[Any]] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> str:
    """Latest stable release version, via the same check the updater uses.

    Delegates to ``package_version.package_status``, which follows github.com's
    ``/releases/latest`` redirect rather than the REST API (no unauthenticated
    per-IP rate limit) and already turns 403/429 into a rate-limit message.
    """

    status = package_status(repo=repo, opener=opener, timeout=timeout)
    error = str(status.get("error") or "").strip()
    version = str(status.get("latest_version") or "").strip()
    if error:
        raise InstallError(f"could not find the latest release: {error}")
    if not version:
        raise InstallError("could not determine the latest release")
    return version


def _download(
    url: str,
    *,
    opener: Callable[..., AbstractContextManager[Any]],
    timeout: float,
) -> bytes:
    try:
        with opener(_release_page_request(url), timeout=timeout) as response:
            # The opener is injectable for tests, so its read() is untyped.
            return bytes(response.read())
    except urllib.error.HTTPError as exc:
        raise InstallError(f"{url} returned HTTP {exc.code}: {exc.reason}") from exc
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise InstallError(f"could not download {url}: {exc}") from exc


def install_desktop_app(
    *,
    app_dir: Path,
    version: str = "",
    repo: str | None = None,
    opener: Callable[..., AbstractContextManager[Any]] = urllib.request.urlopen,
    runner: desktop_build.Runner | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_S,
    pubkey: str = MINISIGN_PUBKEY_B64,
    open_after_install: bool = True,
) -> dict[str, Any]:
    """Download, verify and install ``Ciaobot.app`` into ``app_dir``.

    Returns a summary dict with the installed version, path and trusted comment.
    Raises ``InstallError`` or ``SignatureError`` without having written
    anything into ``app_dir`` except a staging directory it cleans up.
    """

    runner = runner or desktop_build.run_step
    app_dir = Path(app_dir).expanduser()
    destination = app_dir / APP_BUNDLE_NAME

    # Refusing rather than replacing is not politeness: since macOS 14, writing
    # over an app bundle this process did not create needs App Management
    # approval, so an overwrite would either raise a TCC prompt or fail partway
    # through and leave a gutted bundle. Updates belong to the in-app updater.
    if destination.exists():
        # Distinguish our app from a browser-installed PWA of the same name.
        # Both Safari and Chrome write "Ciaobot.app" when the user adds the PWA
        # to the Dock, and telling them to run `ciao desktop uninstall` was a
        # dead end: that command refuses to delete a bundle it did not create.
        ours = (
            destination / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME
        ).is_file()
        if ours:
            raise InstallError(
                f"{destination} already exists. Update it from the app "
                "(menu bar -> Update) rather than reinstalling over it, or run "
                "`ciao desktop uninstall` first."
            )
        raise InstallError(
            f"{destination} exists but is not the Ciaobot desktop app -- most "
            "likely a browser-installed shortcut of the same name. Move it to "
            "the Trash in Finder (or pass --app-dir to install elsewhere), "
            "then run `ciao desktop install` again."
        )
    # macOS does not create ~/Applications, and _default_app_dir() falls back to
    # it on a non-admin account. The launcher this replaced made it with
    # mkdir(parents=True); without that, install failed on exactly the accounts
    # that cannot write /Applications.
    try:
        app_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallError(f"could not create {app_dir}: {exc}") from exc

    version = (version or "").strip().removeprefix("v")
    if not version:
        version = resolve_latest_version(repo=repo, opener=opener)

    bundle_name, signature_name = asset_names(version)
    bundle_url = asset_url(version, bundle_name, repo=repo)
    payload = _download(bundle_url, opener=opener, timeout=timeout)
    signature = _download(
        asset_url(version, signature_name, repo=repo), opener=opener, timeout=timeout
    ).decode("utf-8", errors="replace")

    # Before anything touches app_dir.
    trusted = verify_minisign(payload, signature, pubkey=pubkey)

    # Extracted inside app_dir so the move into place is a rename on the same
    # filesystem, which is atomic; /tmp is frequently a different volume.
    #
    # Deliberately *not* desktop_build.install_staged_and_relaunch, even though
    # it does the same swap: it first quits any running instance, and its
    # pgrep pattern matches the bundle path suffix wherever that bundle lives.
    # Installing into one root while an app runs from another would therefore
    # quit the app the user is using. A first install has nothing to swap, so
    # the plain rename below is both simpler and safer.
    work = Path(tempfile.mkdtemp(prefix=".ciaobot-install-", dir=app_dir))
    try:
        archive = work / bundle_name
        archive.write_bytes(payload)
        # /usr/bin/tar rather than tarfile: this is the extraction path verified
        # to leave the bundle's ad-hoc signature reporting "valid on disk", and
        # bsdtar handles the extended attributes inside an .app correctly.
        result = runner(
            ["/usr/bin/tar", "-xzf", str(archive), "-C", str(work)],
            cwd=str(work),
            timeout=int(EXTRACT_TIMEOUT_S),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise InstallError(f"could not extract {bundle_name}: {detail}")
        extracted = work / APP_BUNDLE_NAME
        if not (extracted / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME).is_file():
            raise InstallError(
                f"{bundle_name} did not contain a usable {APP_BUNDLE_NAME}"
            )
        extracted.rename(destination)
    except OSError as exc:
        raise InstallError(f"could not install into {app_dir}: {exc}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # Setup installs the app before it has finished writing the LaunchAgent, and
    # the app starts the engine on launch, so it asks for the bundle to be left
    # closed rather than racing its own installer.
    opened = False
    if open_after_install:
        result = runner(["/usr/bin/open", "-a", str(destination)], cwd=str(app_dir), timeout=60)
        opened = result.returncode == 0
    return {
        "version": version,
        "path": str(destination),
        "source": bundle_url,
        "trusted_comment": trusted,
        "opened": opened,
    }


def uninstall_desktop_app(*, app_dir: Path) -> dict[str, Any]:
    """Remove ``Ciaobot.app`` from ``app_dir``.

    Needed because the bundle no longer comes from a Homebrew cask, so
    ``brew uninstall`` will not take it away.
    """

    app_dir = Path(app_dir).expanduser()
    destination = app_dir / APP_BUNDLE_NAME
    if not destination.exists():
        return {"removed": False, "path": str(destination)}
    if not (destination / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME).is_file():
        # A browser-installed PWA can also be called Ciaobot.app. Only the
        # Tauri bundle carries this executable, and deleting the wrong one is
        # not recoverable.
        raise InstallError(
            f"{destination} is not the Ciaobot desktop app "
            f"(no Contents/MacOS/{desktop_build.APP_EXECUTABLE_NAME}); leaving it alone"
        )
    try:
        shutil.rmtree(destination)
    except OSError as exc:
        raise InstallError(f"could not remove {destination}: {exc}") from exc
    return {"removed": True, "path": str(destination)}
