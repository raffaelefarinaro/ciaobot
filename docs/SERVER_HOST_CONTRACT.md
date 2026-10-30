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
| Identity | `inspect_host_bundle` (macOS only) | the installed bytes' immutable identity | a well-formed host for one explicit signing policy — **not** that we installed it |
| Ownership | `verify_owned_host` | whether an existing private record matches the inspected bundle | this machine installed these bytes, under the policy that record names |

**Syntax is not identity.** `parse_service_command` accepts a hosted command
whose executable is named `Ciaobot Server` even when no such file exists. The
host parser is pure and cross-platform, so it runs in the Windows job; its
argument paths are parsed with `PurePosixPath` so macOS argv fixtures parse
identically off macOS, and it deliberately refuses a backslash because the same
string names different files on POSIX and Windows.

**Identity is not ownership.** `inspect_host_bundle` returns a `HostOwnership`
snapshot for any correctly built bundle that satisfies the selected policy —
including one a stranger built. A consumer must not render a command from that
snapshot and treat it as "ours".

**Only a matching existing private record is ownership.** `verify_owned_host`
reads the owner-only record, inspects the bundle under the exact policy that
record names, and requires the canonical bundle path, identity, revision,
protocol, executable digest, both slice CDHashes, the whole file set and the
signing identity (schema 2 only) to be equal. A missing record is a refusal; it
is never a reason to trust a bundle, and there is no direct-engine fallback.

## The command contract

```python
from pathlib import Path, PurePosixPath
from ciao.server_host import (
    default_bundle_path,
    host_service_argv,
    parse_service_command,
    verify_owned_host,
)

# Syntax only:
parse_service_command(
    ["/Applications/Ciaobot Server.app/Contents/MacOS/Ciaobot Server",
     "serve", "--python", "/usr/bin/python3"]
)

# Render the hosted command AFTER verify_owned_host(bundle) has returned. A
# verified snapshot carries a str path; wrap it in PurePosixPath for rendering:
verified = verify_owned_host(default_bundle_path())
host_service_argv(PurePosixPath(verified.bundle_path), Path("/usr/bin/python3"))
```

The accepted shapes are exactly:

- **hosted** — `[<abs>/Ciaobot Server, serve, --python, <abs python|python3|python3.N>]`
  (host executable and interpreter basenames checked, both paths absolute);
- **direct** — `[<abs python|python3|python3.N>, [-I], -m, ciao.cli, run|supervise]`
  or `[<abs ciao>, run|supervise]`.

The hosted host itself launches `[<python>, -I, -m, ciao.cli, supervise]`
(`SUPERVISOR_ARGUMENTS` in `ServerHost.swift`); the tests read that Swift
constant and require it to parse as a direct `supervise` command that
`ciao.cli` really exposes.

Every path must be absolute and already normalized (no `.`, `..`, doubled or
trailing `/`), so the rendered command names the canonical bundle a verification
resolved. Repeated, unknown, extra or relative arguments, a non-string element and
a wrong host or interpreter basename are refusals carrying a stable `ServerHostError.code`. The direct
shape is the existing migration shape, stated explicitly rather than as a
recovery shim.

`host_service_argv` runs no subprocess. It enforces the same absolute-POSIX and
`Ciaobot Server.app`-name rules and returns the exact tuple. Because it can only
see names, its docstring requires a prior `verify_owned_host`; B2 owns enforcing
that at the call site.

## The identity snapshot

`HostOwnership` is frozen, its two mappings are read-only copies, and it holds
only the host's own immutable identity:

```
schema, bundle_path, bundle_id, host_revision, host_protocol,
executable_sha256, per_arch_cdhashes{arm64,x86_64}, bundle_files{relpath: sha256},
signing{kind: "developer-id", team_id}   # schema 2 only
```

