# Docs & README Revamp — Workspaces, Projects, Memory, Claude Overrides

## Resume block

- Status: proposed
- Current checkpoint: C0
- Next action: User review of Markdown + HTML companion; approve direction before file edits
- Blocker: none
- Implementation repository: raffaelefarinaro/ciaobot
- Generated plan output: docs/plans/DOCS_README_REVAMP_PLAN.md
- Visual companions: docs/plans/DOCS_README_REVAMP_PLAN.html (interactive before/after + diagrams)
- Verified on: 2026-08-24 against README.md:1-254, docs/ARCHITECTURE.md:1-414, ciao/config.py:22-47, ciao/execution_modes.py:1-58, ciao/providers/claude.py:540-543, CHANGELOG.md:699-704

This block is the handoff contract. Any model that resumes the work should read it first, then read the current checkpoint and open questions before doing anything else.

## Outcome and user value

After this change a first-time reader understands in <2 minutes, without reading ARCHITECTURE.md, (1) what Ciaobot is for (knowledge work, not coding), (2) how to use it (install → pick workspace → new project → chat → archive), (3) how workspaces split life areas and each owns a vault slice + projects, (4) how projects inject context into every turn, (5) how memory self-heals but still asks the user, and (6) what we deliberately change from stock Claude Code. A single hero screenshot plus 3 lightweight inline diagrams replaces walls of text, and the docs stay honest via test-enforced indexes.

## Scope and non-goals

In scope:
- Rewrite/reshuffle `README.md` (The idea, Memory and the vault, Features) for plain-words clarity; preserve Install, Why it exists, Providers, Documentation table
- Add one new README section “What we change from stock Claude Code” (≈10 lines) sourced from `ciao/execution_modes.py:24` + `ciao/config.py:30`
- Add `docs/ARCHITECTURE.md` 1-paragraph “Claude harness overrides” anchor so README can link to it (test-enforced index keeps it)
- Add/update 3 diagrams + 1 screenshot slot in `README.md` (no external image host; checked-in `docs/` assets):
  1. Workspace → Project → Chat hierarchy (sidebar + vault path)
  2. Vault layout per workspace (`memory-vault/<workspace>/projects/active/...`, `INDEX.md`, `Workspace/`)
  3. Memory pipeline (archive → insights → proposals → curation vs auto-apply)
  4. PWA hero screenshot placeholder (real screenshot captured on macOS; plan ships a wireframe placeholder until then)
- Keep `docs/hero.png` as top hero; screenshots go under `docs/screenshots/` (git-tracked, <300KB each)

Out of scope for this release:
- Rewriting `docs/ARCHITECTURE.md` beyond the 1-paragraph anchor + module index line (already test-enforced by `tests/test_architecture_doc.py`)
- New PWA UI work (HOME_LANES etc.) — only docs/screenshots
- Video/GIF, external embeds, or CDN images (CSP + offline)
- Vault migration or runtime code changes

## Current-state evidence

Observed (files read):

