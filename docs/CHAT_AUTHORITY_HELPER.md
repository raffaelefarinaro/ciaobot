# macOS chat authority helper: design

Status: design for #3623 slice 3. Nothing in this document is installed or
wired. `chat_evidence` stays unavailable (`remote_client` for the PWA,
`unprovisioned` for the native app) until the later slices below ship and pass
a PR-scoped security review. It builds on the principal-isolation spike
(`experiments/ciao-3623-isolation-spike/CAPABILITY_MATRIX.md`, rows cited as
A1 to D4), the protocol library `ciao/chat_authority.py` and the selected
direction in `docs/CHAT_INGRESS_AUTHORITY.md`.

## Open maintainer decisions

These are not settled by this design and need an explicit answer before the
slices that depend on them:

1. **Operator key replacement.** This design disallows replacement: the key is
   enrolled once, and changing it means the admin-authorized reset (which
   destroys state and keys, equivalent to revoking every grant) followed by a
   fresh enrollment. Confirm, or accept a replace operation that bumps the
   epoch and revokes all grants.
2. **Is an admin prompt alone enough to authorize enrollment?** It stops a
   same-user copy of the app racing the real one, but it cannot tell the user
   which package or app asked for it (see the package-substitution row in the
   threat model).
3. **Accepting the byte-copy residual risk (B4).** An agent that runs a copy of
   the signed app, or inherits an Accessibility grant, can submit as the
   operator. The helper proves which binary signed, not who typed.
4. **Scope of the Swift daemon, anchor and package.** Slices 2 and 4 are a
   native binary pair plus a signed installer, a large permanent surface
   (release signing, notarization, an uninstaller). Confirm this is wanted
   before slice 2 starts.

## Decisions this design takes as given

* Scope is forgery only: an agent running as the engine's user must not be able
  to mint or alter an eligible grant, turn event, withdrawal or epoch. It is
  not a claim that a person typed a message (B4, C2).
* macOS only. Linux, Windows and the PWA stay `unsupported_platform` or
  `remote_client`. The remote PWA cannot reach a Mach service and is never an
  operator channel.
* One-time, admin-approved install of a root-owned service that runs under a
  dedicated account. The native Mac app is the operator channel.
* No fallback to same-user state: absence, loss, downgrade or failed
  verification of the helper disables grants and evidence admission.

## Components and trust zones

| Part | Runs as | Lives in | Trusted for |
|------|---------|----------|-------------|
| `ciao-authority` (authority daemon) | `_ciaoauth` | `/Library/PrivilegedHelperTools`, root:wheel | grant, turn and epoch state, event signing, XPC service |
| `ciao-authority-anchor` (anchor) | root | same directory | the monotonic counter, admin-authorized enrollment, reset and uninstall |
| Ciaobot for Mac (operator app) | the console user | `/Applications`, user-writable | holding the operator key, native composer, nothing else |
| `ciao-authority-bridge` | the console user (spawned by the engine) | inside the app bundle | carrying engine reads to the daemon over XPC |
| The engine and every agent tool | the console user | anywhere | **not trusted**; read-only lookups |

The agent and the engine share a UID, so everything user-writable is in the
agent's reach: the app bundle (A7), the bundled engine and its Python runtime,
the PWA files the app's web view loads, the app's preferences and keychain
items it can be made to use. The design never lets a decision depend on one of
those.

**The daemon cannot be `chat_authority.py` running from the bundled
engine.** That runtime is user-writable, so a root-owned daemon that executes
it would hand root-level code execution to the agent. The daemon is a native
binary installed root-owned. The Python library stays the reference model;
see "Protocol parity" below.

## Installation and account provisioning

### Mechanism

* **`SMAppService.daemon` is rejected.** It registers a plist that points into
  the app bundle, so the root-run executable would be in a directory a
  same-user process (and Sparkle, by design) can rewrite (A7). Registering it
  is therefore not a boundary. It cannot create the dedicated account either.
* **`SMJobBless`** installs a root-owned copy into `/Library/PrivilegedHelperTools`
  after an Authorization Services admin prompt, but it is deprecated since
  macOS 13 and also cannot create the account.