It holds **no** engine version, workspace, node role or credential. `bundle_files`
maps each sealed file's bundle-relative POSIX name to its SHA-256, and names
exactly the files the selected policy seals. The production revision-4 host has
the four the builder seals: `Contents/Info.plist`,
`Contents/MacOS/Ciaobot Server`, `Contents/Resources/CiaobotServer.icns` and
`Contents/_CodeSignature/CodeResources` (`REQUIRED_BUNDLE_FILES`). The signed
revision-5 host has those four plus the stapled notarization ticket
`Contents/CodeResources` (`SIGNED_BUNDLE_FILES`), which is a different path from
the signature's own resource. Code signing does not seal the ticket the way it
seals ordinary resources, so the record hashes it as ordinary immutable material.
A new resource is a new host revision, not a wider mapping, and there is no
free-form allowlist.

`inspect_host_bundle` refuses:

- a non-macOS platform, before any native probe runs;
- a policy that does not exist: the signed revision without an explicit
  `DeveloperIDSigning`, a signing identity under any other revision, or any
  revision/protocol pair that is neither the production one nor a superseded one;
- a relative, symlinked or missing bundle, one not named `Ciaobot Server.app`,
  and a symlinked `Contents`;
- a symlinked, special or unreadable file, an unlistable directory, a stray file
  (bundled Python, a config, a thin staging product, an extra signature file) or
  a stray directory anywhere in the tree;
- a missing sealed file, an `Info.plist` that does not parse, a wrong `CFBundleIdentifier`, `CFBundleExecutable`,
  protocol (an integer: a plist `<true/>` is not `1`), bundle revision or
  `LSMinimumSystemVersion`;
- for the production policy, a bundle that fails `codesign --verify --strict`
  (which checks every architecture of a universal binary by default), is not
  ad-hoc signed, or is signed under another identifier;
- for the signed policy, everything listed under "The signed host policy" below;
- a `codesign -dv` `Format=` other than `app bundle with Mach-O universal` over
  exactly `arm64` and `x86_64`, or a slice that reports no CDHash / a CDHash that
  is not 40 lowercase hex;
- a probe that cannot start or exceeds its bound — a refusal, not a crash.

The only native tool it runs is `/usr/bin/codesign` (`--verify --strict`,
`-dv --verbose=4`, and for a signed host the requirement and entitlement display
reads), by absolute path, with a fixed argv, no shell, `capture_output`, and a
ten-second bound (`NATIVE_TIMEOUT_SECONDS`). The absolute path keeps the
service's `PATH` from substituting it. `lipo` is deliberately not used:
`/usr/bin/lipo` is an xcode-select shim that needs the Command Line Tools (a host
machine may not have them, and a background job can raise the install prompt) and
follows `DEVELOPER_DIR`. No signing, launch, `open`, `launchctl`, `xcrun`,
`stapler`, `spctl` or network call, and no installed bundle, engine or TCC state
is touched. Notarization and Gatekeeper acceptance are release checks in
`scripts/build-server-host.py` and CI, never a runtime startup probe.

## The signed host policy (revision 5)

The revision-4 contract is unchanged and is still the production default: this
engine publishes, installs and verifies an ad-hoc signed revision-4 host. An
explicitly signed host is a second, finite policy that a private record selects.

```python
SIGNED_HOST_REVISION = 5
SIGNED_SCHEMA_VERSION = 2

@dataclass(frozen=True)
class DeveloperIDSigning:
    team_id: str          # exactly [A-Z0-9]{10}

def developer_id_requirement(team_id: str) -> str: ...
```

`DeveloperIDSigning` validates the team before any requirement text exists, and
`developer_id_requirement` builds exactly the text
`scripts/build-server-host.py::designated_requirement` signs with (the two are
asserted identical by `tests/test_server_host_contract.py`):

```
identifier "local.ciaobot.server"
and anchor apple generic
and certificate 1[field.1.2.840.113635.100.6.2.6] /* exists */
and certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */
and certificate leaf[subject.OU] = "<EXPECTED_TEAM_ID>"
```

`<EXPECTED_TEAM_ID>` is configuration notation, never a value in this repository.
The requirement names the team and the two Developer ID extensions, never a leaf
certificate fingerprint or CDHash, so a renewal within the same team keeps the
same logical identity — and a code-specific or leaf-pinned designated
requirement is refused rather than accepted as a stable upgrade identity.

