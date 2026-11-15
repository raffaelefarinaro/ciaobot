# Design only. Invoke from a trusted PowerShell -NoProfile session after review.
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Repository,
    [Parameter(Mandatory=$true)][string]$TempRoot,
    [string]$HostedRunner = "local"
)
$ErrorActionPreference = "Stop"
$exitCode = 1
$root = $null
try {
    # Resolve executable paths before removing environment; no interpreter yet.
    $pythonExe = (Get-Command $Python -CommandType Application).Source
    $gitExe = (Get-Command git -CommandType Application).Source
    $repo = (Resolve-Path -LiteralPath $Repository).Path
    $bootstrap = Join-Path $PSScriptRoot "bootstrap.py"
    $harness = Join-Path $PSScriptRoot "baseline.py"
    $root = Join-Path (Resolve-Path -LiteralPath $TempRoot).Path ("ciao-3061-" + [Guid]::NewGuid().ToString("N"))
    [IO.Directory]::CreateDirectory($root) | Out-Null
    $keep = @{}
    foreach ($name in @("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE")) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($null -ne $value) { $keep[$name] = $value }
    }
    foreach ($item in @(Get-ChildItem Env:)) {
        [Environment]::SetEnvironmentVariable($item.Name, $null)
    }
    foreach ($name in $keep.Keys) { [Environment]::SetEnvironmentVariable($name, $keep[$name]) }
    $folders = @{
        HOME="home"; USERPROFILE="home"; LOCALAPPDATA="localappdata"; APPDATA="appdata"
        XDG_CONFIG_HOME="config"; XDG_STATE_HOME="state"; XDG_CACHE_HOME="cache"
        CIAO_MEMORY_DIR="memory"; CIAO_LAUNCH_AGENTS_DIR="launchagents"; CIAO_QUEUE_LOCK_DIR="locks"
        TMPDIR="tmp"; TMP="tmp"; TEMP="tmp"
    }
    foreach ($name in $folders.Keys) {
        $path = Join-Path $root $folders[$name]
        [IO.Directory]::CreateDirectory($path) | Out-Null
        [Environment]::SetEnvironmentVariable($name, $path)
    }
    # -I: no script/CWD/user-site/env import paths. -S: no site/.pth hooks.
    # -B: no bytecode caches. These apply from FIRST interpreter startup.
    & $pythonExe -I -S -B $bootstrap $gitExe $repo $root $harness $HostedRunner
    $exitCode = $LASTEXITCODE
} catch {
    # Never emit native exception text, environment values or credential paths.
    Write-Output '{"event":"BLOCKED","reason":"external-launcher-error-redacted"}'
} finally {
    try {
        if ($exitCode -eq 0 -and $null -ne $root) {
            if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw "cleanup ownership" }
            $info = Get-Item -LiteralPath $root -Force
            if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "cleanup link" }
            # Bootstrap returns zero only after matching the owned parent's cleanup receipt.
            # Only the GUID directory created above; never an arbitrary caller tree.
            Remove-Item -LiteralPath $root -Recurse -Force
            if (Test-Path -LiteralPath $root) { throw "cleanup incomplete" }
        }
    } catch {
        $exitCode = 1
        Write-Output '{"event":"BLOCKED","reason":"launcher-cleanup-unconfirmed-redacted"}'
    }
    # Failure: preserve only this disposable fixture for review; hosted VM
    # teardown removes it. Local cleanup requires inspected, link-aware removal.
}
exit $exitCode
