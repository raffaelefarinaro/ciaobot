# Ciaobot for Mac — agent guide

`docs/reference.md` is the product and behaviour reference; read the section for the area you change before editing it, and update it when behaviour changes. Keep `README.md` focused on the product summary and download link.

## Build and test

This directory is the Mac app inside the Ciaobot repository; the repository's root `AGENTS.md` applies too. Run every command below from `mac/`.

CI is the `mac-app` job in the root `.github/workflows/ci.yml` (macOS 15). It runs on develop PRs whose diff touches `mac/`, and on every release PR and manual run, and blocks on, in order: `swift test`, `swift build`, `scripts/build-app.sh`, `swift test --filter AppAssetsTests`, `plutil -lint .build/Ciaobot.app/Contents/Info.plist`. The app does not bundle or import the engine.

On the maintainer's Mac, `xcode-select` points at the Command Line Tools, which cannot link the package manifest. Prefix every SwiftPM command:

```bash
DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer swift test
```

Traps:
- `swift test` exits nonzero on any failure; never pipe it into `grep`/`tail` and then commit in the same `&&` chain — the pipeline's exit status is the filter's, so a red suite still commits. Check the `Executed … with 0 failures` line first.
- Under full-suite load `SparklePackagingTests` can fail with `fork: Resource temporarily unavailable` (local process limit). Re-run that suite alone with `--filter SparklePackagingTests`; green there means a flake, a real failure fails again.
- Never set `timeoutIntervalForResource` on a WebSocket session. URLSession leaves the TCP connection open when it times out a WebSocket task itself (cancel and invalidation do not release it), so a capped, reconnecting stream leaks connections until new ones fail with `ENOMEM`. Detect dead peers with `sendPing`.
- Tests never open a real menu or leave a status item in the menu bar; follow the fakes in `MenuBarTests` / `MenubarChatsTests`.
- In a test shell script, probing a descriptor with `{ true >&$n; }` makes `/bin/sh` itself hold fds 10 and 11 (its saved copies), so a leak check must allow those two. Plant the descriptor you expect to stay closed at a known number (`fcntl(fd, F_DUPFD, 40)`), as `NativeInstallerTests.testOnlyTheEventDescriptorIsInherited` does.

## Git

All pull requests target `develop` of `raffaelefarinaro/ciaobot`, like the rest of the repository. Releases use the `release-mac` skill (`skills/release-mac/` at the repository root); it carries a release freeze until Mac publishing moves into this repository.

## Agent learnings

- When the local gate fails to compile with "Sparkle not found" while a fresh worktree of the same commit builds, the checkout's stale `.build/out` cache is the cause; delete `.build/out` rather than chasing the code.
- The supplied brand PNGs carry a print resolution (`icon-512.png` at 300 dpi, the wordmarks at ~207 dpi), so `NSImage.size` is not their pixel size. Compare `NSBitmapImageRep.pixelsWide/pixelsHigh` in tests and keep `.resizable()` in views that size them by points.