- `README.md:1-254` — 254 lines. Structure: Install (12-62), Why it exists (64-78), It follows you (76-78), Who it's for (81-86), The idea (88-107), Memory and the vault (110-117, 144-153), Features (119-178, 5 groups), What ships by default (181-214), Providers (217-224), A personal project (227-230), Documentation table (234), Built on (253). No screenshot besides `docs/hero.png:3` (pixel mascot). Workspaces vs Projects described twice but never visually: `README.md:99` "split life areas into sidebar workspaces, then organize work inside projects" and `121-124` "Sidebar workspaces per life area… Projects group related chats" — hierarchy implied, not shown. Vault path convention not stated in README (only in `docs/ARCHITECTURE.md:397` `projects/active/<name>/<name>.md`). Memory: `README.md:111` capped `ciao:memory`/`ciao:profile` + `117` "pipeline turns it into durable knowledge" + `144-147` "confident facts applied automatically and unsure ones queued" — but self-healing (audit/repair, `[expires:]` pruning, `os-audit`) lives only in `docs/ARCHITECTURE.md:282-315` + `ciao/execution_modes.py:1-58` comments, not in plain words in README.
- `docs/ARCHITECTURE.md:53` indexes `ciao/execution_modes.py` but has no user-facing “what we disable from Claude Code” section. `CHANGELOG.md:699-704` is the only public plain sentence: "Superseded harness skills are hidden… removed via `skillOverrides` and denied at execution". `ciao/execution_modes.py:24` lists 9 skills (`schedule`, `loop`, `design-sync`, `update-config`, `fewer-permission-prompts`, `doctor`, `run`, `run-skill-generator`, `dataviz`); `ciao/config.py:30` adds 9 tools + `Skill(*)` denys. `ciao/providers/claude.py:536-543` wires `settings={"skillOverrides": …}` and `setting_sources=["user","project","local"]`.
- `docs/hero.png` exists; `docs/screenshots/` does not. No screenshot in README; first-time visitors have no UI anchor.
- `DESIGN.md` + `web/README.md` — DESIGN tokens exist; screenshots must match them (if not read yet, assume).

Assumed (not yet verified):
- Screenshot capture requires running app on macOS; placeholder wireframe acceptable for plan review.
- 3 inline diagrams as SVG in README (or checked-in SVG assets) will not blow the 2MB render cap; HTML companion can embed them inline.

What will be reused: existing README sections, `docs/ARCHITECTURE.md` layout, `docs/hero.png`, DESIGN tokens, test-enforced doc indexes.

## Recommended direction

One README pass + one ARCHITECTURE anchor + 3 SVGs + 1 screenshot slot. Tail the template; do not invent a second approval surface.

**New README order (proposed, keeps Install at top):**

1. Hero + 1-line tagline (keep `docs/hero.png`)
2. **30-second mental model** (new, 4 lines + inline diagram): `Workspace (life area, owns vault slice) → Project (folder + doc, injects `[Project context]`) → Chat (turns, delegates, loops)` — screenshot thumb to the right.
3. **Install** (keep, minor trim)
4. **Workspaces & Projects — how to use** (new, replaces scattered bullets): plain steps: pick/create workspace in sidebar → New Project → chat → archive → proposal queue. Inline diagram 1 + vault path `memory-vault/<workspace>/projects/active/<name>/`.
5. **Memory that compounds (and asks)** (new, replaces current "Memory and the vault" wall): two columns — "Auto" (archive → insights → confident facts filed, `[expires:]` pruned) vs "Asks you" (unsure facts → `Workspace/Memory-Proposals.md`, aging notes → Needs review, `os-audit` weekly). Link to `docs/ARCHITECTURE.md` pipeline.
6. **What we change from stock Claude Code** (new, 6 bullets, plain words): we hide the harness's cloud routines/loops and the settings/diagnostics skills because the PWA owns them; we keep all workspace skills; see ARCHITECTURE anchor for the list. No jargon `skillOverrides` in the bullet text; link the file:lines for the curious.
7. Features (trim 20% — keep 5 groups but compress: merge "Files and documents" + "Voice")
8. What ships by default / Providers / Documentation table (keep)

**Diagrams (SVG, code-owned, no external deps):**
- D1 — Hierarchy: sidebar workspaces (pink/cyan dots) → project cards → chat list; vault path annotation.
- D2 — Vault per workspace: `~/ciaobot/memory-vault/personal/…`, `.../work/…`, `projects/active`, `Workspace/Learnings.md`, `INDEX.md`.
- D3 — Memory pipeline: `Chat archived → Session insights (fast model) → Routing [memory]/[project]/[learnings] → Auto-apply confident / Queue unsure → Daily curation + Weekly hygiene + os-audit`.

Each diagram lives as `docs/diagrams/*.svg` (source) and is embedded inline in README via relative `![](...)` + duplicated as inline SVG in the HTML companion for 1:1 review.

