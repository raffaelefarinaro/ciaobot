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
| `host_revision` | `1` |
| `host_protocol` | `1` |

`host_revision` and `host_protocol` are the host's own revision, never the
engine release version. `publish.yml` currently rebuilds the archive on every
release, so its `sha256` and `size` (computed from the bytes that release
serves) and the host's CDHash can differ between releases that share revision
`1`; whether to reuse one archive across releases is still open. The selector
accepts a host entry only when the platform, arch, filename, bundle id,
revision, protocol and positive size all match exactly; `host_revision`/`host_protocol` reject a boolean, so `True`
cannot stand in for revision `1`.

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

The host is macOS 13+ universal (arm64 and x86_64 in one binary) and ad-hoc
signed, not Developer ID signed. There is no per-user compiler at install time:
the prebuilt archive is the artifact a user receives. Its Accessibility and
Automation grants are tied to the exact signed bytes, so any rebuild changes the
CDHash and can require the user to reapprove the prompt.

## What this does not do

- It does not activate, install, load or launch the host, and it does not touch
  launchd, `~/Applications`, TCC or any permission.
- It does not make the engine updater overwrite an installed host: an installed
  compatible host is never auto-replaced by an engine update, because the host
  is authenticated but opt-in and its revision is independent of the engine.
- A host upgrade is a separate, explicit, warned action reviewed on its own; it
  is not implied by an engine update and is not implemented here.
- It documents no release/admin procedure to run: cutting a release, signing it
  and publishing it remain the maintainer's, and are not performed by this
  change.
