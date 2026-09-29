---
name: ciao-upkeep
description: Keep Ciaobot current between releases — check every dependency and pin we install (Python, npm, Defuddle, gws, GitHub Actions, the Node floor, Playwright), keep the stock skill catalog, ciao-capabilities and the marketing site in sync with the code, sweep doc rot, and maintain the watchlist in docs/UPKEEP.md. Trigger on "keep ciao up to date", "check dependencies", "any updates", "check the site", "watchlist", "upkeep", or after a feature ships that adds a user-facing capability.
---

# Ciaobot upkeep

> Contributor/project skill — lives in the repo's workspace `skills/` folder, **not** `ciao/stock/skills/`. It is for people working *on* Ciaobot and is deliberately not packaged or shipped to end-user installs. `ciao sync-skills` mirrors it into the runtime-discovered `.claude/skills/` catalog. Don't move it into `ciao/stock/`.

The standing checks that keep the repo honest **between** releases. The release
skill (`/ciao-release`) deliberately does none of this — it cuts, it verifies,
it ships, and it does not fix. Anything upkeep finds becomes a commit on
`develop`, reviewed on its own PR.

Two things to keep straight:

- **Check** — compare, report, file. Cheap, safe, repeatable.
- **Adopt** — change a pin, fix a doc, edit the site. A normal PR against
  `develop`, with the gates. Never bundled into a release.

Everything here runs in the main checkout on a branch. No worktree.

## 0. The watchlist first

`docs/UPKEEP.md` is the working document: topics we keep an eye on over time,
with the last-checked date and the decision. **It is edited during ordinary
work**, not swept periodically — the moment you notice a topic worth watching,
add a row. `AGENTS.md` points at it so any agent working in this repo knows it
exists.

Read it before starting. Then pick the section below, run the check, and update
the `Last checked` column for what you actually looked at. A stale date on a
row you did not check is worse than no date.

## 1. Dependencies — everything we install

`ciao/dependency_updates.py::check_available_updates` reports updates for
**two files only**: `pyproject.toml` and `web/package.json`. It is correct for
what it covers, and it covers less than we install. `AUTO_UPDATE_KEYS` is
`("claude-agent-sdk",)`, so `--apply` during a release bumps exactly one
package; everything else is report-only. Treat its output as one section of
this one.

```bash
env -u PYTHONPATH .venv/bin/python -c "
import pathlib
from ciao.dependency_updates import check_available_updates
for u in check_available_updates(pathlib.Path('$(pwd)')):
    flags = ('auto' if u.auto else 'manual', 'safe' if u.is_safe else 'major')
    print(f'{u.key:28} {u.ecosystem:7} {u.current:12} -> {u.latest:12} {\" \".join(flags)}')"
```

The full inventory — what is pinned where, and what the checker misses:

| Thing | Pinned in | Covered? |
|---|---|---|
| Python deps (incl. `firecrawl-anydoc>=0.2.4,<0.3.0`) | `pyproject.toml` | yes — reported only |
| Frontend deps | `web/package.json` | yes — reported only |
| **Defuddle CLI** | `ciao/stock/defuddle/package.json` → `0.19.3` | **no** — separate manifest |
| **`gws` CLI** | `metadata.version` in `ciao/stock/skills/gws-shared/SKILL.md` → `0.22.5` | **drift only, not updates** |
| AnyDoc | rides the `pyproject.toml` pin | yes |
| GitHub Actions | `actions/*@v4`/`@v5`, `astral-sh/setup-uv@v6` | **no** |
| Node floor | `.nvmrc` **and** `web/scripts/check-node.mjs` | **no** |
| Playwright | `web/` | **no** |

The three unchecked ones need their own check:

```bash
# Defuddle: a separate manifest, installed privately by ciao/defuddle_install.py
npm view defuddle version
grep -A3 '"dependencies"' ciao/stock/defuddle/package.json

# gws: the pin lives in the stock skill's frontmatter, not in a manifest
npm view @googleworkspace/cli version 2>/dev/null || echo "check github.com/googleworkspace/cli"
grep -m1 'version:' ciao/stock/skills/gws-shared/SKILL.md

# Actions
grep -rhoE 'uses: [^ ]+@v[0-9]+' .github/workflows | sort -u
```

**The Node floor is a paired edit and the failure is silent.** `.nvmrc` and
`SUPPORTED_RANGE` in `web/scripts/check-node.mjs` must move together. Below the
range, every jsdom-environment vitest file fails to start its worker — and
vitest still prints `Test Files N passed` for the files that *did* run. A green
suite that skipped a third of itself. When raising the floor, change both in
one commit and re-run `cd web && npm test` on the new version.

**Adopting an update is a normal PR, not a release step.** Bump it on a branch,
run the gates (`mypy ciao`, `pytest -n auto tests/`, `npm test`, `npm run
build`), and let the release carry it later. Never blanket-upgrade majors.

For `gws`, the pin and the generated skills travel together: bump
`metadata.version`, then regenerate with the installed CLI
(`ciao.gws_skills.regenerate_stock_gws_skills`), and commit both. A pin moved
without regeneration ships skills that document commands the CLI no longer has.

## 2. Stock skills ↔ capabilities ↔ the marketing site

Three places describe the same product to three different audiences, and
nothing enforces that they agree. The sync tests cover `ciao/` modules
(`test_architecture_doc.py`), `CIAO_*` env vars
(`test_env_vars_documented.py`) and API routes (`test_pwa_api_docs.py`). They
cover **none** of these three surfaces.