**Screenshot:** one real PWA capture (Home lanes + chat + pinned file) at `docs/screenshots/pwa-overview.png` (~200KB, macOS light mode + Ciao pink accent). Plan ships a wireframe placeholder; implementation replaces it with a real capture via `ego-browser` or manual capture. Alt text required.

## Alternatives and rejected options

### Keep README as-is, add only a screenshot
Rejected: screenshots alone don't fix the hierarchy/memory confusion reported; text still buries the mental model on line 99 among 5 feature blocks.

### Full ARCHITECTURE rewrite + GIF onboarding
Rejected: too much churn for this release; GIFs cannot be commented in markdown and inflate repo size; test gate would fail on missing module index if we restructured heavily.

### External hosted diagrams (Miro/Excalidraw link)
Rejected: offline/broken-link risk, no review in PR, loses DESIGN token alignment. Inline SVG is durable and CSP-safe.

## Visual review

HTML companion `docs/plans/DOCS_README_REVAMP_PLAN.html` answers: “Does the new README read in <2 minutes at 420px and at desktop width?” It renders:

- Before/after toggle for the README's first 2 screens (current 254-line flow vs proposed 30-second model + diagrams)
- The 3 diagrams as inline SVG (click to enlarge, with file path + source file:line backing each node)
- Screenshot slot with placeholder wireframe + final capture checklist (resolution, theme, accent)
- “What we change from Claude Code” plain-words card with expandable file:line details

Excalidraw not needed — hierarchy + pipeline are simple orthogonal flows better as inline SVG inside the HTML (per `visual-output.md:21-23`).

## Decisions and hard-to-reverse bets

| ID | Decision | Rationale | Status |
| --- | --- | --- | --- |
| D-01 | 3 SVG diagrams + 1 screenshot slot, not a GIF/video | Cheap, reviewable in PR, offline, <300KB; GIF breaks comments + size | Proposed |
| D-02 | Add README “What we change from stock Claude Code” in plain words; ARCHITECTURE gets 1-paragraph anchor with file:lines | Users asked “what are we disabling”; public docs currently only in `CHANGELOG.md:699` + code comments | Proposed |
| D-03 | Keep `docs/hero.png`; add `docs/screenshots/` + `docs/diagrams/` as new asset dirs | Preserves existing hero; isolates new assets for git/code-review | Proposed |
| D-04 | README mental-model up top (30-sec), Install stays second | Install is the CTA but idea must land first at a glance | Proposed |
| D-05 | Diagrams checked in as SVG + embedded via `![](...)` in README; HTML duplicates them inline for review | So markdown stays commentable (`html-artifact` loses comments per `SKILL.md`) but review can toggle | Proposed |
| D-06 | No runtime/code changes in this plan | Docs-only release; follow-up code PRs stay small | Proposed |

## Open questions with recommended defaults

| ID | Question | Recommended default | Status |
| --- | --- | --- | --- |
| Q-01 | Screenshot tone: light pink (brand) vs dark? | Light mode + pink accent (matches `DESIGN.md` default `pink`, most legible) | Open |
| Q-02 | Where to store the real screenshot capture script? | Manual capture first; optionally add `scripts/capture-pwa.sh` using `ego-browser` later | Open |
| Q-03 | Should ARCHITECTURE anchor duplicate the 9-skill list or just link lines? | List the 9 names in ARCHITECTURE (stable API) + link `execution_modes.py:24`; README keeps plain words only | Open |
| Q-04 | Keep the “Agent-safe control plane” paragraph in README (`README.md:105`) or move to `docs/MCP.md`? | Trim to 2 lines + link `docs/MCP.md` — keeps README short | Open |

## Not yet specified (fog of war)

- Whether the weekly skill-evolution proposal flow deserves its own fourth diagram or stays as one sentence — clear once D1-D3 land and we see density.
- Exact screenshot framing (Home lanes vs Chat + pinned file) — depends on which best shows workspace→project→chat.
- Whether `INTEGRATIONS.md` needs a matching Claude-overrides note — likely no, but will surface if reviewers ask for env-var mapping.

