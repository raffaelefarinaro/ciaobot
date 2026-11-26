# Capability matrix: what a dedicated principal can enforce against a same-UID agent

Slice 1 of #3623 (forgery-only scope; macOS first; native Mac app as operator channel; Linux and
Windows fail closed). Measured 2026-11-26. Raw output: `results/raw-results.txt`.

Setup. Tart VM `ciao-macos-test` (macOS 15.7.7 arm64, **SIP disabled**), user `admin` (uid 501, admin
group) plays the agent and the app user. Provisioned by root: user `_ciao` (uid 299), root-owned binary
and pin file in `/Library/PrivilegedHelperTools`, root-owned LaunchDaemon plist, `_ciao`-owned state
(`0700`) and socket directory. The image's passwordless sudo was removed before attack probes (the
agent has no admin credential, as the design assumes). Daemon verifies each peer by kernel peer
credential plus code-signing requirement of the peer's audit token, then HMAC-signs an event with a key
only it can read. "Agent" probes run as uid 501 without sudo.

## Results

| # | Question | Measured result | Verdict |
|---|----------|-----------------|---------|
| A1 | Same UID reads daemon state or key | `Permission denied` (state `0700 _ciao`, key `0600`) | Enforced |
| A2 | Same UID writes, deletes or renames state, the socket, the socket dir or `/var/ciao-authority` | all `Permission denied` | Enforced |
| A3 | Same UID replaces daemon binary, pin file or LaunchDaemon plist (root:wheel) | all `Permission denied`, also for a member of the `admin` group | Enforced |
| A4 | Same UID stops or signals the daemon, `launchctl bootout` | `Operation not permitted` (kill -9, kill -STOP, bootout) | Enforced |
| A5 | Same UID binds the real socket path | blocked (dir owned by `_ciao`, `EADDRINUSE`/`EACCES`) | Enforced |
| A6 | Same UID squats a socket at a user-writable path | works (`/tmp/fake.sock`) | Not enforced: clients must use only the root-provisioned path |
| A7 | Admin-group user replaces an app in `/Applications` (`root:admin 775`) | write succeeds | Not enforced: the operator app's location is not a boundary. The pin must name a signing identity or cdhash, never a path |
| B1 | Socket peer identity, daemon as `_ciao` | **fails**: `SecCodeCopyGuestWithAttributes` returns errno 13 (EACCES) for another user's process, so no code identity is available | Not usable as non-root |
| B2 | Socket peer identity, daemon as root | works via `LOCAL_PEERTOKEN` + `SecCodeCheckValidity`: real app accepted; other cdhash, other identifier, python, an unrelated own client all denied | Enforced for "which binary" |
| B3 | XPC Mach service in a daemon running as `_ciao` with `xpc_connection_set_peer_code_signing_requirement` | works as non-root: pinned app accepted; other cdhash and other identity silently dropped (no reply, caller hangs without a timeout) | Enforced for "which binary"; preferred over the socket |
| B4 | **Byte copy of the pinned app run by the agent** (cdhash, or cert-pinned requirement) | **accepted** over socket and XPC | Not enforced. Code identity proves which bytes ran, not that a human was in front of them |
| B5 | Agent re-signs a copy with its own cert or ad-hoc, same identifier | denied under a cdhash or `certificate leaf = H"..."` requirement; an identifier-only requirement would admit it | Enforced only with cdhash or cert pin (never identifier-only) |
| B6 | App upgrade under a cert-leaf requirement | new cdhash with same cert accepted without touching the pin; cdhash pin needs a root write per upgrade | Cert (Team ID in production) pin supports upgrades |
| B7 | DYLD_INSERT_LIBRARIES into the real pinned app (dylib sends a forged SUBMIT; control run without DYLD first) | **VM (macOS 15.7.7, SIP off): dylib ran and the daemon returned ACCEPTED for both the plain and the hardened-runtime (`flags=0x10002 adhoc,runtime`) app.** HOST (macOS 27.0.1, SIP on), re-run with controls: plain app: dylib ran; hardened app: control run fine (exit 0), dylib did **not** run | Hardened runtime blocks injection only with SIP on (the normal case); with SIP off it blocks nothing. Not measured on 15.7.7 with SIP on. An earlier raw block that showed DENIED came from a stale dylib with a hardcoded path to the `_ciao` daemon (B1), not from the app being rejected; it was removed |
| B8 | `task_for_pid` by non-root same UID against a same-UID app (plain or hardened), the daemon, launchd | `kr=5` for all, in the VM and on the SIP-on host | Enforced (no get-task-allow). Debugger attach needs developer mode or an interactive session; measured `cannot get permission to debug`, developer mode off |
| C1 | Synthetic keystrokes (CGEvent) into the pinned composer with **no** Accessibility/PostEvent/ListenEvent grant for anyone (rows deleted from system and user TCC.db, `tccd` restarted, fresh daemon state and truncated composer log per run; run twice, before and after the grant cases) | `AXIsProcessTrusted=false PostEventAccess=false`; composer log empty, daemon `events.log` 0 lines, 0 SUBMIT in the daemon audit log. A sqlite-inserted grant for the ad-hoc poster binary itself did not take effect either (still false, 0 events) | Enforced by TCC |
| C2 | Same, with the grant owned by the ancestor/responsible process (`/usr/libexec/sshd-keygen-wrapper`, the only grant present; fresh daemon state, truncated log) | `AXIsProcessTrusted=true`; composer log shows one forged submission, daemon `events.log` 1 line, 1 SUBMIT in the audit log, reply `ACCEPTED` (the stock image ships this grant, plus osascript, python, tart-guest-agent) | **Not enforced**: attribution goes to the responsible process, so any Accessibility/PostEvent grant the agent can inherit (Terminal, an IDE, a launcher) defeats it |
| D1 | `kill -9`, KeepAlive, root re-install (binary and plist replaced) | restarted; key fingerprint, state and audit log unchanged | Survives |
| D2 | VM reboot | LaunchDaemons start, key and state intact, socket recreated | Survives |
| D3 | Linux, uid separation (Docker Desktop kernel) | state, socket, rename, kill, `/proc/pid/mem` and environ all denied for a second uid; `SO_PEERCRED` returns uid/gid/pid | Enforced for files |
| D4 | Linux, agent and operator UI share a uid | peer is indistinguishable by uid. A non-root authority cannot read the peer's `/proc/pid/exe`; a root authority (needs CAP_SYS_PTRACE in a container) sees only a sha256, identical for a copied binary | Not enforced: no signing identity, same as B4 |

