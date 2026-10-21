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
  `Origin` or `Referer` that matches the request host, the request scheme
  (normalising `ws`/`wss`) and the effective port. A request that carries a
  proxy-declared `X-Forwarded-Host` naming that origin's host is accepted on
  the host alone: a TLS-terminating proxy legitimately presents http to the
  engine while the browser's origin is https, and the port may differ across
  the hop too. A browser cannot set `X-Forwarded-Host`, so only a fronting
  proxy declares it.
- `_LOOPBACK_ONLY_API` is the only peer-scoped surface: the loopback-only
  local feed (`/api/menubar-chats`; legacy route
  name, no native client), the update
  coordinator's drain handshake, `/api/admin/restart-blockers` (the
  reason-tagged list `ciao service` gates restart/stop on), and `POST /api/auth/host-sign-in` (it mints a one-time setup token and opens it, through an owner-only file, in the host's own default browser; it returns nothing a caller could reuse, so another local user or process can at most open a tab on the owner's screen, 10 s cooldown; the redirect port is the socket's own listening port, never the Host header's, and the token file is recreated owner-only on every mint). It requires a loopback TCP source address, a loopback `Host` (`localhost`, `127.0.0.1`, `::1`) and, when the request carries `Origin` or `Referer`, one naming this exact origin, for every method. The peer address is what makes a caller local; the `Host` check refuses a DNS-rebound page on this machine, which is a loopback peer whose `Origin` matches its own (attacker-chosen) `Host`. A request that fails takes the normal session path, so Settings still reads restart blockers with its session. A loopback peer carrying
  reverse-proxy headers (`Forwarded`, `Via`, any `X-Forwarded-*`,
  `X-Real-IP`, `X-Client-IP`, `True-Client-IP`, `CF-Connecting-IP`, any
  `Tailscale-*`) is not local: `tailscale serve` and
  other proxies on this machine connect from 127.0.0.1 on behalf of remote
  callers. `is_loopback_client` is the only "is it local" check in the
  codebase and nothing else grants access on it.
- `host_browser_request` (`ciao/web/routes_api.py`) is the "browser on the
  computer running Ciaobot" predicate, and it is an **exact origin** check, not
  a host-name check. It requires all of:
  - a loopback TCP peer (`is_loopback_client`): a real local connection, with
    no reverse-proxy headers, so a proxied caller is not the host;
  - a local `Host`: `localhost`, `127.0.0.1`, `::1`, or the accepted local
    spelling `0.0.0.0` (users copy that from the bind-address log; it is a
    local `Host` spelling gated by the loopback peer, not a loopback network
    address);
  - a browser origin that matches the request's own origin in **scheme, host
    and effective port**. `Origin` is the browser's declaration and takes
    precedence; `Referer` is only read when there is no `Origin`. A
    cross-port (`http://localhost:3000` against an engine on `:8443`),
    cross-scheme, cross-host, `null`, malformed or credential-bearing value is
    a refusal. A request carrying neither header is not a browser request and
    keeps the peer/Host gating alone.
  The port matters: two pages on `localhost` at different ports are different
  origins, and only the engine's own page is the host browser.
- Moving the install workspace (`/api/workspace-move/dirs`, `/plan` and the
  `POST`) needs the session *and* `is_loopback_client`, a local `Host` and a
  same-origin `Origin`: browsing folders and moving the install are for the
  computer running Ciaobot. The peer check narrows a session route here; it
  grants nothing. A remote browser gets the CLI command instead.
- Opening a terminal on the engine's machine (`GET`/`POST /api/host/terminal`)
  needs the session *and* the same host-browser rule (`host_browser_request`:
  loopback peer, local `Host`, same-origin `Origin`/`Referer`). It is
  deliberately not in `_LOOPBACK_ONLY_API`, because that set lets a loopback peer
  skip the session, and any local process or DNS-rebound page could then open
  terminals. The body carries an action id or a chat/project id, never a command
  or a path. `GET` reports `not_on_host` (200) to a signed-in remote browser so it
  can hide the buttons; `POST` answers 403.
