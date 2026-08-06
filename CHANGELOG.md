# Changelog

## v0.7.2 - 2026-08-06

### Removed
- **`ciao menubar` is gone.** `Ciaobot.app` is the menu bar now, so the old rumps status-bar helper and its `com.ciao.menubar` LaunchAgent are retired: `ciao setup` unloads and deletes that plist on upgrade, because leaving it registered means launchd retrying an executable that no longer ships. Nothing to do by hand (`90afd6f`)
- **The `voice-local` and `tts-local` extras are gone**, along with the `POST /api/voice/install-local`, `POST /api/tts/install-local`, and `POST /api/apfel/install` routes. On-device voice needs no install step any more, so there is nothing for them to install. An existing `pip install 'ciaobot[voice-local]'` becomes a plain install (`90afd6f`)
- Four dependencies dropped: `openai`, `rumps`, `mlx-whisper`, and `kokoro-onnx`. `OPENAI_API_KEY` is no longer read for voice — using Codex as a provider is unaffected (`90afd6f`)

### Added
- **`ciao desktop install`** downloads and installs `Ciaobot.app` without a Gatekeeper prompt. macOS only assesses bundles carrying a download quarantine flag, which browsers and Homebrew casks set but a command-line download does not — so the ad-hoc signed app launches directly, with no "Apple could not verify" dance. Because Apple's notary check is therefore not what guards the download, the installer verifies the release's minisign signature against the same key the in-app updater uses and refuses to install anything that fails. `ciao desktop uninstall` removes it. The first-run wizard now runs the install for you (`--no-desktop-app` on `ciao setup` opts out), and a failed download degrades to the menu-bar launcher rather than failing setup (`90afd6f`)
- **Chat titles are generated on-device** through the bundled `ciaobot-native` sidecar instead of shelling out to the `apfel` Homebrew CLI, with a cloud model as fallback. Existing `apple`/`apfel` title settings keep working (`90afd6f`)
- **Dictation in the comment composer** — annotate a file or a message by voice, including while the agent is still working, in which case the comment rides along on your next message (`9773344`)
- **Keyboard shortcuts now work in the browser**, not just the desktop app, on whichever modifier is actually free: new chat, dictation, and archive are `Cmd+T`/`Cmd+D`/`Cmd+A` in the app and `Option+N`/`Option+D`/`Option+A` in the PWA, where the browser has already spent the Cmd versions on new-tab, bookmark, and select-all. Settings → Shortcuts is shown to everyone with the labels for how you are running it (`a3c2a44`)
- `scripts/check-desktop.sh` runs the same desktop checks CI does — Swift sidecar build, `cargo fmt --check`, `cargo clippy -D warnings`, `cargo test`, a universal `tauri build` — and asserts the sidecar ends up bundled, universal, signed, and runnable inside the built app. Previously nothing compiled the Rust shell locally, so those failures only appeared in CI (`90afd6f`)

### Changed
- **Voice is one on-device engine each, and free**: Apple's dictation models for hearing and `AVSpeechSynthesizer` for speaking, both through the `ciaobot-native` sidecar bundled in `Ciaobot.app`. The cloud pair went with them — keeping it meant an API key, two engine pickers, per-minute billing, and a second code path to duplicate what the OS already does. The cost is reach and it is real: voice now needs a **macOS 26+ host with the desktop app installed**, and Settings says so plainly instead of failing when you press record. The constraint is on the *host* only, so a phone or iPad talking to a Mac host still gets voice (`90afd6f`)
- **The documented install is now `brew install …/ciaobot` then `ciao run`**, with the wizard installing the app. The `ciaobot-desktop` cask remains as a fallback and still hits the Gatekeeper block, so its caveats still describe the Open Anyway steps (`90afd6f`)
- **Password protection is on by default and can no longer be switched off from Settings.** The first-run wizard asks for the dashboard password (confirmed, minimum 4 characters); `POST /api/auth/settings` sets or changes it but rejects `auth_required: false` rather than ignoring it. The only way out is `PWA_AUTH_REQUIRED=false` in the workspace `.env` — an explicit operator decision made on the machine itself (`2cb10f4`)
- Settings → Automations is now readable: a headline says whether anything is broken and names it, failing automations sort first, and every row states what it does and when it runs instead of carrying capability chips and a success rate. Rows are actionable — a failing Session insights can be re-run over every archive that is missing them, with a one-off model override for when the configured model is the problem (`POST /api/automation/backfill-insights` accepts `{"model": …}`), and the separate "Insights backfill" row is gone: it is that action. Settled one-time migrations fold away behind a disclosure (`cd9d266`)
- Settings → Context no longer describes the workspace guide as one instruction file per CLI. There is a single `CLAUDE.md`; `AGENTS.md` is a symlink to it (`cd9d266`)
- Settings → Skills links to Providers for the skills, plugins, and MCP servers each CLI brings on its own (`cd9d266`)
- The restart notice is a movable card instead of a full-screen takeover, so it no longer blocks the app while a restart finishes (`78b83bd`)

### Fixed
- Settings → Automations stops listing automations that cannot run here. `job_runs_latest.json` never expires an entry, so a job removed from the code (the startup PWA rebuild, dropped in v0.6.x) kept a stale green row forever; retired ids are now filtered out. Weekly Dependency review is hidden unless its `system-dependency-review` schedule is actually installed — it is no longer a stock schedule, so nothing triggered it on a fresh install (`cd9d266`)
- First-run setup no longer re-registered the retired `com.ciao.menubar` LaunchAgent immediately after deleting it. Normally the plist was already gone and the block did nothing, but when the unlink failed it left launchd retrying a missing executable forever — the exact state the cleanup exists to prevent (`75b6555`)
- The restart notice bounds the message it renders, so a long server error can no longer stretch the card off-screen (`e85ceee`)
- Toggling launch-at-login no longer mislabels which machine it applies to when viewing a host's Settings from a client (`3d30473`)