**Stock skills** — `ciao/stock/skills/`. A new bundled skill that nobody adds
to the catalog is invisible to the two audiences that matter:

```bash
ls ciao/stock/skills/ | grep -v '^gws-'
```

For each one, check it appears in:

- `ciao/stock/skills/ciao-capabilities/SKILL.md` — the right section, **and**
  trigger keywords in the frontmatter `description`. A feature that is described
  but not triggerable does not get found.
- `site/builtin.html` — the stock table, with a link to its `SKILL.md`. Verify
  the links resolve rather than assuming:
  ```bash
  git grep -oh "stock/skills/[a-z0-9-]*/SKILL.md" -- site/ \
    | sed 's|stock/skills/||; s|/SKILL.md||' | sort -u \
    | while read -r s; do
        [ -f "ciao/stock/skills/$s/SKILL.md" ] && echo "OK   $s" || echo "DEAD $s"
      done
  ```
  And the reverse: a stock skill nobody links from the site.
- `ciao/stock/skills/ciao-cli/commands.json` and the `OPERATIONS` table in
  `ciao/mcp_server.py` must agree, and with `docs/AGENT_CLI.md`. Three copies
  of one catalog, no test.

**Capabilities** — for a user-facing feature, `ciao-capabilities` is the answer
to "can you…?". After any feature that adds one: add the entry, add the
trigger words, and skim the changelog since the last tag for features that
shipped without one.

## 3. The marketing site

`site/` is seven hand-written HTML files, 1201 lines, published by
`.github/workflows/pages.yml` on any push to `main` that touches `site/**`. So a
wrong claim goes live the moment it merges, and `workflow_dispatch` means a fix
does not need a release.

It has no tests. Nothing compares it to the code it describes. That is the whole
argument for checking it.

For each release's removals and renames, sweep it:

```bash
git diff <last-tag>..develop --stat
# for each removed route, env var, page, setting, subagent or skill:
git grep -n -iE "<identifier>" -- site/
```

This range is the worked example. It removed `GET /api/automation`, the
`/api/menubar-notifications` feed, the `push_all_devices` and `trusted_url`
settings, the Settings → Notifications and → Automations tabs, three stock
subagents and three stock skills. `site/features.html` still has a full
**Automations** section with a screenshot. It happens to be *correct* —
`/automations` survives as a redirect to `/schedules` (`web/src/router.ts`) —
but nothing but a human checking knew that, and the same section is one rename
away from being wrong. Sweep, then confirm rather than assume.

Also worth a look each pass: `site/builtin.html`'s subagent copy against what
`ciao/stock/agents/` actually ships, and the feature descriptions against the
current Settings tabs. Screenshots in `site/assets/img/` are the slow rot —
an image of a UI that no longer exists, with the alt text still describing it.

## 4. Doc rot

The sync tests prove structure, not truth — a paragraph describing a removed
engine, a renamed env var or a deleted page passes them. v0.8.0 shipped both:
`PWA_API.md` still documented a cloud transcription engine four releases after
its removal, and `INTEGRATIONS.md` still claimed a policy two releases after it
was dropped.

```bash
env -u PYTHONPATH .venv/bin/python -m pytest \
  tests/test_architecture_doc.py tests/test_env_vars_documented.py tests/test_pwa_api_docs.py
```

Then, for identifiers the range removed, sweep by hand across `README.md`,
`INTEGRATIONS.md`, `PWA_API.md`, `docs/`, `DESIGN.md` **and** `site/`.

## 5. Advisories and triage

`pip-audit`, `npm audit` and `npm run lint` are `|| true` in `ci.yml` — advisory
by design, so a release is not held hostage by a transitive CVE. But advisory
means *someone reads them*, and today nobody does. Run them, and file an issue
for anything `high`/`critical` that has a fix.

Triage, on the same pass: `gh issue list --state open` and
`gh pr list --state open`. Close what is fixed or dead, label what is not
(`[Bug]` → `bug`, `[Feature]` → `enhancement`, per `ciao-support`), and surface
the rest. A stale open-issue list is how a two-year-old report gets rediscovered
as new.

## Reporting

Every check ends the same way: what you looked at, what changed, and what you
filed — with the issue number. Update `docs/UPKEEP.md`'s `Last checked` column
in the same commit as whatever else you changed, so the document never claims
coverage it does not have.

## Traps

- **A green `npm test` proves nothing about the Node floor.** Below
  `^20.19 || ^22.13 || >=24`, vitest prints `Test Files N passed` for the files
  that ran. Check `node --version` and read the count.
- **`check_available_updates` fails open.** Every network call is wrapped in
  `except Exception: pass`, and `release.py` prints "Dependency update check
  skipped" on any exception. A network failure reads exactly like "no updates
  available". Confirm you got a real report, not a skipped one.
- **Regenerating `gws-*` skills is not the same as bumping the pin.** They are
  two files and one is generated; bump `metadata.version` *and* regenerate, or
  the two disagree and nothing checks it.
- **Never `git add -A` in this checkout.** Another session may be editing it.
  Stage explicit paths and read `git diff --cached` before committing.
- **Use `rtk proxy git …` for anything you are about to commit on.** The RTK
  hook filters git output, and it has reported a clean tree over seven modified
  files.
- **Editing `site/` publishes on merge to `main`.** It is not a draft. Check the
  claim against the code before you commit it, not after.
