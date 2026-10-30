# Publishing and authenticating the server host

This is the release-side contract for the prebuilt universal macOS server host
(`docs/SERVER_HOST.md`, #1009) inside the signed engine release manifest
(#1022, child D of #1008). It describes what the release publishes and what a
consumer may trust from it. It does **not** activate a host, install anything, or
change the engine updater; installer activation and host upgrade approval are
later children of #1008.

## What the release publishes

The engine release manifest keeps schema `1`. Alongside the wheel it may now
carry one optional `server-host` artifact authenticating the fixed archive:

```
ciaobot-server-host-macos-universal-v1.tar.gz
```

The artifact entry fixes every field a consumer acts on, independent of the
engine version:

| Field | Value |
|---|---|
| `kind` | `server-host` |
| `platform` | `macos` |
| `arch` | `universal` |
| `filename` | `ciaobot-server-host-macos-universal-v1.tar.gz` |
| `bundle_id` | `local.ciaobot.server` |
| `host_revision` | `4` |
| `host_protocol` | `2` |

`host_revision` and `host_protocol` are the host's own revision, never the
engine release version. `publish.yml` currently rebuilds the archive on every
release, so its `sha256` and `size` (computed from the bytes that release
serves) and the host's CDHash can differ between releases that share revision
`4`; whether to reuse one archive across releases is still open. The selector
accepts a host entry only when the platform, arch, filename, bundle id,
revision, protocol and positive size all match exactly; `host_revision`/`host_protocol` reject a boolean, so `True`
cannot stand in for revision `4`.

### The explicitly signed entry

Two entry shapes exist, and nothing in between. The legacy shape above is what
every release publishes today. `--server-host-team-id` emits the signed shape
instead, for a Developer ID signed, notarized archive built by
`scripts/build-server-host.py`:

| Field | Value |
|---|---|
| `host_revision` | `5` |
| `host_protocol` | `2` |
| `signing` | exactly `{"kind": "developer-id", "team_id": "<EXPECTED_TEAM_ID>"}` |

The team is ten uppercase letters or digits, validated before any requirement
text is built; `<EXPECTED_TEAM_ID>` is configuration notation, never a value in
this repository. The selector refuses a signed entry under revision 4, a
revision-5 entry with no `signing` object or with any other `kind`, extra or
missing signing fields, a malformed team, and any of the legacy identity
mismatches. A signing team without `--server-host` is a build error, and a
refused build writes no manifest.

Supplying the flag does **not** publish a signed host: `publish.yml` still runs
the un-flagged build until a separately approved activation, so the release keeps
serving revision 4. Authenticating the expected team in the manifest is what lets
the installer persist it in the schema-2 ownership record and every later runtime
verification state it explicitly (`docs/SERVER_HOST_CONTRACT.md`). The manifest
signature must verify before the team is used, so editing the team after signing
produces no trusted value.

## Wheel-only releases remain supported

The host is opt-in. A manifest with no `server-host` entry is a valid schema-1
manifest and a wheel-only release, and that is what every historical release
looks like; `verify_manifest` accepts it unchanged. A consumer that needs the
host must ask for it explicitly, and absence is a clear refusal rather than a
wheel fallback:

```python
manifest = verify_manifest(raw, signature_text)   # proves authenticity first
entry = select_server_host_artifact(manifest)     # raises when there is none
```

The selector validates the entry's shape and supported identity, but it does
**not** prove the manifest authentic. The caller must verify the signature
first (`verify_manifest`, or `verify_signature` over the same bytes); the
selector trusts those bytes and must never be the only check.

The selector refuses a host that is ambiguous: a second `server-host` entry, or
the host filename reused under another kind. Which bytes a consumer stages must
follow from the signed identity, not from list order. `verify_manifest` applies
only the generic per-entry rules and never pins the host identity or its
uniqueness: the engine updater verifies every manifest through it and only takes
the wheel, so a later host revision must not refuse the wheel update on engines
that predate it.

## Build the manifest

Positional wheels are still required; the host is an optional flag:

```sh
python -m ciao.release_manifest build \
  --version 1.2.3 \
  --out ciaobot-engine-manifest.json \
  --server-host /tmp/ciaobot-server-host-build/ciaobot-server-host-macos-universal-v1.tar.gz \
  dist/ciaobot-1.2.3-py3-none-any.whl
```

For the explicitly signed archive, add the team the build was signed by:

```sh
python -m ciao.release_manifest build \
  --version 1.2.3 \
  --out ciaobot-engine-manifest.json \
  --server-host /tmp/ciaobot-server-host-build/ciaobot-server-host-macos-universal-v1.tar.gz \
  --server-host-team-id <EXPECTED_TEAM_ID> \
  dist/ciaobot-1.2.3-py3-none-any.whl
```

The named host archive must be a regular, non-empty file with exactly the fixed
name; a mismatch is refused before the manifest is written, so a refused build
leaves any existing manifest untouched. The archive itself is built separately
by `scripts/build-server-host.py` (see `docs/SERVER_HOST.md`), which compiles
both architectures, assembles the bundle, ad-hoc signs it once, archives it and
re-extracts to strict-verify the signature before the manifest ever names it.

## In `publish.yml`

On a published release the workflow, after preparing the clean engine-check
Python:

1. runs `scripts/build-server-host.py --output "$RUNNER_TEMP/server-host-build"`
   with that Python (3.12+) — it compiles the universal host, ad-hoc signs,
   archives and re-verifies. The archive is written outside the repository and
   the signed app tree is never modified;
2. writes the manifest with `--server-host` pointing at the verified archive,
   keeping the same release signing key and the `ciao.release_manifest verify`
   gate;
3. attaches the host archive as the seventh release asset and asserts the exact
   name is present while the tag is still known.

The release now carries seven assets: `install.sh`, `install-engine.sh`,
`install.ps1`, the wheel, the manifest, its signature, and the host archive. It
adds no app archive, no `latest.json` feed, no verifier and no bundled runtime,
and it introduces no new secret, codesign identity, notarization step or
environment knob.

## Platform and permission identity

The host is macOS 13+ universal (arm64 and x86_64 in one binary) and, in the
production revision-4 archive, ad-hoc signed: there is no per-user compiler at
install time, because the prebuilt archive is the artifact a user receives. Its
Accessibility and Automation grants are tied to the exact signed bytes, so any
rebuild changes the CDHash and can require the user to reapprove the prompt.

The opt-in revision-5 policy is Developer ID signed and notarized instead. It
carries the hardened runtime, a secure timestamp and exactly one entitlement
(`com.apple.security.automation.apple-events`), and it is accepted only for the
team the signed manifest names — the designated requirement pins the bundle, the
Developer ID extensions and the team, never a certificate fingerprint or CDHash,
so a certificate renewal within the team keeps the same logical identity. The
signed policy is not published by `publish.yml` yet: the release continues to
serve the ad-hoc revision-4 host until a separately approved activation, and a
signed archive's real ticket layout and round trip remain a promotion gate for
the first protected rehearsal rather than a claim made here.

Revision 2 (#1249) names the executable `Ciaobot Server`, so the Login Items row reads "Ciaobot Server".

Revision 3 (#1659) adds the three folder usage descriptions
(`NSDesktopFolderUsageDescription`, `NSDocumentsFolderUsageDescription`,
`NSDownloadsFolderUsageDescription`), so the macOS prompt for each folder shows
an explanation line instead of a blank one. Those keys are part of the sealed
plist, so the revision moved with them, and the installer can now replace a
revision-2 host it can prove is ours (`ciao/server_host_install.py`), restoring
the old bundle and the old record if the replacement fails. The installer warns
once that macOS will ask again for Accessibility and Automation, because those
grants are tied to the exact signed bytes.

Revision 4 (#1642) changes only the host's startup stop handling: the launch
reads a private signal queue, so a stop that arrives just before the launch
still prevents the spawn. The sealed bytes change, so the installer replaces a
revision-2 (v1.6.0) or revision-3 (v1.6.1) host the same way and the user is warned
once. The revision-2 allowance is removed one release after revision 4 ships.

Revision 5 is the explicitly signed revision (#2329). It is understood by the
manifest selector and the runtime contract but is not published: `publish.yml`
keeps building and signing the revision-4 archive until a separately approved
activation, and no installed host is migrated to it here.

## What this does not do

- It does not activate, load or launch the host, and it does not touch launchd,
  `~/Applications`, TCC or any permission. Acquiring and installing the host is
  the installer's separate, opt-in job (child E1, `ciao/server_host_install.py`);
  the release only authenticates the archive.
- It does not make the engine updater overwrite an installed host: an installed
  compatible host is never auto-replaced by an engine update, because the host
  is authenticated but opt-in and its revision is independent of the engine.
- A host upgrade is a separate, explicit, warned action reviewed on its own; it
  is not implied by an engine update and is not implemented here.
- It documents no release/admin procedure to run: cutting a release, signing it
  and publishing it remain the maintainer's, and are not performed by this
  change.