Where the expected team comes from, and where it does not:

1. the release manifest names it, and the manifest's minisign signature must
   verify before the value is used (`ciao.release_manifest`);
2. the installer persists it in the schema-2 ownership record (that call site is
   a later child; this contract only accepts such a record);
3. every later verification reads it from that record and states it to
   `inspect_host_bundle`. It is never learned from the bundle,
   `codesign -dv`, the keychain or the environment.

The signed policy requires, on the bundle and on **every** slice:

- `codesign --verify --strict --all-architectures` (integrity of a universal
  binary);
- `codesign --verify --strict --all-architectures --test-requirement
  =<requirement>`: the external expected requirement, evaluated
  cryptographically against the certificate chain. Display text such as
  `Authority=` or `TeamIdentifier=` is supporting evidence, never the proof;
- the bundle identifier `local.ciaobot.server` and exactly the `arm64` and
  `x86_64` slices;
- the hardened-runtime CodeDirectory flag (`CS_RUNTIME`, `0x10000`), not merely a
  `Runtime Version=` line, and a secure timestamp;
- the canonical designated requirement (comments and `exists` spellings are
  normalized; identifiers, teams and hashes are not);
- exactly one entitlement, `{"com.apple.security.automation.apple-events": true}`.
  A missing, false, coerced (`1` is not `true`), extra, runtime-relaxing
  (JIT, debugger, disabled library validation, `get-task-allow`) or malformed
  blob is refused. The usage-description strings stay in `Info.plist` and are not
  part of this policy.

A disproved policy is the stable code `signature_rejected`; a probe that could not
start or timed out stays `inspection_failed`. The distinction lets the installer
say "this bundle is not the signed host" rather than "this Mac could not check",
and neither result authorizes activation. A failed signed check is never retried
as ad-hoc acceptance.

## The ownership record

