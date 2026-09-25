# Remote-client boundary audit (issue #546)

## Boundary found

The client relay previously made the host PWA, the local API, and the desktop
webview share one localhost origin. The proxy selected host versus local by
path, but a page already running on the local origin could call every local
route directly. The loopback-only `/api/node/handover` exception was reachable
without a session. The Tauri `main` capability also granted native commands to
every `http://localhost:*` page, including remote host content. Native drop
events copied absolute paths into page JavaScript.

## Implemented slice

- Client content uses the `localhost` origin. Local node, device, drop, and
  `/device` surfaces require the separate `127.0.0.1` origin, a loopback TCP
  peer, and (for API requests) `X-Ciao-Local-Control: 1` with a matching
  origin. Requests from the content origin fail closed, while a navigation from
  the control origin is redirected to the content origin. Frontend transitions
  use bounded full-document helpers rather than an SPA router hop.
- A control-origin login/connect can issue a state-bound, one-time, 60-second
  `/api/auth/bridge`; it is redeemed only on `localhost`, sets a host-only local
  session there, and never returns or forwards the raw host session. The bridge
  and `/device/return` redirects are `no-store`/`no-referrer`.
- The relay refuses host redirects and refresh responses, strips host
  `Set-Cookie`, captures a rotated host session only from a successful response,
  and refuses an HTTPS-to-HTTP peer. WebSocket authentication/policy closes are
  terminal and distinct from transport outages; the client no longer reconnects
  those failures in a loop.
- The macOS capability is limited to bundled local pages. The remote PWA no
  longer has a Tauri capability, and the `trigger_app_update` command and
  permission are removed; updates remain tray-controlled. The main webview
  accepts only the local engine's loopback origins without HTTPS downgrade.
- Native Finder drops send only a short-lived grant id, bounded display names,
  and opaque file references. The absolute path list remains in the Rust/server
  grant and is not put in the browser event. Chat/project upload contracts are
  bounded and do not return absolute paths; the server expands
  `ciao-drop:drop_<32 hex>` only in the provider prompt. Invalid/corrupt node
  state is preserved, reported as invalid, and cannot fall through to host
  behavior.
- Regression tests cover the two origins, missing control capability headers,
  bridge binding/replay, Tauri capability contents, invalid state, refused relay
  redirects/downgrades, bounded drop/upload failures, WebSocket classification,
  and hostile HTML trying to fetch local controls or invoke Tauri.

## Verification

- `pytest -q tests/test_remote_boundary.py tests/test_node_proxy.py tests/test_auth_security.py tests/test_workspace_html.py`
- `pytest -q tests/test_node_state.py`
- `uv run --project . --extra test pytest -n auto tests/` — 4686 passed, 1 skipped.
- `cd web && npm test` — 1511 passed across 114 files.
- `cd web && npm run build` — typecheck and production build passed.
- `mypy ciao` and `mypy --no-incremental ciao` pass (142 source files).
- `rustup run 1.90.0 rustfmt --edition 2024 --check src/lib.rs src/capture.rs` and
  `cargo test --lib` pass (57 Rust tests). `./scripts/check-desktop.sh --fast`
  passes its format, clippy, and test gates.

## Remaining work (not claimed resolved)

1. Replace dashboard-password/raw host-session relay credentials with named,
   revocable per-device credentials in the macOS Keychain, bind them to a
   stable engine identity, and require verified TLS by default.
2. Constrain workspace file/image/write/open APIs to authorized roots with
   resolved-symlink checks and explicit secret/runtime/provider-path denials.
3. Add a native-only local control channel. The current `127.0.0.1` origin is a
   browser origin boundary, not a replacement for that channel; it intentionally
   disables remote-content attempts to use local controls and may make a
   client-side Finder drop unavailable until the native bridge is completed.
4. Isolate service workers, caches, local storage, drafts, and notification
   cursors per connection profile, and clear them on unpair/profile changes.
5. Negotiate protocol version, engine identity, and capabilities before writes.
   This slice validates persisted roles and basic peer transport, but does not
   yet negotiate a versioned engine identity or reject an otherwise plausible
   incompatible peer.
6. Separate app-only and engine-only update state and complete protocol/redirect
   coverage across every HTTP, WebSocket, reconnect, and revocation path.

The relay remains a compatibility bridge. Direct app-only remote mode should
not be treated as safe until all six items above are implemented.
