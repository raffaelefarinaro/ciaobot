# Server-host contract: syntax, identity, ownership

`ciao/server_host.py` is the fail-closed validator behind #1008 (child B1) for
the macOS `Ciaobot Server.app` native host built by
`scripts/build-server-host.py` and documented in
[`SERVER_HOST.md`](SERVER_HOST.md). Its tests are
`tests/test_server_host_contract.py`.

It exists because a filename is not proof. The service that will render the
launchd command (B2), the installer that writes the ownership record and the
updater that decides whether a running host is still the installed one all had
to agree on one answer to "is this bundle our host, and is this invocation
sound?", and no consumer should be able to answer it for itself, differently.

## The three questions, and the order they must be asked

| Layer | Function | Answers | Proves |
|---|---|---|---|
| Syntax | `parse_service_command` | what an argv *says* | nothing about the files it names |
| Identity | `inspect_host_bundle` (macOS only) | the installed bytes' immutable identity | a well-formed, ad-hoc-signed universal host — **not** that we installed it |
| Ownership | `verify_owned_host` | whether an existing private record matches the inspected bundle | this machine installed these bytes |

**Syntax is not identity.** `parse_service_command` accepts a hosted command
whose executable is named `CiaobotServerHost` even when no such file exists. The
host parser is pure and cross-platform, so it runs in the Windows job; its
argument paths are parsed with `PurePosixPath` so macOS argv fixtures parse
identically off macOS, and it deliberately refuses a backslash because the same
string names different files on POSIX and Windows.

**Identity is not ownership.** `inspect_host_bundle` returns a `HostOwnership`
snapshot for any correctly built, ad-hoc-signed, universal host — including one a
stranger built. A consumer must not render a command from that snapshot and treat
it as "ours".

**Only a matching existing private record is ownership.** `verify_owned_host`
reads the owner-only record, inspects the bundle, and requires the canonical
bundle path, identity, revision, protocol, executable digest, both slice CDHashes
and the whole file set to be equal. A missing record is a refusal; it is never a
reason to trust a bundle, and there is no direct-engine fallback.

## The command contract

```python
from pathlib import Path
from ciao.server_host import parse_service_command, host_service_argv

# Syntax only:
parse_service_command(
    ["/Applications/Ciaobot Server.app/Contents/MacOS/CiaobotServerHost",
     "serve", "--python", "/usr/bin/python3"]
)

# Render the hosted command AFTER verify_owned_host(bundle) has returned:
host_service_argv(Path.home() / "Applications" / "Ciaobot Server.app",
                  Path("/usr/bin/python3"))
```

The accepted shapes are exactly:

- **hosted** — `[<abs>/CiaobotServerHost, serve, --python, <abs interpreter>]`
  (host executable basename checked, both paths absolute);
- **direct** — `[<abs python|python3|python3.N>, [-I], -m, ciao.cli, run|supervise]`
  or `[<abs ciao>, run|supervise]`.

Repeated, unknown, extra or relative arguments, a non-string element and a wrong
host basename are refusals carrying a stable `ServerHostError.code`. The direct
shape is the existing migration shape, stated explicitly rather than as a
recovery shim.

`host_service_argv` runs no subprocess. It enforces the same absolute-POSIX and
`Ciaobot Server.app`-name rules and returns the exact tuple. Because it can only
see names, its docstring requires a prior `verify_owned_host`; B2 owns enforcing
that at the call site.

## The identity snapshot

`HostOwnership` is frozen, and holds only the host's own immutable identity:

```
schema, bundle_path, bundle_id, host_revision, host_protocol,
executable_sha256, per_arch_cdhashes{arm64,x86_64}, bundle_files{relpath: sha256}
```

It holds **no** engine version, workspace, node role or credential. `bundle_files`
maps every regular sealed file's bundle-relative POSIX name to its SHA-256; it
must name at least `Contents/Info.plist`,
`Contents/MacOS/CiaobotServerHost`, `Contents/Resources/CiaobotServer.icns` and
`Contents/_CodeSignature/CodeResources` (`REQUIRED_BUNDLE_FILES`).

