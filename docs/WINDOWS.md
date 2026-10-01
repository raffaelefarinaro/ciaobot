# Windows (preview)

Ciaobot's engine runs natively on Windows 11. This is a **preview**: it is the
same engine and the same PWA as on macOS, installed per user, and the first
Windows releases may still have rough edges. Report problems at
<https://github.com/raffaelefarinaro/ciaobot/issues> and say you are on Windows.

## Requirements

- Windows 11 (build 22000 or newer), x64 or ARM64. Windows 10 is not supported.
- PowerShell 5.1 or newer (the one that ships with Windows is enough).
- No administrator rights. Everything is installed for your own account.
- Internet access to GitHub for the install.
- A browser. The PWA is served by the engine at `http://localhost:8443` (the port
  is `PWA_PORT` in the workspace `.env`).

## Install

In PowerShell:

```powershell
irm https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.ps1 | iex
```

When it finishes it prints a one-time sign-in link and opens it in your browser.
The link signs you in once. Do not share it. If you lose it, run
`ciao setup-url --workspace <your workspace folder>` for a new one.

`iex` cannot take arguments, so to pass an option run the script as a script
block:

```powershell
& ([scriptblock]::Create((irm https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.ps1))) -Workspace 'D:\Ciaobot' -NoStart
```

Options:

| Option | What it does |
|---|---|
| `-Workspace <folder>` | Put the workspace (your notes, memory and settings) in this folder instead of `%USERPROFILE%\Ciaobot`. |
| `-NoStart` | Install the engine but do not register the logon task, do not start it and do not print a sign-in link. The installer prints the two commands that finish the job later. |
| `-Uninstall` | Remove what the installer added (see Uninstall). Your workspace is kept. |
| `-Version <x.y.z>` | Install a specific release instead of the latest. |
| `-DryRun` | Download the release and verify it against its signed manifest, then stop without installing anything. Needs uv to be installed already. |

(`-ReleaseDir <folder>` installs from a local release folder and needs `-Version`;
it is for testing a release candidate.)

If your execution policy blocks the one-liner, save `install.ps1` from the release
and run it with
`powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`.

The script is not Authenticode-signed, so SmartScreen may warn about a saved
copy. Its trust anchor is different: it verifies a signed release manifest and
the wheel's SHA-256 before it installs anything, and it stops if either does
not match.

## What the installer puts on your machine