## Feedback and decision log

| ID | Location | Feedback | Decision | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| F-01 | README / The idea vs Workspaces | Need plain-words workspace→project→chat mental model + diagrams, not just bullets | New 30-sec model + D1 hierarchy diagram | open | `README.md:88-107` vs `ARCHITECTURE.md:397` |
| F-02 | README / Memory | Memory self-heals but still asks — not clear when auto vs when queued | Split "Auto vs Asks you" + D3 pipeline diagram linking `ARCHITECTURE.md:282-315` | open | User question 2026-08-24 |
| F-03 | Docs / Claude overrides | What are we disabling from default Claude? Not explained in normal words | New README section + ARCHITECTURE anchor citing `execution_modes.py:24`, `config.py:30` | open | User question 2026-08-24 |
| F-04 | README / visuals | Do we need images/screenshots? | Yes: 1 screenshot + 3 SVGs per D-01 | open | `docs/hero.png` only visual today |

## Implementation checkpoints

Each checkpoint has an exit condition so another model can tell whether the work is actually ready to move on.

### C0. Start or resume
- Read the Resume block and current checkpoint.
- Read `README.md:1-254`, `docs/ARCHITECTURE.md`, `ciao/execution_modes.py:1-58`, `ciao/config.py:22-47`.
- Confirm that the plan's status still matches the repository.
Exit evidence: the plan records the next concrete action and any blocker.

### C1. Ground the plan (done in this doc)
- Evidence above cites observed files/lines; assumptions marked.
Exit evidence: reviewer can verify current-state claims without chat history.

### C2. Set direction (done in this doc)
- Recommended order + D1-D6 set; alternatives rejected above.
Exit evidence: one executable direction, not a menu.

### C3. Build the review artifact
- Write this Markdown plan; write `docs/plans/DOCS_README_REVAMP_PLAN.html` via `html-artifact` rules (one file, inline style/script, viewport meta, dark mode, ~420px first, <2MB, no fetch).
- Keep Markdown and HTML aligned on names/diagrams/status IDs.
Exit evidence: `file_surface` called for Markdown + HTML; both pass format checks.

### C4. Review the artifact
- Inspect Markdown pinned; inspect HTML narrow + wide; remove filler/invented paths.
Exit evidence: first screen of HTML makes the new README understandable without the chat.

### C5. Get approval
- Surface plan + name touched areas: `README.md`, `docs/ARCHITECTURE.md`, `docs/diagrams/*.svg` (new), `docs/screenshots/pwa-overview.png` (new).
- Capture comments in F-01..F-04; resolve Q-01..Q-04 or leave open with default+owner.
- Mark plan `approved` only after user says so.
Exit evidence: plan states approved vs deferred.

### C6. Implement
- Re-read approved plan; edit `README.md` order + new sections; add ARCHITECTURE anchor; create `docs/diagrams/` SVGs; add screenshot placeholder then real capture; keep file:line citations.
Exit evidence: every planned doc change has a commit or explicit deferral.

### C7. Verify
- `pytest tests/test_architecture_doc.py tests/test_pwa_api_docs.py` (doc index gates) + `cd web && npm run build` (if touched) + visual check of README rendered on GitHub + HTML companion at 420px.
Exit evidence: verified vs assumed clearly marked.

### C8. Close or hand off
- Set final status `complete`/`deferred`/`blocked`; record verified commit; leave next action only if work remains.
Exit evidence: another model can tell continue vs stop.

## Verification and rollout

- Gates: `pytest tests/test_architecture_doc.py` (every `ciao/*.py` indexed in `docs/ARCHITECTURE.md`) and `pytest tests/` full run before claiming complete.
- Visual: open `README.md` on GitHub (mobile + desktop) + the HTML companion; confirm diagrams legible at 420px, screenshot alt text present, no broken relative links.
- Rollout: docs-only `develop` PR; no migration; screenshots/diagrams are additive so roll-forward is safe. Mention in `CHANGELOG.md` under docs.

