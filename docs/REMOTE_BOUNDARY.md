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
  `/api/auth/check`, `/api/startup-status`, `/api/active-chats`,
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
  name, no native client) and the update
  coordinator's drain handshake. It reads the TCP source address, never the
  `Host` header, which a caller controls. A loopback peer carrying
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
- Model-authored HTML is served from `/api/workspace-html` under a sandboxing
  CSP (`sandbox allow-scripts`, no `allow-same-origin`, `connect-src 'none'`,
  `form-action 'none'`), so an artifact cannot reach the API or the local-only
  feeds at all. Coverage is in `tests/test_workspace_html.py`.
- Native Finder drops still send only a short-lived grant id, bounded display
  names, and opaque file references; the absolute path list stays in the
  server-side grant and is expanded only in the provider prompt.
- There is no native capability of any kind left: the macOS app and its shell
  are retired (`#579`), so a remote browser gets nothing a local one does not,
  and `trigger_app_update` does not exist. A browser is never a local control
  path.

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
