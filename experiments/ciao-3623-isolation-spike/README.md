# #3623 slice 1: principal-isolation spike

Throwaway experiment. No product code, nothing under `ciao/`, not shipped in the wheel. Result:
[`CAPABILITY_MATRIX.md`](CAPABILITY_MATRIX.md); raw output in `results/raw-results.txt`.

Question: can a dedicated `_ciao` account plus a root LaunchDaemon (Unix socket or XPC) stop a same-UID
agent from forging ordinary-chat evidence (see `docs/CHAT_INGRESS_AUTHORITY.md`)?

## Read the results with these sandbox caveats

- The macOS VM has **SIP disabled**. Hardened-runtime enforcement of DYLD injection does not apply there, so
  B7 in the VM proves nothing about a normal Mac; the SIP-on result comes from the host (macOS 27.0.1, not 15).
- The VM image **pre-grants TCC Accessibility/PostEvent** to `sshd-keygen-wrapper`, `osascript`, python and
  `tart-guest-agent`. The C1 "no grant" cases delete these rows and restart `tccd`; C2 re-adds only one.
  Passwordless sudo is removed before attack probes; the install steps use the VM's admin password.
- Self-signed certificate instead of Developer ID, `_ciao` created by `dscl` not an installer, no
  `SMAppService`, Linux only in a Docker Desktop container, Windows not tested.

## Layout

- `src/authority.swift`: socket daemon, peer credential plus audit-token code requirement, HMAC-signed events.
- `src/xpcd.swift`: the same over an XPC Mach service (libxpc enforces the peer requirement).
- `src/app.swift`: stand-in operator app (CLI modes for the harness, `gui` composer).
- `src/inject.c`, `src/tfp.c`, `src/poster.swift`: attacker tools (DYLD injection, task_for_pid, CGEvent keystrokes).
- `scripts/build.sh`, `scripts/install.sh`: build and provision inside the VM (sudo; creates `_ciao`).
- `scripts/vm_probe_c1.sh`, `scripts/vm_probe_b7.sh`: the clean synthetic-input and injection re-runs.
- `scripts/linux_probe.py`: uid and SO_PEERCRED probe, run in a throwaway container.

## Reproduce

Use only a disposable VM; `install.sh` creates a user and daemons and must never run on a real machine.

```
# once: Tart 2.x, image ghcr.io/cirruslabs/macos-sequoia-base:latest (macOS 15.7.x arm64, login admin/admin)
tart clone ghcr.io/cirruslabs/macos-sequoia-base:latest ciao-macos-test
tart run --no-graphics ciao-macos-test &
# ssh needs SSH_ASKPASS with a script that echoes the password (no sshpass); then:
scp -r experiments/ciao-3623-isolation-spike/{src,scripts} admin@$(tart ip ciao-macos-test):spike/
ssh admin@<vm> 'cd spike && sh scripts/build.sh && sudo sh scripts/install.sh'
# XPC variant: install build/xpcd as /Library/PrivilegedHelperTools/com.ciao.spike.xpc, LaunchDaemon with
#   UserName _ciao and MachServices com.ciao.spike.xpc; client: build/app xpc <text>
# attacks, as the unprivileged user: zsh scripts/vm_probe_c1.sh ; zsh scripts/vm_probe_b7.sh
docker run --rm -v "$PWD/experiments/ciao-3623-isolation-spike/scripts/linux_probe.py:/p.py:ro" python:3.12-slim python3 /p.py
tart stop ciao-macos-test
```

The other rows (file access, peer identity, upgrade) were run as ad-hoc shell snippets; their exact command
lines are not kept, only the output in `results/raw-results.txt`.