## What is enforceable

1. Isolation of the authority itself on macOS: executable, pin, service definition, state, signing key and
   socket directory cannot be read, written, replaced, stopped or squatted by the agent's UID, and survive
   restart, re-install and reboot (A1 to A5, D1, D2). This answers "can a compromised engine account
   rewrite grants, events, keys or revocation state": no.
2. Binary identity of the caller: a cdhash or certificate-pinned requirement distinguishes the signed app from
   arbitrary same-UID code (B2, B3, B5, B6). Use XPC or a root helper; a non-root socket daemon cannot verify
   peers (B1).
3. Agent cannot attach to or inject into the hardened-runtime app on a SIP-on system (B7 host, B8), and cannot synthesize input when no Accessibility grant reaches it (C1). B7 is not enforced at all with SIP off.

## What is not enforceable

1. "A human typed this." A copied app binary (B4), an inherited Accessibility grant (C2), and any input
   surface of the app (CLI, URL scheme, AppleScript, pasteboard, accessibility value setting) are all
   reachable by a same-UID agent. The authority can establish "the pinned app submitted these exact bytes",
   not "a person authored them". With a TCC grant held anywhere in the agent's process ancestry, the composer
   can be driven.
2. Anything in the app's own same-UID data (preferences, caches, the engine channel) as input to a decision.
3. Linux and Windows have no equivalent of the signed-app requirement here; Linux gives only uid and a file
   hash (D4). Fail closed, as decided.

## Conclusion for the next slice

- A privileged-daemon boundary is real for the authority's state, keys and service on macOS. Build the
  authority as a root-provisioned LaunchDaemon with its own account, root-owned binary and pin, and an XPC
  Mach service. Use `xpc_connection_set_peer_code_signing_requirement` with a Developer ID Team ID and
  identifier requirement (the cert-leaf test stands in for it), not a cdhash (breaks Sparkle upgrades) and
  not an identifier alone. Require a client timeout, since a rejected peer gets silence.
- Do not claim the boundary stops forged "operator" input. The honest guarantee is: forgery by agent code
  that cannot drive the signed app. Make that precise in the user disclosure: it holds only while no
  Accessibility, PostEvent or Automation permission reaches the agent process tree, and the app has no
  programmatic input surface and ships hardened runtime without `get-task-allow` or
  `allow-dyld-environment-variables`.
- The slice 2 design should add: the app refuses to submit when it detects `AXIsProcessTrusted`
  style injected events (check `CGEventSourceStateID` and event source PID/userData on the composer's key
  events; not tested here), a per-send user-presence signal that is not an event (for example a
  click-to-confirm that requires the app to be frontmost and was not synthesized), and a Settings-visible
  status that disables grants if a risky TCC grant is present on the engine's ancestry. These are
  mitigations; none was measured here.
- Install and upgrade need an admin password on each OS (creating `_ciao`, writing
  `/Library/PrivilegedHelperTools` and `/Library/LaunchDaemons`). Engine-initiated upgrades of the
  authority are impossible by design: a user-level updater cannot replace it. The bundle in
  `/Applications` is replaceable by an admin-group user, so the pin must be a signing identity.
- Open items for the next slice: (1) re-measure B7 and the hardened-runtime case on a SIP-on macOS 15; the
  VM has SIP off and Cirrus images pre-grant Accessibility; (2) Developer ID, notarization and a real Team ID
  requirement; (3) `SMAppService` daemon registration and its user-approval prompt, which replaces a
  hand-written install script; (4) the interaction with the remote PWA, which cannot reach the XPC channel;
  (5) Windows.

## Limits of this spike

SIP-off VM; image pre-seeds TCC grants and ssh/sudo; self-signed certificate rather than Developer ID;
`_ciao` created by `dscl` rather than the installer; no `SMAppService`; Linux was a Docker Desktop
container with default capabilities, not systemd or the Hostinger server (not needed once the container
answered the uid and peer-credential questions); Windows not tested; no live engine, vault or `~/.ciao`
was touched.
