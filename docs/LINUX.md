# Linux hosting

Ciaobot's Python backend and PWA can run on Ubuntu 24.04 with Python 3.12 and
Node 22. Use a browser or installed PWA from macOS, Windows, Linux, or a phone.
The agents execute on the server and use its files and credentials. Apple-native
voice and Apple Intelligence require a Mac host; select Claude/OpenCode models
for Linux routines. Native Windows and Linux desktop bundles are separate work.

## Install

Use a dedicated account and separate application code from workspace data.
The following administrator commands assume a source checkout (at the desired
release/ref) is already at `/opt/ciaobot/source`, including the Linux support.
The macOS release installer is not a Linux installer.

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

For HTTPS reverse proxy hosting, set these values in the workspace `.env`:

```dotenv
PWA_HOST=127.0.0.1
PWA_PORT=8443
PWA_AUTH_REQUIRED=true
CIAO_ALLOWED_ORIGINS=bot.example.com
```

Keep the generated `PWA_AUTH_TOKEN`. Authenticate provider CLIs as `ciaobot`,
not root. The Claude SDK includes a Claude binary; `ciao auth claude --print-only`
prints the login command. Run it from an interactive SSH session as that account.
Install OpenCode separately if using that provider, then run `ciao auth opencode`.
Provider OAuth may require completing a browser flow on your own computer.

The service account also needs a Git author identity for automatic workspace
snapshots. If it has no configured identity, set `GIT_AUTHOR_NAME`,
`GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, and `GIT_COMMITTER_EMAIL` in the workspace
`.env` to the identity you want recorded for automation (for example `Ciaobot`
and `ciaobot@localhost`). This does not change the administrator's Git settings.

## Service

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
service. Paths must be absolute. The unit preserves the virtualenv interpreter,
sets `HOME` and tool `PATH`, uses a private umask, and restarts on failure.
Subprocesses belong to the same service control group and are stopped with it.
The application handles its own `.env`; do not also use an `EnvironmentFile`.

Settings → Restart drains active chat work and re-executes the backend. A direct
`systemctl stop/restart` is an administrative stop, not that application drain:
wait for active work to finish first. Systemd allows up to 120 seconds for shutdown.

## HTTPS access

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

Linux source installs are updated by the administrator — never as the ciaobot
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

## Verification

Run `mypy ciao`, `pytest tests/`, and frontend tests/build from the checkout.
On the live host verify authenticated HTTP, event/chat WebSockets, a provider
turn and tool execution, archive/insights, schedule execution, Settings restart,
and recovery after a reboot. Missing provider authentication prevents real chat
verification even when the server, PWA, and automated tests are healthy.
