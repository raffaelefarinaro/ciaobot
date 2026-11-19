---
name: release-mac
description: Release the Ciaobot for Mac app (the Swift package in mac/) — build, Developer ID sign, notarize, generate the signed appcast, and publish a version plus the update feed. Use whenever cutting a Mac client version, shipping an update, regenerating the appcast, or when a user asks why an installed client is not updating. Not for engine releases; those use ciao-release.
---

# Releasing Ciaobot for Mac

> **Publishing is operator-only.** The app's source, its update feed (the `updates` appcast, see [Feed](#feed)) and the release pipeline all live in this repository. `release-on-main` creates the version release `v<version>` when a release PR merges into `main`, and `publish.yml` builds the engine for it. The DMG is attached and both appcast feeds are published by hand, in the order given in section 6. Four things stay with the operator and never run in CI or in an agent session: the Developer ID Application identity, the App Store Connect notary key, the Sparkle EdDSA private key (all in the operator's keychain or outside the repository), and the merge into `main`. Without `--repo`, `scripts/publish-release.sh` targets `raffaelefarinaro/ciaobot`. An urgent 1.x client fix is cut in `raffaelefarinaro/ciaobot-mac` with that repository's copy of this skill, then brought here with `git subtree pull --prefix=mac https://github.com/raffaelefarinaro/ciaobot-mac.git main`.

This repository is public. Treat every committed file as publishable: no
private keys, app-specific passwords, keychain profile names, Apple Account
identifiers, absolute paths, or CI secrets may ever be added. Release
credentials live in the release operator's login keychain.

Read `mac/docs/reference.md` first for the current commands; every path and command below is relative to `mac/` and runs from there; this skill is the sequence and
the judgement around them. If a script's flags disagree with this document,
the script wins — fix this document in the same change.

## Before you start

You need, on one trusted Mac:

- Xcode 24 or later with the command line tools selected
  (`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`).
- A **Developer ID Application** certificate for this project's team in the
  login keychain (`security find-identity -v -p codesigning` shows it).
- An **App Store Connect API key** with Developer access, preferred over a
  notarytool keychain profile because it involves no keychain item and no
  interactive approval. Download the `.p8` once and keep it at
  `~/.appstoreconnect/private_keys/AuthKey_<KEY_ID>.p8` with mode `0600`, outside
  the repository. Its Key ID and Issuer ID are identifiers, not secrets; the
  `.p8` file is. A `notarytool` keychain profile still works as a fallback, but a
  keychain-stored notary credential has disappeared on this machine before.
- The **Sparkle EdDSA private key** in the same keychain. Generate it once with
  Sparkle's `generate_keys`; it prints the public key that belongs in
  `release/update-config.env`. Losing this key stops all future updates.
- Clean `develop` and a green `mac-app` check for the commit you are releasing.

Check both keys still exist before building: losing either late in the process
wastes the whole run.

**Find the notarization credential before asking for it.** Which one this Mac
uses (an API key id and issuer, or a `notarytool` keychain profile) is recorded
in the release operator's private notes, never in this repository. Read those
notes first. Then confirm the credential works without building anything:
`xcrun notarytool history --keychain-profile <name>` (or `--key/--key-id/--issuer`)
lists the previous release's submission as `Accepted`. Ask the operator only
when the notes name nothing and no candidate passes that check.

On the first signing run the keychain asks whether `generate_appcast` may use
the update key. Choose **Always Allow**; that exception is per binary path, so a
Sparkle upgrade may ask once again. If the release runs unattended, expect it to
wait on that dialog rather than fail.

## 1. Decide the version

The version is the `version` in the repository's `pyproject.toml`. Bump it in the
commit that warrants the release. `scripts/build-app.sh` derives
`CFBundleShortVersionString` from it and `CFBundleVersion` as major × 10000 +
minor × 100 + patch, so 2.0.0 is build 20000, above the build 14 that 0.5.0
shipped. Minor and patch must stay below 100; any other shape fails the build
before the Swift build starts. Sparkle compares `CFBundleVersion` as an integer,
so it must increase monotonically; never reuse a build number.