`inspect_host_bundle` refuses:

- a non-macOS platform, before any native probe runs;
- a relative, symlinked or missing bundle, and a symlinked `Contents`;
- a symlinked or special file, a stray file (bundled Python, a config, a thin
  staging product) or a stray directory anywhere in the tree;
- a missing sealed file, a wrong `CFBundleIdentifier`, `CFBundleExecutable`,
  protocol, bundle revision or `LSMinimumSystemVersion`;
- a bundle that fails `codesign --verify --strict`, or is not ad-hoc signed;
- an executable whose `lipo -archs` is not exactly `arm64 x86_64`, or whose
  `codesign -dv --arch <arch>` reports no CDHash / a CDHash that is not 40
  lowercase hex.

The only native commands it runs are the public, read-only `codesign --verify
--strict`, `codesign -dv --verbose=4` and `lipo -archs`, with a fixed argv, no
shell, `capture_output`, and a ten-second bound (`NATIVE_TIMEOUT_SECONDS`). No
signing, launch, `open`, `launchctl` or Accessibility call, and no installed
bundle, engine or TCC state is touched.

## The ownership record

The record is a JSON object at an explicit path — the default is
`~/.local/state/ciaobot/server-host.json` (`DEFAULT_OWNERSHIP_PATH`) — written
**outside** the sealed app. A record inside the bundle would change the very
digest the next read compares and would be covered by the signature it vouches
for, so a record path inside the recorded bundle is refused. The module never
writes the record: B1 defines the reader and the shape, and B2's installer
serializes `HostOwnership.to_record()` owner-only, only after a verified
installation.

`read_host_ownership` is strict. It refuses a missing file (`missing_record`), a
symlink or non-regular file, malformed JSON, a duplicate JSON key anywhere
(including nested), an unknown or non-integer schema (`unsupported_schema` — a
JSON `true` is not `1`), a wrong bundle id / revision / protocol, a hash that is
not 64 lowercase hex, a CDHash that is not 40 lowercase hex, a `bundle_files` name
that is absolute, backslashed, non-canonical or traversing, a mapping missing a
required sealed file, and extra or missing top-level fields. Values are never
coerced. On macOS it additionally requires the record's owner to be this uid and
its mode to grant nothing to group or other; the uid is read through `getattr`
so the module imports and type-checks on Windows.

## Sidecar, defaults and the launchd bounds

The service's disk-sidecar facts a consumer needs are constants here, so B2 does
not re-spell them:

- bundle `Ciaobot Server.app`, id `local.ciaobot.server`, executable
  `CiaobotServerHost`, icon `CiaobotServer.icns`;
- `HOST_REVISION` / `HOST_PROTOCOL` (`CiaobotServerHostProtocol`) = `1`, tied to
  the host, never the engine release;
- `MINIMUM_SYSTEM_VERSION` `13.0`;
- `STOP_GRACE_SECONDS` 35 and `EXIT_TIMEOUT_SECONDS` 45. The launchd job's
  `ExitTimeOut` must exceed the host's 35 s stop grace, which itself exceeds the
  Python supervisor's own grace, so launchd never sweeps the job group out from
  under the host's own stop. This is a later consumer's obligation; the constants
  exist so the plist and the host cannot drift.

There are no environment variables and no compatibility shims. The defaults are
hardcoded home-anchored constants; an environment-redirectable install path is a
path an unprivileged writer could redirect.

## What a validated disk record does not say

`verify_owned_host` validates the **bytes on disk**. It cannot answer what a
running launchd job is executing: a plist edited on disk is not the loaded job,
and a validated bundle is not a live engine. The loaded launchd command remains a
requirement of the updater (later child C), which must read the live job rather
than trust the file.

## Not in B1

Service activation, the CLI/plist/discovery/sign-in consumers, an ownership-receipt
write API, artifact extraction, any change under `~/Applications` or the private
runtime, and any new environment variable. B2 is mandatory before the updater (C)
and the packaging child (E). B1 alone is not service integration.