* **Chosen: a Developer ID signed, notarized installer package** (`pkgbuild`
  with a postinstall script) that the app opens in Installer. The user
  approves it with an administrator password once. Installer verifies the
  package signature; the root postinstall verifies each payload binary
  against the pinned requirement (below) after copying it into a root-owned
  temporary directory, then moves it into place, so nothing is read from a
  user-writable path after the check. The package is built by `/release-mac`
  and is distributed beside the app, never produced from the app bundle at
  runtime. This is to be re-measured on the Tart VM with a real Developer ID
  identity in the provisioning slice (spike open items 2 and 3).

### What the postinstall creates (idempotent)

1. A hidden role account `_ciaoauth` and group `_ciaoauth` via `dscl`:
   unique ID chosen free in 200 to 399, shell `/usr/bin/false`, home
   `/var/empty`, password `*`, `IsHidden 1`, not a member of `admin` or
   `staff` (the spike used uid 299 by hand).
2. `/Library/PrivilegedHelperTools/com.ciaobot.chat-authority` and
   `com.ciaobot.chat-authority-anchor`: root:wheel `0755`.
3. `/Library/LaunchDaemons/com.ciaobot.chat-authority.plist` and
   `...-anchor.plist`: root:wheel `0644`.
4. `/Library/Application Support/Ciaobot/ChatAuthority/` root:wheel `0755`
   with `state/` owned by `_ciaoauth` `0700` (blob, keys) and `anchor/`
   root:wheel `0700` (counter). Excluded from Time Machine.
5. `launchctl bootstrap system` of both plists.

The requirement is compiled into the daemon and anchor binaries (covered by
their signature), not read from a separate pin file; it can only change with a
new signed binary.

### Launchd plist (authority daemon)

```xml
<plist version="1.0"><dict>
  <key>Label</key><string>com.ciaobot.chat-authority</string>
  <key>ProgramArguments</key><array>
    <string>/Library/PrivilegedHelperTools/com.ciaobot.chat-authority</string>
  </array>
  <key>UserName</key><string>_ciaoauth</string>
  <key>GroupName</key><string>_ciaoauth</string>
  <key>MachServices</key><dict><key>com.ciaobot.chat-authority</key><true/></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>Umask</key><integer>63</integer>
  <key>WorkingDirectory</key><string>/var/empty</string>
</dict></plist>
```

No `EnvironmentVariables`, no sockets in user-writable paths (A6: only the
root-provisioned Mach service name is ever used). The anchor plist is the
same with `UserName` omitted (root) and its own Mach service. Both survive
`kill -9`, re-install and reboot (D1, D2); neither can be stopped by the
agent's UID (A4).

## Pinning the caller

`xpc_connection_set_peer_code_signing_requirement` is used on every
listener (B3). A non-root daemon cannot check a socket peer (B1), and a
rejected peer receives silence, so every client call has a timeout.

* Requirement for the operator app and the bridge: Developer ID chain
  (`anchor apple generic`, the Developer ID CA and Developer ID Application
  leaf OIDs), `certificate leaf[subject.OU] = "<TEAMID>"` and an exact
  `identifier` (`com.ciaobot.mac` for the app, a different identifier for
  the bridge). **Never a cdhash** (breaks every Sparkle upgrade, B6) **and never
  an identifier alone** (B5).
* Forbidden entitlements are expressed in the requirement itself:
  `and !(entitlement["com.apple.security.get-task-allow"] exists) and
  !(entitlement["com.apple.security.cs.allow-dyld-environment-variables"]
  exists)`. The provisioning slice checks the string with `csreq -t` against
  real signed binaries (a requirement-language detail not measured here).
* Two things the requirement language cannot express are checked in daemon
  code from the peer's audit token (`SecCodeCopyGuestWithAttributes`, then
  `SecCodeCopySigningInformation`): the hardened-runtime flag
  (`kSecCodeSignatureRuntime`) must be set, and the build number must meet a
  minimum. The build number is parsed to an integer and compared numerically;
  `info[CFBundleVersion]` in a requirement compares strings (`"9"` sorts above
  `"10"`) and is not used. A helper release raises the minimum to drop a
  vulnerable old app. These code checks are part of the one design, not a
  compatibility path.