- `POST /api/auth/settings` (change the PWA password) needs the session and,
  from anywhere but the host, the current password. A browser on the host
  (`host_browser_request`: loopback peer with no proxy headers, local `Host`,
  and an exact same-origin `Origin`/`Referer` that includes the port) may skip
  the current password; the session is never
  skipped, so this route stays out of `_LOOPBACK_ONLY_API`. `GET
  /api/auth/settings` reports `on_host` from the same predicate so the card can
  hide the field. A `tailscale serve` or other proxied caller is not the host.
  An SSH local forward counts as the host; that caller can already read `.env`.
  Any password change re-keys the session signer, so every other device is
  signed out and must sign in with the new password.
- The browser terminal's grant (`ciao_terminal`) is revoked by the auth
  actions, and it is revoked by the *bearer*, never by the session. A
  `POST /api/auth/logout` revokes the grant that request presents and
  nothing else: two browsers signed in inside the same second can hold
  byte-identical `ciao_session` cookies, so the login cookie cannot name
  a browser, and revoking "the grant for this session" would end another
  browser's terminal. A logout that presents no terminal cookie revokes
  nothing. A *successful* `POST /api/auth/settings` password change ends
  **every** grant, on every browser, and clears the `ciao_terminal` cookie
  — the password is the terminal's own credential, so every browser has to
  unlock again, including the one that made the change. A refused password
  change ends nothing. The grant is revoked here; the terminal socket's own
  re-check against this store is the terminal-stream child's work and does
  not exist yet, so no socket is closed by this today. The signed session
  cookie remains required for every route either way, and neither action
  names a user or a device: there are no per-device identities to revoke
  against.
- The welcome card's `GET /api/welcome/password` and `POST /api/welcome/open-folder`
  need the session *and* `host_browser_request`, and are not in
  `_LOOPBACK_ONLY_API`. A tailnet visitor through `tailscale serve` fails
  `is_loopback_client` (the `Tailscale-*`/`X-Forwarded-For` headers) and the
  loopback `Host` check. The password is returned only while
  `.runtime/welcome-pending` exists; dismissing or changing the password deletes
  it. An SSH port-forward looks local, and anyone with SSH to the host can
  already read `.env`.
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
  through session routes that also require the host: `/api/workspace-move/dirs`,
  `/api/workspace-move/plan`, `POST /api/workspace-move`,
  `/api/host/terminal`, `GET /api/welcome/password`, `POST /api/welcome/open-folder`,
  `POST /api/settings/providers/{provider}/connect` and `POST /api/auth/settings`
  (current-password exemption only)
  check the loopback peer, a local `Host` and an exact same-origin `Origin`
  (scheme, host and effective port);
  `GET /api/workspace-move` is session-only and reports `local: false` for a
  non-host browser;
  `/api/workspace-open` checks the loopback peer only. Each refuses a non-host
  peer; Connect answers a remote browser 403 `not_on_host` with the sign-in
  `command` to run on the host.

- **Memory handover widens what unattended text can reach.** At `memory_handover: all`
  (operator-only, `PATCH /api/workspaces/{name}`), the nightly run may accept a
  queued `[memory]`/`[profile]` fact into the always-loaded `AGENTS.md` regions, and
  that queued text can originate in untrusted sources (imported chats, fetched
  pages). The exposure is bounded, not removed: the write goes through the
  event-shape and duplicate guards, at most five facts per curation run (counted
  from the sidecar's `via: agent` accepts since the live lease began, and only
  while a lease is live), only for a row the run predicted accept and that is not
  `decide: you`, and every accept leaves an undo receipt in History. The acting
  commands read the setting only from the installed engine's registry (found
  through the service definition, ignoring `--workspace`, `CIAO_WORKSPACE` and
  `CIAO_RUNTIME_ROOT`) and refuse unless that registry maps the workspace to the
  vault being written. A run that can already execute shell could end and retake
  its lease to reset the cap, as it could edit the registry; the file-tool deny
  patterns and the operator's Undo are the remaining controls.

A second device installs its own engine and reads `GET /api/addresses` to find
this one. There is nothing to unpair and no session to bridge.

### Residual: forged decision rows (#1745)

An agent with vault write access can forge the `via=pwa` memory rows and `actor=user` task rows the weekly rules pass reads as operator decisions. This is bounded: the resulting `Memory-Review-Rules.md` and `Task-Rules.md` only shape what memory review proposes and how tasks are filed, are linted on write and again on read, and are fenced as evidence. They never accept, promote or change permissions.

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
