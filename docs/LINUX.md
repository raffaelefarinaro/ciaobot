# Linux hosting

Ciaobot's engine and PWA run on Ubuntu 24.04 (a similar distribution works) as
a systemd service under a dedicated account. Use a browser or installed PWA
from macOS, Windows, Linux, or a phone. The agents execute on the server and
use its files and credentials. Apple Intelligence requires a Mac host; select
Claude/OpenCode models for Linux routines. A native Linux desktop bundle is
separate work.

## Install (one line)

Run the installer as the account that will own Ciaobot — a dedicated account,
never root:

```sh
curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh
```

The installer installs this release's engine wheel for the invoking user with
uv (bootstrapping uv into `~/.local/bin` first when the machine has none, with
Python 3.13 pinned for the engine), runs `ciao setup`, and — unless it is
given `--no-service` — asks for `sudo` once to install and start the systemd
service through `ciao service install`: `sudo` is needed only to write
`/etc/systemd/system/ciaobot.service` root-owned. `ciao` itself lands in the
account's uv tool bin directory (by default `~/.local/bin`; the installer
prints the exact path). Every download is pinned: the release wheel is
compared against the sha256 stamped into the installer before anything is
installed, and the uv release archive is digest-pinned and checked before it
is extracted. `ciao setup` generates the dashboard password in the workspace
`.env` and prints a one-time login URL; existing passwords and configuration
are preserved on rerun.

Flags (pass what follows to the script after `sh -s --` when piping):

