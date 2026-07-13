# Changelog

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
