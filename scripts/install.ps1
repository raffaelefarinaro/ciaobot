# Ciaobot engine installer for Windows 11, part 1 (#696, C8).
#
# Latest release:
#   irm https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.ps1 | iex
# A specific release (iex cannot take arguments, so use a script block):
#   & ([scriptblock]::Create((irm https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.ps1))) -Version 0.9.2
# From a saved file, when the execution policy blocks it:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
#
# Installs the engine as a uv tool from the release wheel after verifying the
# signed release manifest with the verifier embedded below, a verbatim copy of
# the one install-engine.sh runs (a test keeps them identical). Setup, service,
# update and uninstall are not part of this script yet.
#
# PowerShell 5.1 compatible: keep it ASCII (Windows PowerShell 5.1 misreads a
# BOM-less UTF-8 file), no ternary, no `&&`, and no `exit` - this text is
# usually run by `iex` in the caller's own session, where a top-level variable
# assignment would leak into it and an `exit` would close their terminal.

[CmdletBinding()]
param(
    [string]$Version = '',
    [string]$ReleaseDir = '',
    [switch]$DryRun
)

function Install-Ciaobot {
    [CmdletBinding()]
    param(
        [string]$Version,
        [string]$ReleaseDir,
        [bool]$DryRun
    )

    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'

    $Repo = 'raffaelefarinaro/ciaobot'
    $ReleaseBase = "https://github.com/$Repo/releases/download"
    $UvVersion = '0.12.17'
    $PythonVersion = '3.13'   # must equal PYTHON_VERSION in install-engine.sh; a test enforces it
    $CryptographyPin = 'cryptography==50.0.0'
    # Same key as ciao/release_manifest.py RELEASE_PUBLIC_KEY (a test keeps them equal).
    $ReleasePublicKey = 'RWSDUnIeQDnpmnNJiTjLmN6XOVFqgn1A0EXvTVG7AJIZXJxhyFN9osxm'
    $ManifestName = 'ciaobot-engine-manifest.json'
    $SignatureName = 'ciaobot-engine-manifest.json.sig'

    # Verbatim copy of the <<'PY' heredoc body in install-engine.sh
    # read_manifest; test_install_ps1.py fails if the two differ. It is pasted
    # at column 0, and so is its closing '@: the here-string is the only place
    # in this function where a line may start there, and no line of the body
    # may start with '@.
    $VerifierSource = @'
# --- embedded verifier (keep in sync with ciao/release_manifest.py) ---
import base64, hashlib, json, sys
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

manifest_path, sig_path, expected_version, public_key = sys.argv[1:5]
def die(msg):
    print(msg, file=sys.stderr); sys.exit(1)
raw_key = base64.b64decode(public_key.strip().splitlines()[-1], validate=True)
if len(raw_key) != 42 or raw_key[:2] != b"Ed": die("bad embedded public key")
key_id, key = raw_key[2:10], raw_key[10:]
text = open(sig_path, encoding="utf-8").read().strip()
if not text.startswith("untrusted comment:"):
    text = base64.b64decode(text, validate=True).decode("utf-8")
lines = [l.rstrip("\r") for l in text.splitlines() if l.strip()]
if len(lines) != 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith("trusted comment: "):
    die("malformed manifest signature")
blob = base64.b64decode(lines[1], validate=True)
if len(blob) != 74: die("malformed manifest signature")
alg, sig_key_id, signature = blob[:2], blob[2:10], blob[10:]
if sig_key_id != key_id: die("manifest signed by a different key")
data = open(manifest_path, "rb").read()
if alg == b"ED": message = hashlib.blake2b(data, digest_size=64).digest()
elif alg == b"Ed": message = data
else: die("unsupported signature algorithm")
trusted = lines[2][len("trusted comment: "):]
pk = Ed25519PublicKey.from_public_bytes(key)
try:
    pk.verify(signature, message)
    pk.verify(base64.b64decode(lines[3], validate=True), signature + trusted.encode("utf-8"))
except InvalidSignature:
    die("manifest signature does not match")
manifest = json.loads(data)
if manifest.get("schema") != 1 or manifest.get("version") != expected_version:
    die("manifest is not for version " + expected_version)
wheels = [a for a in manifest.get("artifacts", []) if a.get("kind") == "wheel"]
if len(wheels) != 1: die("manifest must list exactly one wheel")
w = wheels[0]
name, digest, size = str(w.get("filename", "")), str(w.get("sha256", "")), w.get("size")
if "/" in name or "\\" in name or not name.endswith(".whl") or len(digest) != 64 or not isinstance(size, int):
    die("manifest wheel entry is malformed")
print(name, digest, size)
'@

    function Fail([string]$Message) {
        throw "Ciaobot installer: $Message"
    }

    # --- refuse anything but Windows 11 on x64/ARM64 with PowerShell 5.1+ ---
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        Fail 'this installer supports Windows 11; macOS uses install.sh and Linux servers use docs/LINUX.md'
    }
    $build = [System.Environment]::OSVersion.Version.Build
    if ($build -lt 22000) {
        Fail "the Ciaobot engine requires Windows 11 (build 22000 or newer); this is build $build"
    }
    $arch = $env:PROCESSOR_ARCHITEW6432
    if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
    if ($arch -ne 'AMD64' -and $arch -ne 'ARM64') {
        Fail "unsupported processor architecture: $arch (x64 and ARM64 only)"
    }
    if ($PSVersionTable.PSVersion -lt [version]'5.1') {
        Fail "PowerShell 5.1 or newer is required; this is $($PSVersionTable.PSVersion)"
    }
    if ($ReleaseDir -and -not $Version) {
        Fail '-Version is required with -ReleaseDir'
    }

    $savedProtocol = [System.Net.ServicePointManager]::SecurityProtocol
    $savedNoModify = $env:UV_NO_MODIFY_PATH
    $savedInstallDir = $env:UV_INSTALL_DIR
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ('ciaobot-install-' + [guid]::NewGuid().ToString('N'))

    try {
        # Windows PowerShell 5.1 defaults to TLS 1.0/1.1 on some builds; GitHub needs 1.2.
        [System.Net.ServicePointManager]::SecurityProtocol =
            [System.Net.ServicePointManager]::SecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12
        New-Item -ItemType Directory -Path $tmp -Force | Out-Null

        function Get-ReleaseFile([string]$Url, [string]$Destination) {
            for ($attempt = 1; $attempt -le 3; $attempt++) {
                try {
                    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing -TimeoutSec 60
                    return
                } catch {
                    if ($attempt -eq 3) { throw }
                    Start-Sleep -Seconds 2
                }
            }
        }

        # --- resolve the release the way install-engine.sh does ---
        if (-not $Version) {
            $request = [System.Net.HttpWebRequest]::Create("https://github.com/$Repo/releases/latest")
            $request.Method = 'HEAD'
            $request.AllowAutoRedirect = $true
            $request.UserAgent = 'ciaobot-install.ps1'
            try {
                $response = $request.GetResponse()
            } catch {
                Fail "could not determine the latest release version: $($_.Exception.Message)"
            }
            try { $finalUrl = $response.ResponseUri.AbsoluteUri } finally { $response.Close() }
            $Version = ($finalUrl.TrimEnd('/') -split '/')[-1]
        }
        $Version = $Version -replace '^v', ''
        # The version reaches a download URL and the manifest verifier, so accept only a plain release tag.
        if ($Version -notmatch '^[0-9A-Za-z.-]+\z') {
            Fail "invalid version: $Version"
        }
        $base = "$ReleaseBase/v$Version"

        # --- find or install uv ---
        $uv = $null
        $found = Get-Command uv -ErrorAction SilentlyContinue
        $localBin = Join-Path $env:USERPROFILE '.local\bin'
        if ($found) {
            $uv = $found.Source
        } elseif (Test-Path -LiteralPath (Join-Path $localBin 'uv.exe')) {
            $uv = Join-Path $localBin 'uv.exe'
        } elseif ($DryRun) {
            Fail 'uv is required for -DryRun (it is not installed)'
        } else {
            Write-Host "Installing uv $UvVersion for this user..."
            $uvInstaller = Join-Path $tmp 'uv-installer.ps1'
            try {
                Get-ReleaseFile "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-installer.ps1" $uvInstaller
            } catch {
                Fail "could not download the uv installer: $($_.Exception.Message)"
            }
            # Pinned version and uv's official installer; the user's PATH is left alone.
            $env:UV_NO_MODIFY_PATH = '1'
            $env:UV_INSTALL_DIR = $localBin
            $powershellExe = (Get-Process -Id $PID).Path
            & $powershellExe -NoProfile -ExecutionPolicy Bypass -File $uvInstaller | Out-Null
            if ($LASTEXITCODE -ne 0) { Fail 'could not install uv' }
            $uv = Join-Path $localBin 'uv.exe'
        }
        if (-not (Test-Path -LiteralPath $uv)) { Fail "uv is not available at $uv" }

        # --- fetch the signed manifest and its signature ---
        foreach ($name in @($ManifestName, $SignatureName)) {
            $destination = Join-Path $tmp $name
            if ($ReleaseDir) {
                $source = Join-Path $ReleaseDir $name
                if (-not (Test-Path -LiteralPath $source)) { Fail "$name is not in $ReleaseDir" }
                Copy-Item -LiteralPath $source -Destination $destination
            } else {
                try {
                    Get-ReleaseFile "$base/$name" $destination
                } catch {
                    Fail "could not download $name from $base : $($_.Exception.Message)"
                }
            }
        }
        $verifier = Join-Path $tmp 'verify_manifest.py'
        [System.IO.File]::WriteAllText($verifier, ($VerifierSource -replace "`r`n", "`n") + "`n", (New-Object System.Text.UTF8Encoding($false)))

        # --- verify the manifest with the same Python verifier install-engine.sh runs ---
        $manifestInfo = & $uv run --quiet --no-project --python $PythonVersion --with $CryptographyPin `
            python $verifier (Join-Path $tmp $ManifestName) (Join-Path $tmp $SignatureName) $Version $ReleasePublicKey
        if ($LASTEXITCODE -ne 0) { Fail 'release manifest verification failed' }
        $parts = (@($manifestInfo) -join ' ').Trim() -split '\s+'
        if ($parts.Count -ne 3) { Fail 'release manifest verification failed' }
        $wheelName = $parts[0]
        $wheelSha = $parts[1]
        $wheelSize = $parts[2]
        if ($wheelName -notmatch '^[^/\\]+\.whl\z') { Fail 'release manifest verification failed' }

        # --- fetch the wheel and compare it to the signed entry ---
        $wheel = Join-Path $tmp $wheelName
        if ($ReleaseDir) {
            $source = Join-Path $ReleaseDir $wheelName
            if (-not (Test-Path -LiteralPath $source)) { Fail "$wheelName is not in $ReleaseDir" }
            Copy-Item -LiteralPath $source -Destination $wheel
        } else {
            try {
                Get-ReleaseFile "$base/$wheelName" $wheel
            } catch {
                Fail "could not download $wheelName : $($_.Exception.Message)"
            }
        }
        $actualSha = (Get-FileHash -LiteralPath $wheel -Algorithm SHA256).Hash.ToLowerInvariant()
        $actualSize = (Get-Item -LiteralPath $wheel).Length
        if ($actualSha -ne $wheelSha -or "$actualSize" -ne $wheelSize) {
            Fail 'downloaded wheel does not match the signed manifest'
        }

        if ($DryRun) {
            Write-Host "Dry run: $wheelName for Ciaobot $Version matches the signed manifest. Nothing was installed."
            return
        }

        # --- install ---
        & $uv tool install --force --python $PythonVersion $wheel
        if ($LASTEXITCODE -ne 0) { Fail 'uv tool install failed' }
        $binDir = (& $uv tool dir --bin | Select-Object -First 1)
        if ($LASTEXITCODE -ne 0 -or -not $binDir) { Fail 'could not locate the uv tool bin directory' }
        $ciao = Join-Path $binDir 'ciao.exe'
        if (-not (Test-Path -LiteralPath $ciao)) { Fail "the installed ciao entry point is missing: $ciao" }

        Write-Host ''
        Write-Host "Ciaobot $Version is installed: $ciao"
        $onPath = @($env:Path -split ';' | Where-Object { $_.TrimEnd('\') -eq $binDir.TrimEnd('\') }).Count -gt 0
        if (-not $onPath) {
            Write-Host "$binDir is not on your PATH. This installer does not change it; run: & '$ciao' --version"
        }
        Write-Host "Next: run '$ciao setup' (service and autostart arrive in a later release)."
    } finally {
        [System.Net.ServicePointManager]::SecurityProtocol = $savedProtocol
        $env:UV_NO_MODIFY_PATH = $savedNoModify
        $env:UV_INSTALL_DIR = $savedInstallDir
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Install-Ciaobot -Version $Version -ReleaseDir $ReleaseDir -DryRun $DryRun.IsPresent
