# Ciaobot one-line installer plan

## Decision

Ciaobot will use one supported macOS installation path:

```bash
curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh
```

The installer installs a per-user `Ciaobot.app`, configures the engine
LaunchAgent plus the app's `Ciaobot` tray LaunchAgent, and starts both. Each release contains the app, architecture-specific
embedded Python runtimes, the native archive verifier, the signed updater
archive, its signature, the installer, and `latest.json`.

There is no production Homebrew, PyPI, or DMG path. A DMG is intentionally not
built or attached. Apple Developer signing and notarization are not required;
the installer verifies the release archive with the embedded public key before
extracting it. The app remains ad-hoc signed, so Apple trust prompts and TCC
permission re-grants remain possible tradeoffs.

## Installer behavior

1. Validate macOS version, architecture, and required system commands.
2. Resolve a specified version or the latest GitHub release.
3. Download the archive, signature, and native verifier.
4. Verify the verifier checksum and then verify the signed archive. The native
   verifier accepts both raw minisign text and Tauri's base64-wrapped signature
   asset format.
5. Extract and validate the app bundle and bundled runtime.
6. Swap the app atomically, preserving the previous app for recovery.
7. Register `Ciaobot.plist` for the tray process and start it with launchd when
   the installer is allowed to start services. The app's tray menu can still
   disable this login item later.
8. If an existing configured workspace is referenced by the current
   LaunchAgent, reuse it and preserve its `.env` and password. Otherwise,
   leave the workspace unconfigured and start the app's bootstrap mode so the
   first-run onboarding asks whether to create or adopt a folder. The fresh
   path must never generate a hidden random dashboard password.

The script supports `--version`, `--app-dir`, `--no-start`, and `--dry-run`.
Users can inspect the downloaded script before running it instead of piping it
directly to `sh`.

## Updates and maintenance

- Normal updates use the Tauri updater, which replaces the app and embedded
  engine together and restarts the LaunchAgent.
- Re-running the installer is the recovery and explicit-version path.
- The browser update endpoints remain diagnostics and point to the app-owned
  updater/installer; they do not invoke a package manager.
- `ciao setup` only scaffolds the workspace and LaunchAgent. It never downloads
  an app release.
- The old Python archive downloader is removed; only safe app-bundle cleanup
  remains in the CLI for migration support.
- The old Homebrew formula/tap updater and PyPI end-user path are removed from
  release workflows and new-user documentation.

## Release and verification

- `publish.yml` builds the PWA, both embedded runtimes, the universal app,
  native verifier, installer, signature, and updater metadata.
- The embedded Python standalone release is pinned in CI and refreshed
  deliberately, with SHA-256 verification for both architectures.
- CI selects universal Xcode and the pinned Rust toolchain before building the
  cross-architecture runtime, because native Python dependencies may compile
  from source for x86_64 under Rosetta.
- `release-smoke.yml` installs with a restricted PATH, checks the app bundle,
  runtime, LaunchAgent, startup API, and reruns the installer as an update /
  recovery check.
- Rust tests must cover app-bundle engine resolution and native verifier builds.
- Shell syntax, installer asset contracts, focused Python tests, the full
  backend suite, and the PWA build are required before release.

## Explicitly out of scope

- Building or attaching a DMG.
- Apple Developer ID signing or notarization.
- Homebrew formula/cask maintenance.
- Publishing Ciaobot itself to PyPI.
