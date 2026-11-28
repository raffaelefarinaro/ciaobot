# Docs and credits check

Run from `/ciao-upkeep` § 4. Each item names the source of truth to compare
against. Read-only first; fix afterwards.

**Check, do not bloat.** A finding is fixed by editing the wrong sentence in
place or deleting the stale one, never by appending a section, a caveat or a
changelog line. A doc that grew during upkeep needs a reason in the PR body.

## docs/CREDITS.md

Build the list of what ships or is invoked, then diff it against the file:

- Python: `[project] dependencies` in `pyproject.toml` (direct runtime deps
  only; the security floor pins such as `urllib3` and `aiohttp` are
  transitive and need no line).
- Web: `dependencies` in `web/package.json` plus Vite (devDependencies are
  build and test tools; leave them out).
- Agents: `ADAPTERS` in `ciao/adapter_install.py` and the `AgentSpec` rows in
  `ciao/providers/acp/agents.py` (adapter, CLI it drives, licence).
- External tools the engine runs: `git grep -ohE 'resolve_tool\("[a-z]+"' ciao`,
  `_find_soffice`, and the optional CLIs in `INTEGRATIONS.md`.
- Mac and installers: `mac/Package.swift` (Sparkle), `mac/engine-runtime.json`
  (python-build-standalone), uv in `scripts/install.sh`.
- Fonts, icons, artwork: `@font-face` and font files under `web/`, `site/`,
  `mac/`; icon packages in `web/package.json`.
- Copied code: `THIRD_PARTY_NOTICES.md` and `Ported from` headers in `ciao/`.

Licences come from the installed metadata (`importlib.metadata`, the
`license` field in `node_modules/<pkg>/package.json`, `npm view <pkg> license`).
Every credited URL must answer 200. Remove what no longer ships; label nothing
"historical".

## README.md

- Requirements match the code: macOS minimum is `platforms` in
  `mac/Package.swift`, Apple silicon only (arm64 runtime in
  `mac/engine-runtime.json`), Linux distribution as in `docs/LINUX.md`.
- Install commands: each asset URL exists on the latest release
  (`gh release view --json assets`), and every flag shown is accepted by
  `scripts/install.sh`.
- First-run text matches `HomeWelcomeCard.vue` and the onboarding components;
  every `Settings → X` label exists in `web/src/lib/settingsNav.ts`; every
  `ciao …` command exists in `ciao/cli.py`.
- Relative links and anchors resolve; screenshots it embeds still show the
  current UI.

## site/

- `pytest tests/test_documentation_navigation.py` (internal links, anchors,
  docs hub links).
- External links to `/blob/main/` 404 until the release that adds the file
  merges to `main`: confirm the path exists on `develop` instead.
- Install copy on `index.html`, `migrate.html` and `remote.html` matches the
  README and `docs/LINUX.md`.
- Spot-check every number and named behaviour on the page you check against
  the source it describes (for `memory.html`: `STEP_FILES` in
  `ciao/care_report.py`, the step files' limits, `ciao/stock/schedules.json`,
  `_stagger_time` in `ciao/schedules.py`). Record which pages you read.

## docs/*.md, INTEGRATIONS.md, PWA_API.md, DESIGN.md, web/README.md, mac/docs

- `pytest tests -k "doc or readme or architecture or upkeep or skill" -q`
  covers modules, routes, env vars, CLI commands and doc links.
- By hand, what the tests cannot see: commands and flags quoted in prose
  (run `--help`), version floors (Node in `web/scripts/check-node.mjs`,
  OpenCode in `ciao/opencode_cli.py`, adapter pins), and paragraphs about
  removed features (sweep the identifiers removed since the last tag).

## Report

What you verified and against what, what you changed, and what you could not
verify and why. Update the `Docs and credits` row in `docs/UPKEEP.md`.