* The anchor accepts only the daemon (its requirement and euid `_ciaoauth`).
* Reads are accepted from the app and the bridge, and only from the enrolled
  console user's euid; commands only from the app.

What the pin proves is which binary is calling, not who is at the keyboard (B4):
a byte copy of the signed app run by the agent satisfies it. Section "Threat
model" states what remains.

## XPC interface

Low-level XPC (not `NSXPCConnection`), one request and one reply, each a JSON
document in a data value, so the Python client and the Swift service share one
wire. The reference for the read side is `ciao/chat_authority_client.py`.

Envelope: request `{"v":1,"op":...,"nonce":<32 hex>, ...}`, reply
`{"v":1,"ok":true,"nonce":<echo>,"result":{...}}` or
`{"v":1,"ok":false,"nonce":<echo>,"code":<stable>}`. A client treats a
missing or wrong nonce, a version other than 1, a reply over 8 MiB or a
non-object as a failure. There is exactly one protocol version at a time: a
mismatch is `degraded` and a re-install prompt, not a negotiated older mode.

| Op | Callers | Effect |
|----|---------|--------|
| `health` | app, bridge | `ready`, `helper_version`, `state_counter`, `epoch`, `authority_key_id`, event public key |
| `eligible_events` | app, bridge | `ChatAuthority.eligible_events`; every filter before the limit; the request carries the intended `destination` and `audience` and the reply echoes them with the grant id |
| `create_grant`, `revoke_grant`, `withdraw_grant`, `withdraw_turn`, `submit_turn` | app only | a signed operator command (below) |
| `enroll_operator_key` | app only, needs an admin authorization | store the operator public key **once**; refused while a key is enrolled. There is no replace: changing the key is the admin-authorized reset (state, keys and counter destroyed, which revokes everything) followed by a fresh enrollment |

The engine and the agent can call only the first two. The Python client has
no command, signing or submit method (asserted by a test), and
`chat_authority.py`'s `HmacSigner` is never used by the engine.

The engine reaches XPC through `ciao-authority-bridge`, a small signed
binary the engine spawns (JSON on stdin and stdout, one call per process or a
framed stream), so Python needs no `ctypes` to libxpc. A `SubprocessTransport`
implementing `Transport` with the 2 second timeout is a later slice. Lookups
return text the agent could already read in the chat, so identifying the
engine here adds nothing; the bridge pin and the euid check only keep other
local users from reading.

## Key custody

| Key | Kind | Held by | Purpose |
|-----|------|---------|---------|
| Operator key | ECDSA P-256, non-exportable | the app: Secure Enclave key in a keychain access group of the app's Team ID, no per-use biometric | signs each command (grant, revoke, withdraw, submit) |
| Event key | Ed25519 | daemon, `state/` `0600` | signs each stored turn event; the public half is published through `health` so verifiers hold no forging ability |
| State key | 256-bit HMAC key | daemon, `state/` `0600`, independent of the event key | authenticates the whole persisted blob |

This closes three library gaps: the operator verifier in the daemon holds only
a public key (no symmetric forgery by whoever can read the verifier), state and
event keys are separate, and operator signing is asymmetric. `chat_authority.py`
already takes `Signer` and `Verifier` by protocol, so an asymmetric pair is
a drop-in for the library's tests; the Swift port implements the same bytes.

The operator key proves "the pinned app on this device signed this", which
matches B4: a copied app can use its keychain item as well. It is a second
binding on the XPC peer pin, not a person check. Enrollment goes through the
anchor and requires an Authorization Services admin right
(`com.ciaobot.chat-authority.enroll`), because otherwise a same-user copy of
the app could race the real one for the first enrollment. The key is never
replaced in place: a replacement would let whoever passes the prompt take over
existing grants and events, so replacing means reset, which revokes
everything (open decision 1). The daemon also
records the enrolling console user's uid and serves reads only to it.

## Rollback, missing-blob and storage defences

