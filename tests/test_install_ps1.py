"""Tests for the Windows 11 engine installer, scripts/install.ps1 (#838, C8).

The script is a text file a Windows user pipes into `iex`, so nothing here runs
PowerShell: what is asserted is the shape of the file (ASCII, no `exit`, the
constants, the verifier copy) and the order of its steps, which is the one
property a failed run cannot report. The advisory Windows CI job runs the same
file under both PowerShell hosts.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

from ciao.release_manifest import RELEASE_PUBLIC_KEY
from tests.test_engine_installer import VERSION, WHEEL_BYTES, WHEEL_NAME, _manifest_bytes
from tests.test_release_manifest import _keypair, _sign

REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install.ps1"
SH_SCRIPT = REPO_ROOT / "scripts" / "install-engine.sh"
SCRIPT_TEXT = SCRIPT.read_text(encoding="ascii")
SH_TEXT = SH_SCRIPT.read_text(encoding="utf-8")


def _ps1_verifier_source() -> str:
    """The Python program the PowerShell here-string holds, as it ships."""
    lines = SCRIPT_TEXT.splitlines()
    start = next(i for i, line in enumerate(lines) if "$VerifierSource = @'" in line) + 1
    end = next(i for i, line in enumerate(lines[start:], start) if line == "'@")
    return "\n".join(lines[start:end]) + "\n"


def _sh_verifier_source() -> str:
    """The Python program the sh heredoc holds, as it ships."""
    lines = SH_TEXT.splitlines()
    start = next(i for i, line in enumerate(lines) if "<<'PY'" in line) + 1
    end = next(i for i, line in enumerate(lines[start:], start) if line.strip() == "PY")
    return "\n".join(lines[start:end]) + "\n"


def _run_verifier(
    tmp_path: Path, manifest_bytes: bytes, sig_text: str, version: str, key_text: str
) -> subprocess.CompletedProcess[str]:
    manifest = tmp_path / "ciaobot-engine-manifest.json"
    signature = tmp_path / "ciaobot-engine-manifest.json.sig"
    manifest.write_bytes(manifest_bytes)
    signature.write_text(sig_text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-", str(manifest), str(signature), version, key_text],
        input=_ps1_verifier_source(),
        capture_output=True,
        text=True,
        check=False,
    )


def _top_level_statements(text: str) -> list[str]:
    """Statements outside any block, comments and blank lines dropped.

    The here-string body is cut out first: it is Python, full of braces that are
    not PowerShell braces at all.
    """
    statements: list[str] = []
    depth = 0
    for line in text.replace(_ps1_verifier_source(), "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if depth == 0:
            statements.append(stripped)
        depth += line.count("{") - line.count("}")
    assert depth == 0, "unbalanced braces in install.ps1"
    return statements


def test_install_ps1_is_ascii_and_has_no_exit() -> None:
    # Windows PowerShell 5.1 reads a BOM-less file as the ANSI code page, so a
    # non-ASCII byte anywhere here is a mangled script, not a cosmetic problem.
    SCRIPT.read_bytes().decode("ascii")

    # `irm ... | iex` runs this text in the caller's interactive session, where
    # `exit` closes their terminal; every failure below is a `throw`.
    assert not re.search(r"^\s*exit\b", SCRIPT_TEXT, re.MULTILINE), (
        "install.ps1 would close the terminal it was piped into"
    )

    assert "$ErrorActionPreference = 'Stop'" in SCRIPT_TEXT
    # TLS 1.0/1.1 is the 5.1 default on some builds, and GitHub refuses both.
    assert "Tls12" in SCRIPT_TEXT


def test_release_constants_match_install_engine_sh() -> None:
    # The two installers have to pin the same uv, the same Python and the same
    # key, or "the same verified engine" is two different things on two
    # platforms.
    names = ("UV_VERSION", "PYTHON_VERSION", "CRYPTOGRAPHY_PIN", "RELEASE_PUBLIC_KEY", "repo")
    sh = {
        match.group(1): match.group(3)
        for match in re.finditer(
            r"^(UV_VERSION|PYTHON_VERSION|CRYPTOGRAPHY_PIN|RELEASE_PUBLIC_KEY|repo)=(\"?)(.*?)\2$",
            SH_TEXT,
            re.MULTILINE,
        )
    }
    ps1 = {}
    for name, pattern in (
        ("UV_VERSION", r"\$UvVersion = '(.*?)'"),
        ("PYTHON_VERSION", r"\$PythonVersion = '(.*?)'"),
        ("CRYPTOGRAPHY_PIN", r"\$CryptographyPin = '(.*?)'"),
        ("RELEASE_PUBLIC_KEY", r"\$ReleasePublicKey = '(.*?)'"),
        ("repo", r"\$Repo = '(.*?)'"),
    ):
        match = re.search(pattern, SCRIPT_TEXT)
        assert match, f"install.ps1 no longer defines ${name}"
        ps1[name] = match.group(1)

    assert set(sh) == set(names)
    for name in names:
        assert ps1[name] == sh[name], (
            f"install.ps1 {name}={ps1[name]!r}, install-engine.sh {name}={sh[name]!r}"
        )

    # Q-05 named 3.12, but the macOS installer ships 3.13 and the release wheel
    # is built for it; a second Python here is an engine nobody tested.
    assert sh["PYTHON_VERSION"] == "3.13"

    # The key is the one ciao/release_manifest.py signs and verifies with.
    assert sh["RELEASE_PUBLIC_KEY"] == RELEASE_PUBLIC_KEY

    # The same URL shapes, so a release that installs on macOS installs here.
    for fragment in (
        "https://github.com/$Repo/releases/download",
        "https://github.com/$Repo/releases/latest",
        '"$ReleaseBase/v$Version"',
        "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-installer.ps1",
        "'ciaobot-engine-manifest.json'",
        "'ciaobot-engine-manifest.json.sig'",
    ):
        assert fragment in SCRIPT_TEXT, f"install.ps1 lost a release URL shape: {fragment!r}"


def test_embedded_verifier_matches_install_engine_sh() -> None:
    # Two copies of one verifier, on purpose: install.ps1 cannot download a
    # verifier of its own without a new release asset, and nothing new is
    # downloaded, so old release tags keep installing. A drifted copy would
    # verify under different rules on the two platforms, so it fails here.
    assert _ps1_verifier_source() == _sh_verifier_source(), (
        "the verifier embedded in install.ps1 is no longer the one install-engine.sh runs"
    )


def test_verifier_body_cannot_end_the_here_string() -> None:
    # A here-string ends at the first line whose first characters are '@, so
    # nothing in the Python body may start with it: that would leave the
    # PowerShell copy silently truncated.
    for line in _sh_verifier_source().splitlines():
        assert not line.startswith("'@"), f"the verifier body would end the here-string: {line!r}"

    closers = [line for line in SCRIPT_TEXT.splitlines() if line == "'@"]
    assert closers == ["'@"], "install.ps1 must close $VerifierSource exactly once, at column 0"


def test_embedded_verifier_still_accepts_and_rejects(tmp_path: Path) -> None:
    # Comparing the two copies proves they are equal; running the PowerShell one
    # proves it is the verifier, not a copy of it that reads the manifest.
    raw = _manifest_bytes()
    private_key, public_key, key_id = _keypair()
    signature = _sign(raw, private_key, key_id)

    accepted = _run_verifier(tmp_path, raw, signature, VERSION, public_key)
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.split() == [
        WHEEL_NAME,
        hashlib.sha256(WHEEL_BYTES).hexdigest(),
        str(len(WHEEL_BYTES)),
    ]

    refused = _run_verifier(
        tmp_path, raw.replace(b'"1.2.3"', b'"9.9.9"'), signature, VERSION, public_key
    )
    assert refused.returncode == 1
    assert "does not match" in refused.stderr


def test_verification_precedes_install() -> None:
    # Handing a wheel to a tool environment before the signature and the digest
    # have been checked would be no check at all.
    verify = SCRIPT_TEXT.index("python $verifier")
    digest = SCRIPT_TEXT.index("Get-FileHash -LiteralPath $wheel")
    install = SCRIPT_TEXT.index("tool install")

    assert verify < digest < install

    # `iex` runs this text in the caller's scope, so the param block, the one
    # function and its call are the only things allowed at the top level: a
    # top-level assignment would leave a variable and a preference behind in an
    # interactive session.
    assert _top_level_statements(SCRIPT_TEXT) == [
        "[CmdletBinding()]",
        "param(",
        "[string]$Version = '',",
        "[string]$ReleaseDir = '',",
        "[switch]$DryRun",
        ")",
        "function Install-Ciaobot {",
        "Install-Ciaobot -Version $Version -ReleaseDir $ReleaseDir -DryRun $DryRun.IsPresent",
    ]


def test_install_ps1_has_no_powershell_crypto() -> None:
    # Signature verification runs in the same Python verifier install-engine.sh
    # uses, under uv. A second verification path in PowerShell would be a second
    # trusted code path, and 5.1 (.NET Framework) has no Ed25519 to write one
    # with, so none is allowed here.
    outside = SCRIPT_TEXT.replace(_ps1_verifier_source(), "")
    for banned in ("Ed25519", "VerifyHash", "ECDsa", "System.Security.Cryptography"):
        assert banned not in outside, (
            f"install.ps1 verifies crypto in PowerShell ({banned}) instead of the shared verifier"
        )