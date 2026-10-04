# Ciaobot Server native host

`native/server-host/ServerHost.swift` is the production, persistent macOS host
for Ciaobot (#1009, child A of #1008). It is small on purpose: a stable,
immutable permission identity in front of a Python engine that stays
independently updatable. The installer acquires and records it (#1050, child E1)
and `ciao setup` / service registration activate it from a verified snapshot
(#1060, child E2): a hosted definition is written only when
`ciao.server_host.verify_owned_host` proves the installed bundle, is preserved
across re-runs, and otherwise the direct shape is kept. Host upgrade and rollback
are later children.

## What it does

The host exposes exactly two fixed operations, revision 1:

```sh
Ciaobot Server.app/Contents/MacOS/CiaobotServerHost serve --python /absolute/interpreter
Ciaobot Server.app/Contents/MacOS/CiaobotServerHost request-accessibility
```

- **`serve`** launches exactly `[python, -I, -m, ciao.cli, supervise]` as a child
  and stays alive until that child exits, then mirrors its status: a normal exit
  forwards the exit code, an uncaught signal maps to `128 + signal`, and a launch
  failure prints a diagnostic on stderr and exits `1`. The child inherits the
  host's working directory, environment, stdout and stderr; nothing is captured,
  buffered, rotated or deleted, and no `DYLD`/`PYTHONPATH` or other environment
  knob is set. The event loop stays responsive — there is no blocking wait on
  the main queue.
- **`request-accessibility`** calls
  `AXIsProcessTrustedWithOptions(prompt: true)` once and keeps the process alive
  for five seconds so macOS can present the asynchronous settings prompt. It
  never grants anything itself, never reads, clicks, types or scripts, and never
  runs AppleScript.

The parser is strict. It accepts exactly `serve --python <absolute existing
executable>` or `request-accessibility` and rejects repeated, unknown and extra
arguments, relative and empty paths, and non-executable targets. Invalid argv
exits `2` before AppKit is set up or any permission call is reachable. A
symlinked or dot-alias path is resolved by the filesystem as an ordinary
executable path.

## Process group and job ownership

The child is spawned with the public `posix_spawn` API, **not**
`Foundation.Process`. `Process` makes the child the leader of a new process
group and session, which would take the supervisor out of launchd's job group
and let the engine (and the provider servers it starts) survive a host crash or
SIGKILL as orphans. With `posix_spawn` the host passes no `POSIX_SPAWN_SETPGROUP`:
the supervisor stays in the host's process group and session, so the helpers it
starts with `tree_spawn_options(dies_with_engine=True)` — the engine, the OpenCode
server — do too, exactly as `ciao/os_support/processes.py`'s `dies_with_engine`
contract expects. Not every subprocess the supervisor starts shares the group:
independent background runs deliberately use their own new session, and those
are not expected to die with the job.

Because the host shares that one group, **it never calls `killpg` on it**:
signalling the group would also signal the host itself.
launchd remains the final job-group owner. When the host exits or crashes,
launchd's own cleanup of the job group reaches the supervisor, the engine and
the OpenCode server. The host's own escalation therefore targets only the
tracked, still-unreaped child PID. A standalone host run outside launchd cannot
kill the descendants after it dies — that is launchd's responsibility, not a
gap the host can close from inside the group.

`POSIX_SPAWN_SETSIGDEF` resets `SIGTERM`/`SIGINT` (which the host ignores) to
their defaults and `POSIX_SPAWN_SETSIGMASK` clears the inherited signal mask, so
the Python supervisor installs its own handlers normally. `POSIX_SPAWN_CLOEXEC_DEFAULT`
plus `posix_spawn_file_actions_addinherit_np` on fds 0–2 close every other
descriptor, so the child never inherits the host's incidental open files.

## Signals and lifetime

A stop reaches the host three ways, and all three forward to the supervisor the
same way: `SIGTERM`/`SIGINT` through safe dispatch signal sources, and a graceful
macOS **Quit Apple Event** (`kAEQuitApplication`) through a pinned
`NSApplicationDelegate` that replaces AppKit's default handler in
`applicationWillFinishLaunching`, before the deferred child launch can run.
Without that replacement, loginwindow's quit on logout, restart or shutdown —
and `quit app "Ciaobot Server"` — would run
`NSApplication.terminate(_:)` and exit `0` immediately, orphaning the supervisor.
`SIGTERM`/`SIGINT` never use Foundation work inside a POSIX signal callback, and
neither the signal nor the Apple Event handler performs any desktop-control
operation; the Quit handler only calls the controller's stop path.

On the first stop:

1. If the child has not launched yet, nothing is launched and the host exits `0`.
   The launch waits for the first event-loop turn, so a stop delivered while
   AppKit is still starting is handled before the launch decision. A signal
   landing in the tiny window before the handlers are installed still kills the
   host, which is also safe: no child exists yet.
2. Otherwise the host forwards `SIGTERM` to the tracked child PID exactly once
   and arms one `SIGKILL` escalation for `STOP_GRACE_SECONDS` (35 s, deliberately
   longer than the Python supervisor's own 30 s grace so the child's grace wins
   and the host is the outer bound). The escalation signals **only** the tracked,
   still-unreaped child PID — never the shared group.
3. When the child exits, the escalation is cancelled and the child is reaped
   through a process-exit dispatch source and `waitpid` on the serial main queue.
   The PID is held until its status is collected, so the kernel cannot reuse it
   and the escalation can never signal a recycled PID.

The exit status is honest. A requested stop returns `0` for the codes the real
supervisor answers a stop with: exit `0`, exit `130` (the supervisor saw the stop
before its first launch or during its restart backoff), exit `143` (the engine it
terminated died by the forwarded `SIGTERM`), and death by the forwarded
`SIGTERM` itself. A stalled supervisor that had to be SIGKILLed (`137`), any
other non-zero exit after the stop, or a crash (`128 + signal`) is preserved, so
launchd and the service logs see a failed shutdown instead of a false success.
Without a stop, `130`, `143` and every other code pass through unchanged.

The host never restarts or relaunches itself or its child. The Python
supervisor (`ciao/supervise.py`) still owns restart-code 75, the crash-loop
backoff and the engine descendants; the host is only the outer process. Because
launchd is the final job-group owner, the launchd job must give the host time to
finish its own stop with an `ExitTimeOut` above the 35 s grace. That plist is
built by the later service child (#1008 child B), not here.

## Identity and versioning

| Field | Value |
|---|---|
| Bundle ID | `local.ciaobot.server` |
| Display name | `Ciaobot Server` |
| Executable | `CiaobotServerHost` |
| Host protocol | `CiaobotServerHostProtocol` = `1` |
| Bundle version | host revision (`1`), never the engine release |
| Minimum system | `13.0` |
| `LSUIElement` | `true` (accessory app: no window, no Dock) |

The bundle ID is distinct from the retired app, the PWA and the earlier
`local.ciaobot.server-host-experiment` spike, so a permission grant can never be
inherited across identities. `CFBundleVersion`/`CFBundleShortVersionString`
express the host revision; they are deliberately not the engine version.

The bundle ships only the executable, its `Info.plist`, the tracked
`CiaobotServer.icns` and the signature. No Python, configuration or sidecar
lives inside the sealed app. The icon is the exact PR #119 indigo Ciaobot Server
bytes, restored once as a tracked file at `ciao/stock/deploy/CiaobotServer.icns`;
the builder reads that tracked file and never touches Git history at build time.

## Build

```sh
python scripts/build-server-host.py --output /tmp/ciaobot-server-host-build
```

Run it with the development venv's Python (3.12+); the archive re-check needs
`tarfile`'s `data` filter, which the Command Line Tools' `python3` lacks.

The builder:

- refuses a non-macOS platform or a Python older than 3.12 before writing anything, and refuses an existing
  output path including a dangling symlink;
- compiles `arm64-apple-macosx13.0` and `x86_64-apple-macosx13.0` with `xcrun
  swiftc` against the current macOS SDK, staging the thin binaries **outside** the
  `.app` and `lipo`-creating the universal executable into the bundle;
- writes the plist and icon, then ad-hoc signs the finished app once, strict
  verifies it, checks both `lipo -archs` slices, and extracts the per-arch
  CDHashes;
- archives exactly the app subtree into
  `ciaobot-server-host-macos-universal-v1.tar.gz` (ordinary files and directories
  only, modes preserved, uid/gid and user/group names cleared, no absolute paths,
  no thin build files), then re-extracts to a fresh scratch directory and
  strict-verifies the app without recompiling.

It prints the archive path, archive and executable digests, archive size and the
per-arch CDHashes. The build fails if the tracked icon is empty or if `codesign`
reports no CDHash for a slice. It never installs into `~/Applications`, never
writes a launch agent, never launches what it builds and never requests a
permission.

## Ad-hoc signing and reapproval

The host is ad-hoc signed, not signed with a paid Developer ID. Its permission
grants are therefore tied to the exact signed bytes: any rebuild changes the
CDHash and can require the user to reapprove the Accessibility/Automation prompt
in System Settings. That is why ordinary engine updates must keep these bytes
unchanged, and why a host upgrade is a separate, explicit, warned action (the
installer work tracked in #1008). The builder and tests never edit TCC and never remove a grant.

## Tests

`tests/test_server_host.py` runs the pure checks everywhere (identity distinct
from the PWA/retired/experiment IDs, revision independent of engine version, the
icon digest, no Python bundled, the exact both-arch compile commands and
deployment floor, output refusal, non-macOS refusal before writes, injected
build ordering, archive safety and metadata, the `killpg`-free and
`posix_spawn`-only source contract). On macOS with Command Line Tools it
compiles the real host and a small offline C child fixture once, then exercises
the shipped host: fixed supervisor argv, inherited cwd/environment/stdio, the
child's reset signal mask and dispositions, the dropped extra fd, exit and
signal mirroring, exit `130`/`143` treated as a clean stop (and unchanged
without a stop), stop forwarding and reaping, the graceful Quit Apple Event
forwarding a stop and reaping the child, the stalled-child escalation
(`137`, then the test simulates launchd's final job-group cleanup), preserved
non-zero and crash statuses after a stop, launch failure, and the shared
job-group inheritance. The `request-accessibility` branch is **never executed**
by the tests, and no live launchd, `.app`, engine or TCC is involved.

```sh
PYTHONPATH=$PWD python -m pytest tests/test_server_host.py -q
```

## Not in this child

The update/rollback ownership contract, release-manifest coverage of the host
archive, and all live launchd/TCC validation are later children of #1008. Host
acquisition and the ownership record are E1 (#1050); selecting the hosted
service from a verified snapshot in `ciao setup` / service registration is E2
(#1060, `tests/test_server_host_activation.py`) and changes no live service and
runs no `launchctl`, `open` or live permission request. This document describes
the host itself, not the installer transaction.
