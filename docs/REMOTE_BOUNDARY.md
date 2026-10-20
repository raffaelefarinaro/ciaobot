# Remote-client boundary audit (issue #546)

## Superseded: the client-origin model is gone

This audit recorded the boundary that the multi-device client relay needed: a
remote host's UI and API mirrored onto a second Mac through `node_proxy.py`,
split across two loopback origins (`localhost` for proxied content, `127.0.0.1`
for local device controls) with a `X-Ciao-Local-Control: 1` capability header
and a one-time `/api/auth/bridge` to move a session between them.

Node mode is removed (#642). `ciao/node_proxy.py`, `ciao/node_state.py`,
`ciao/native_sessions.py` and `ciao/web/remote_boundary.py` are gone, along
with `/api/node/*`, `/api/device/*`, `/api/native/sessions`, `/api/auth/bridge`
and `/device/return`. There is no second origin to keep separate, so the
audit below is kept as history, not as a description of the code.

## The model that replaced it

**One engine, one origin, one browser.** A browser either reaches this engine's
own origin or it does not. There is no relay, no mirrored bundle, and no
capability header; the origin boundary that the relay used to need is now
carried by the session.

- `ciao/web/auth.py` is the whole boundary. `/api/*` requires the signed
  `ciao_session` cookie, minus a small public allowlist (`/api/auth`,
  `/api/auth/check`, `/api/startup-status`,
  `/api/setup-status`, `/api/setup/finish`, `/api/setup/list-dirs`,
  `/api/setup/inspect-folder`, `/api/setup/mkdir`). The setup routes answer only in first-run bootstrap
  mode and only to a loopback TCP peer (`is_loopback_client`) whose `Host`
  also names loopback: first run binds 0.0.0.0, so the `Host` header alone
  would let a LAN client claim `localhost`. Every `/ws/*` handshake is checked for same-origin and
  then for the session. Every state-changing `/api/*` request must present an
  `Origin` or `Referer` that matches the request host, allowing for a
  proxy-declared `X-Forwarded-Host`.
- `_LOOPBACK_ONLY_API` is the only peer-scoped surface: the loopback-only
  local feed (`/api/menubar-chats`; legacy route
  name, no native client), the update
  coordinator's drain handshake, and `/api/admin/restart-blockers` (the
  reason-tagged list `ciao service` gates restart/stop on). It requires a loopback TCP source address, a loopback `Host` (`localhost`, `127.0.0.1`, `::1`) and, when the request carries `Origin` or `Referer`, one naming this exact origin, for every method. The peer address is what makes a caller local; the `Host` check refuses a DNS-rebound page on this machine, which is a loopback peer whose `Origin` matches its own (attacker-chosen) `Host`. A request that fails takes the normal session path, so Settings still reads restart blockers with its session. A loopback peer carrying
  reverse-proxy headers (`Forwarded`, `Via`, any `X-Forwarded-*`,
  `X-Real-IP`, `X-Client-IP`, `True-Client-IP`, `CF-Connecting-IP`, any
  `Tailscale-*`) is not local: `tailscale serve` and
  other proxies on this machine connect from 127.0.0.1 on behalf of remote
  callers. `is_loopback_client` is the only "is it local" check in the
  codebase and nothing else grants access on it.
- Moving the install workspace (`/api/workspace-move/dirs`, `/plan` and the
  `POST`) needs the session *and* `is_loopback_client`, a localhost `Host` and a
  same-host `Origin`: browsing folders and moving the install are for the
  computer running Ciaobot. The peer check narrows a session route here; it
  grants nothing. A remote browser gets the CLI command instead.
- Opening a terminal on the engine's machine (`GET`/`POST /api/host/terminal`)
  needs the session *and* the same host-browser rule (`host_browser_request`:
  loopback peer, localhost `Host`, same-host `Origin`/`Referer`). It is
  deliberately not in `_LOOPBACK_ONLY_API`, because that set lets a loopback peer
  skip the session, and any local process or DNS-rebound page could then open
  terminals. The body carries an action id or a chat/project id, never a command
  or a path. `GET` reports `not_on_host` (200) to a signed-in remote browser so it
  can hide the buttons; `POST` answers 403.
- `POST /api/auth/settings` (change the PWA password) needs the session and,
  from anywhere but the host, the current password. A browser on the host
  (`host_browser_request`: loopback peer with no proxy headers, loopback `Host`,
  same-host `Origin`, plus an exact same-origin `Origin`/`Referer` check that
  includes the port) may skip the current password; the session is never
  skipped, so this route stays out of `_LOOPBACK_ONLY_API`. `GET
  /api/auth/settings` reports `on_host` from the same predicate so the card can
  hide the field. A `tailscale serve` or other proxied caller is not the host.
  An SSH local forward counts as the host; that caller can already read `.env`.
  Any password change re-keys the session signer, so every other device is
  signed out and must sign in with the new password.
- Model-authored HTML is served from `/api/workspace-html` under a sandboxing
  CSP (`sandbox allow-scripts`, no `allow-same-origin`, `connect-src 'none'`,
  `form-action 'none'`), so an artifact cannot reach the API or the local-only
  feeds at all. Coverage is in `tests/test_workspace_html.py`.
- Native Finder drops still send only a short-lived grant id, bounded display
  names, and opaque file references; the absolute path list stays in the
  server-side grant and is expanded only in the provider prompt.
- There is no native capability of any kind left: the macOS app and its shell
  are retired (`#579`), so a remote browser gets nothing a local one does not,
  and `trigger_app_update` does not exist. A browser is a local control path only
  through session routes that also require the host: `/api/workspace-move*`,
  `/api/host/terminal`, `POST /api/settings/providers/{provider}/connect` and
  `POST /api/auth/settings` (current-password exemption only)
  check the loopback peer, a loopback `Host` and a same-host `Origin`;
  `/api/workspace-open` checks the loopback peer only. Each refuses a non-host
  peer; Connect answers a remote browser 403 `not_on_host` with the sign-in
  `command` to run on the host.

A second device installs its own engine and reads `GET /api/addresses` to find
this one. There is nothing to unpair and no session to bridge.

## Verification

- `PYTHONPATH=$PWD python -m pytest -n auto tests/`
- `mypy ciao`
- `cd web && npm test` and `cd web && npm run build`
  There is no `desktop/src-tauri` gate any more: the shell and its Rust tree are
  gone and the release is the engine (`#655`, `#656`).

## Remaining work (not claimed resolved)

1. Replace the dashboard password with named, revocable per-device credentials
   bound to a stable engine identity, and require verified TLS by default.
2. Constrain workspace file/image/write/open APIs to authorized roots with
   resolved-symlink checks and explicit secret/runtime/provider-path denials.
3. The session cookie is a browser credential, not a native control channel.
   Anything that must not be reachable from a browser at all needs one.
4. Isolate service workers, caches, local storage, drafts, and notification
   cursors per installation, and clear them on reinstall.
5. Negotiate a versioned engine identity before writes, so a browser that
   reached an incompatible engine is refused rather than half-served.
6. Separate app-only and engine-only update state and complete protocol
   coverage across every HTTP, WebSocket, reconnect, and revocation path.

Direct app-only remote mode should not be treated as safe until all six items
above are implemented.