Library gaps 1, 2 and 7, and how the daemon closes them:

* **Monotonic counter in a root-owned store.** The anchor keeps `n` in
  `anchor/counter` (root:wheel `0600`, `F_FULLFSYNC`). The daemon persists as
  write-ahead: ask the anchor to advance to `n+1` (this must succeed first),
  then write the blob (temp file, `F_FULLFSYNC`, atomic rename, directory
  sync) containing `n+1` inside its authenticated payload. The daemon takes an
  exclusive `flock` on the state directory and holds it for its lifetime.
* **At load:** blob counter equal to the anchor's is normal; blob equal to the
  anchor minus one is a crash between the two steps and is accepted once; a
  lower blob is a rollback; a higher one is impossible. Rollback, a blob that
  fails its MAC, a missing blob while the anchor is above 0, and a missing
  anchor while a blob exists all leave the daemon **closed**: `health` returns
  `ready:false`, lookups and commands are refused, nothing is repaired
  automatically. A missing blob with the anchor at 0 is the only fresh
  initialization, and it happens only right after provisioning.
* **One-version rollback inherent to write-ahead (decision).** Because a blob
  one behind the anchor is accepted (crash window), an attacker who can write
  the storage can restore the previous blob once, undoing the latest write (a
  revoke, say). The anchor therefore stores, with the counter, the SHA-256 of
  the blob it expects next and of the previous one; a one-behind blob is
  accepted only if its digest equals the previous digest, and reads are served
  only after the daemon has rewritten the state under the new counter. Advancing
  the anchor after the durable write is rejected (a crash would leave a blob
  ahead of the anchor, indistinguishable from forgery). The Python library's
  `CounterAnchor` carries the counter only; closing the window is the daemon
  slice's job.
* **Command key id.** Each signed command carries the SHA-256 of the operator
  public key, covered by the signature; the daemon refuses a different key id.
* **Pruned grant ids** are not persisted; the id factory must never repeat ids
  (random 128-bit or a persisted monotonic value).
* **Recovery** is the admin-authorized reset, which destroys state and keys.
  A reset is equivalent to revoking every grant; enrolled events are lost.
* **Client side:** one long-lived client per engine process is required. The
  rollback memory lives in the client object, so a client created per call
  would see every counter as the first one and detect nothing. The engine
  holds a single instance for its lifetime; a new process starts with no
  memory and relies on the daemon-side checks above. The engine client remembers the highest `state_counter` it
  has seen and a change of `authority_key_id`, and treats a drop or a change
  as `degraded` for the life of that client (a later higher value does not
  clear it). The app additionally records the last (counter, epoch) it saw in
  its own store, as an early warning only.
* **Not defended:** a restore of the whole volume (VM snapshot, disk image)
  restores blob and anchor together. Time Machine exclusion of the store reduces
  accidental restores; deliberate whole-disk rollback needs root and is out of
  scope. The consequence would be a revoked grant becoming live again, so the
  disclosure says so.
* **Epoch at use, destination and audience** (library gaps 3 and 4): lookups
  accept `expected_epoch` and the caller must pass it and check it again at the
  #3619 admission point; the daemon should also take the intended destination
  and audience as lookup arguments and compare them to the grant, so a caller
  cannot rely on a grant created for another destination. That library change
  is a listed slice, not part of this one.

## Status wiring

The engine keeps deciding platform and channel first, as
`share_evidence.chat_evidence_status` does today. Only when the platform is
`darwin` and the engine itself identified the local native app does it ask the
helper:

| Status | When |
|--------|------|
| `unsupported_platform` | not macOS |
| `remote_client` | macOS, but the request is not the local native app |
| `unprovisioned` | the Mach service is not registered (nothing installed, or uninstalled) |
| `degraded` | registered but not verified: timeout or silence, transport error, bad or mismatched reply, version mismatch, `ready:false` (closed after rollback, missing blob, MAC failure, no enrolled operator key), counter regression, key change, or a provisioning check that fails |
| `available` | only after a verified `health` **and** a passing static provisioning check; reserved for the ingress slice |