| Flag | Meaning |
| ---- | ------- |
| `--workspace DIR` | set up `DIR` (default: `$HOME/Ciaobot`, only when the machine has no Ciaobot unit yet; on a machine whose unit is already installed, setup is skipped and the workspace the unit records is kept) |
| `--no-service` | skip the systemd unit: the engine is installed and you start it yourself |
| `--replace-unit` | have `ciao service install` take over a unit that belongs to a source install (see [Existing source installs](#existing-source-installs)); without it such a machine is refused with exit 3 |
| `--wheel PATH` | install this local wheel instead of downloading the release wheel (CI and rehearsal) |
| `-h` | print usage and exit |

Keep the generated `PWA_AUTH_TOKEN`. Authenticate provider CLIs as the service
account, not root. Claude uses the `claude` CLI installed on the machine (the ACP adapter drives it);
`ciao auth claude --print-only` prints the login command. Run it from an interactive SSH session as that account.
Install OpenCode 2 separately if using that provider (`npm install -g @opencode/cli`, see [INTEGRATIONS.md](../INTEGRATIONS.md#opencode)), then run `ciao auth opencode`.
Codex works the same way: install `@openai/codex` yourself, then run `ciao auth codex`. When the codex CLI is present, the pinned `@agentclientprotocol/codex-acp` adapter is used if it is already on the PATH at that version; otherwise the engine installs it on first start into its own folder, as described in [INTEGRATIONS.md](../INTEGRATIONS.md#codex). The installer has nothing to do for either adapter.
Provider OAuth may require completing a browser flow on your own computer.

The service account also needs a Git author identity for automatic workspace
snapshots. If it has no configured identity, set `GIT_AUTHOR_NAME`,
`GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, and `GIT_COMMITTER_EMAIL` in the workspace
`.env` to the identity you want recorded for automation (for example `Ciaobot`
and `ciaobot@localhost`). This does not change the administrator's Git settings.

## Service

`ciao service install` — the command the installer runs, and the way to
(re)install the unit after the fact — renders the unit for the invoking
account (the account the terminal is in, from its user id, not `$HOME`) and
installs it through a fixed `sudo sh -c` script that stages
`/etc/systemd/system/ciaobot.service` in a root-owned private directory, runs
`systemd-analyze verify` on it, and installs it mode `0644` owned by root. It
then runs `sudo systemctl daemon-reload`,
`sudo systemctl enable ciaobot.service` and, unless `--no-start`,
`sudo systemctl restart ciaobot.service`. When one step fails, the steps
before it have already run, so the failed step is printed and the command
exits 1: inspect with `systemctl status ciaobot` before retrying.

Before changing the installed unit, look at what would change:

```sh
ciao service install --workspace /srv/ciaobot --dry-run
```

prints the unit followed by one `# would run: …` line per command and runs
nothing. `--workspace` overrides the workspace the installed unit records;
`--replace` takes a unit over on purpose (see [Existing source
installs](#existing-source-installs)); `--no-start` installs and enables the
unit without restarting it.

Inspect and operate the service with systemd tools:

```sh
systemctl status ciaobot
sudo journalctl -u ciaobot -n 100 --no-pager
```

The unit preserves the engine interpreter it was installed with, sets `HOME`
and tool `PATH`, uses a private umask, and restarts on failure. Subprocesses
belong to the same service control group and are stopped with it. The
application handles its own `.env`; do not also use an `EnvironmentFile`.

The unit is enabled by the install, so the service starts at boot. Ciaobot
does not read or change a Linux engine's start-at-sign-in state: an
administrator who disabled the unit re-enables it with
`sudo systemctl enable --now ciaobot`.

Settings → Restart drains active chat work and re-executes the backend. A direct
`systemctl stop/restart` is an administrative stop, not that application drain:
wait for active work to finish first. Systemd allows up to 120 seconds for shutdown.

`ciao linux-service` still renders unit text and installs nothing; the
source-install flow below uses it to render a unit by hand.

## Existing source installs

Everything in this section keeps working. This is a root-administered source
checkout: code and venv under `/opt/ciaobot`, workspace `/srv/ciaobot`,
service account `ciaobot`.

```sh
sudo apt-get update
sudo apt-get install python3-venv git
sudo useradd --create-home --home-dir /var/lib/ciaobot --shell /bin/bash ciaobot
# Application code and the virtualenv stay administrator-owned and are never
# writable by the service account: a compromised ciaobot account must not be
# able to modify the code it runs. Otherwise it could rewrite the source, the
# venv launcher, or the linux-service output and escalate through the
# root-installed unit (whose User/ExecStart verify accepts on syntax alone).
sudo install -d -o root -g root /opt/ciaobot
# The workspace holds private vault notes and the one-time setup token, so it
# must not be world-readable: omitting -m defaults to 0755, and the default
# 0022 umask would leave initial notes and .runtime/setup-token readable by
# any other local account (the token redeems via the loopback setup route).
sudo install -d -m 0700 -o ciaobot -g ciaobot /srv/ciaobot
sudo python3 -m venv /opt/ciaobot/venv
sudo /opt/ciaobot/venv/bin/pip install -e /opt/ciaobot/source
sudo npm --prefix /opt/ciaobot/source/web ci
sudo npm --prefix /opt/ciaobot/source/web run build
sudo -u ciaobot -H /opt/ciaobot/venv/bin/ciao setup --workspace /srv/ciaobot
```

Install Node 22 before building; alternatively build the PWA on another machine
and transfer `ciao/web/static/` with the source. The server does not need Vite.
Setup generates the dashboard password in `/srv/ciaobot/.env`, mode `0600`, and
prints a one-time login URL. Existing passwords and config are preserved on rerun.

Generate and inspect the unit, then install it explicitly:

```sh
# Stage under a root-owned private directory, never a fixed /tmp path: on a
# multi-user VPS another local account could pre-create /tmp/ciaobot.service
# and swap its content between verification and installation, and the
# installed unit runs as root. mktemp -d creates the staging directory mode
# 0700 owned by root, and tee creates the file root-owned inside it.
stage=$(sudo mktemp -d)
/opt/ciaobot/venv/bin/ciao linux-service \
  --workspace /srv/ciaobot --user ciaobot --home /var/lib/ciaobot \
  --python /opt/ciaobot/venv/bin/python | sudo tee "$stage/ciaobot.service" > /dev/null
sudo systemd-analyze verify "$stage/ciaobot.service"
sudo install -m 644 "$stage/ciaobot.service" /etc/systemd/system/ciaobot.service
sudo rm -rf "$stage"
sudo systemctl daemon-reload
sudo systemctl enable --now ciaobot
sudo systemctl status ciaobot
sudo journalctl -u ciaobot -n 100 --no-pager
```

`linux-service` only renders text: it neither creates an account nor changes a
service. Paths must be absolute.

`ciao service install` refuses a unit that belongs to a different install: one
whose `ExecStart` interpreter is not this environment's Python (the source
checkout install this section describes), or one that records a different
`CIAO_WORKSPACE` or `User=` than the unit it would write. Overwriting it would
repoint (or break) an install that already exists. Either keep the source
install and continue administering it as the Service, Updates and backups and
Uninstall sections describe, or take the unit over deliberately with `ciao service
install --replace`; removing the old checkout and virtualenv once nothing else
uses them is then up to the administrator.

To migrate a source install to the one-line install, stop the unit once active
work has drained, then run the installer as the service account, pointing it
at the workspace the unit serves and letting it take the unit over:

```sh
sudo systemctl stop ciaobot
sudo -iu ciaobot
curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | \
  sh -s -- --workspace /srv/ciaobot --replace-unit
exit
systemctl status ciaobot
```

Setup preserves the existing workspace configuration, and `ciao service
install --replace` rewrites the unit with the same `User=` and workspace but
the uv tool environment's interpreter. After the run the engine lives in the
service account's uv tool directory (`~/.local/bin/ciao`) and not in code an
administrator owns under `/opt`: the account owns the code it runs. Remove
`/opt/ciaobot` and its virtualenv once nothing else uses them.

## Moving the workspace

Editing `CIAO_WORKSPACE` in the workspace `.env` cannot move it: the engine
finds that `.env` inside the folder the unit names. Move it as the
administrator, on the same disk:

```sh
sudo /opt/ciaobot/venv/bin/ciao workspace-move /srv/new-folder          # plan only
sudo /opt/ciaobot/venv/bin/ciao workspace-move /srv/new-folder --apply
```

It drains running chats, stops the unit, renames the folder, rewrites the old
path in `.env`, `.runtime` and the command links (keeping every file owned by the
service account), copies the account's Claude Code sessions to the new folder's
name, repoints `WorkingDirectory=` and `CIAO_WORKSPACE` in
`/etc/systemd/system/ciaobot.service`, runs `daemon-reload` and starts the unit.
If the engine does not come back, everything is put back. It refuses a folder
the service account cannot enter. Settings → Advanced → Ciaobot folder shows this command
instead of a Move button, since the service account cannot stop its own unit.
To undo, move it back.

Bare-shell discovery (`ciao` run outside the service) and the Memory handover
gate read the workspace from `/etc/systemd/system/ciaobot.service`. They trust
that file only while it is owned by root and not group- or world-writable, and
they never consult the caller's environment. A workspace the current user
cannot read is skipped.

## HTTPS access

For HTTPS reverse proxy hosting, set Settings → Access & devices → Network access to
**This computer only** (the bind address, `127.0.0.1`) and restart the service,
and keep the port in the workspace `.env`:

```dotenv
PWA_PORT=8443
```

The proxy must forward the public host as `X-Forwarded-Host` (nginx:
`proxy_set_header X-Forwarded-Host $host;`), or WebSocket upgrades from the
browser are rejected as cross-origin.

Use Tailscale Serve for private HTTPS access, or point a domain at the VPS and
put Caddy in front of the loopback backend. A minimal Caddyfile is:

```caddyfile
bot.example.com {
    reverse_proxy 127.0.0.1:8443
}
```

Allow SSH and the chosen HTTPS ingress through the firewall. Keep port 8443
on loopback. Caddy handles WebSocket upgrades; HTTPS enables PWA browser
features. For initial local-only browser onboarding, use an SSH tunnel:

```sh
ssh -L 8543:127.0.0.1:8443 your-admin@your-vps
```

Then visit `http://localhost:8543`. A workspace initialized by CLI can be used
directly at the HTTPS hostname with its dashboard password.

## Updates and backups

Run `ciao update` as the account that owns the install to update a one-line
install. It downloads the latest installer and runs it with the flags you pass
(for example `ciao update --no-service`). The installer re-downloads the release
wheel, checks it against the stamped digest, refreshes the engine with
`uv tool install --force`, and re-installs and restarts the service unit; the
workspace, its `.env` and its configuration are kept. `ciao update status`
prints the last update record. Never update as root.

The `sudo` step is the one that can fail. A run with no terminal (for example
one started by a background caller) cannot answer the `sudo` password prompt,
so it fails at the unit step, after `uv tool install --force` has already put
the new code in place. The unit file is then not reinstalled and the service is
not restarted by that run. Run `ciao update` from an interactive shell, so
`sudo` can prompt, before relying on the new release.

`ciao update` on a source checkout refuses with exit status 2 and tells you to
update it with git. Source checkouts are updated as described in the next
section, not by the installer.

Source installs are updated by the administrator — never as the ciaobot
account, which owns neither the source checkout nor the virtualenv. Pin the
chosen source revision, build its frontend, install its Python dependencies,
and restart the service after active work has drained. For staging, prepare a
separate release directory and virtualenv and change the unit's interpreter
path at cutover. Preserve the prior application release for rollback.

Before upgrading, stop the service and back up `/srv/ciaobot` (including `.env`,
`.runtime`, per-workspace agent roots and vaults) and `/var/lib/ciaobot` (provider
credentials and native sessions). Include any separately configured vault paths.
Store backups privately. Git does not include runtime state or credentials.
If an upgrade migrates data, rollback requires its matching pre-upgrade backup,
not just the previous code. Keep only one active host scheduler during migration.

## Uninstall

Run as the account that owns the install; the `sudo` steps remove the unit:

```sh
sudo systemctl disable --now ciaobot && sudo rm /etc/systemd/system/ciaobot.service && uv tool uninstall ciaobot
```

The workspace is kept: the folder with its vault, `.env` and provider
credentials stays on disk, and deleting it is a separate decision the operator
makes by hand. The dedicated account stays until the administrator removes it
with `userdel`, and a migrated source install's `/opt/ciaobot` checkout and
virtualenv are removed the same way.

## Open in Terminal

The Open in Terminal buttons only work when the engine can reach a desktop
session. The documented systemd system service has no `DISPLAY` or
`WAYLAND_DISPLAY`, so the buttons stay hidden. A desktop user running the engine
in their own graphical session gets the first terminal found from
`x-terminal-emulator`, `gnome-terminal`, `konsole`, `xfce4-terminal`, `kitty`
and `alacritty`. A terminal started by the engine is in its cgroup and closes
when the service stops.

## Verification

Run `mypy ciao`, `pytest tests/`, and frontend tests/build from the checkout.
On the live host verify authenticated HTTP, event/chat WebSockets, a provider
turn and tool execution, archive/memory pass, schedule execution, Settings restart,
and recovery after a reboot. Missing provider authentication prevents real chat
verification even when the server, PWA, and automated tests are healthy.
