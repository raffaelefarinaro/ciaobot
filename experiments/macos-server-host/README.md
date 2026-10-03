# Ciaobot Server Host experiment

Isolated, developer-operated macOS spike. It exists to answer one question the
earlier probe in `experiments/macos-permission-identity` did not:

> When a native **Ciaobot Server** host spawns the Python engine as a child, are
> that child's Accessibility and Automation requests attributed to the
> recognizable host, or to the Python process?

This directory builds a small native accessory app, ad-hoc signs it, and runs
fixed read-only child probes under it. It is **not** a production launcher, a
daemon, an IPC listener, or an arbitrary-command service. It touches no installer,
service, update path, PWA, existing bundle, or Windows code, and it changes no
permissions and no TCC database. It never launches the app or requests a grant on
its own; a human does that.

## Scope and what this is not

- No production engine boot, no permission request, no installed-bundle change,
  and no grant is performed by the builder or by these instructions' automation.
- The mutable Python child lives at `<output>/child/child_probe.py`, **outside**
  the signed `.app`, because it is expected to change. The host resolves only
  that fixed sidecar path and spawns `[python, child, mode]` with `Process`; it
  never runs a shell.
- The host reads at most the focused element's **role** and the frontmost
  process **role**. It never reads text, values, window titles, screenshots, or
  process names, and it never clicks, types, or performs an action.
- Real child attribution is a **follow-up gate the coordinator operates**. A
  green build and a successful AX result here are evidence, not proof that the
  full host → Python → osascript chain is attributed to the host.

## Historical reference

PR #119, commit `40747b8955eb0b246a717cd79568cd144d4c997f` ("Distinguish PWA and
server launcher") gave the native **Ciaobot Server** its distinct indigo
mascot/terminal icon, different from the orange **Ciaobot** PWA. `build.py`
extracts that exact `.icns` from Git history (`ciao/stock/deploy/CiaobotServer.icns`),
so a shallow checkout needs that commit available before building.

## Requirements

- macOS with Command Line Tools (`xcrun swiftc`, `codesign`), Python 3.12+.
- No paid developer account, third-party dependency, or environment variable.
- The repository's Git history (for the historical icon).

`build.py` and `child_probe.py` are import-safe on Linux and Windows; only the
macOS-only subprocesses are gated behind the platform check.

## Build

The output directory must not exist. The builder refuses an existing file,
directory, **or symlink** and never follows or replaces it. It writes the app
and the child sidecar, then prints the exact paths, the host executable's
SHA-256, and the signed bundle's CDHash. The CDHash — not the executable hash —
is the signature evidence for the sidecar-update check (`codesign -dv
--verbose=4 <app>`). It never launches the app.

```sh
python3 experiments/macos-server-host/build.py --output "$HOME/Ciaobot Server Host Experiment"
```

Build a second revision to a **new** directory when testing a host change:

```sh
python3 experiments/macos-server-host/build.py \
  --output "$HOME/Ciaobot Server Host Experiment r2" --revision 2
```

**Keep build output and every receipt outside this public repository.**

## Run (human approval only)

Use Launch Services (`open`), not the inner executable from Terminal, so macOS
attributes the request to the app identity. Each invocation writes one JSON
receipt at the explicit absolute `--output` and exits. `open -W` discards the
app's exit code and stderr, so pass `--stdout`/`--stderr` too: **no receipt means
a malformed invocation or a write failure — check `host.err`**. Set `DIR` to the
same output directory the app was built in.

```sh
DIR="$HOME/Ciaobot Server Host Experiment"
APP="$DIR/Ciaobot Server Host (Experiment).app"
```

For the child modes, pass an **absolute** Python interpreter with `--python`.
This is a developer spike, not an exposed API; the absolute-path requirement is
deliberate. Prefer an interpreter whose pid is stable (a venv or Homebrew
`python3`); a `/usr/bin/python3`-style xcrun shim may spawn rather than exec, and
a `parent_mismatch` receipt is **not** evidence.

### 1. Native baseline (no prompt, read-only)

```sh
open -W -n --stdout "$DIR/host.out" --stderr "$DIR/host.err" \
  "$APP" --args native --output "$DIR/native.json"
```

### 2. Request Accessibility (prompts once, five-second lifetime)

```sh
open -W -n --stdout "$DIR/host.out" --stderr "$DIR/host.err" \
  "$APP" --args request --output "$DIR/request.json"
```

macOS shows the prompt asynchronously. In System Settings, verify **both the
displayed name and icon**, then manually enable **Ciaobot Server Host
(Experiment)**. The first request receipt may still say `false` while approval is
pending. Do not run `tccutil reset` against a live identity and do not edit the
TCC database; remove stale entries by hand in System Settings only.

### 3. Python Accessibility child

```sh
PY="$HOME/.venvs/ciaobot/bin/python"   # an absolute, stable-pid interpreter
open -W -n --stdout "$DIR/host.out" --stderr "$DIR/host.err" \
  "$APP" --args python --python "$PY" --output "$DIR/python.json"
```

### 4. Python → osascript (System Events) child

