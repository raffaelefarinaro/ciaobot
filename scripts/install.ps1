# Ciaobot engine installer for Windows 11, part 2 (#696, C8).
#
# Latest release:
#   irm https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.ps1 | iex
# A specific release (iex cannot take arguments, so use a script block):
#   & ([scriptblock]::Create((irm https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.ps1))) -Version 0.9.2
# Another workspace folder, and/or no logon task and no first start:
#   & ([scriptblock]::Create((irm https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.ps1))) -Workspace 'D:\Ciaobot' -NoStart
# Uninstall what an install added (the workspace is kept):
#   & ([scriptblock]::Create((irm https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.ps1))) -Uninstall
# From a saved file, when the execution policy blocks it:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
#
# Installs the engine as a uv tool from the release wheel after verifying the
# signed release manifest with the verifier embedded below, a verbatim copy of
# the one install-engine.sh runs (a test keeps them identical). Then it does
# what install-engine.sh does after the engine lands, so a Windows install is
# the same verified engine, running: the install receipt, the ciao entry point
# on the user PATH, the workspace, the logon task (ciao setup --load-launchd),
# the started engine (ciao service start), a wait for it to answer, and the
# one-time sign-in URL. -Uninstall undoes exactly those steps, in reverse.
# Updating an install that is already here is not covered yet: re-running
# repairs a half-finished one, and any failure undoes only what that run did.
#
# PowerShell 5.1 compatible: keep it ASCII (Windows PowerShell 5.1 misreads a
# BOM-less UTF-8 file), no ternary, no `&&`, and no `exit` - this text is
# usually run by `iex` in the caller's own session, where a top-level variable
# assignment would leak into it and an `exit` would close their terminal.

[CmdletBinding()]
param(
    [string]$Version = '',
    [string]$ReleaseDir = '',
    [switch]$DryRun,
    [string]$Workspace = '',
    [switch]$NoStart,
    [switch]$Uninstall
)