The record is a JSON object at an explicit path — the default is
`~/.local/state/ciaobot/server-host.json` (`default_ownership_path()`) — written
**outside** the sealed app. A record inside the bundle would change the very
digest the next read compares and would be covered by the signature it vouches
for, so a record path inside the recorded bundle is refused; both paths are
resolved first, so a symlinked ancestor cannot hide the overlap. The module never
writes the record: B1 defines the reader and the shape, and the installer
(#1008 child E) serializes `HostOwnership.to_record()` owner-only, only after a
verified installation.

`read_host_ownership` is strict. It refuses an absent file (`missing_record`;
a record it cannot stat for any other reason is `invalid_ownership`, not
missing), a symlink or non-regular file (the read goes through an `O_NOFOLLOW`
descriptor that must be the inode the checks examined), malformed JSON, a duplicate JSON key anywhere
(including nested), an unknown or non-integer schema (`unsupported_schema` — a
JSON `true` is not `1`), a wrong bundle id / revision / protocol, a hash that is
not 64 lowercase hex, a CDHash that is not 40 lowercase hex, a `bundle_files`
mapping that is not exactly the sealed names of its own policy (so an absolute,
backslashed, non-canonical or traversing name is refused), a `bundle_path` not
named `Ciaobot Server.app`, and extra or missing top-level fields, and an
`executable_sha256` that differs from the executable's `bundle_files` digest.
Values are never coerced. On macOS it additionally requires the record's owner to
be this uid and its mode to grant nothing to group or other; that check sits
inside a `sys.platform == "darwin"` branch, so mypy on Windows does not see
`os.getuid`.

The schema selects the policy before any other field is read, and the two cannot
be mixed:

| Schema | Revision / protocol | Signing | `bundle_files` |
|---|---|---|---|
| `1` (production, unchanged) | `4` / `2` | no `signing` key at all | the historical four |
| `2` (explicitly signed) | `5` / `2` | exactly `{"kind": "developer-id", "team_id": "<EXPECTED_TEAM_ID>"}` | the four plus `Contents/CodeResources` |

Schema 1 cannot claim revision 5, cannot carry a signing identity and cannot name
the ticket; schema 2 cannot claim revision 4, cannot omit or widen its signing
object and cannot fall back to the four-file map. There is no coercion between
the two and no unknown extra key. `HostOwnership.to_record()` writes exactly the
schema's field set, so a schema-1 document stays byte-shape-compatible with every
record already on disk.

## Sidecar, defaults and the launchd bounds

The service's disk-sidecar facts a consumer needs are constants here, so B2 does
not re-spell them:

- bundle `Ciaobot Server.app`, id `local.ciaobot.server`, executable
  `Ciaobot Server`, icon `CiaobotServer.icns`;
- `HOST_REVISION` = `4` and `HOST_PROTOCOL` (`CiaobotServerHostProtocol`) = `2`, tied to
  the host, never the engine release. These are still the production defaults;
  `SIGNED_HOST_REVISION` = `5` and `SIGNED_SCHEMA_VERSION` = `2` name the opt-in
  policy and change nothing about what the release publishes;
- `MINIMUM_SYSTEM_VERSION` `13.0`;
- `STOP_GRACE_SECONDS` 35 and `EXIT_TIMEOUT_SECONDS` 45. The launchd job's
  `ExitTimeOut` must exceed the host's 35 s stop grace, which itself exceeds the
  Python supervisor's own grace, so launchd never sweeps the job group out from
  under the host's own stop. This is a later consumer's obligation; the constants
  exist so the plist and the host cannot drift.

There are no Ciaobot-specific environment variables and no compatibility shims.
The defaults (`default_bundle_path()`, `default_ownership_path()`) are
home-anchored and resolved at call time, like the install receipt's path; they
follow `Path.home()` and nothing else.

## The superseded host

A host revision bump has to be able to replace the host an older install left
behind, and it has to be able to refuse one it cannot prove is ours (#1659).

`SUPERSEDED_HOST_PROTOCOLS` = `{2: 1, 3: 2}` maps each revision this engine
supersedes (revision 2, the v1.6.0 host, and revision 3, the v1.6.1 host) to the
protocol it spoke.
`read_superseded_ownership` and `verify_superseded_host` are the same strict
reader and the same exact record-and-bundle match as their current-revision
counterparts, run against the revision the record names, each proven against its
own pair; a revision outside the map (1, 5, the current one) is refused.
Superseded records are the historical schema 1 only: a schema-2 or signed host is
never accepted by the replacement path. `read_host_ownership` and
`verify_owned_host` accept exactly the two current policies above (schema
1/revision 4 and schema 2/revision 5): a revision-2 or -3 record is not proof that
the installed bundle is the host this engine speaks to.

`verify_superseded_host` answers one narrower question — *was this bundle
ours?* — and it exists for one caller: the installer, which moves such a bundle
aside, writes the new record, renames the new bundle in, and puts the old
bundle and the old record back if anything fails. A bundle whose record names
any other revision, a foreign bundle and a tampered one are all refused exactly
as before, so the replacement path cannot be used to replace a host Ciaobot
cannot prove it owns. Delete the revision-2 entry of
`SUPERSEDED_HOST_PROTOCOLS` one release after revision 4 ships. Delete the map,
`read_superseded_ownership` and `verify_superseded_host` once no supported
install can still hold a revision-3 bundle, i.e. after a release that ships
revision 4 has been out for a full release cycle.

The record is outside the sealed app on purpose: an engine update changes the
engine and its sidecar files, never the host bytes, so the host keeps its CDHash
and the user's Accessibility/Automation grants, which are tied to those signed
bytes (see [`SERVER_HOST.md`](SERVER_HOST.md)). Only a new host revision replaces
the bundle and its record together.

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

The signed revision-5 policy (#2329) is likewise contract only: it does not
migrate an installed host, replace a bundle, change the installer's call site,
publish a signed archive, or run a notarization/staple/Gatekeeper check at
startup. Production keeps publishing and installing revision 4 until a separately
approved activation.
