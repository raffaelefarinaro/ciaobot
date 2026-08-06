"""Quarantine-free first install of Ciaobot.app from a GitHub release.

The signature check here is not a nicety: installing outside a Homebrew cask
means the download carries no quarantine flag, so Gatekeeper never assesses the
bundle and Apple's notarization check is not what guards it. These tests pin the
"fails closed" contract that replaces it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from ciao import desktop_build, desktop_install
from ciao.desktop_install import InstallError, SignatureError, verify_minisign

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- minisign fixtures -------------------------------------------------------
#
# A real keypair is generated per test rather than checking in a signed 13 MB
# bundle, so the tests stay offline and can also produce *invalid* material.


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def _pubkey_file(public, *, key_id: bytes = b"\x01\x02\x03\x04\x05\x06\x07\x08") -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = public.public_bytes(Encoding.Raw, PublicFormat.Raw)
    body = base64.b64encode(b"Ed" + key_id + raw).decode()
    text = f"untrusted comment: minisign public key\n{body}\n"
    return base64.b64encode(text.encode()).decode()


def _sig_file(
    private,
    payload: bytes,
    *,
    key_id: bytes = b"\x01\x02\x03\x04\x05\x06\x07\x08",
    prehashed: bool = True,
    trusted: str = "timestamp:1\tfile:Ciaobot.app.tar.gz",
) -> str:
    algorithm = b"ED" if prehashed else b"Ed"
    signed = hashlib.blake2b(payload, digest_size=64).digest() if prehashed else payload
    signature = private.sign(signed)
    body = base64.b64encode(algorithm + key_id + signature).decode()
    global_sig = base64.b64encode(private.sign(signature + trusted.encode())).decode()
    text = (
        "untrusted comment: signature from tauri secret key\n"
        f"{body}\ntrusted comment: {trusted}\n{global_sig}\n"
    )
    return base64.b64encode(text.encode()).decode()


def test_the_packaged_pubkey_matches_the_one_tauri_signs_with() -> None:
    """A drifted copy would reject every genuine release, or trust a stranger.

    tauri.conf.json is not packaged into the wheel, so the constant cannot be
    read from it at runtime; this is the only thing keeping the two in step.
    """
    config = REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
    if not config.exists():  # pragma: no cover - running from an installed wheel
        pytest.skip("desktop/src-tauri/tauri.conf.json is not present")
    pubkey = json.loads(config.read_text())["plugins"]["updater"]["pubkey"]
    assert pubkey == desktop_install.MINISIGN_PUBKEY_B64


def test_a_genuine_signature_verifies_and_returns_its_trusted_comment() -> None:
    private, public = _keypair()
    payload = b"a tarball"
    trusted = verify_minisign(payload, _sig_file(private, payload), pubkey=_pubkey_file(public))
    assert trusted == "timestamp:1\tfile:Ciaobot.app.tar.gz"


def test_a_pure_ed_signature_also_verifies() -> None:
    """Tauri emits prehashed ``ED``; accepting ``Ed`` too keeps a signer change
    from silently breaking installs."""
    private, public = _keypair()
    payload = b"a tarball"
    signature = _sig_file(private, payload, prehashed=False)
    assert verify_minisign(payload, signature, pubkey=_pubkey_file(public))


def test_a_tampered_payload_is_refused() -> None:
    private, public = _keypair()
    signature = _sig_file(private, b"the signed bytes")
    with pytest.raises(SignatureError, match="does not match its signature"):
        verify_minisign(b"the served bytes", signature, pubkey=_pubkey_file(public))


def test_a_signature_from_another_key_is_refused_by_key_id() -> None:
    signer, _ = _keypair()
    _, expected_public = _keypair()
    payload = b"a tarball"
    signature = _sig_file(signer, payload, key_id=b"\x09\x09\x09\x09\x09\x09\x09\x09")
    with pytest.raises(SignatureError, match="different key"):
        verify_minisign(payload, signature, pubkey=_pubkey_file(expected_public))


def test_a_signature_from_another_key_with_a_forged_key_id_is_still_refused() -> None:
    """The key id is a hint, not a credential; the Ed25519 check is the barrier."""
    signer, _ = _keypair()
    _, expected_public = _keypair()
    payload = b"a tarball"
    with pytest.raises(SignatureError, match="does not match its signature"):
        verify_minisign(payload, _sig_file(signer, payload), pubkey=_pubkey_file(expected_public))


def test_an_altered_trusted_comment_is_refused() -> None:
    private, public = _keypair()
    payload = b"a tarball"
    signature = base64.b64decode(_sig_file(private, payload)).decode()
    tampered = signature.replace("file:Ciaobot.app.tar.gz", "file:something-else.tar.gz")
    with pytest.raises(SignatureError, match="trusted comment has been altered"):
        verify_minisign(
            payload,
            base64.b64encode(tampered.encode()).decode(),
            pubkey=_pubkey_file(public),
        )


@pytest.mark.parametrize(
    "signature",
    ["", "not base64 at all!!", base64.b64encode(b"untrusted comment: only\n").decode()],
    ids=["empty", "not-base64", "no-signature-line"],
)
def test_malformed_signatures_are_refused_rather_than_skipped(signature: str) -> None:
    _, public = _keypair()
    with pytest.raises(SignatureError):
        verify_minisign(b"a tarball", signature, pubkey=_pubkey_file(public))


# --- install -----------------------------------------------------------------


def _fake_bundle_tarball(tmp_path: Path) -> bytes:
    """A minimal Ciaobot.app tarball shaped like the release artifact."""
    staging = tmp_path / "staging"
    macos = staging / desktop_install.APP_BUNDLE_NAME / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / desktop_build.APP_EXECUTABLE_NAME).write_bytes(b"\xcf\xfa\xed\xfe pretend Mach-O")
    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staging / desktop_install.APP_BUNDLE_NAME, arcname=desktop_install.APP_BUNDLE_NAME)
    return archive.read_bytes()


class FakeRelease:
    """Serves release assets from memory, standing in for urlopen."""

    def __init__(self, payload: bytes, signature: str) -> None:
        self.payload = payload
        self.signature = signature
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):  # noqa: ARG002 - urlopen signature
        url = getattr(request, "full_url", str(request))
        self.urls.append(url)
        body = self.signature.encode() if url.endswith(".sig") else self.payload
        return _Response(body)


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc) -> None:
        return None


def _runner(calls: list[list[str]]):
    """Runs tar for real (it is what preserves the bundle) but never opens an app."""

    def run(args, *, cwd, timeout):
        calls.append(list(args))
        if args[0].endswith("open"):
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return desktop_build.run_step(args, cwd=cwd, timeout=timeout)

    return run


def _install(tmp_path: Path, app_dir: Path, *, sign_with_wrong_key: bool = False, **kwargs):
    private, public = _keypair()
    payload = _fake_bundle_tarball(tmp_path)
    signer = _keypair()[0] if sign_with_wrong_key else private
    release = FakeRelease(payload, _sig_file(signer, payload))
    calls: list[list[str]] = []
    result = desktop_install.install_desktop_app(
        app_dir=app_dir,
        version="9.9.9",
        opener=release,
        runner=_runner(calls),
        pubkey=_pubkey_file(public),
        **kwargs,
    )
    return result, release, calls


def test_install_places_the_bundle_and_opens_it(tmp_path: Path) -> None:
    app_dir = tmp_path / "Applications"
    app_dir.mkdir()
    result, release, calls = _install(tmp_path, app_dir)

    installed = app_dir / desktop_install.APP_BUNDLE_NAME
    assert installed.is_dir()
    assert (installed / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME).is_file()
    assert result["version"] == "9.9.9"
    assert result["opened"] is True
    # The tarball is fetched from the tag matching the requested version.
    assert any("/download/v9.9.9/Ciaobot_9.9.9_universal.app.tar.gz" in url for url in release.urls)
    assert any(args[0].endswith("open") for args in calls)
    # No staging or work directory left behind.
    assert {path.name for path in app_dir.iterdir()} == {desktop_install.APP_BUNDLE_NAME}


def test_install_refuses_a_bad_signature_and_writes_nothing(tmp_path: Path) -> None:
    """The whole point: a release that does not verify must not reach /Applications."""
    app_dir = tmp_path / "Applications"
    app_dir.mkdir()
    with pytest.raises(SignatureError):
        _install(tmp_path, app_dir, sign_with_wrong_key=True)
    assert list(app_dir.iterdir()) == []


def test_install_refuses_to_overwrite_an_existing_bundle(tmp_path: Path) -> None:
    """Overwriting another app's bundle needs macOS App Management approval, so
    updates belong to the in-app updater, not to this command."""
    app_dir = tmp_path / "Applications"
    app_dir.mkdir()
    existing = app_dir / desktop_install.APP_BUNDLE_NAME
    (existing / "Contents" / "MacOS").mkdir(parents=True)
    (existing / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME).write_text("old")

    with pytest.raises(InstallError, match="already exists"):
        _install(tmp_path, app_dir)
    assert (existing / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME).read_text() == "old"


def test_install_refuses_a_missing_app_directory(tmp_path: Path) -> None:
    with pytest.raises(InstallError, match="does not exist"):
        _install(tmp_path, tmp_path / "nope")


def test_install_reports_a_tarball_without_the_app_bundle(tmp_path: Path) -> None:
    app_dir = tmp_path / "Applications"
    app_dir.mkdir()
    private, public = _keypair()
    empty = tmp_path / "empty.tar.gz"
    with tarfile.open(empty, "w:gz") as tar:
        decoy = tmp_path / "README"
        decoy.write_text("not an app")
        tar.add(decoy, arcname="README")
    payload = empty.read_bytes()
    with pytest.raises(InstallError, match="did not contain"):
        desktop_install.install_desktop_app(
            app_dir=app_dir,
            version="9.9.9",
            opener=FakeRelease(payload, _sig_file(private, payload)),
            runner=_runner([]),
            pubkey=_pubkey_file(public),
        )
    assert list(app_dir.iterdir()) == []


# --- uninstall ---------------------------------------------------------------


def test_uninstall_removes_the_desktop_bundle(tmp_path: Path) -> None:
    app_dir = tmp_path / "Applications"
    macos = app_dir / desktop_install.APP_BUNDLE_NAME / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / desktop_build.APP_EXECUTABLE_NAME).write_text("binary")

    result = desktop_install.uninstall_desktop_app(app_dir=app_dir)
    assert result["removed"] is True
    assert not (app_dir / desktop_install.APP_BUNDLE_NAME).exists()


def test_uninstall_is_quiet_when_nothing_is_installed(tmp_path: Path) -> None:
    result = desktop_install.uninstall_desktop_app(app_dir=tmp_path)
    assert result["removed"] is False


def test_uninstall_leaves_a_browser_installed_pwa_alone(tmp_path: Path) -> None:
    """A Chrome/Safari PWA shortcut can also be named Ciaobot.app, and deleting
    the wrong one is not recoverable. Only the Tauri executable identifies ours."""
    app_dir = tmp_path / "Applications"
    pwa = app_dir / desktop_install.APP_BUNDLE_NAME / "Contents" / "MacOS"
    pwa.mkdir(parents=True)
    (pwa / "app_mode_loader").write_text("chrome pwa")

    with pytest.raises(InstallError, match="not the Ciaobot desktop app"):
        desktop_install.uninstall_desktop_app(app_dir=app_dir)
    assert (pwa / "app_mode_loader").exists()
