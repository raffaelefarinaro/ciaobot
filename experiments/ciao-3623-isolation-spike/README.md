# #3623 slice 1: principal-isolation spike

Throwaway experiment. No product code, nothing under `ciao/`, not shipped in the wheel. Result:
[`CAPABILITY_MATRIX.md`](CAPABILITY_MATRIX.md); raw output in `results/raw-results.txt`.

Question: can a dedicated `_ciao` account plus a root LaunchDaemon (Unix socket or XPC) stop a same-UID
agent from forging ordinary-chat evidence (see `docs/CHAT_INGRESS_AUTHORITY.md`)?

## Layout

- `src/authority.swift`: socket daemon, peer credential plus audit-token code requirement, HMAC-signed events.
- `src/xpcd.swift`: the same over an XPC Mach service (libxpc enforces the peer requirement).
- `src/app.swift`: stand-in operator app (CLI modes for the harness, `gui` composer).
- `src/inject.c`, `src/tfp.c`, `src/poster.swift`: attacker tools (DYLD injection, task_for_pid, CGEvent keystrokes).
- `scripts/build.sh`, `scripts/install.sh`: build and provision inside the VM (sudo; creates `_ciao`).
- `scripts/linux_probe.py`: uid and SO_PEERCRED probe, run in a throwaway container.

## Reproduce

Use only a disposable VM (see `.loop-engineering/execute-20261124/tart-vm.md`). `install.sh` creates a user and
daemons and must never run on a real machine.

```
scp -r experiments/ciao-3623-isolation-spike/{src,scripts} admin@<vm>:spike/
ssh admin@<vm> 'cd spike && sh scripts/build.sh && sudo sh scripts/install.sh'
# socket daemon: build/app who|submit <text>; XPC: build/xpcd as _ciao (MachServices plist), build/app xpc <text>
# attacks: run as the unprivileged user after removing the passwordless sudoers entry
docker run --rm -v "$PWD/experiments/ciao-3623-isolation-spike/scripts/linux_probe.py:/p.py:ro" python:3.12-slim python3 /p.py
```

The probe command lines used for each matrix row are visible in the headings of `results/raw-results.txt`.