| What | Where |
|---|---|
| The engine, in its own `uv tool` environment (Python 3.13, installed with `--link-mode copy` so nothing in it is a hard link into uv's cache) | `uv tool dir` (usually `%APPDATA%\uv\tools\ciaobot`) |
| The `ciao` command | `ciao.exe` in `uv tool dir --bin` (usually `%USERPROFILE%\.local\bin`), added to your **user** `PATH` through the registry |
| uv itself, only if you did not already have it | `%USERPROFILE%\.local\bin\uv.exe` (not added to `PATH`) |
| Your workspace | `%USERPROFILE%\Ciaobot`, or the folder you gave `-Workspace` |
| The logon task `\Ciaobot\Engine` | Task Scheduler (per user), defined by `%LOCALAPPDATA%\Ciaobot\service\Ciaobot-Engine.xml` |
| What the installer needs to undo itself | `%LOCALAPPDATA%\Ciaobot\install-state.json` |
| The install receipt | `%USERPROFILE%\.local\state\ciaobot\install-receipt.json` |

The installer refuses to overwrite a `ciao.exe` it did not install: move the
other one away and run it again. Running it again over its own install is a
repair, and if any step fails it undoes the steps that run completed.

New terminals see `ciao` straight away; open a new terminal if the current one
does not.

## How the engine runs

The installer registers a per-user Task Scheduler task, `\Ciaobot\Engine`. It
starts when you sign in to Windows, runs under your account without elevation,
and runs `pythonw.exe -m ciao.cli supervise` with your workspace as its working
directory. `pythonw.exe` has no console window. `ciao supervise` starts the
engine as a child process and starts it again when the engine asks for a
restart (Settings -> Restart) or when the task is restarted after a failure
(Task Scheduler retries every minute).

The task runs while you are signed in, not at boot. Sign in and the engine
comes up; sign out and it stops.

Manage it with `ciao service`:

```powershell
ciao service status    # is the task registered, and does the engine answer on its port
ciao service start     # run the task (registers it first if it is missing)
ciao service stop      # end the task and the engine with it
ciao service restart   # stop, then start
```

`stop` and `restart` refuse while a chat is running; add `--force` to stop it
anyway. The macOS desktop actions of `ciao service` (`login`, `migrate`,
`migration-classify`, `rollback`) and `update-engine` are not available on
Windows and say so.

## Logs

Because the engine runs without a console, its output goes to two files in the
workspace's runtime folder:

- `<workspace>\.runtime\ciao.stdout.log`
- `<workspace>\.runtime\ciao.stderr.log`

For example, `Get-Content "$env:USERPROFILE\Ciaobot\.runtime\ciao.stderr.log" -Tail 100`.

## Connect your agent

Ciaobot has no model account of its own. It drives a CLI you have signed in to.

**Claude Code.** Install it with its native Windows installer, then sign in:

```powershell
irm https://claude.ai/install.ps1 | iex
claude auth login
```

**OpenCode.** Ciaobot needs OpenCode 2.0.16 or newer in the 2.x line. It installs
with npm, so you need Node.js first:

```powershell
npm install -g @opencode/cli
ciao auth opencode
```

See [INTEGRATIONS.md](../INTEGRATIONS.md#opencode) for what the 1.x package names
break and for other install routes.

**How the engine finds these tools.** The engine does not inherit the PATH of the
terminal you happened to start it from. It reads the two `Path` values in the
registry that every new sign-in is built from (the machine one, then your user
one), so a tool you can run by name in a new terminal is found by the engine
without restarting it. npm installs `opencode.cmd` and `opencode.ps1` wrapper
files next to the real program; the engine runs the real program behind the
wrapper, and runs script-based npm tools such as `gws` as `node.exe <script>`
with the arguments passed as a list. It never goes through `cmd.exe`. If OpenCode
is installed somewhere that is not on `PATH`, set `CIAO_OPENCODE_BIN` to its full
path (see [INTEGRATIONS.md](../INTEGRATIONS.md)).

Workspace snapshots and the memory backup use Git. If `git --version` fails in a
new terminal, install [Git for Windows](https://gitforwindows.org/).

## Known differences from macOS

- No Apple Intelligence (it needs a Mac host) and no macOS desktop shell. Pick
  Claude or OpenCode models.
- The engine starts at your logon, not at boot, and stopping it is a hard stop
  of the whole process tree (hence the `--force` on busy chats).
- Files that hold secrets (`.env`, `secrets\`, receipts) are protected with a
  Windows ACL that grants only your account and SYSTEM, since Windows has no
  `0600` mode bits. If the ACL cannot be set, the write fails instead of
  continuing with a readable file.
- The skill, command and subagent mirrors under `.claude\` and `.opencode\` use
  NTFS junctions (folders) and hard links with a `<name>.ciao-link` marker file
  (files) instead of symlinks, so Developer Mode is not required. Do not delete
  the `.ciao-link` files by hand: they are how Ciaobot knows a file is its own.
- Workspace Git repositories are set to `core.autocrlf=false`, so Git for
  Windows stores the bytes Ciaobot wrote and your notes are not rewritten with
  Windows line endings on commit. A value you set yourself in a repo is left
  alone.
- Defender real-time scanning made no measurable difference to stopping the
  engine or freeing its files (about 1.5 s either way, measured in the #857
  spike). Not measured: how deep vault or `node_modules` paths behave beyond 260
  characters. If you hit a "path too long" error, please open an issue with the
  path and what you were doing.

## Update and rollback

Engine updates on Windows land with #857. Until then:

- `ciao service update-engine` is not available on Windows.
- Settings -> Home update buttons are not supported on Windows yet.
- Re-running `install.ps1` repairs a half-finished install; it is not an
  updater.

This section will be replaced when #857 ships.

## Uninstall

```powershell
& ([scriptblock]::Create((irm https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.ps1))) -Uninstall
```

This ends and deletes the `\Ciaobot\Engine` task and its XML, uninstalls the
engine's `uv tool` environment, deletes the install receipt and the installer's
state file, and removes the `PATH` entry the installer added (and only that
entry). It leaves `uv` in place, because it runs other tools, and it leaves your
workspace folder untouched; the script prints where the workspace is. Delete the
folder yourself if you want your notes gone.

If a step cannot finish (a locked file, for example), the script says which one
and prints the command that completes it.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `ciao` is not recognised | Open a new terminal. If it still fails, check that the directory printed by `uv tool dir --bin` is in your user `PATH` (`[Environment]::GetEnvironmentVariable('Path','User')`). |
| The browser shows nothing at `http://localhost:8443` | `ciao service status`. If the task is registered but the engine is not reachable, read `<workspace>\.runtime\ciao.stderr.log`, then `ciao service restart`. Another program may already use the port: change `PWA_PORT` in the workspace `.env` and restart. |
| The engine is not running after a restart of Windows | The task starts when you sign in, not at boot. Sign in, or run `ciao service start`. Confirm the task exists: `schtasks /Query /TN \Ciaobot\Engine`. |
| `Ciaobot engine task is not registered` | Run `ciao setup --workspace <folder> --load-launchd --yes`, then `ciao service start`. |
| Installer says a `ciao.exe` exists and was not installed by Ciaobot | Another program put a `ciao.exe` in the uv bin directory. Move it away and run the installer again. |
| The installer stops with a manifest or digest error | The download did not match the signed manifest. Run it again; if it repeats, open an issue with the version. |
| PowerShell blocks the script | Use the `-ExecutionPolicy Bypass -File` form shown under Install. |
| A provider shows as not installed although it works in your terminal | `ciao` and the engine read the registry `Path`, not the PATH of an already-open terminal. Install the tool, open a new terminal, and check Settings again; for OpenCode outside `PATH` set `CIAO_OPENCODE_BIN`. |
| You lost the sign-in link | `ciao setup-url --workspace <folder>` prints a new one. |