Reading a process role needs **both** the Automation grant (System Events) and
Accessibility for the responsible process; this mode exercises both.

```sh
open -W -n --stdout "$DIR/host.out" --stderr "$DIR/host.err" \
  "$APP" --args osascript --python "$PY" --output "$DIR/osascript.json"
```

The host stays alive while the child runs, bounds and drains the child's stdout
and stderr without pipe deadlock, enforces a finite timeout, terminates and reaps
a stalled child, and classifies the transport outcome as `ok`, `launch`, `exit`,
`timeout`, `oversized`, `parse`, or `parent_mismatch`. A permission denial is a
**valid negative experiment receipt**, not a transport failure; the receipt
preserves the denied result and never reports success on child exit alone. Only
`ok` implies the parent proof held.

## Receipts

Every receipt includes the host PID, bundle ID/name, actual bundle path, the
host's own AX trust state (`host_accessibility_trusted`, which is **not** proof
that the child had access), the mode, and a timestamp. Child-mode receipts add
the child script path, Python path, child PID, the child's self-reported PID and
PPID, whether the reported parent matches the host (`child_parent_matches_host`),
whether the self-reported pid matches the spawned pid (`child_pid_matches_spawn`),
the exit status, the termination reason (`exit` / `uncaught_signal`), the timeout
flag, whether the drains completed (`child_output_drained`), bounded
stdout/stderr with byte counts and truncation flags
(`child_stdout_bytes`/`child_stderr_bytes`,
`child_stdout_truncated`/`child_stderr_truncated`), the parsed child receipt, and
`transport_status`. Receipts are written atomically.

The child's own receipt records `mode`, `pid`, `ppid`, `platform`, and then:

- `python`: `accessibility_trusted`, `focused_element_result`, `role_result`,
  `focused_role`, and `ax_classification` (`ok` / `no_grant` /
  `no_focused_element` / `error`). A missing focused element is distinct from a
  missing grant.
- `osascript`: the exact `osascript_argv`, `osascript_returncode`,
  `osascript_timed_out`, `osascript_error_code`, `osascript_stdout_truncated` /
  `osascript_stderr_truncated`, bounded stdout/stderr, and
  `osascript_classification` (`ok` / `automation_denied` /
  `accessibility_denied` / `timeout` / `exit`). Automation denial (`-1743`) is
  separated from Accessibility denial (`-1719` / `-25211`), matched on the exact
  trailing code so `-17430` is not mistaken for `-1743`. No public API can
  automatically grant either; only a human can approve in System Settings.

## Evidence matrix (do not infer untested rows)

| Case | Procedure | Required evidence |
|---|---|---|
| Native baseline | `native` before any grant | `host_accessibility_trusted` recorded; honest `focused_element_result` (a denial here is a valid datum) |
| Python AX | `python` with the intended interpreter | Child PID/PPID match the host; raw AX codes; `ax_classification` |
| Python → osascript | `osascript` with the intended interpreter | Child PID/PPID match; `automation_denied` vs `accessibility_denied` vs `ok` separated |
| Relaunch | Repeat `native`/`python` through `open` after quits | Result retained without rebuild |
| Mutable sidecar update | Copy a revision fixture to `<output>/child/child_probe.py` with the same read-only operations; repeat the child modes | Host executable SHA-256 **and** signed bundle CDHash unchanged; only the sidecar differs |
| Interpreter replacement | Re-run a child mode with a different absolute `--python` at a disposable path, only if needed to disambiguate an existing Python grant | Whether attribution follows the host identity or a pre-existing interpreter grant |
| Host rebuild (negative case) | Build revision 2 to a new directory; replace only this experiment app at a stable path; repeat | Record whether reapproval is needed, plus SHA-256/CDHash changes |

### Reading the result honestly

Success under an already-granted Python interpreter is **not** proof that a new
host identity is responsible. Confirm responsibility with scoped TCC/`tccd` logs
that name the host's code requirement, and check the child's `ppid` proof in the
receipt. Separate these three questions, which the receipts above keep distinct:

- **Accessibility** vs **Automation** — different grants, different denial codes.
- **Signed host resource integrity** — the mutable sidecar is outside the signed
  bundle, so updating it must not change the host hash or signature; verify with
  `codesign --verify --strict`.
- **Genuine result vs untested assumption** — a build, a prompt, or a child exit
  alone proves none of the above.

Ad-hoc signatures do not promise stable grants after host changes. A production
helper would need a narrowly scoped authenticated transport and real tool
routing; this experiment grants agents no desktop control and reroutes nothing.

## Do not

- Run the installer, replace the live engine, reboot, or modify actual grants.
- Commit receipts or build output to the repository.
- Point `--output` inside the signed `.app` (directly, with a `..` segment, or
  through a symlinked parent); the host refuses it because a receipt there breaks
  `codesign --verify --strict`.
- Remove the PWA, the Python grant, existing Ciaobot bundles, or production
  service files when cleaning up.

Cleanup: remove only **Ciaobot Server Host (Experiment)** from System Settings →
Privacy & Security, and delete its explicitly named output directory.