`ciao/chat_authority_client.helper_status` implements the helper half and
returns only names from `CHAT_EVIDENCE_STATUSES`, and requires the caller to
pass the static provisioning check (no default, so health alone can never
read `available`). `chat_evidence_status` is not changed in this slice and
still returns `available: False` in every branch. `static_provisioning_ok`
(implemented and tested against a temporary tree) lstats the plists, binaries
and every parent directory: owned by root, not group or world writable, not a
symlink; and checks that the daemon account exists and is not in `admin`.

## Disclosure text

Shown before the install prompt and in Settings while the helper exists:

> Ciaobot can let memory and care use messages you type in the Ciaobot for
> Mac app, but only after you install a small background service. Installing it
> needs your administrator password, once; check that the installer window names
> the Ciaobot publisher as signer before you enter it. The service keeps its own record of
> what you agreed to, so an AI agent running as you cannot change that record
> or create messages of its own. This protects against forgery by agent code.
> It does not prove that you personally typed a message. It stops holding if
> any app in the chain that starts your agents (for example Terminal, an IDE or
> a launcher) has Accessibility, Input Monitoring, Automation or Post Event
> permission, because an agent can then drive the app's text box as if it were
> you. Protection against code injected into the app itself also depends on
> macOS System Integrity Protection being on; with it off, injection is not
> blocked. Review System Settings, Privacy and Security for these before turning
> sharing on. Restoring your whole disk from an older copy can bring back a
> permission you had removed. Messages typed in the browser or on a phone are
> never covered. You can remove the service at any time.

Ciaobot cannot read the TCC database without Full Disk Access, so it cannot
promise to detect a risky grant. The in-app mitigations from the spike (reject
events whose source is not the hardware keyboard, a per-send click that must
come from the frontmost app, a Settings warning) are unmeasured; none is
claimed.

## Operator channel requirements

* The composer that submits eligible turns must be a **native** view. The Mac
  app today is a web view of the engine's PWA (`WebWindow.swift`), and that
  page and its scripts are served from files the agent's user can edit, so a
  `WKScriptMessageHandler` bridge from page content would let agent-influenced
  JavaScript submit. The page may show state; it must not hold the operator
  channel.
* The app has no programmatic input surface (URL scheme, AppleScript
  dictionary, command line flag, pasteboard-triggered send), ships the hardened
  runtime with no entitlements (from `mac/scripts/package-notarized.sh`, which
  signs with `--options runtime` and no entitlements, and `engine_sign.py
  check --distribution`; read from the scripts, not yet measured on a shipped
  artifact, and Sparkle's `Downloader.xpc` keeps the entitlements Sparkle
  signed it with). Ad-hoc development builds (`package-app.sh`) are not
  hardened and never satisfy the Developer ID pin, so the feature cannot be
  exercised with one. It uses the exact bytes of the native composer
  as the `submit_turn` payload before provider augmentation.
* The app holds the operator key and signs; it never forwards a signature
  request from the engine.

## Upgrade and uninstall

* **App update (Sparkle):** replaces the app only. Team ID plus identifier
  pinning accepts the new cdhash with no helper change (B6). Engine updates do
  not touch the helper.
* **Helper update:** impossible from a user-level updater by design. The app
  compares `health.helper_version` and the required protocol version with its
  own; a difference shows a Settings prompt that opens the new signed package
  (admin password). Until then the status is `degraded`. The package keeps the
  account, state, keys and counter, and the anchor counter continues.
* **Uninstall:** an admin-authorized `Remove chat authority` runs a signed
  uninstaller: `launchctl bootout`, remove plists, binaries, state, keys,
  counter and the `_ciaoauth` account, after confirming that stored events
  and grants are destroyed. Afterwards the status is `unprovisioned` and
  ordinary chat is private as before. Deleting only the app leaves the service
  running but unusable (no operator app to sign commands); the Settings page
  and `/release-mac` notes tell the user to remove it first.

## Threat model

Attacker: code running as the console user (the agent and anything it starts),
with no administrator credential, able to read and write everything that user
can, run the signed app binary and copy it.