function Install-Ciaobot {
    [CmdletBinding()]
    param(
        [string]$Version,
        [string]$ReleaseDir,
        [bool]$DryRun,
        [string]$Workspace,
        [bool]$NoStart,
        [bool]$Uninstall
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
    # The backend value is ciao/install_receipt.py's, not a second spelling of it;
    # a test keeps the two equal and runs the receipt with these flags.
    $ServiceBackend = 'windows-task'
    $TaskName = '\Ciaobot\Engine'            # must equal windows_service.TASK_NAME; a test enforces it
    $TaskFileName = 'Ciaobot-Engine.xml'     # must equal windows_service.TASK_FILE_NAME; a test enforces it
    $DefaultPort = 8443                      # the fallback install-engine.sh uses when the .env has no PWA_PORT
    $HealthAttempts = 60                     # must equal health_attempts=60 in install-engine.sh; a test enforces it
    $DefaultWorkspaceName = 'Ciaobot'        # install-engine.sh: workspace="$HOME/Ciaobot"
    $StateDir = Join-Path $env:LOCALAPPDATA 'Ciaobot'
    # Must equal windows_service.live_task_dir() - the directory `ciao setup`
    # writes the task XML into, which is the file an uninstall has to remove.
    $TaskDir = Join-Path $StateDir 'service'
    $StateFile = Join-Path $StateDir 'install-state.json'
    # Must equal ciao.install_receipt.default_receipt_path(); a test enforces it.
    $ReceiptPath = Join-Path $env:USERPROFILE '.local\state\ciaobot\install-receipt.json'
    # The rollback and the uninstall both read these, so they exist before the
    # first step that can fail: the `catch` runs against whatever is set at the
    # moment it fires.
    $uv = $null
    $binDir = ''
    $toolDir = ''
    $toolWasInstalled = $false
    $done = @()
    $mutated = $false

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

    # --- native commands -----------------------------------------------------
    #
    # One place for the 5.1 trap: with $ErrorActionPreference='Stop' a native
    # command that writes to stderr - which every ciao subcommand eventually
    # does - becomes a terminating error the moment its stderr is redirected,
    # so the redirect and the preference are scoped to the call and the caller
    # tests the exit code. $Quiet drops stderr from the captured output; a call
    # that returns nothing keeps stderr on the console, the way install-engine.sh
    # leaves `ciao setup`'s own guard messages visible.
    function Invoke-Native([string]$Command, [string[]]$Arguments, [bool]$Quiet) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            if ($Quiet) {
                $output = & $Command @Arguments 2>$null
            } else {
                $output = & $Command @Arguments
            }
        } finally {
            $ErrorActionPreference = $previous
        }
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output }
    }

    # `schtasks` messages are localized, so nothing here parses them: the exit
    # code of /Query is the only answer about a task this script can rely on,
    # exactly as ciao/windows_service.py decides it.
    function Invoke-Schtasks([string[]]$Arguments) {
        return (Invoke-Native 'schtasks.exe' $Arguments $true).ExitCode
    }

    function Test-TaskRegistered {
        return (Invoke-Schtasks @('/Query', '/TN', $TaskName)) -eq 0
    }

    # Is the `ciaobot` tool there? The listing is indented output
    # (`- ciaobot v1.2.3` under a heading), so the name is matched where uv
    # prints it, the way install-engine.sh's preflight matches it. Only spaces
    # and tabs count as the indentation: a `[\s]` class would match the newline
    # and let the pattern walk into a different line's entry.
    function Test-ToolInstalled([string]$Uv) {
        if (-not $Uv) { return $false }
        # -join "`n", never [string]: casting an array to a string joins it with
        # spaces, which would put every entry on one line and make the per-line
        # match below answer about a listing that does not exist.
        $listing = (@((Invoke-Native $Uv @('tool', 'list') $true).Output) -join "`n")
        return $listing -match '(?m)^[ \t]*-[ \t]+ciaobot([ \t]|$)'
    }

    # `uv tool dir` / `uv tool dir --bin`, one path out. Only the first line is
    # read: uv prints the path and nothing else, but a warning on stdout would
    # otherwise become the first half of a path that resolves to nothing.
    function Get-UvPath([string]$Uv, [bool]$Bin) {
        $arguments = @('tool', 'dir')
        if ($Bin) { $arguments += '--bin' }
        $result = Invoke-Native $Uv $arguments $true
        if ($result.ExitCode -ne 0) { return '' }
        $lines = @($result.Output | Where-Object { ([string]$_).Trim() -ne '' })
        if ($lines.Count -eq 0) { return '' }
        return ([string]$lines[0]).Trim()
    }

    function Test-EngineAnswering([int]$Port) {
        # -UseBasicParsing because 5.1 would otherwise try to load the IE engine
        # to parse the response, which is not there on a server install.
        try {
            $response = Invoke-WebRequest -Uri "http://localhost:$Port/api/startup-status" `
                -UseBasicParsing -TimeoutSec 5
            return $response.StatusCode -eq 200
        } catch {
            return $false
        }
    }

    # PWA_PORT out of the workspace .env, the same source install-engine.sh
    # reads; the last occurrence wins, as that pipeline's `tail -1` does.
    function Read-WorkspacePort([string]$Path) {
        if (-not $Path) { return $DefaultPort }
        $envFile = Join-Path $Path '.env'
        if (-not (Test-Path -LiteralPath $envFile)) { return $DefaultPort }
        $port = $DefaultPort
        foreach ($line in Get-Content -LiteralPath $envFile) {
            if ($line -match '^\s*PWA_PORT="?(\d+)') { $port = [int]$Matches[1] }
        }
        return $port
    }

    # --- the user PATH -------------------------------------------------------
    #
    # The registry, read raw and written back as the kind it already had.
    # [Environment]::SetEnvironmentVariable reads the old value expanded and
    # writes it as REG_SZ, which would freeze every %VAR% in a REG_EXPAND_SZ
    # user PATH and change its type under every other tool that reads it; #854
    # settled that for the hint ciao/os_support/shell_hints.py prints, and this
    # is the same rule doing it for the user. HKCU only: no admin.
    function Get-UserPath {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
        if (-not $key) { return '' }
        try {
            return [string]$key.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
        } finally {
            $key.Close()
        }
    }

    function Set-UserPath([string]$Value) {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
        if (-not $key) { throw 'the user environment key is not writable' }
        try {
            # A PATH that does not exist yet is written as the kind Windows gives
            # a user PATH; an existing one keeps the kind it has.
            $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
            if ($null -ne $key.GetValue('Path', $null, 'DoNotExpandEnvironmentNames')) {
                $kind = $key.GetValueKind('Path')
            }
            $key.SetValue('Path', $Value, $kind)
        } finally {
            $key.Close()
        }
        # A raw registry write is invisible to the programs already running, and
        # Explorer - which owns the taskbar - is one of them. A user-scope
        # SetEnvironmentVariable broadcasts WM_SETTINGCHANGE, so a throwaway
        # variable is set and cleared to do exactly that: the Path itself cannot
        # be the vehicle, or it would be rewritten the way #854 rejects.
        [System.Environment]::SetEnvironmentVariable('Ciaobot_Install_Notify', '1', 'User')
        [System.Environment]::SetEnvironmentVariable('Ciaobot_Install_Notify', $null, 'User')
    }

    function Test-UserPathEntry([string]$PathValue, [string]$Directory) {
        # Case-insensitive and trailing-`\`-tolerant, because both spellings of
        # the same directory are the same entry and a second copy of it is
        # exactly what this check exists to prevent.
        $wanted = $Directory.Trim().TrimEnd('\')
        if (-not $wanted) { return $false }
        foreach ($entry in $PathValue -split ';') {
            $candidate = $entry.Trim().Trim('"').TrimEnd('\')
            if ($candidate -and $candidate -ieq $wanted) { return $true }
        }
        return $false
    }

    function Add-UserPathEntry([string]$PathValue, [string]$Directory) {
        # Prepended, the way the hint already prints it: this is our own
        # directory and the first entry is the one a user meets first.
        if (-not $PathValue) { return $Directory }
        return "$Directory;$PathValue"
    }

    function Remove-UserPathEntry([string]$PathValue, [string]$Directory) {
        # Every other entry comes back byte for byte: the uv bin directory is
        # shared with the user's other uv tools, so an entry that is not the one
        # this installer added is not this installer's to rewrite.
        $wanted = $Directory.Trim().TrimEnd('\')
        $kept = @()
        foreach ($entry in $PathValue -split ';') {
            $candidate = $entry.Trim().Trim('"').TrimEnd('\')
            if ($candidate -and $candidate -ieq $wanted) { continue }
            $kept += $entry
        }
        return ($kept -join ';')
    }

    function Read-InstallState {
        if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
        try {
            return (Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json)
        } catch {
            return $null
        }
    }

    # "Gone" is proven, not assumed: the port closed and no pythonw.exe *process*
    # is left running out of the tool directory. Windows refuses to delete a
    # running executable, and uv tool uninstall would leave a half-removed
    # environment behind. It has to be the process, not the file: every venv on
    # disk ships a Scripts\pythonw.exe, so looking for one would report the engine
    # as running forever and make every undo wait out its full timeout.
    function Wait-EngineStopped([string]$ToolDirectory, [int]$Port) {
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $deadline) {
            $busy = $false
            if ($Port -gt 0) { $busy = Test-EngineAnswering $Port }
            if ((-not $busy) -and $ToolDirectory) {
                $running = @(Get-Process -Name 'pythonw' -ErrorAction SilentlyContinue |
                        Where-Object { $_.Path -and $_.Path.StartsWith($ToolDirectory, 'OrdinalIgnoreCase') })
                if ($running.Count -gt 0) { $busy = $true }
            }
            if (-not $busy) { return $true }
            Start-Sleep -Milliseconds 500
        }
        return $false
    }

    # --- undo ----------------------------------------------------------------
    #
    # One function for the failure path and for -Uninstall, so "uninstall
    # removes exactly what install adds" is one list read twice. It never
    # throws: each step is its own try/catch that records the failure and moves
    # on, because a rollback that stops at the first refusal leaves more behind
    # than it has to, and every step left over is named at the end with the
    # command that finishes it. It removes nothing it did not add: the steps
    # are the ones this run completed, and the PATH entry only when the state
    # file says this installer added it.
    # Every value the undo needs is a parameter, not a variable read out of the
    # enclosing scope: a nested PowerShell function can read its caller's locals,
    # but a rollback that silently reads the wrong one because of a rename is the
    # last place to find out.
    function Undo-Install([string[]]$Steps, [string]$Uv, [string]$ToolDirectory,
                          [string]$Workspace, [string]$PathEntry) {
        $failed = @()

        # UNDOES: task
        # The engine runs pythonw.exe out of the uv tool environment, and Windows
        # will not delete a running executable, so this also stops what `ciao
        # service start` started and proves the process is gone. The task itself
        # goes before the tool, because its RestartOnFailure (every minute, 999
        # times) would start a dying engine again.
        if (($Steps -contains 'task') -or ($Steps -contains 'taskfile')) {
            try {
                # A task that is not running is already stopped: /End exits
                # non-zero and that is not a failure worth reporting.
                $null = Invoke-Schtasks @('/End', '/TN', $TaskName)
            } catch {
                $failed += "ending the logon task ($($_.Exception.Message))"
            }
            try {
                if (-not (Wait-EngineStopped $ToolDirectory (Read-WorkspacePort $Workspace))) {
                    $failed += "the engine process is still running (schtasks /Query /TN $TaskName)"
                }
            } catch {
                $failed += "waiting for the engine to stop ($($_.Exception.Message))"
            }
            # Kept separate from the wait above: a process that would not go is
            # a fact to report, not a reason to leave a task registered.
            try {
                if (Test-TaskRegistered) {
                    $deleted = Invoke-Schtasks @('/Delete', '/TN', $TaskName, '/F')
                    if ($deleted -ne 0) {
                        $failed += "schtasks /Delete /TN $TaskName /F exited $deleted"
                    }
                }
            } catch {
                $failed += "deleting the logon task ($($_.Exception.Message))"
            }
        }

        # UNDOES: taskfile
        if ($Steps -contains 'taskfile') {
            try {
                # -ErrorAction Stop, because -ErrorAction SilentlyContinue in a
                # try/catch never throws and a locked file would be reported as a
                # clean uninstall. Test-Path first, so a file that is already gone
                # is not a failure.
                $taskFile = Join-Path $TaskDir $TaskFileName
                if (Test-Path -LiteralPath $taskFile) {
                    Remove-Item -LiteralPath $taskFile -Force -ErrorAction Stop
                }
            } catch {
                $failed += "the task definition (Remove-Item '$TaskDir\$TaskFileName')"
            }
        }

        # UNDOES: state
        if ($Steps -contains 'state') {
            try {
                if (Test-Path -LiteralPath $StateFile) {
                    Remove-Item -LiteralPath $StateFile -Force -ErrorAction Stop
                }
            } catch {
                $failed += "the install state file (Remove-Item '$StateFile')"
            }
        }

        # UNDOES: path
        if (($Steps -contains 'path') -and $PathEntry) {
            try {
                Set-UserPath (Remove-UserPathEntry (Get-UserPath) $PathEntry)
                if (Test-UserPathEntry $env:Path $PathEntry) {
                    $env:Path = Remove-UserPathEntry $env:Path $PathEntry
                }
            } catch {
                $failed += "the user PATH entry ($($_.Exception.Message))"
            }
        }

        # UNDOES: tool
        if (($Steps -contains 'tool') -and $Uv) {
            try {
                $removed = Invoke-Native $Uv @('tool', 'uninstall', 'ciaobot') $true
                if ($removed.ExitCode -ne 0) {
                    $failed += "uv tool uninstall ciaobot exited $($removed.ExitCode)"
                }
            } catch {
                $failed += "the uv tool environment ($($_.Exception.Message))"
            }
        }

        # UNDOES: receipt
        # Last, and only because the tool is already gone: detect_install_mode()
        # trusts a receipt only while its own interpreter is the one running, so
        # a receipt left behind here cannot hand the machine to a removed engine.
        if ($Steps -contains 'receipt') {
            try {
                if (Test-Path -LiteralPath $ReceiptPath) {
                    Remove-Item -LiteralPath $ReceiptPath -Force -ErrorAction Stop
                }
            } catch {
                $failed += "the install receipt (Remove-Item '$ReceiptPath')"
            }
        }

        return $failed
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
    if ((-not $Uninstall) -and $ReleaseDir -and -not $Version) {
        Fail '-Version is required with -ReleaseDir'
    }

    $savedProtocol = [System.Net.ServicePointManager]::SecurityProtocol
    $savedNoModify = $env:UV_NO_MODIFY_PATH
    $savedInstallDir = $env:UV_INSTALL_DIR
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ('ciaobot-install-' + [guid]::NewGuid().ToString('N'))
    $pathEntry = ''
    $workspace = ''

    try {
        # Windows PowerShell 5.1 defaults to TLS 1.0/1.1 on some builds; GitHub needs 1.2.
        [System.Net.ServicePointManager]::SecurityProtocol =
            [System.Net.ServicePointManager]::SecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12

        # --- uninstall: what an install added, in reverse, and nothing else ---
        #
        # No download and no manifest: an uninstall has to work on a machine
        # that can no longer reach GitHub. What is removed is decided by what is
        # there - the state file for the PATH entry this installer added, and
        # schtasks/uv/the filesystem for the rest - never by what this run did,
        # because this run did nothing.
        if ($Uninstall) {
            $state = Read-InstallState
            if ($state -and $state.workspace) { $workspace = [string]$state.workspace }
            if ($state -and $state.path_entry) { $pathEntry = [string]$state.path_entry }
            $found = Get-Command uv -ErrorAction SilentlyContinue
            $localBin = Join-Path $env:USERPROFILE '.local\bin'
            if ($found) {
                $uv = $found.Source
            } elseif (Test-Path -LiteralPath (Join-Path $localBin 'uv.exe')) {
                $uv = Join-Path $localBin 'uv.exe'
            }
            if ($uv) {
                $toolDir = Get-UvPath $uv $false
                if (Test-ToolInstalled $uv) { $done += 'tool' }
            } else {
                Write-Host 'uv is not installed, so the engine environment cannot be removed.'
            }
            if (Test-TaskRegistered) { $done += 'task' }
            if (Test-Path -LiteralPath (Join-Path $TaskDir $TaskFileName)) { $done += 'taskfile' }
            if (Test-Path -LiteralPath $StateFile) { $done += 'state' }
            if ($pathEntry) { $done += 'path' }
            if (Test-Path -LiteralPath $ReceiptPath) { $done += 'receipt' }
            $undone = @(Undo-Install -Steps $done -Uv $uv -ToolDirectory $toolDir -Workspace $workspace -PathEntry $pathEntry)
            if ($undone.Count -eq 0) {
                Write-Host 'Ciaobot is uninstalled.'
            } else {
                Write-Host "Ciaobot could not be fully uninstalled; finish by hand: $($undone -join '; ')"
            }
            Write-Host 'uv was left in place: it is not this installer''s to remove, and it runs other tools.'
            if ($workspace) {
                Write-Host "Your workspace was kept: $workspace"
            }
            return
        }

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

        # --- preflight: refuse to take over a `ciao` this installer did not put
        # there, before anything is downloaded. A re-run over our own engine is
        # allowed - it is how a half-finished install is repaired, and the
        # reinstall below replaces the environment underneath it - but `ciao` is
        # a common enough name that clobbering someone else's program is not this
        # script's call. `uv tool list`, not the entry point alone, is what
        # settles it: the listing is the only place a *uv-owned* `ciao` says so.
        $binDir = Get-UvPath $uv $true
        if (-not $binDir) { Fail 'could not locate the uv tool bin directory' }
        $toolWasInstalled = Test-ToolInstalled $uv
        $existingCiao = Join-Path $binDir 'ciao.exe'
        if ((Test-Path -LiteralPath $existingCiao) -and -not $toolWasInstalled) {
            Fail "$existingCiao exists and was not installed by Ciaobot; move it away and re-run"
        }

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

        # --- the engine, the receipt, the PATH, the workspace, the task -------
        #
        # Every step from here to the end changes the machine, so $mutated is
        # set first and the `catch` below undoes exactly the steps this run
        # completed, newest first. `ciao` and `schtasks` are the wrong tools for
        # the undo: `ciao` is the thing being removed, and there is no
        # unregister action, so the task goes through schtasks and the tool
        # through uv.

        # ADDS: tool
        $mutated = $true
        # A tool that was already there is not this run's to remove on failure.
        if (-not $toolWasInstalled) { $done += 'tool' }
        # --link-mode copy, and not uv's default hard link into its cache: on
        # Windows a cached .pyd/.dll shared with another venv on the machine can
        # be locked by a process that has nothing to do with Ciaobot, and a
        # locked file cannot be deleted, so uninstall and rollback would fail for
        # as long as that unrelated process runs. #857's spike measured it; own
        # files cost disk and install time.
        $installed = Invoke-Native $uv @('tool', 'install', '--force', '--link-mode', 'copy', '--python', $PythonVersion, $wheel) $true
        if ($installed.ExitCode -ne 0) { Fail 'uv tool install failed' }
        $binDir = Get-UvPath $uv $true
        if (-not $binDir) { Fail 'could not locate the uv tool bin directory' }
        $ciao = Join-Path $binDir 'ciao.exe'
        if (-not (Test-Path -LiteralPath $ciao)) { Fail "the installed ciao entry point is missing: $ciao" }
        $toolDir = Get-UvPath $uv $false
        if (-not $toolDir) { Fail 'could not locate the uv tool directory' }
        $toolPython = Join-Path $toolDir 'ciaobot\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $toolPython)) {
            Fail "the installed engine interpreter is missing: $toolPython"
        }
        # `uv tool install --force` can exit 0 while leaving an environment this
        # interpreter cannot import from. Probed here, before the receipt says
        # the install exists: a failure now is the same failure one step later
        # with a receipt already written.
        $probe = Invoke-Native $toolPython @('-c', 'import ciao.install_receipt') $true
        if ($probe.ExitCode -ne 0) { Fail 'the installed engine is not importable' }

        # ADDS: receipt
        # Written by the module that owns the schema, validation, atomic write and
        # owner-only DACL: PowerShell never builds this JSON. The receipt says
        # windows-task even under -NoStart, exactly as install-engine.sh writes
        # launchd when --no-start was passed.
        # A receipt that was already there belongs to the earlier install this
        # run is repairing, so this run did not create it and must not delete it
        # on a later failure: the step is recorded as done only when it was
        # absent a moment ago.
        $receiptWasThere = Test-Path -LiteralPath $ReceiptPath
        $receipt = Invoke-Native $toolPython @('-m', 'ciao.install_receipt', 'write',
            '--version', $Version,
            '--executable', $ciao,
            '--python', $toolPython,
            '--service-backend', $ServiceBackend,
            '--service-label', $TaskName) $true
        if ($receipt.ExitCode -ne 0) { Fail 'could not write the install receipt' }
        if (-not $receiptWasThere) { $done += 'receipt' }

        # ADDS: path
        # The uv bin directory is shared with the user's other uv tools, so
        # whether this run added it is what the state file records below.
        $userPath = Get-UserPath
        $pathEntryAdded = $false
        if (-not (Test-UserPathEntry $userPath $binDir)) {
            Set-UserPath (Add-UserPathEntry $userPath $binDir)
            $pathEntry = $binDir
            $pathEntryAdded = $true
            $done += 'path'
        }
        # Process scope only: so `ciao` also works in the terminal that ran this
        # script, which is what makes that terminal's PATH write worth doing at
        # all. Undo and uninstall take it back out again.
        if (-not (Test-UserPathEntry $env:Path $binDir)) {
            $env:Path = Add-UserPathEntry $env:Path $binDir
        }

        # The workspace is never removed by this script, not even on a rollback:
        # it is the user's notes and memory, and an install that failed is the
        # last thing that should decide what happens to them. It is created
        # before the state file, because the state file records where it is.
        if (-not $Workspace) { $Workspace = Join-Path $env:USERPROFILE $DefaultWorkspaceName }
        New-Item -ItemType Directory -Path $Workspace -Force | Out-Null
        $workspace = (Get-Item -LiteralPath $Workspace).FullName

        # ADDS: state
        # Two facts nothing else records: whether this installer added the PATH
        # entry above (an empty path_entry means it was already there), and where
        # the workspace is, so an uninstall can print it. Not the receipt: that
        # schema is Python-owned and has no field for either.
        $stateWasThere = Test-Path -LiteralPath $StateFile
        # Read before the write, because this file is the only record of who put
        # the PATH entry there. A repair re-run finds the entry already present,
        # takes $pathEntry = '' for itself, and must carry the earlier run's
        # value forward instead: writing '' over it would make the next
        # -Uninstall leave an entry nobody owns behind, for good.
        $previousState = Read-InstallState
        if ((-not $pathEntryAdded) -and $previousState -and $previousState.path_entry) {
            $pathEntry = [string]$previousState.path_entry
        }
        $state = [ordered]@{ path_entry = $pathEntry; workspace = $workspace; version = $Version }
        New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
        $json = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($StateFile, (ConvertTo-Json -InputObject $state), $json)
        # A state file that was already there is this install's to keep, so only a
        # file this run created is a step a rollback may delete.
        if (-not $stateWasThere) { $done += 'state' }

        # ADDS: task
        # ADDS: taskfile
        # `--load-launchd` is what registers \Ciaobot\Engine and starts it;
        # -NoStart omits it, so the workspace is still scaffolded and no task
        # exists, exactly as --no-start does in install-engine.sh. Neither
        # --yes (a fresh install has no repoint to confirm) nor --python (win32
        # takes the interpreter from the running ciao, which is the tool
        # environment's own pythonw.exe) is passed. stderr is left visible so
        # setup's own guard message reaches the user.
        $setupArgs = @('setup', '--workspace', $workspace)
        if (-not $NoStart) { $setupArgs += '--load-launchd' }
        # What was there before this call is what decides what a rollback may
        # remove: a re-run over an already-registered engine has to leave that
        # engine's task in place when it fails, and only the steps this run
        # performed are this run's to undo.
        $taskFile = Join-Path $TaskDir $TaskFileName
        $taskWasRegistered = Test-TaskRegistered
        $taskFileWasThere = Test-Path -LiteralPath $taskFile
        $setup = Invoke-Native $ciao $setupArgs $false
        # Recorded before the exit code is judged, because `ciao setup` can fail
        # *after* it has registered the task: a rollback that only learned about
        # the task here would leave it registered, RestartOnFailure would keep
        # the engine alive, and `uv tool uninstall` would then fail on the
        # running pythonw.exe. Recorded only when this run is what created them,
        # so a repair re-run does not delete the task it found.
        if ((-not $taskWasRegistered) -and (Test-TaskRegistered)) { $done += 'task' }
        if ((-not $taskFileWasThere) -and (Test-Path -LiteralPath $taskFile)) { $done += 'taskfile' }
        # `ciao setup` reports a workspace whose memory regions could not be set
        # up by exiting 3, which is not a failed install: everything else it
        # scaffolds is there, and a rollback would throw all of that away. Only
        # that one code is tolerated, the same way install-engine.sh tolerates it.
        switch ($setup.ExitCode) {
            0 { }
            3 { Write-Warning 'ciao setup could not set up memory regions (see the warning above); continuing install' }
            default { Fail 'ciao setup failed' }
        }

        if ($NoStart) {
            # -NoStart promised no service: no task registered (above), no
            # engine started, no URL. Everything an install that does not run
            # still owns has been written by this point.
            Write-Host ''
            Write-Host "Ciaobot $Version is installed: $ciao"
            Write-Host "Workspace: $workspace"
            Write-Host 'No logon task was registered and nothing was started. To register it later, run:'
            Write-Host "  $ciao setup --workspace $workspace --load-launchd"
            Write-Host "  $ciao service start --workspace $workspace"
            return
        }

        $started = Invoke-Native $ciao @('service', 'start', '--workspace', $workspace, '--json') $false
        if ($started.ExitCode -ne 0) { Fail "could not start the engine; try: $ciao service status" }

        # A slow first boot is not a failed install: the task is registered and
        # will come up. Say so instead of failing an install that did everything
        # it promised.
        $port = Read-WorkspacePort $workspace
        $healthy = $false
        for ($attempt = 1; $attempt -le $HealthAttempts; $attempt++) {
            if (Test-EngineAnswering $port) {
                $healthy = $true
                break
            }
            Start-Sleep -Seconds 1
        }
        if (-not $healthy) {
            # The same sentence install-engine.sh prints: a warning, not a
            # failure, and not a rollback either - the task is registered, so the
            # engine is on its way up.
            Write-Warning 'Ciaobot engine installer: the engine is still starting; check: ciao service status'
        }

        # Printed to the terminal and nowhere else: the URL is a one-time
        # credential that signs the person at this keyboard in. A failure must not
        # reach the user as "Open Ciaobot: " with nothing after it.
        $urlOutput = Invoke-Native $ciao @('setup-url', '--workspace', $workspace) $true
        $urlLines = @($urlOutput.Output | Where-Object { ([string]$_).Trim() -ne '' })
        $url = ''
        if ($urlLines.Count -gt 0) { $url = ([string]$urlLines[-1]).Trim() }
        if (($urlOutput.ExitCode -ne 0) -or -not $url.StartsWith('http://')) {
            Fail "could not create the sign-in link; run: $ciao setup-url --workspace $workspace"
        }

        Write-Host ''
        Write-Host "Ciaobot $Version is installed: $ciao"
        Write-Host "Workspace: $workspace"
        if ($pathEntry) {
            Write-Host "Added to your PATH for new terminals (and to this one): $pathEntry"
        }
        Write-Host "Open Ciaobot: $url"
        Write-Host 'This link signs you in once. Do not share it.'
        # Only when there is a terminal to open it in: redirected output means
        # this text is a log file, and a browser popping up over a CI log is
        # nobody's idea of a good time.
        if (-not [Console]::IsOutputRedirected) {
            try { Start-Process $url | Out-Null } catch { Write-Host "Could not open a browser; open the link above." }
        }
    } catch {
        $message = $_.Exception.Message
        if ($mutated) {
            # Never silent about a rollback that did not finish: what is left is
            # the user's to remove, and the message says which.
            $undone = @(Undo-Install -Steps $done -Uv $uv -ToolDirectory $toolDir -Workspace $workspace -PathEntry $pathEntry)
            if ($undone.Count -eq 0) {
                Write-Host "$message. This run's changes were undone."
            } else {
                Write-Host "$message. Some of this run's changes could not be undone: $($undone -join '; ')"
            }
        }
        throw
    } finally {
        [System.Net.ServicePointManager]::SecurityProtocol = $savedProtocol
        $env:UV_NO_MODIFY_PATH = $savedNoModify
        $env:UV_INSTALL_DIR = $savedInstallDir
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Install-Ciaobot -Version $Version -ReleaseDir $ReleaseDir -DryRun $DryRun.IsPresent -Workspace $Workspace -NoStart $NoStart.IsPresent -Uninstall $Uninstall.IsPresent