Semantic versioning applies to the client: a fix is a patch, a feature is a
minor, a change users must act on is a major.

## 2. Land the change through a pull request

Branch, push, open a PR against `develop` in `raffaelefarinaro/ciaobot`, and wait for the `mac-app` check. Never tag or release a
commit that CI has not built: the release artifact must be reproducible from a
commit that passed the suite.

## 3. Build and notarize

Build the release artifact from the merged commit, not from a dirty worktree:

```sh
git -c core.fsmonitor=false worktree add --detach "$SCRATCH" <commit>
cd "$SCRATCH/mac"
CIAOBOT_RELEASE_UPDATES=1 \
SIGNING_IDENTITY='Developer ID Application: …' \
NOTARY_KEY_ID='<App Store Connect key id>' \
NOTARY_ISSUER='<App Store Connect issuer id>' \
scripts/package-notarized.sh
```

The app embeds the engine, which `scripts/build-app.sh` builds from that exact
commit; it needs a clean checkout, Python 3.12 or 3.13 (`python3.12`,
`python3.13` or a `python3` of that version on `PATH`) and Node. 
`package-notarized.sh` signs the engine's Mach-O files first
(`scripts/engine_sign.py sign --distribution`: Developer ID, hardened runtime,
timestamp, no entitlements, never `--deep`; the SDK's own `claude` keeps
Anthropic's signature and is only verified), then Sparkle and the app. The
Apple-facing credentials stay in the login keychain (the identity) and in the
`0600` `.p8` outside the repository; they are passed only through the
environment variables above. `make-dmg.sh` and `make-appcast.sh` refuse an
engine that `engine_sign.py check --distribution` rejects. The Developer ID
signing and notarization of the engine-bearing app were rehearsed on a scratch
build (#3128 G); the operator-only steps in section 6 still apply.

The API key path defaults to `~/.appstoreconnect/private_keys/AuthKey_<KEY_ID>.p8`;
override it with `NOTARY_KEY`. A `notarytool` keychain profile works instead via
`NOTARY_PROFILE`.

`CIAOBOT_RELEASE_UPDATES=1` is what makes the client update-capable. Without it
the build is inert and its menu reports that updates are unavailable — that is
the correct state for local, debug and test builds. Never ship a build that
claims update support without the flag, and never set the flag on a build whose
`release/update-config.env` is wrong.

The script signs Sparkle's nested helpers inside out, signs the app, submits to
Apple, **requires** an `Accepted` result, staples the ticket, and verifies
Gatekeeper on the bundle (and on the extracted ZIP when `CIAOBOT_ALSO_ZIP=1`).
It then builds `Ciaobot-<version>.dmg` from that same stapled bundle with
`scripts/make-dmg.sh`, submits the image on its own, requires `Accepted`,
staples and validates it, checks Gatekeeper accepts the image, and prints its
SHA-256. Treat its failure as fatal; a rejected or unstapled archive or image
must not be published.

**One artifact ships: the DMG.** It is both the human download and Sparkle's
update enclosure. Its volume holds `Ciaobot.app` next to an `Applications`
symlink, so installing means dragging the app into Applications instead of
running it from the download folder. The volume opens as the styled install
window (fixed size, brand background with an arrow, large icons, no toolbar or
sidebar) laid out by `mac/scripts/dmg_layout.py`. A copy run from Downloads is translocated
by macOS, and Sparkle then refuses to update it. `scripts/make-dmg.sh` refuses
an unstapled app, an app another team signed, a bundle identifier other than
`local.ciaobot.mac`, an app inside an Applications folder, and an existing
output; it always detaches the image it mounts for inspection.

A ZIP is opt-in (`CIAOBOT_ALSO_ZIP=1`) for anyone who still needs one, and no
`.sha256` file is written: the digest is printed for the release notes.

If notarization cannot authenticate, stop and fix the credential: check the
`.p8` path and its `0600` mode, or recreate a keychain profile. Never fall back
to a password, an unsigned build, or a skipped notarization.

## 4. Verify the artifact by hand

A release has one artifact, `Ciaobot-<version>.dmg`. Compare it against the
digest printed at build time or the digest GitHub shows on the asset:

```sh
shasum -a 256 Ciaobot-<version>.dmg
hdiutil verify Ciaobot-<version>.dmg
xcrun stapler validate Ciaobot-<version>.dmg
spctl --assess --type open --context context:primary-signature --verbose=4 Ciaobot-<version>.dmg
mnt=$(mktemp -d "${TMPDIR:-/tmp}/ciaobot-dmg.XXXXXX")
hdiutil attach -readonly -nobrowse -mountpoint "$mnt" Ciaobot-<version>.dmg
ls -l "$mnt/Applications" "$mnt/Ciaobot.app"    # Applications -> /Applications
layout=$(mktemp "${TMPDIR:-/tmp}/ciaobot-layout.XXXXXX")
python3 -I scripts/dmg_layout.py dsstore --volume-name Ciaobot --app-name Ciaobot.app > "$layout"
cmp "$mnt/.DS_Store" "$layout" && echo layout-ok
rm -f "$layout"
codesign --verify --deep --strict --verbose=2 "$mnt/Ciaobot.app"
xcrun stapler validate "$mnt/Ciaobot.app"
plutil -p "$mnt/Ciaobot.app/Contents/Info.plist" | grep -E 'SU(FeedURL|PublicEDKey|AutomaticallyUpdate)|CFBundle(Short)?Version'
hdiutil detach "$mnt"
rmdir "$mnt"
```

The plist must carry the feed URL and public key from
`release/update-config.env`, and Gatekeeper must report `accepted` with
`source=Notarized Developer ID`. If a check fails, still run the `hdiutil
detach` line: never leave a volume mounted, and never publish an image whose
volume lacks the app or the `Applications` symlink. `layout-ok` proves the
`.DS_Store` survived signing, notarization and stapling unchanged; then open the
stapled DMG in Finder, check the window (fixed size, background art and arrow,
the two icons readable in Light and Dark mode), and eject it. The image is not a `.pkg`:
it installs nothing by itself, it only makes the drag to Applications the
obvious action.

## 5. Generate the appcast

```sh
scripts/make-appcast.sh \
  --archive Ciaobot-<version>.dmg \
  --download-url https://github.com/<owner>/<repo>/releases/download/v<version>/ \
  --output appcast.xml
```

`--download-url` is a **prefix ending in `/`** because Sparkle appends the
archive filename. The script mounts the DMG read-only inside its scratch
directory, verifies the app inside it, detaches it on every exit path, and then
hands the DMG itself to `generate_appcast`, so the EdDSA signature covers the
published bytes. It refuses an archive another team signed, without a
TeamIdentifier, without Developer ID Application authority, unnotarized,
wrong-version, misnamed (`Ciaobot-<version>.dmg` only), or non-arm64. It only
reads; it never publishes.

## 6. Publish, one asset, feed last

Order matters: an appcast must never point at an archive that is not yet
downloadable, and no client may be offered a 2.x build before its DMG exists.

1. **Merge the release PR into `main`** (admin only). `release-on-main` tags
   `v<version>` at that commit and creates its GitHub release with the CHANGELOG
   section as the notes. That release is the latest release from this moment,
   so the download page points at it until step 3 is done.
2. **Wait for `publish.yml` to go green** on that release. It builds the engine
   and the wheel, and it skips the `updates` feed release.
3. **Attach the DMG** to `v<version>`:
   `scripts/publish-release.sh --tag v<version> --asset Ciaobot-<version>.dmg
   --notes notes.md`. The DMG is what people download, install from, and what
   Sparkle downloads as the update enclosure. The script attaches exactly one
   file and refuses a second asset, a ZIP or a `.sha256`. When `v<version>`
   already exists, as it does after step 1, the script uploads the DMG onto it
   and creates nothing; its `--notes`, `--title`, `--draft` and `--prerelease`
   are not applied to an existing release. Put the printed digest in the
   release notes by hand with `gh release edit v<version> --notes-file
   notes.md`. The script creates the release, pre-release if asked, only when
   `v<version>` is absent.
4. **Publish `appcast.xml`** as an asset on the dedicated `updates` release in
   this repository, generated in section 5. The asset name must be exactly
   `appcast.xml` because the feed URL embeds it; `gh release upload` uses the
   local filename, so copy the generated file to that name first. Keep the
   `updates` release a pre-release so it never becomes "latest" and never
   replaces the download page. `gh release edit --prerelease=true` is the flag
   that actually sets it. The 2.x app reads this feed (`UPDATE_FEED_URL`).
5. **Append the same signed item** to the legacy `ciaobot-mac` feed, so 0.5.x
   clients are offered the 2.x app: download `updates/appcast.xml` from
   `raffaelefarinaro/ciaobot-mac`, add the one `<item>` that step 4's appcast
   holds for this version, and upload the combined file as `appcast.xml` on that
   repository's `updates` release. Keep every existing item. See
   [Bridging 0.5.x](#bridging-05x).
6. Confirm each feed downloads the appcast you just published, and diff it
   against the local file: `UPDATE_FEED_URL` from `release/update-config.env`,
   then `https://github.com/raffaelefarinaro/ciaobot-mac/releases/download/updates/appcast.xml`.

Releases 0.1.6 and older keep their ZIP (and checksum) assets until the feed
points at a DMG release. Remove those old assets only **after** the new appcast
is live and an installed client has moved to it; deleting them early breaks
clients that are still fetching the old enclosure.

A brand-new asset on a brand-new release can briefly 404 on the public
`/releases/download/<tag>/<asset>` path while the API already serves it. Re-upload
and retry before touching the feed URL: the URL in the shipped app must not
change to work around a propagation delay.

Replacing the appcast asset is the only intentional overwrite in this flow;
versioned archives are immutable. Never delete a published version: installed
clients may still be fetching it.

## 7. Prove the update chain

Unit tests cannot prove an update installs. On a disposable Mac or VM:

1. Install the previous release by opening its DMG and dragging the app to
   Applications, launch the installed copy, and **leave it open**.
2. Publish the new build as above.
3. Wait for a check (or use Check for Updates…) and confirm the running client
   is replaced and relaunched while in use, with the bundle identifier and
   version updated, and the separate Ciaobot engine still running untouched.
4. Confirm the new version's menu still reports updates as available.

Also try one negative case — a tampered DMG or a wrong-key signature — and
confirm the client refuses it and keeps running.

## Feed

`UPDATE_FEED_URL` in `release/update-config.env` is `https://github.com/raffaelefarinaro/ciaobot/releases/download/updates/appcast.xml`: the asset `appcast.xml` on the release tagged `updates` in this repository.

- The `updates` release is a **pre-release**. `releases/latest` never resolves to a pre-release, so the feed release cannot become the latest release, replace the download page, or be read as the engine manifest source.
- `publish.yml`'s `build-engine` job skips that release. Publishing the feed fires the same `release: published` event as a version release, so the job's `if:` also requires `github.event.release.tag_name != 'updates'`. `tests/test_ci_workflow.py::test_publish_workflow_skips_the_updates_feed_release` pins the guard.

### Bridging 0.5.x

Installed 0.5.x clients still poll `https://github.com/raffaelefarinaro/ciaobot-mac/releases/download/updates/appcast.xml`. Append the 2.0.0 item to that file once, as section 6 step 5 describes, and never again. The other items in the file stay. Its `CFBundleVersion` is 20000, above the 14 that 0.5.0 shipped, or Sparkle will not offer it. Append it only after the 2.0.0 DMG it points at is downloadable.

## Recovery

- **Bad appcast entry:** remove it and publish a corrected appcast pointing at a
  **higher** build. Sparkle will not downgrade.
- **Bad build already installed:** publish a fixed higher build; do not try to
  revoke the version.
- **Lost EdDSA key:** updates stop for installed clients until they install a
  key rotation by hand. This is why the key is backed up before the first
  updater release.
- **Failed notarization:** fix the cause and rebuild from the merged commit.
  Never staple or publish a rejected archive or image.

## Report

End with: version and build, commit SHA, Apple submission outcome, the DMG
SHA-256, feed URL, and exactly what was verified on a real machine versus what
remains unverified. Do not describe a release as shipped until CI, notarization
and the published download have all been checked.