| Attack | Result |
|--------|--------|
| Read or rewrite state, keys, counter, binaries, plists, socket dir | refused by file ownership (A1 to A5) |
| Stop, signal or unload the services | refused; KeepAlive restarts (A4, D1) |
| Squat the service name or a socket path | the client uses only the system-domain Mach name (A6) |
| Call the XPC service from another binary | silence; client times out (B3, B5) |
| Send commands without the operator key | refused: signature and counter check |
| Replay a captured signed command | refused: per-principal counter, also after restart |
| Restore an older blob | refused: anchor counter (above) |
| Delete the blob to get a fresh empty state | refused: anchor above 0 |
| Replace the bundled helper or the package contents | root postinstall verifies the signature on a root-owned copy; Installer verifies the package |
| Ride an upgrade to get a weaker app accepted | pin has a minimum version and forbids get-task-allow and dyld-env entitlements |
| Run a byte copy of the signed app and drive it | **accepted** (B4); the operator key is usable by the same binary |
| Synthetic keystrokes into the composer | refused only while no Accessibility or Post Event grant reaches the agent's process tree (C1, C2); disclosed |
| DYLD injection or `task_for_pid` into the app | blocked with SIP on and hardened runtime (B7 host, B8); not enforced with SIP off |
| Edit the PWA the web view shows | harmless: the operator channel is not web content |
| Swap the installer package the app opens for one signed by another Developer ID | verified by the app (Team ID, identifier, signed manifest hash) before opening; **a same-user race between check and open is not closed**, so the Installer signer display and the admin password are the last defence (residual) |
| Whole-disk or VM snapshot restore | **not defended**; disclosed |
| Root, or the admin password | out of scope |
| Race the first operator enrollment | refused: enrollment needs an admin authorization |
| Other local users reading chat text | refused: reads limited to the enrolled console user's euid |

Residual risks in order of weight: a swapped installer package winning the race
between the app's check and Installer's read; a granted Accessibility, Input Monitoring or
Automation permission in the agent's ancestry; byte-copy use of the pinned app;
disk-level rollback; a compromised Developer ID or build pipeline; macOS
releases that change TCC or XPC attribution.

## Protocol parity

The daemon is Swift, `chat_authority.py` is Python. The Python module remains
the specification and the engine-side model. Both are held to a shared vector
file (canonical command, event and state bytes, signatures, counter
behaviour, every refusal code) generated by Python and checked in Swift, so
the two cannot drift. Changing the protocol means changing both and the
vectors in one PR.

## Slices after this one

1. **Library hardening (Python), with a no-replace enrollment rule (done in
   `ciao/chat_authority.py`, vectors in `tests/vectors/chat_authority_v1.json`):** asymmetric operator verifier and separate
   state and event keys in `chat_authority.py`; destination and audience
   arguments on lookup; `state_counter` and write-ahead hook in the storage
   protocol; shared vector file.
2. **Daemon and anchor binaries (Swift, new SwiftPM package under `mac/`):** the
   protocol port, the two stores, XPC listener with the peer requirement, the
   rollback and missing-blob rules, a CLI test harness; tests with a fake
   anchor and in-process XPC.
3. **Bridge and `SubprocessTransport`:** wire the Python client to the real
   service; timeout and fail-closed behaviour tested against the daemon.
4. **Package, provisioning and uninstall:** `pkgbuild` with the postinstall,
   `/release-mac` signing and notarization, the uninstaller, real Developer ID
   re-measurement of the pin on a SIP-on macOS 15 Tart VM, the enrollment
   authorization right. Needs real macOS install testing per `AGENTS.md`.
5. **Native operator composer and operator key in the app:** native view,
   Secure Enclave key, command signing, Settings card, disclosure, the
   helper-update prompt.
6. **Ingress and status:** call the client from chat ingress and from the
   memory and care evidence lookups, set `available` in `chat_evidence_status`
   only on verified health, #3619 admission re-check of the epoch. Isolated
   fixtures from `CHAT_INGRESS_AUTHORITY.md` run here with real processes in
   separate principals.
7. **Independent security review and the SIP-on re-measurement of B7**, before
   any status can read `available` for users.