### Maintenance
- `claude-agent-sdk` 0.2.128 → 0.2.131
- LICENSE carries an explicit copyright line and the full Apache appendix (`aa4c919`)
- `uv.lock` synced (`e823ec9`)
- Corrected two stale in-code docs this release falsified: the capabilities skill still advertised cloud OpenAI transcription and `mlx-whisper` with a `CIAO_TRANSCRIPTION_MODEL` override, and `ChatLayout` still called the modifier shortcuts desktop-only (`75b6555`)

## v0.7.1 - 2026-08-04

### Added
- feat(voice): switch cloud transcription to gpt-transcribe and surface model in Settings (`2e2e4b6`)
- Native macOS permission checks for microphone, notifications, and camera, so dictation can tell "not yet asked" from "denied" instead of failing opaquely (`ad37753`)
- Chat header chip naming the routing provider, model, and thinking level at a glance — the full pill on desktop, the brain button alone on narrow screens (`b7d1656`, `f61ee89`, `ca37d4e`)

### Changed
- Merge pull request #245 from raffaelefarinaro/chore/sync-develop-v0.7.0 (`95298a0`)
- Surface delegate parent/child links and hide nested subchats from home. (`7976647`)
- Reclaim Codex threads on archive, delete, and new session. (`01f08e6`)
- Merge branch 'feat/ollama-haiku-0731-default' into develop (`290381d`)
- Skip result-ready toasts and pushes for delegate chats. (`166e3a1`)
- refactor(memory): collapse bounded memory into CLAUDE.md regions (`6952d67`)
- Enable Claude SDK ``exclude_dynamic_sections`` so cwd/git/OS leave the system-prompt cache prefix (`6efb45c`)
- Give the bounded-memory region grammar and guide diagnostics one owner in `memory_tool`, and drop dead parameters left by the region migration — including the unused `target` argument on the `memory_proposal_resolve` MCP tool and the `user_entries`/`expired_user_entries` aliases in the memory audit payload (`62a4539`)
- Ollama's haiku tier moves to `deepseek-v4-flash:0731-cloud`, as does the insights fallback used when a caller has no workspace context (the backfill script). Session insights on Automatic are unaffected: they follow the chat's workspace sonnet tier, which for Ollama is `kimi-k2.7-code:cloud` (`9dfbae9`)
- Remove the 100%/120% font-scale preset buttons; the font baseline is set once and no longer toggled from the UI (`3e68489`)
- Settings → Automation no longer names a single model for an Automatic routine. Chat titles and Session insights both resolve from the chat's workspace, so the summary now says so and lists the per-workspace models; `/api/settings/routines` gained `title_model_by_workspace` / `insights_model_by_workspace` (`ccb0108`)
- Architecture review cleanup (#247): split the `node_*`/package handlers into `ciao/web/routes_node.py` and the MCP HTTP endpoints into `ciao/web/routes_mcp.py`, extract `SettingsAutomation.vue` and a shared `useFileComments` composable, and delete 11 dead control-plane methods. `docs/ARCHITECTURE.md` now indexes every top-level `ciao/` module, enforced by `tests/test_architecture_doc.py` (`07a7626`)

### Fixed
- Fix the dev-mode desktop rebuild (Settings → Restart) failing with an unexplained `tauri build` error: the build now locates a Rust toolchain that is installed but off `PATH` (Homebrew's rustup keeps `cargo` in `opt/rustup/bin`, invisible to a launchd- or Finder-started engine), refuses up front with install instructions when there is genuinely no toolchain, and reports a failed step's stderr instead of only npm's command echo
- Stop session insights and the schedule attention classifier failing on slow or large-context sessions (#248): the per-call timeout is now `CIAO_INSIGHTS_TIMEOUT_S` (default 600s, was a flat 120s against a path measured at 214–253s), transcripts are trimmed to `CIAO_INSIGHTS_MAX_INPUT_CHARS` oldest-first so they fit the model's context window, and an oversized-input 400 is no longer retried with the identical payload
- Home-screen arrow keys moved two cards per press in the desktop app (the keys were handled by two listeners at once) and Esc did nothing in the browser (it was bound desktop-only). Both now work in the PWA and the app, and Esc closes a chat even while typing in the composer (`bd681fe`, `8558806`)
- The live token counter no longer sums the same context once per tool call, which inflated it into the millions; it reports the latest context size (`9b6bcb9`)
- A Task subagent's thinking no longer leaks into the parent turn's trace or persists as a stray message after the subagent ends, and expanding a subagent panel no longer collapses the activity bubble around it (`b11476d`, `4b0c4d4`)
- fix(web): keep pinned-file edits when a model turn ends (`c64ba6f`)
- fix: restore VoiceTranscriber config for cloud gpt-transcribe (`183a1e4`)

### Maintenance
- chore: bump openai to 2.52.0 (`12a65e3`)
- `npm run tauri dev` works again: vite now binds the port `tauri.conf.json`'s `devUrl` expects, and the notification-center delegate is skipped in an unbundled process where macOS raises an uncaught `NSException` (`5a15dd0`)
- Remove the dead control-surface A/B benchmark: `ciao/control_surface_benchmark.py`, its test, and the `benchmark-control-surfaces` CLI subcommand. `control_surface` now accepts `legacy` and `mcp`; an `auto` value falls back to `mcp`, which is what the pre-decided evaluation already resolved to (`04deda3`)

## v0.7.0 - 2026-08-02

### Added
- feat(web): rescale font baseline and add 100%/120% preset toggle (`5dcb90d`)
- feat: show connected remote clients on host Settings page (`754b66a`)
- feat(web): rescale font baseline and add 100%/120% preset toggle (`fd87548`)
- feat(web): surface thinking-level picker in chat model dropdown (`a45c2a6`)
- feat(web): remove manual thinking-promotion affordance (`67bb61f`)
- feat(node): make client mode mirror the host, with one device panel (`9f5820d`)
- feat: schedule-triggered chat banner with durable schedule backlink (`c641de5`)
- feat(web): document keyboard shortcuts in Settings (`fa3517d`)
- feat(engine): rebuild the desktop app on a dev-mode restart (`64a956f`)
- feat(web): remove the product tour (`420c987`)
- feat(labels): add label-hygiene audit for open GitHub issues (`f079e2d`)
- feat(chats): delegates, writable sub-chats that wake their supervisor (`8375ceb`)
- feat(web): nest delegate chats under their supervisor in the sidebar (`e113df4`)
- feat(web): drag chats between projects (`eb48ea3`)
- feat: add custom compatible providers (`c7949dd`)
- feat: validate vault frontmatter (`51a72ad`)
- feat: validate vault markdown links (`5bd679e`)
- feat: report vault health findings (`fc8a1be`)
- feat: add plan mode composer toggle (`cfa0f23`)

### Changed
- Merge pull request #213 from raffaelefarinaro/chore/sync-develop-v0.6.5 (`98204db`)
- remove onboarding card and getting-started checklist (`ba1ffe1`)
- Merge branch 'fix/subagent-synthesis-nudge' into docs/gatekeeper-first-launch (`c44c9e2`)
- Merge branch 'fix/cask-app-deletion' into docs/gatekeeper-first-launch (`50beeed`)
- Know how to issue bugs and requests on gh (`341fcb6`)
- Merge pull request #239 from raffaelefarinaro/feat/schedule-banner (`64adef4`)
- Merge pull request #232 from raffaelefarinaro/docs/gatekeeper-first-launch (`76a58a0`)
- Merge branch 'chore/issue-233-mcp-1.29' (`949b3c8`)
- Merge branch 'fix/issue-187-branch-backup' (`2f6f75c`)
- Merge branch 'chore/issue-235-label-hygiene' (`da2d56a`)
- Merge branch 'fix/file-surface-viewers' (`a525c26`)
- refactor: delete the agent handoff primitive (`a47e8cc`)
- Merge branch 'main' into feature/delegate-subchats (`42fe312`)
- Merge chore/dead-mcp-role into main (`499a857`)
- Merge custom providers and chat drag work (`1222124`)
- Merge pending feature branch changes (`ea2623d`)
- Merge develop into feat/issue-237-vault-health (`039fc27`)
- Merge pull request #242 from raffaelefarinaro/fix/develop-mypy-baseline (`3dac15f`)
- Merge develop after type-check fix (`dc98e5f`)
- Merge pull request #241 from raffaelefarinaro/feat/issue-237-vault-health (`a1118ae`)
- Merge remote-tracking branch 'origin/develop' into feat/issue-236-plan-toggle (`a230ff5`)
- Merge pull request #243 from raffaelefarinaro/feat/issue-236-plan-toggle (`1d711ea`)

### Fixed
- fix(subagents): hold the synthesis nudge when the parent asked a question (`ff04782`)
- fix: stop the bundle refresh from deleting the cask's Ciaobot.app (`89143a3`)
- fix: never regenerate the legacy launcher when the desktop app is installed (`f928713`)
- fix(web): route every destructive action through the shared confirm modal (`17c1113`)
- fix(web): stable render keys for chat transcript items (`403892a`)
- fix(web): announce new and forked chats over /ws/events for live sidebar sync (`d7d7bf5`)
- fix: clear the background-agent count when the CLI never reports completion (`7157060`)
- fix(notify): give a client node the host's notifications (`208d416`)
- fix(files): make file_surface actually open the panel, and say when it did not (`17e4c43`)
- fix(notify): stop an empty first poll from eating the first notification (`d2a80a4`)
- fix(web): classify a client as local from the TCP peer, not X-Forwarded-For (`078b627`)
- fix(node): only serve hashed /assets locally in client mode (`ffc2591`)
- fix(home): tint home-recent pulse bubble with the chat's workspace color (`fe364c6`)
- fix(notify): skip chat_result_ready + push for banner-only subagent replies (`b19b002`)
- fix(desktop): stage promise-backed screenshot drops (#238) (`2ebdb16`)
- fix(providers): put absolute paths in the image manifest (`926c17a`)
- fix(web): make the loop marker on a chat row visible (`ac8af95`)
- fix(web): keep the thinking chips reachable on the real gpt-5.6-sol (`c6a4f98`)
- fix(engine): widen interrupt sentinel to cover 'for tool use' variant (`ff955ce`)
- fix(notify): keep the banner floor off replies the user asked for (`5e2d0b5`)
- fix(web): bind the shortcut refs in both template branches (`717d73e`)
- fix(desktop): make the bundle swap recoverable and never guess at liveness (`c1c12b1`)
- fix(desktop): look for the real Mach-O name inside the bundle (`d053655`)
- fix(web): make switching off fable actually switch (`fcd49a3`)
- fix(web): re-measure the viewport on resume so --app-h unlatches (`7f52be2`)
- fix(web): keep the comment compose popover above the keyboard (`d9eea26`)
- fix(desktop-drop): scope screenshot-unreadable message to NSIRD paths (#238) (`eb90e3a`)
- fix(mcp): report honest client-presence signal from file_surface (`a531c71`)
- fix(backup): converge on a per-commit backup ref when the branch diverged (#187) (`9c36431`)
- fix(desktop-drop): keep a friendly message for non-screenshot permission errors (`8026905`)
- fix(delegates): stop telling the supervisor chat_get returns a transcript (`ae07e06`)
- fix(delegates): report a quota-deferred delegate as deferred, not failed (`1669266`)
- fix(claude): keep background shell work in turn (`236c668`)
- fix: improve retry and deferred message handling (`8b4ba84`)
- fix: guard plan toggle draft cleanup (`63663fc`)
- fix: close vault health lint review gaps (`ff60908`)
- fix: surface vault traversal failures safely (`a71f7e2`)
- fix: avoid secondary vault traversal (`b1d9ad4`)
- fix: restore develop type checks (`256d14c`)
- fix: restore plan mode return state (`d098ae1`)
- fix(preflight): skip nested git checkouts when expanding changed dirs (`34a13f1`)
- fix(codex): keep a completed turn's answer visible when it has no final_answer (`5258da5`)
- fix(providers): create the custom-provider token file at 0600 (`7d4e545`)

### Maintenance
- docs: document the real Gatekeeper approval flow and the cask app conflict (`201b9e6`)
- test(memory-injector): drift-pin the Issue labeling section (`6330f11`)
- chore: retire the anonymous bug-report form, keep GitHub issues only (`23cce3b`)
- chore: drop the retired [Report] label from the issue-labeling convention (`add0cde`)
- docs(prompt): describe what file_surface actually skips (`884104a`)
- test(providers): stop CIAO_ACTIVE_WORKSPACE leaking into the no-env case (`55e958d`)
- chore(web): rebuild bundle after the merge (`e0774f6`)
- chore(web): clear the lint backlog down to typed-any work (`4267c6b`)
- chore(web): rebuild bundle after the lint cleanup (`56f6b6c`)
- chore(web): type the remaining API responses, lint now clean (`09496bc`)
- chore: bump mcp SDK to >=1.29.0,<2.0 (#233) (`4323307`)
- docs(control_plane): explain the flakiness, not just the failure (`cfa3f84`)
- test: isolate CIAO_WORKSPACE so three ollama tests stop failing locally (`3c2907f`)
- chore(mcp): drop the dead principal role plumbing (`2b248aa`)
- docs: explain GWS sandbox certificate retry (`88aba56`)
- docs: design focused vault health lint (`b7b0989`)
- docs: plan focused vault health lint (`54cbdfe`)
- docs: describe vault health validation (`239bbbc`)
- test: cover plan chip pending state (`cc6635c`)
- chore: consolidate pending workspace changes (`2e5e447`)
- chore: consolidate pending workspace changes (`2b6393f`)
- docs: describe vault lint checks (`ab2b8b3`)
- docs: clarify plan mode restoration (`3eea1f5`)
- chore: ignore local operator secrets directory (`06de7f9`)
- docs(capabilities): catalog custom providers and plan mode (`f8152a2`)
- test(web): drop the stub for the deleted ProviderSubchatPanel (`7cb80ad`)
- docs(release): correct two stale traps (`7659d67`)
- docs(release): record the cargo prerequisite (`36b2385`)
- docs: correct comments the v0.7.0 changes made wrong (`f8259ea`)
- chore: drop the author's absolute paths from the repo (`3fc96bd`)

## v0.6.5 - 2026-07-30

### Changed
- A device connected as a client now looks exactly like opening the host's own
  address in a browser: it serves the host's app build instead of its own, so a
  newer host can no longer be driven by an older local screen without saying so.
  The password in Settings is the host's, the one you typed to get in, and every
  card on that page belongs to the host and now names it.
- Everything about the computer in front of you moved to one page, **This
  device** (`/device`), reachable from the client banner and from Settings: its
  role, its connection to the host, disconnect, and its own version and update.
  Nothing else in the app is about the local machine anymore, so it is always
  clear whose data a screen is showing. That page keeps working when the host is
  unreachable, since it is where you go to disconnect.

### Fixed
- A device connected as a client gets notifications again. Its menu bar read a
  queue that only the machine running the chats ever writes, so on a client it
  stayed empty forever and the native banner never fired. The host's tray showed
  your notifications while you were sitting at the other Mac, which left browser
  push, the least reliable channel, as the only way to reach you. The tray now
  reads that queue through the engine, so in client mode it shows the host's
  notifications and both machines alert you at the same time.
- Changing the host's password from a connected client no longer cuts that
  client off.
- A file the assistant surfaces now actually opens. Closing one pinned file used
  to block every later surface request in that chat, silently, so deliverables
  stopped appearing with no sign anything had been suppressed. Dismissals are
  remembered per file instead of per chat: the file you closed stays closed
  across reconnects, a new one still opens. An explicit surface also replaces
  whatever is pinned, and on a phone it opens the file viewer instead of doing
  nothing, since there is no side panel there.
- The `file_surface` tool reports how many clients were listening, so the
  assistant can no longer claim a file is in your panel when nobody had the chat
  open.
- The loop indicator in a chat's loop banner is now legible. It inherited body
  text size, so the heartbeat glyph was lost next to the loop title and the
  Start/Stop buttons.
- Pasted URLs are read with `defuddle` first instead of `WebFetch`. The rule
  existed but lived only in a file that PWA chat turns never load, so it never
  applied where most turns happen; `WebFetch` is now explicitly the fallback for
  non-HTML targets.
- The release verification job works again. It had been failing since v0.6.3
  because a freshly installed app cannot launch unattended while the download
  quarantine flag is set, which halted the app before it started its engine.
  This never affected published releases — the engine, app, and Homebrew
  packages shipped normally each time — only the automated check that installs
  and launches them afterwards.
- The "background agents running" indicator no longer sticks after the agents
  have finished. Ciaobot waited on a completion record that the CLI sometimes
  writes only at the start of the next turn, or never writes at all, so the
  count could sit at one until you sent another message. It now reads each
  agent's own transcript as well, and clears the count whenever it stops
  watching instead of leaving a badge that nothing can take down.

### Maintenance
- The desktop app records engine start-up failures to its log instead of
  discarding them, so a backend that fails to come up says why.
- Release verification can now be re-run on demand against an already published
  version, and runs on any change to itself, rather than only once per release.
- CI launches the app bundle it just built and waits for the engine, so a
  start-up regression is caught on the pull request that introduces it.

## v0.6.4 - 2026-07-29

### Fixed
- fix: configure smoke workspace before app launch (`8623999`)

## v0.6.3 - 2026-07-29

### Fixed
- fix: detach app launch in release smoke (`5cb7683`)

## v0.6.2 - 2026-07-29

### Fixed
- fix: launch installed app directly in release smoke (`ed47745`)

## v0.6.1 - 2026-07-29

### Fixed
- Workspace switches now land on the home screen instead of opening and
  loading an arbitrary chat. Creating a chat in another workspace no longer
  waits for the previous first chat's transcript.
- Homebrew installs now trust and install the engine formula before the cask;
  the release smoke test follows the same flow, and generated casks use the
  current macOS dependency syntax.

## v0.6.0 - 2026-07-29

### Added
- **Native macOS app.** The Homebrew cask now installs `Ciaobot.app` together
  with the engine. The Tauri shell owns the window, menu bar, native
  notifications, Finder drag-and-drop bridge, start-at-login preference, and
  a single signed updater that advances the engine and app together. Existing
  Homebrew installs migrate in place: the workspace and server LaunchAgent are
  reused, the legacy menu-bar helper is disabled with a recoverable backup,
  and the old `Ciaobot Server.app` moves to the Trash once the engine is
  healthy.
- **Host/client nodes.** A Ciaobot instance can now run as a *client* that tunnels to a
  *host* over a passworded connection. The client proxies API calls and WebSocket streams
  to the active leader, rewrites `Origin`/`Referer` on state-changing requests, and keeps
  the menu bar tray in sync with standby/leader status. Peer URLs are normalised
  (protocol prefix, default port 8443) so a bare hostname is enough
  (`257022b`, `66b04b5`, `0374c97`, `c8f31ba`, `8b58041`, `1a8ef5d`).
- **Comments as overlays.** The legacy comment sidebars are gone. Comments now open as
  floating, hover-pinned popovers in the transcript, the file viewer, and the pinned-file
  panel, all sharing one `CommentComposePopover`. The composer drops the redundant
  selection quote — the highlight already shows what was picked — and editing an existing
  comment happens in a popover anchored to its chip rather than a full-height drawer.
  A comment now also carries the role and paragraph of the text it quotes, so the model
  is not left guessing which reply a repeated phrase came from
  (`501a01a`, `25f31ea`, `82d60dd`, `0da5348`, `6e627a4`, `76f1d4c`).
- **Superseded harness skills are hidden.** The bundled CLI ships skills that either bypass
  Ciaobot's own surfaces (cloud routines, harness cron loops, design sync) or duplicate one
  the PWA owns (settings, permissions, diagnostics, per-project run stubs). These are now
  removed from the model's context via `skillOverrides` *and* denied at execution, which
  keeps the model from reaching for the wrong surface and saves the per-turn context cost
  of their descriptions (`556263c`).
- **Auto-turn marks.** A user turn fired by a loop or schedule is marked `↻ auto` in the
  transcript, so a self-driven turn is no longer indistinguishable from something you typed
  (`556263c`, `6e627a4`).
- **Per-workspace accent colour.** Each workspace picks one of five presets in Settings and
  the PWA tints its chrome to match, so which workspace you are in is visible at a glance
  instead of something you read off a label (`9ae33bb`).
- **MCP settings you can actually edit.** Servers, secrets, and asset labels are editable
  from Settings, and the Providers page lists only the platform connectors and skills that
  are enabled (`9c42f5e`, `5afe526`).
- **AI OS context hygiene.** Entity tagging, memory expiration, and an `os-audit` suite for
  workspace roots, vault links, skill budgets, instruction clashes, and stale memory
  (`7ae596d`).
- `/pr` skill, and releases are now gated on `/code-review --fix` (`1cbd8e8`).

### Changed
- Host and client settings are simplified for host mode, with the card moved lower in
  Settings (`1aeb3e8`, `2baee33`).
- Hovering a comment no longer re-renders the whole transcript (`ae41941`).
- Claude's mid-turn progress narration folds into Activity instead of interrupting the
  reply (`7e60815`).
- Creating a loop over MCP starts its cadence immediately and arms it for the next server
  boot, and loop mutations broadcast `loops_changed` so open tabs stop showing stale state
  until a reload. `autostart` only ever governed server boot, so a fresh loop used to sit
  stopped while the model reported it running (`556263c`).
- Trimmed over-constraint from the system prompt for Claude 5-generation context
  engineering (`094b0cd`).
- Dependency refresh across backend and frontend (`3843eaa`).

### Security
- **Workspace names are the user's, not the app's.** Nine sites branched on a
  workspace being called literally `personal` or `work` — the names the first
  release happened to ship (#197).

  Most consequentially, a workspace with any other name had its vault placed
  *next to* `memory-vault/` instead of inside it, where the vault index, the
  linter and the memory-proposal scans never looked. Vault resolution now lives
  once on the config, is the same for every name, and is *pure* — it reads the
  workspace registry and never probes the filesystem, so a vault cannot silently
  relocate mid-life. New workspaces — including a fresh first workspace from
  setup — always receive `<CIAO_VAULT_ROOT>/<workspace-name>`. An adopted notes
  folder or install whose vault sits at an older location keeps that location
  pinned and persisted: nothing moves silently, and `ciao os-audit` (and PWA
  diagnostics) direct the operator to an interactive Ciaobot migration chat
  that can inspect conflicts, back up the source, update the registry, and
  restart into the standard layout.

  Saving *any* workspace setting used to rewrite that workspace's `vault_root`
  to its bare name, which pointed a setup-created or external vault at a
  directory that did not exist while the real content stayed put. Fixed:
  workspace locations are read-only in Settings, existing locations are
  preserved, and request-body paths such as `/` or `..` cannot redirect agent
  writes or filesystem scans. Named vault children are also rejected when a
  symlink would resolve them outside the configured vault.

  Also: the sync-conflict chat no longer hard-errors without a `personal`
  project; title/insights model routing resolves against the primary workspace;
  a new project with no workspace lands in one that exists; the vault linter
  checks every workspace root; the model-bucket fallback no longer keys on the
  name `work`; an unregistered workspace name gets the default denylist instead
  of an empty one; and legacy unprefixed vault entries, memory proposals, and
  skill proposals take an explicit owner from the registry rather than from
  directory order, transcript labels, or a workspace literally named
  `personal`, which could expose or file a private workspace's data in a client
  workspace.
- Ciaobot no longer ships an opinion about the self-hosted n8n MCP. It was
  denied by default in a workspace named `personal` and nowhere else; it is
  project-scoped in `.mcp.json`, so it exists only where you configured it, and
  which workspaces see it belongs in that workspace's "Extra disallowed tools"
  field.
- **Local-only endpoints are gated on the peer address, not the `Host` header.**
  `/api/node/handover` and `/api/menubar-chats` were reachable unauthenticated from the
  network: the first could force-promote a node — demoting the real host, pushing its vault
  — and forward the stored host session cookie to a URL of the caller's choosing; the
  second leaked chat titles and workspace names. Both now require a loopback peer or a
  session, and the handover target must be the host the node is actually connected to.
  Setup-token redemption moved to the same check — its old gate read the caller-supplied
  `Host` header. Enabling password protection from a non-local device is still allowed and
  now logged with the peer address: a headless host reached over a tailnet has no localhost
  browser, so requiring a local caller would leave it permanently unprotectable
  (`4fad3a2`).
- The pre-commit secret scan no longer exempts everything under any `tests` directory or
  any file merely named `test_*`. A `tests/credentials.json` inside the vault, or a stray
  `test_config.json`, was being committed and pushed unscanned (`4fad3a2`).

### Fixed
- **Loops:** a loop whose target chat was deleted no longer recreates one every interval
  forever — the replacement chat is persisted instead of being discarded by the status
  write-back — and an orphaned loop is no longer re-homed into an arbitrary workspace
  (`4fad3a2`).
- **Transcript:** past turns keep their Activity trace while a later reply streams, and a
  tool call that was denied no longer leaves an Outputs card for a file it never wrote —
  including on Codex, whose approval ids needed mapping to the tool call they gate
  (`4fad3a2`, `556263c`).
- **Unattended turns really do run in bypass.** The mode was documented and plumbed but
  never applied, so a loop that fetched a page and wrote a file auto-denied its own first
  tool call and did nothing, every interval. A message you type while a tick is in flight
  is no longer treated as unattended either, so it keeps its approval prompts.
- **Comment editing works again.** Clicking a staged comment's chip reopens it in the
  shared composer; the edit popover had lost its markup.
- **Comment composer:** the popover clamps itself to the viewport, so commenting near the
  bottom of a transcript or file no longer puts Save below the fold where a fixed-position
  element cannot be scrolled to (`4fad3a2`).
- **API errors:** the "redeploy your server" hint is now reserved for a genuinely missing
  route. A plain-text 500, a proxy 502/503, and a real `404 {"error": …}` each report
  themselves, instead of sending people to redeploy a healthy build (`4fad3a2`).
- **Node/auth:** stop client login loops and the host auth-check storm; restore
  host-password login; guard against self-proxying; restore the standby role check in
  `get_proxy_target_url` (`4db2291`, `2abf265`, `e7436e0`, `7859140`).
- **MCP:** schedules and loops inherit the calling chat's project and workspace;
  project/chat resolution defaults to the active session; `chat_archive` targets the caller
  chat (`7a32a9c`, `6211c44`, `f5205c2`).
- **Loops:** preserve project context and auto-create a chat when the loop target is gone
  (`276c2d8`).
- **Chat:** reject prompts and WebSocket requests to archived chats early; skip non-vision
  model fallbacks for image prompts; broadcast `chat_archived` over `/ws/events` for
  cross-tab sync; defer meta-question openers to the first assistant reply
  (`2732fab`, `a28323f`, `8f46354`, `65ac6cd`).
- **Mobile/UI:** stop iOS auto-zoom on input focus and hide the homepage under an open
  sidebar; stretch lone home tiles with an auto-fit grid; keep the model selector dropdown
  clear of header controls; anchor late subagent panels to their spawning turn; auto-align
  the active workspace and expand the parent project when navigating
  (`5bbcb11`, `a518af9`, `33b3b02`, `b5d260b`, `feba77b`).
- **Streaming:** refine trace-buffer clearing and the thinking threshold (`8ebf888`).
- **Providers/GWS:** name the host and error category on connection failures; require
  confirmation before a GWS health "re-authenticate" push; debounce GWS health monitor
  false alarms (`31652ec`, `3cd67ca`, `5ba9a18`).
- **Preflight secret scan:** ignore worktrees and env templates, and stop `secretary.md`
  style names from warning (`30e00b1`, `aeaa77b`; scope corrected in `4fad3a2`, above).
- **Sessions:** recover from a non-fast-forward push rejection via auto-merge (`041b84a`).
- Entity tagger no longer false-positives on README collisions and self-scans (`51616b3`).
- Removed stale Pi references from engine comments, docstrings, and tests (`88e1fa0`).
- CI: cleared the mypy and PWA-docs errors blocking Ciao CI (`3c00005`, `989cb5c`, `cb7ee30`).

### Maintenance
- Documented auth settings, menu bar chats, and node connect in the API docs (`4df84ca`).
- Added #agentswelcome AI agent contribution guidelines (`e14cabc`).
- Aligned docs and the capability skill with shipped Settings and MCP memory (`5524f29`).
- Bumped the service-worker cache name to v0.6.0 so clients pick up the new build.

## v0.5.3 - 2026-07-23

### Added
- feat: command palette, vault backlinks, mypy-clean backend + quality tooling (#167) (`1cb614c`)

### Fixed
- fix(release): keep a top-level __version__ literal for the release tool (`39867b5`)

### Maintenance
- chore(release): bump service-worker cache name to v0.5.3 (`4892668`)

## v0.5.2 - 2026-07-21

### Added
- feat(chat): auto-surface freshly written .md/.csv in the pinned panel (`3d0878e`)
- feat(schedules): make automations explainer an info callout, cover auto-archive (`87cb2dd`)
- feat: add startup insights backfill option and refactor backfill core logic (`d2690d1`)
- feat(settings): add automations tab with background runs history and insights backfill trigger (`bd5f0a8`)
- feat(chat): drag files/folders into the composer to insert their path (`a7421df`)
- feat(gws): unify OAuth scopes across profiles, document the profile wrapper (`62717c3`)
- feat(config): deny PWA-irrelevant harness tools by default in every workspace (`bd7f0e9`)
- feat(pwa): surface replies that Ollama-routed models bury in thinking (`6adfa9f`)
- feat(projects): add reorder endpoint for sidebar drag-to-reorder (`a1167d2`)
- feat(pwa): home-screen jump-back-in grid, project drag-reorder, tidy sidebar (`f192efd`)

### Changed
- Merge pull request #161 from raffaelefarinaro/chore/sync-develop-v0.5.1 (`bc5f03e`)
- ui(settings): dock MCP tool usage counters into the card header (`10e6058`)
- Surface out of credit errors and trigger automatic retry (`21cc545`)
- web: allow billing and spend limit errors to auto-retry even with progress (`2c02036`)
- web: fix trace summary wrapping and hide secondary metadata on narrow screens (`034dbf3`)
- refactor(mcp): trim redundant vault tools, enrich schedule/loop docstrings, rename provider-consultation to agent-handoff (`3b0661e`)
- refactor(mcp): trim tool catalog 78->42, migrate plumbing to CLI (`f702dd8`)
- refactor(config): drop the unused personal denylist alias, dedup extras branch (`da1bdbe`)

### Fixed
- fix(chat): stop double-rendering subagent activity in the parent trace (`45b6943`)
- fix(chat): merge trailing tool calls into the single turn Activity group (`99059ac`)
- fix(chat): auto-resume turns dropped by a mid-response connection close (`6d18866`)
- fix(mcp): stop strict_mcp_config from suppressing claude.ai connectors (`5d1e4b8`)
- fix(claude): name host on ENOTFOUND, silence benign SDK control-task errors (`78595b1`)
- fix(chat): show each assistant reply as its own bubble, hide rate-limit noise (`5c6410f`)
- fix(chat): stop queued follow-up messages from vanishing on a non-retryable error (`02e7642`)
- fix(chat): redesign the in-chat permission approval card (`14b95b0`)
- fix(chat): stitch /messages history across a mid-conversation SDK session rotation (`c60c964`)
- fix(chat): persist queue reorder/edit/remove when there's no live stream (`f27176c`)
- fix(chat): fold repeated compaction status ticks into one live trace line (`af5c574`)
- fix(loops): resume into a fresh chat when a loop's target was archived (`7ec9b95`)
- fix(schedules): record last_run_chat_id synchronously on auto-fire (`3cb001c`)
- fix(triage): exclude startup triage's own runs from the failure report (`8f4ec60`)
- fix(triage): scope the self-loop exclusion to triage dispatch only (`3b7f3aa`)
- fix(chat): resume instead of replaying on a mid-turn quota limit (`566b6e4`)
- fix(pwa): restore the Backfill insights endpoint after the scripts refactor (`e0210ac`)

### Maintenance
- docs(readme): position Ciaobot as an owned second brain vs Claude Cowork (`ab717f3`)
- docs(readme): add Ciao! mascot hero banner (`2862626`)
- chore: bump openai dependency to 2.46.0 (`aec717e`)
- chore: rebuild PWA bundle (`77d4ed4`)
- docs(release): make the pre-release quality review a mandatory blocking gate (`080da58`)
- chore: disable Claude Code Artifacts across all dispatched sessions (`07515a6`)
- chore(skills): clarify sync summary wording (custom-agent symlinks) (`41baadb`)
- docs(capabilities)/chore(pwa): catalog drag-to-composer, fix misleading comments (`b6f63a1`)

## v0.5.1 - 2026-07-19

### Added
- feat(ollama): default opus tier to minimax-m3:cloud (`f01543b`)
- feat(chat): manage queued messages server-side and reconnect live turns faster (`3e3d813`)
- feat(web): add CSV table preview and cell editing in the file viewer (`903e7f5`)
- feat(web): add cell-level CSV comments with row/column anchors for the agent (`8c26f63`)
- feat(chat): show newly created files in Outputs chips (`855864d`)
- feat(web): run all missed schedules and show workspace missed counts (`ad7bbbb`)
- feat(web): nest all subagent activity inside collapsible reasoning trace; clean up oneshot routing and dev defaults (`1dc74a7`)
- feat(web): display both input and output live token counts in ChatPanel (`b666948`)
- feat(critique): restrict default critique panel to opus/fable, prioritize native Anthropic, and display effective models in PWA settings UI (`f83553f`)
- feat(mcp): make MCP the default control surface with graceful legacy fallback (`7cd8b87`)
- feat(web): two-line slash-command picker rows, drop source tag (`3b00902`)
- feat(mcp): surface per-tool usage in Settings (`a057246`)
- feat(pwa): order provider models by tier in model pickers (`518d5a7`)
- feat(settings): align home-tab cards and fix stale nav references (`9026ad5`)
- feat(critique): add OpenAI (Codex) fable to the default panel (`f53f058`)
- feat(gws-auth): request one full scope set with a --scopes override (`018caad`)

### Changed
- Merge pull request #153 from raffaelefarinaro/chore/sync-develop-v0.5.0 (`7dce211`)
- web: disable in-app toasts and unread markers for background agents (`d484a0f`)
- Update retry tests to match new _drive_stream async generator signature (`20763fd`)
- refactor(web): fold stop into the composer send button (`fdb4130`)
- web: guard WebSocket event handlers from orphaned connections to prevent duplicate stream appends (`9dc848a`)
- Move Google Workspace card to Workspaces tab and suppress allowed rate limit status events (`4785327`)
- Merge fix/chat-sync-stale-state: heal three chat live-sync bugs (`44ebf5e`)

### Fixed
- fix(reporting): reject leaked mock-object reprs in bug reports (`f4b73d7`)
- Fix duplicate assistant message rendering, support turn-based history merging, and persist streaming start times (`e966bae`)
- fix(chat): keep PWA live through capability fallback (`e19542c`)
- fix(web): show AskUserQuestion prompts from text/type payloads (`ecb5cf8`)
- fix(oneshot): drop tool schemas and MCP from routine calls (`9bc661d`)
- fix(web): hide Claude requesting status outside Activity (`9beacc0`)
- fix(web): show restart overlay during drain instead of chat errors (`53eb371`)
- fix(deploy): retry git pull on transient network errors (`83a9e48`)
- fix(web): keep Working Activity during mid-turn history sync (`0318c44`)
- fix: inject resolved codex binary dir into child process PATH (`cf34f56`)
- fix: suppress allowed rate limit status events in chat stream (`313a572`)
- fix(web): stop duplicating Anthropic tiers as a Claude (Ollama) section (`4e21d91`)
- fix(sync): heal three stale live-state bugs in chat streaming (`b598968`)
- fix(chat): preserve queued messages when a turn pauses on a question (`0663c49`)
- fix(pwa): anchor file-viewer comment bubbles to their cell reliably (`8bed282`)
- fix(memory): stop proposal flood and route facts by scope (`013e6d6`)

### Maintenance
- docs(pwa): document queue_state and queue management WS messages (`b2da446`)
- chore(memory): disable Claude Code auto-memory when working in Ciaobot (`f231a0e`)
- docs(pwa-api): document /api/mcp/usage route (#158) (`df7b297`)
- docs(critique): point /critique command at the adversarial-review panel (`22d0238`)
- docs(capabilities): catalog CSV table viewer, cell comments, and missed-schedule catch-up (`0c8b74f`)

## v0.5.0 - 2026-07-16

### Added
- feat(web): stack message action buttons vertically in bottom corners (`134bb05`)
- feat(reporting): anonymous bug reporting via Google Form (`efc8aea`)
- feat: allow editing text and markdown files directly in the sidebar (`1ec0746`)
- feat(web): XML-tagged reference blocks for quoted comment context (`ad02317`)
- feat(reporting): wire the Ciaobot Bug Reports Google Form (`a674356`)
- feat(gws): detect dead Google logins and add server-managed re-login (#145) (`5ccc0d9`)

### Changed
- Merge pull request #135 from raffaelefarinaro/chore/sync-develop-v0.4.29 (`552c51b`)
- Merge pull request #136 from raffaelefarinaro/docs/release-skill-and-capabilities (`1a4be33`)
- Merge pull request #138 from raffaelefarinaro/feat/vertical-message-actions (`76cdc18`)
- Merge pull request #139 from raffaelefarinaro/fix/137-oversized-sdk-message-buffer (`ef44b12`)
- Merge remote-tracking branch 'origin/develop' into feat/anonymous-bug-reporting (`357927b`)
- Merge pull request #140 from raffaelefarinaro/feat/anonymous-bug-reporting (`83fcbc7`)
- Merge pull request #141 from raffaelefarinaro/feat/sidebar-file-editing (`9721de2`)
- Merge pull request #142 from raffaelefarinaro/feat/xml-comment-context (`2efb126`)
- Merge pull request #144 from raffaelefarinaro/fix/title-tier-dropdown (`eea0465`)
- Merge remote-tracking branch 'origin/main' into develop (`a2f1459`)
- perf(menubar): poll tray status every 2s instead of 10s (`7b7da1c`)
- Merge pull request #149 from raffaelefarinaro/fix/143-oneshot-error-detail (`bb3a8ff`)
- Merge pull request #147 from raffaelefarinaro/feat/faster-tray-poll (`4576c5b`)
- Merge pull request #146 from raffaelefarinaro/fix/duplicate-optimistic-user-bubble (`8aa2323`)
- Merge remote-tracking branch 'origin/fix/askuserquestion-picker-stuck' into develop (`9b5da37`)
- Merge remote-tracking branch 'origin/fix/145-gws-token-health-relogin' into develop (`8fe22db`)

### Fixed
- fix(claude): survive oversized CLI JSON messages (#137) (`46da3a7`)
- fix(settings): show model tier on Chat titles row under Automatic (`b4dbdc3`)
- fix(web): stop duplicate user bubble from stranded optimistic send (`206c8fa`)
- fix(oneshot): propagate upstream error detail + bounded retry (#143) (`6c405ba`)
- fix(web): stop answered AskUserQuestion picker from sticking on screen (`fd83e16`)

### Maintenance
- docs(skills): add ciao-release skill and cover forks/consultations/retry in ciao-capabilities (`5cb1d87`)
- docs(skills): expand ciao-release checklist (open PRs/issues, fresh review + simplify) (`7c3a89e`)
- docs(skills): make ciao-release a project skill, not a stock skill (`04f67b7`)
- build: rebuild frontend after merging main (sidebar editing) into develop (`b5464c6`)
- chore(deps): safe patch bumps for 0.5.0 (scrapling 0.4.11, dompurify 3.4.12, vue 3.5.40) (`1614976`)

## v0.4.29 - 2026-07-16

### Added
- feat(web): implement compact chat activity design and grouped token usage formatting (`6e0974f`)
- feat: implement cross-provider sub-chats, conversation forks, and compact activity layout (`4272d37`)
- feat: show last run time, link to chat, and pulsing indicators on automations (`c36dc50`)
- feat: auto-retry connection errors and fix schedule/run detail issues (`f112f43`)

### Changed
- Merge pull request #133 from raffaelefarinaro/chore/sync-develop-v0.4.28 (`6499a40`)
- refactor: sync workspace switcher, fix subchat concurrency check, and update static build (`a901749`)
- refactor: simplify contextLabel helper in SchedulesView.vue (`43368d1`)
- refactor: simplify fork snapshot logic in chatFork.ts (`b914caa`)
- refactor: refine consultations attachment logic in ChatPanel.vue (`ad1c265`)
- Merge origin/develop into develop (`ccc9641`)

### Maintenance
- docs: design cross-provider sub-chats (`c5290ee`)
- docs: design conversation forks (`9eadf4d`)
- docs: plan forks and provider consultations (`f167037`)
- test: add test case for switchWorkspace with transition options (`72c1b79`)
- build: update static files index.html (`9114edd`)
- build: update static files index.html (`a93123b`)

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
