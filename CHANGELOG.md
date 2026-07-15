# Changelog

## v0.4.28 - 2026-07-15

### Added
- feat(web): show clickable edited-file chips under each reasoning trace (`c966ef2`)
- feat(upgrade): self-restart when the installed package version changes (`be9037c`)

### Changed
- Merge pull request #131 from raffaelefarinaro/chore/sync-develop-v0.4.27 (`166e0c1`)

### Fixed
- fix(vault-lint): cut false positives (code spans, escaped, templates, dupes, .venv) (`d95cf2a`)
- fix(titles): make apfel opt-in, default Automatic to provider haiku (`e1a239d`)

### Maintenance
- chore: sync develop with main after v0.4.27 (`9201872`)
- docs(system-prompt): tell the agent to finish approved steps, not announce and stop (`6f71f1e`)

## v0.4.27 - 2026-07-15

### Added
- feat(web): pulse the active-work dot on a running loop (`a7c1eff`)

### Changed
- Merge pull request #128 from raffaelefarinaro/chore/sync-develop-v0.4.26 (`ead9055`)

### Fixed
- fix(pwa): service worker falls back to the app shell on navigation (`5c83cb3`)
- fix(web): remount the chat view when switching chats (`ecfcee7`)
- fix(vault-lint): cut false positives — code spans, escaped brackets, templates, same-stem duplicates, .venv (#129) (`68d8be5`)

## v0.4.26 - 2026-07-15

### Added
- feat(automations): user-facing routine descriptions + rework stock routine set (`8b15936`)

### Changed
- Merge pull request #125 from raffaelefarinaro/chore/sync-develop-v0.4.25 (`e021925`)

### Fixed
- fix(setup): refuse a workspace in a macOS TCC-protected folder (`669eeda`)
- fix(automations,chat,menubar): 0.4.26 review-pass batch (`74cdbc5`)

### Maintenance
- chore: sync develop with main after v0.4.25 (`158da88`)

## v0.4.25 - 2026-07-15

### Added
- feat(web): add 'Continue this chat' to the transcript file preview (`f19337d`)
- feat(automations): user-facing routine descriptions; new generic Workspace hygiene routine; macOS notifications reframed to lead with the menu bar; onboarding checklist close affordance; drop the operator-only self-improvement review from the shipped set (`518cf4f`)

### Changed
- Merge pull request #123 from raffaelefarinaro/chore/sync-develop-v0.4.24 (`328cbba`)
- web: align and vertically center PWA buttons (`557a1f5`)
- ui: move setup screen checkboxes under advanced and position advanced at the bottom (`f3c283f`)
- Route automations through workspace defaults (`613b8a7`)
- web: suppress duplicate background agent finish toasts when nudge synthesis is active (`7712ff5`)
- Merge branch 'fix/bootstrap-schedule-gate-and-skills-timeout' into develop (`d33a04b`)
- Merge branch 'fix/bootstrap-schedule-gate-and-skills-timeout' into develop (half-archived self-heal) (`f441a8d`)
- style: align height of add-project-btn with archive-btn in sidebar (`6ede4e2`)

### Fixed
- fix: preserve and restore locked package skills (`5c4b1b2`)
- fix: hold schedulers until setup completes (bootstrap mode) (`ba06de3`)
- fix(chats): self-heal chats stuck in a half-archived state (`346e3bd`)
- fix(menubar): never crash the tray on a missing icon asset (`1c41321`)
- fix(setup): refuse a workspace in a macOS TCC-protected folder (`817efe7`)

## v0.4.24 - 2026-07-14

### Added
- feat(voice): default TTS to a male voice to match the avatar (`a662e33`)

### Changed
- Merge pull request #107 from raffaelefarinaro/chore/sync-develop-v0.4.22 (`f28bfca`)
- Merge pull request #108 from raffaelefarinaro/fix/tray-single-window (`ebcf751`)
- Merge pull request #109 from raffaelefarinaro/feat/male-tts-voice (`92454da`)
- Merge pull request #110 from raffaelefarinaro/fix/tts-env-doc-comment (`8bf9f75`)
- release: prepare v0.4.23 (`bf9ef5a`)
- Merge pull request #111 from raffaelefarinaro/release/v0.4.23 (`d1bd16c`)
- Merge pull request #112 from raffaelefarinaro/chore/sync-develop-v0.4.23 (`e0a7036`)
- Merge pull request #113 from raffaelefarinaro/fix/frontmatter-clickable-url (`e01d037`)
- Merge pull request #114 from raffaelefarinaro/fix/chat-ws-auto-reconnect (`860b539`)
- Merge pull request #116 from raffaelefarinaro/fix/ws-origin-proxy-aware (`fc83dbc`)
- Merge pull request #117 from raffaelefarinaro/fix/open-pwa-or-plain-browser (`616ef97`)
- Merge pull request #118 from raffaelefarinaro/fix/related-wikilink-pills (`a210784`)
- Distinguish PWA and server launcher (`40747b8`)
- Merge pull request #120 from raffaelefarinaro/fix/chat-scroll-pin-margin (`f418194`)
- Merge pull request #119 from raffaelefarinaro/codex/distinguish-ciaobot-pwa-server (`4ff61c5`)
- Merge pull request #121 from raffaelefarinaro/codex/fix-chat-registry-recovery (`4028649`)

### Fixed
- fix(menubar): focus the installed PWA instead of spawning a new window (`229b49c`)
- fix(config): don't trip env-var doc check with a CIAO_TTS_* glob in a comment (`ca452d0`)
- fix(web): make bare-URL frontmatter values clickable in file viewers (`a3edafb`)
- fix(web): auto-reconnect the active chat's WebSocket on an unexpected drop (`f61d1f4`)
- fix(auth): proxy-aware WebSocket origin check (#115) (`9dc5a95`)
- fix(menubar): open the installed PWA or a plain browser tab, drop app-mode (`6ee2727`)
- fix(web): make RELATED/links frontmatter wikilink pills clickable (`7f87594`)
- fix(web): pin short chats to the bottom (kill the empty-gap scroll bug) (`e128616`)
- fix: prevent project chat registry loss (`aa50b73`)

## v0.4.23 - 2026-07-14

### Added
- feat(voice): default TTS to a male voice to match the avatar (`a662e33`)

### Changed
- Merge pull request #107 from raffaelefarinaro/chore/sync-develop-v0.4.22 (`f28bfca`)
- Merge pull request #108 from raffaelefarinaro/fix/tray-single-window (`ebcf751`)
- Merge pull request #109 from raffaelefarinaro/feat/male-tts-voice (`92454da`)
- Merge pull request #110 from raffaelefarinaro/fix/tts-env-doc-comment (`8bf9f75`)

### Fixed
- fix(menubar): focus the installed PWA instead of spawning a new window (`229b49c`)
- fix(config): don't trip env-var doc check with a CIAO_TTS_* glob in a comment (`ca452d0`)

## v0.4.22 - 2026-07-14

### Added
- feat(settings): add an Install button for apfel (`c5e1408`)

### Changed
- Merge pull request #104 from raffaelefarinaro/chore/sync-develop-v0.4.21 (`21d24fa`)
- perf(ollama): disable attribution header on local-daemon routes (#98) (`afd92fb`)
- Merge pull request #105 from raffaelefarinaro/feat/notify-archive-apfel-perf (`d195bff`)

### Fixed
- fix(notify): suppress the push when a chat auto-archives on completion (`1e1ccb7`)

## v0.4.21 - 2026-07-14

### Changed
- Merge pull request #97 from raffaelefarinaro/chore/sync-develop-v0.4.20 (`dda4a3c`)
- Animate the sidebar brand refresh with pixel scrambling instead of showing sync text. (`5037ddc`)
- Shrink message copy/read controls so chat bubbles use more width. (`ef0bbe0`)
- Merge branch 'develop' of https://github.com/raffaelefarinaro/ciaobot into develop (`134da85`)
- Clarify loop controls so Stop and Run now are not shown together. (`80e76fc`)
- Merge pull request #99 from raffaelefarinaro/fix-tray-open-app-mode (`d12d025`)
- Shorten Start at Login tray label (`5a50994`)
- Merge pull request #100 from raffaelefarinaro/fix-tray-open-app-mode (`d79879c`)
- Merge pull request #102 from raffaelefarinaro/codex/fix-chat-registry-stale-writes (`e73eea5`)

### Fixed
- fix(macos): "Open Ciaobot" from the tray now actually opens the app window (`65b89b9`)
- fix(web): restore chat scrolling with an inner messages wrapper. (`82e037b`)
- fix(notify): reliable local banners + Ciaobot icon, not Python (`03817ff`)
- fix(chats): stop new_session from resurrecting archived chats (`0be5194`)
- fix chat registry stale writes (`77101eb`)

## v0.4.20 - 2026-07-14

### Added
- feat(setup): auto-refresh Ciaobot.app on version upgrade (`9da52bd`)
- Add read-aloud action to user messages. (`fb692f8`)
- feat(macos): open the UI in the browser + web-push notifications (drop pywebview) (`f53dbc9`)

### Changed
- Use lowercase titles across the PWA and browser tab. (`7fd5c34`)
- Merge pull request #90 from raffaelefarinaro/chore/sync-develop-v0.4.19 (`fe34016`)
- Merge pull request #89 from raffaelefarinaro/guard-ciao-setup-workspace (`f91b9a5`)
- Merge remote-tracking branch 'origin/develop' into codex/integrate-pr-91 (`3b643a2`)
- Merge pull request #91 from raffaelefarinaro/fix-menubar-helper-venv (`d4524a0`)
- Merge remote-tracking branch 'origin/develop' into codex/integrate-pr-92 (`922f2e5`)
- Merge pull request #92 from raffaelefarinaro/polish-mac-icon-and-name (`a0c1979`)
- Merge remote-tracking branch 'origin/develop' into codex/integrate-pr-93 (`2d30667`)
- Merge pull request #93 from raffaelefarinaro/fix/chat-bubble-left-padding (`ba51cce`)
- Merge remote-tracking branch 'origin/develop' into codex/fix-pr-88 (`b4efbd9`)
- Merge pull request #88 from raffaelefarinaro/style/lowercase-app-titles (`293152d`)
- Center the chat composer and fix short-chat scroll in split view. (`59ffa2e`)
- Resolve Obsidian wikilinks in the vault file viewer. (`747d1af`)
- Merge remote-tracking branch 'origin/develop' into goal-pwa-web-notifications (`8f6f495`)
- Merge pull request #95 from raffaelefarinaro/goal-pwa-web-notifications (`998331a`)
- Use defuddle for YouTube transcripts in web-research. (`2de1f1f`)

### Fixed
- fix(setup): guard ciao setup against hijacking the workspace (`490b320`)
- fix(macos): menu-bar helper must resolve the venv python (tray wouldn't open) (`aa158be`)
- fix(macos): margined app icon + present the window as "Ciaobot" (`3fc2b43`)
- fix(web): add left padding to chat message bubbles (`7ff005c`)
- fix(web): rebalance chat bubble horizontal padding (`5eff8a6`)
- fix(web): align header icon hover highlights with sidebar (`40d26b4`)
- fix(web): align FileViewerModal toolbar icons with app SVG set (`1d4ac16`)
- fix: address review of #95 (macOS push enablement, device-local gate, PWA deletion, delivery-aware fallback) (`22b7a5a`)

### Maintenance
- docs: document /api/vault-markdown-paths in PWA_API.md (`4ad4006`)

## v0.4.19 - 2026-07-14

### Changed
- Merge pull request #85 from raffaelefarinaro/chore/sync-develop-v0.4.18 (`4a937db`)
- harden: guarantee the app opens + stop releases from stale local develop (`896d513`)
- Merge pull request #86 from raffaelefarinaro/harden-window-launch-and-release (`3392435`)
- release: prepare v0.4.19 (`28b0b44`)

## v0.4.18 - 2026-07-14

### Changed
- Merge pull request #82 from raffaelefarinaro/chore/sync-develop-v0.4.17 (`3153b2c`)
- Merge pull request #83 from raffaelefarinaro/fix-window-venv-launch (`d6b7761`)

### Fixed
- fix(macos): open the window via the venv python, not the app-bundle symlink (`8358a5d`)

## v0.4.17 - 2026-07-14

### Changed
- Merge pull request #78 from raffaelefarinaro/chore/sync-develop-v0.4.16 (`edaeb8a`)
- Merge pull request #80 from raffaelefarinaro/fix-macos-window-identity-and-upgrade (`b6b4e49`)

### Fixed
- fix(macos): native window Dock identity + self-heal server after brew upgrade (`4c7c7c1`)
- fix(macos): persist WebKit localStorage so the welcome tour shows once (`c56d142`)

### Maintenance
- ci: fix release automation so publish and develop-sync actually run (`de45709`)

## v0.4.16 - 2026-07-13

### Changed
- Surface chats blocked on AskUserQuestion in the sidebar, bell, and tray. (`1bfc286`)
- Adopt develop/main release workflow with automated publishing. (`a8284ea`)
- Auto tier-fallback on capability errors (Claude, Ollama, OpenRouter) (`7c937a3`)
- Merge onboarding into one Settings card and hide it when complete. (`611a130`)
- Embed PWA in native WebKit window and unify macOS tray notifications. (`11a0be9`)
- Merge pull request #73 from raffaelefarinaro/worktree-fix-ci-menubar-env (`b08ae6c`)
- Merge branch 'develop' of https://github.com/raffaelefarinaro/ciaobot into develop (`d918a68`)
- Merge remote-tracking branch 'origin/develop' into worktree-fix-dup-window (`b9cb9a8`)
- Merge pull request #75 from raffaelefarinaro/worktree-fix-dup-window (`a0e6063`)

### Fixed
- Fix getting-started flash on PWA cold boot. (`ebe494f`)
- fix(web): unify header icon button overlays (brain, archive, bell) (`1dda251`)
- fix(web): keep chat pinned to bottom after send and streaming (`a2fc8e5`)
- fix(web): highlight only the exact active model, not same-tier models from other providers (`6d5afd8`)
- fix(menubar): make the native window single-instance; drop dead PWA lookup (`21f8b59`)
- fix(macos): give the app icon the orange PWA background (`4bd5448`)
- fix: surface mid-turn interruptions and stop menu-bar update crashes (`df1289f`)
- fix(web): stop reply-shaped chat titles from contentless prompts (`fbd300b`)

### Maintenance
- docs: document CIAO_MENUBAR_EXECUTABLE plist placeholder (`f91370e`)

## v0.4.15 - 2026-07-13

### Changed
- Self-heal local voice extras wiped by app upgrades (`c453608`)
- Remove prompt expand/collapse on automation detail pages (`187a31b`)
- Replace README screenshots with a grouped feature list (`765ac1f`)
- Merge Ciaobot variables into an existing workspace .env (`af3cfd4`)
- Keep menu bar animations running while the tray menu is open (`d0afe28`)
- Triage runtime errors into a fix-it chat at startup; cap service logs (`e0863d4`)
- Capitalize the sidebar wordmark to Ciaobot (`e71ac2a`)
- Reject reply-shaped title outputs and harden the title prompt (`5697a40`)
- Harden background plumbing: oneshot models, backup retries, sync logs (`6173759`)
- Report the real title engine, warn on missing apfel, allow Codex titles (`163a97b`)
- Back off events WebSocket reconnects that never complete a handshake (`3008cf6`)

### Maintenance
- Test fast-mode suffix stripping in run_oneshot (`4f1d80a`)

## v0.4.14 - 2026-07-13

### Added
- Add Scrapling as an optional web-scraping fallback for web-research (`d6e72f6`)
- Add in-chat loops next to schedules; Schedules page becomes Automations (`6ddd069`)
- Add per-tier Codex model pins with automatic fallback (`7423f58`)
- Add interactive onboarding: tour deep links and getting-started checklist (`49a0261`)

### Changed
- Instruct agents to maintain project canonical docs during chats and nightly curation. (`a81571d`)
- Refresh stock skills: curate gws helpers and fix stale Ciao skill content. (`7c7f368`)
- Simplify stock subagents and refresh them on sync-skills. (`32af9ad`)
- Simplify stock slash commands and seed them into canonical commands/. (`355203d`)
- Simplify shipped system schedules and align them with prompt conventions. (`6641fa6`)
- Surface bounded memory and proposals in Settings → Context. (`a61ca42`)
- Reflect PWA notifications in menu bar (`3a80435`)
- Snapshot in-progress Codex CLI provider work (`3e2a7a6`)
- Map OpenAI models onto the haiku/sonnet/opus families (`84fb9d9`)
- Document stock subagents, commands, and routines in README; slim it down (`b562459`)
- Rework welcome mascot greeting: hover previews, click pins (`2b40896`)
- Stagger system schedule times, drop emoji sentinels, mark disabled schedules (`f334807`)
- Scroll chat to bottom when sending a message (`908bb92`)
- Ignore workspace runtime dirs seeded into the repo by sync-skills (`ac1e0d7`)
- Validate codex thinking levels against the model catalog on PATCH (`7da3572`)
- Document loops in skill triggers, capability catalog, and README (`d93a66a`)
- Rename ciao-schedules skill to ciao-automations (`91fe4f9`)
- Show loop banner in loop-driven chats; split sidebar automation groups (`e262cd5`)
- Run missed schedules once on startup (`6cd4c2a`)
- Simplify automation sidebar group labels (`dee1ac3`)
- Improve PWA and tray UX (`04d08fd`)
- Document Ciaobot design system (`5c266e2`)
- Align sidebar activity indicators (`699294c`)
- Simplify chat message placeholder (`a77379f`)
- Increase assistant message line height (`4ed9667`)
- Complete Fable model support and responsive UI fixes (`49f52ea`)
- Drain active chats before restart (`1433b17`)
- Clarify GitHub issue reporting workflow (`3d3d340`)
- Run dev backend through restart supervisor (`5c731b4`)
- Harden dependency review validation (`b81c65c`)
- Update runtime dependency pins (`427549c`)
- Improve provider-aware context settings (`77180dd`)
- Link workspace AGENTS.md to CLAUDE.md and check it in health (`c67a833`)
- Nudge agents to seed bounded memory when empty (`1583a98`)
- Target bounded memory explicitly in the curation schedule (`4f06ade`)
- Auto-promote user corrections into bounded memory at archive time (`53b5978`)
- Update project canonical docs from insights at archive time (`9f012e1`)
- Explain provider-CLI design rationale in README (#69) (`9a80f73`)
- Show one source link for the linked CLAUDE.md/AGENTS.md guides (`1ef7998`)
- Remove the OpenAI model catalog from routing settings (`9cd1abe`)

### Fixed
- fix(tray): open the selected chat when the PWA is already running (`b35dc06`)
- fix(web): widen chat bubbles so messages use more horizontal space. (`2654834`)
- Fix .env.example export drop, dead doc-updater ref, and hardcoded session secret (`4286f7c`)
- fix(web): unbreak Vite dev proxy writes and WebSockets with auth off (`4273c1a`)
- Fix external vault file uploads (`86a9b6c`)
- Fix model picker metadata and polish PWA controls (`2d0e301`)
- Fix Codex commentary rendering (`9cfa186`)
- Fix workspace-scoped context display (`7cbf2b4`)
- Fix auto-title fallback tests to mock the provider one-shot seam (`81fb398`)

### Maintenance
- docs: restructure README for clearer reader narrative. (`8ce1785`)
- Test shared instruction imports across providers (`a62d4b4`)
- build(web): refresh bundled PWA assets (`be22f5f`)

## v0.4.13 - 2026-07-09

### Added
- feat(tray): spin the icon while a self-update is in progress (`1443731`)
- feat(tray): animate a pixel pulsing dot beside working chats (`0512094`)
- feat(tray): move rarely-touched items into an Advanced submenu (`c44d2ee`)
- feat(web): improve completed-projects modal and fix archive button sizing (`a67b4b8`)
- feat(web): linkify chat file paths and open files in the OS default app (`045b234`)

### Fixed
- fix(web): make the Fix/Close buttons on error toasts actually respond (`fd15515`)
- fix(web): top-align the voice-engine install banner with its text (`812d89a`)
- fix(routes_api): tolerate more than "completed" in a subagent's self-reported sign-off (`892a1a1`)
- fix(web): anchor the standalone subagent panel to its own completion notice (`f34bb26`)
- fix(project_chats): don't pass the "apfel" routing sentinel as a literal model id (`37c9cfa`)

### Maintenance
- build(web): refresh bundled PWA assets (`64f6f64`)

## v0.4.12 - 2026-07-09

### Added
- feat(tray): add a Start Ciao at Login toggle (`1b19698`)
- feat(system-prompt): teach the agent to check runtime logs before filing issues (`938031f`)
- feat(web): clarify first-launch terminal instructions in the setup wizard (`0ae3951`)
- feat(web): make the empty-state mascot greet you in a random language (`85c8e3c`)
- feat(web): render background subagents in a standalone panel (`4a2b8bc`)
- feat(web): add a Retry button for mid-turn API errors (`efcc6bd`)
- feat(schedules): track the attention classifier as a job run (`b1e75dd`)
- feat(tray): show the Ciaobot face on the update-complete notification (`da4c6c4`)

### Changed
- refactor(web): render frontmatter prose and lists inline instead of a collapsible "more" section (`28df9f6`)
- refactor(web): add a shared .touch-hit utility for compact icon buttons (`dde711a`)

### Fixed
- fix(package-version): refresh the Homebrew tap before upgrading (`dc15f11`)
- fix(main): stop repeat-logging identical branch-backup failures (`e37fb4c`)
- fix(routes_api): return 409 instead of raising when a schedule's instance is paused (`f8251b1`)
- fix(web): load subagents even when a chat's final message is already a resolved turn (`0932dc6`)
- fix(web): shrink split-pane minimums so pinned-file view fits narrower windows (`4a1d10c`)

### Maintenance
- docs(readme): rewrite around install-first workflow and workspace-first model (`f6d2b0d`)
- build(web): refresh bundled PWA assets for release/v0.4.12 (`7099cda`)

## v0.4.11 - 2026-07-08

### Added
- feat(tray): open links in an installed PWA when available (`3ce183a`)

### Fixed
- fix(menubar): treat a no-op Homebrew/pip upgrade as a failed update (#58)

### Maintenance
- docs(readme): explain the etymology behind the "Ciaobot" name (`86f016b`)
- build(web): refresh bundled PWA assets for release/v0.4.11 (`9cd2b37`)

## v0.4.10 - 2026-07-08

### Added
- feat(settings): one-click Fix for workspace-health issues (`12a27af`)
- feat(wizard): drop the scratch/existing choice — autodetect the folder (`8fff3fd`)
- feat(wizard): name the first workspace instead of auto personal+work (`4fcb2b3`)

### Changed
- Merge pull request #48 from raffaelefarinaro/fix/brew-install-command (`61d0357`)
- Simplify the macOS menu bar by removing dead notification controls. (`b522174`)

### Fixed
- fix(setup): route fresh installs to the first-run wizard again (`7d3d7cf`)
- fix(menubar): self-heal when Homebrew swaps the install out from under a running process (#52)
- fix(push): drop dead placeholder default in PushManager (#51)

### Maintenance
- docs: use the full brew install path raffaelefarinaro/ciaobot/ciaobot (`adc6c76`)
- chore: gitignore .claude/ session tooling

## v0.4.9 - 2026-07-08

### Fixed
- fix(setup): pin LaunchAgents to the opt interpreter, not the Cellar keg (`16b97e6`)

## v0.4.8 - 2026-07-08

### Added
- feat(setup): auto-open wizard, launchd handoff, single-folder vault adoption (`82e56c0`)
- feat(wizard): single folder — drop the separate vault/notes path input (`ad16878`)
- feat(chat): highlight selection while drafting a comment (`1e3b52a`)
- feat(viewer): remove workspace sandbox from file viewer/editor (`c5aed6c`)
- feat(menubar): animated spinner icon while the assistant is working (`c3a89f7`)
- feat(models): route title and insights calls per workspace bucket (`d58f8ec`)
- feat(web): auth off by default, active-chats endpoint, restart overlay (`4a9a6bb`)

### Changed
- homebrew,docs: slim caveats, real 0.4.7 sha, README install cleanup (`4ef2d27`)
- polish(subagents): harden synthesis nudge, reconcile drain docstrings (`cbb58c2`)

### Fixed
- fix(subagents): synthesize parent report when background agents finish (`ba6b923`)
- fix(tour): keep cards on-screen, fix spotlight, and refresh step content (`173190a`)
- fix(chat): equalize composer button sizes on narrow layouts (`c7a02b9`)
- fix(comments): keep selection Comment pill anchored while scrolling (`864034a`)

### Maintenance
- docs(prompt): drop the per-device branch steering bullet (`40e8131`)

## v0.4.7 - 2026-07-08

### Fixed
- fix(homebrew): install the wheel with its full dependency tree so `brew install` pulls claude-agent-sdk, starlette, uvicorn, and the rest of the pinned deps from PyPI
- fix(homebrew): drop the broken `require "language/python/virtualenv"` and symlink console scripts into the Homebrew prefix `bin`
- fix(deps): declare `python-dotenv` in package dependencies (used by `ciao setup` and config loading)
- fix(setup): register `Ciaobot.app` with LaunchServices before loading LaunchAgents so macOS shows "Ciaobot" instead of "python" in background-activity prompts
- fix(homebrew): install dependency wheels in `post_install`, after Homebrew's install-linkage step, so prebuilt dylibs (e.g. jiter) no longer abort the install with "Failed to fix install linkage" (`c2d8fac`)
- fix(setup): hand the wizard-chosen workspace and port to the relaunched server, and re-exec instead of `os._exit` when restart cleanup wedges, so a foreground `ciao run` comes back configured after the setup wizard instead of dying or re-entering bootstrap (`96b5154`)

### Changed
- homebrew: replace the sandboxed (never-working) post-install auto-setup with a visible banner and caveats pointing at `ciao run` + the browser setup wizard (`d7c4a86`, `c2d8fac`)
- docs: shorten the Homebrew install command to `raffaelefarinaro/ciaobot` and show `ciao run` as the step after `brew install` (`b43e9e1`, `f3d2f5d`)

## v0.4.6 - 2026-07-08

### Added
- feat(skills): add GWS workflow, persona, and recipe skill library (`1eef488`)
- feat(deps): surface available dependency updates from PyPI and npm (`c4e6062`)
- feat(release): update the Homebrew tap formula on publish (`1d1b7b9`)
- feat(chat): live token count and elapsed time in the Working trace (`4e09d5b`)
- feat(web): first-run product tour overlay (`f257f41`)
- feat(voice): read messages aloud with cloud (OpenAI) or local (Kokoro) TTS (`44e26b7`)
- feat(settings): add open-source card linking to the GitHub repo (`6b12104`)
- feat(web): product tour missing-state hints and toast UX polish (`245ad2d`)

### Changed
- Anchor the SubagentPanel before the completion notice, not after the report. (`18bb2ef`)
- Merge pull request #42 from raffaelefarinaro/subagent-panel-placement (`fe99de0`)
- Emit localhost chat links so live updates work. (`563f3e1`)
- Nest the live SubagentPanel inside the Working trace while streaming. (`4cfce88`)
- Nest the live SubagentPanel inside the Working trace while streaming. (`9e4c3b6`)
- Merge pull request #43 from raffaelefarinaro/subagent-live-nesting (`de8d699`)
- Merge remote-tracking branch 'origin/main' into subagent-panel-live-nesting (`fd8bb91`)
- release: regenerate gws-* stock skills from the gws CLI (`1fa2fef`)
- polish(settings): tidy routine-context layout, drop redundant hint (`455ac28`)
- polish(settings): explain how to change the main workspace path (`18359e4`)
- refactor(prompt): move Ciaobot system instructions into system_prompt.md (`828ea3d`)
- polish(settings): label local title engine, fix select chevron spacing (`6b200b3`)

### Fixed
- fix(dag,skillevo): record non-OK node status + write stubs for under-cap no-proposal skills (`bc01710`)
- fix(schedules): wait for background subagents before auto-archive + robust classifier routing (`ec86289`)
- fix(settings): keep instruction expand chevron off the left edge (`c394a42`)

### Maintenance
- docs(readme): position Ciaobot for knowledge work and credit upstream tools (`d02bff7`)
- chore(models): default OpenRouter tiers to anthropic -latest aliases (`2bb4b38`)
- chore(models): move remaining OpenRouter defaults to -latest aliases (`c01b3cd`)
- docs(readme): illustrate chat annotations and pinned files with screenshots (`863990b`)
- test(models): align model-bucket expectation with -latest alias defaults (`72094d7`)
- chore(web): refresh bundled index.html from the latest PWA build (`e5eb2cf`)
- docs(readme): condense the install/setup-wizard walkthrough (`3b42be5`)

## v0.4.5 - 2026-07-08

### Added
- feat: menubar update/notifications and shared pane title styling (`4dd52bc`)
- feat: menubar unread dots on chats and icon badge count (`1e06395`)

### Changed
- Remove dispatch_schedules gate and improve schedule run-now UX. (`15f18e5`)
- Show workspace tags on menubar open-chat entries. (`f0b3c9e`)
- Ship gws skills as stock and document Google Workspace setup in the PWA. (`c3832e9`)
- Make background subagent work visible in the PWA. (`0017f94`)
- Merge pull request #40 from raffaelefarinaro/remove-dispatch-schedules-flag (`29e0a61`)

### Fixed
- Fix schedule running state and finish dispatch_schedules cleanup. (`d3f790b`)

## v0.4.4 - 2026-07-07

### Added
- feat: add `ciao setup-url` and print login URL + PATH hint after setup (`500ab24`)
- feat: menu bar Notifications submenu lists unread chats, matching the PWA bell (`de20c4b`)
- feat: tidy routine model settings for tier-less providers (`b87c602`)

### Changed
- refactor: drop Telegram-specific labels from archived transcripts (`a7bbe06`)

## v0.4.3 - 2026-07-07

### Added
- feat: detect gws via login-shell PATH; add Install gws button (#36) (`d4ed632`)
- feat: collapse live thinking trace by default in chat (`ebefeb1`)

### Changed
- Merge branch 'main' of https://github.com/raffaelefarinaro/ciaobot (`6dea3c9`)

### Fixed
- fix: setup_workspace honors existing .env vault root on re-run (#29) (`c08bba9`)
- fix: evaluate provider-alias tier placeholder instead of showing raw template (#30) (`0005864`)
- fix: move menu bar template icons out of the PWA build output dir (#31) (`62f024c`)
- fix: default skills auto-update off and theme to system (#32) (`6ffca13`)
- fix: reliable chat updates during subagent/background work (#34) (`7e7b16a`)
- fix: return 409 instead of 500 when running a schedule while paused (#35) (`dd6f357`)
- fix: update check no longer fails on GitHub rate limits (use public redirect) (#33) (`666239e`)
- fix: tie server lifecycle to menu bar and stop baking one-time token into app launcher (#37) (`cd8acd4`)

### Maintenance
- chore(deps): bump claude-agent-sdk to 0.2.111 (`e0eb1e1`)

## v0.4.2 - 2026-07-07

### Fixed
- fix: boot screen shows real version; drop startup PWA rebuild phase (#27) (`e7d68f3`)

## v0.4.1 - 2026-07-07

### Added
- feat: LaunchAgents identify as Ciaobot; app launcher starts the server (#25) (`ba2ccbb`)

## v0.4.0 - 2026-07-07

### Added
- feat: workspace-first one-folder setup; localhost DX; self-relaunching run (#23) (`4bfe201`)

## v0.3.0 - 2026-07-07

### Added
- feat: wizard polish, macOS menubar by default, PyPI 'ciaobot' distribution (#20) (`183b8db`)

## v0.2.3 - 2026-07-07

### Added
- feat: folder picker + one-folder setup wizard (#18) (`8e8de47`)

### Maintenance
- docs: bump README install URL to v0.2.2 (`81100ed`)

## v0.2.2 - 2026-07-07

### Added
- feat: git-init picked workspace folders; self-update from GitHub releases (#16) (`8c21905`)

### Fixed
- fix: never create/switch workspace branches; smooth fresh-install start (#14) (`ad6451a`)

### Maintenance
- chore: set Homebrew formula sha256 for v0.2.1 tarball; bump README install URL (`6c98ad6`)
- chore: remove Homebrew distribution support (pip-only for now) (#13) (`5800358`)

## v0.2.1 - 2026-07-07

### Added
- feat: macOS menu bar companion showing Ciaobot server status (`7f7973b`)
- feat: menu bar notifications, open chats, reachable addresses; app bundle icon (`f7753a3`)
- feat: install Ciaobot.app to /Applications; open chats inline in menu bar (`ce5fbf1`)
- feat: mute-banners toggle in menu bar; drop Open Chats header (`0ca7323`)
- feat: backfill WebSearch on OpenRouter-routed chats via web plugin (#8) (`f20dabe`)
- feat: monochrome template icon for the macOS menu bar (#9) (`f1ed5ea`)
- feat: ciao-capabilities stock skill + onboarding capabilities tour (#10) (`ec62913`)

### Fixed
- fix: install Homebrew formula from the release sdist, not the source tarball (`c4ff29e`)

### Maintenance
- chore: set Homebrew formula sha256 for v0.2.0 tarball (`b50e045`)
- docs: add release install instructions to README; note PWA build for source installs (`8b90d0b`)

## v0.2.0 - 2026-07-06

First public release of Ciaobot: a local-first web app that turns Claude Code into a personal assistant and second brain, with chats, projects, files, schedules, and memory in one interface.

### Added
- Claude Code-backed chats in a PWA with workspace and project navigation, file preview/edit/restore, and per-project context injection.
- Multiple providers: Claude Code (subscription or Anthropic API key), Ollama (local daemon or Ollama Cloud), and OpenRouter — with explicit per-chat model routing, a oneshot provider, and a model selector in the UI.
- Markdown vault memory: chats archive into a plain-markdown vault, session insights are extracted, and memory proposals are drafted for review — never promoted automatically.
- Scheduled routines for projects and workspaces, with delivery modes, a weekly-review template, and `{{ERROR_LOG}}` / `{{ISSUE_REPORT}}` prompt placeholders.
- Stock skills shipped with the package: ciao-schedules, create-chat, vault-read, web-research, workspace-authoring.
- Voice transcription (including local on-device MLX Whisper on Apple Silicon), push notifications, and in-app package updates.
- Setup automation: idempotent `ciao setup` (initial `.env`, workspace docs, vault seed; macOS LaunchAgent and a `Ciaobot.app` shortcut), plus a Homebrew formula.
- Debug report generation and an issues route for agent-driven troubleshooting.
- Release and safety tooling: `ciao-public-preflight` (private-data scan for public exports), `ciao-package-smoke`, and `ciao-prepare-release` (version bump, changelog, checks, draft PR).
