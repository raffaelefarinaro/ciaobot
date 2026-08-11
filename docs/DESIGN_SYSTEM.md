# Ciaobot PWA design system

Status: adopted for new work, migration in progress
Written: 2026-08-11
Scope: `web/` (the PWA). Not the desktop shell or the Python app.

This document is the contract. When adding or changing UI in `web/`, follow it.
When it and an existing component disagree, the component is wrong — but do not
rewrite unrelated code to fix that; note it and move on.

Companion docs: `docs/plans/HOME_LANES_REDESIGN_PLAN.md` (the first surface built
on this system).

---

## 1. Why this exists

The app already has a good token layer and a global primitive layer. The problem
is that neither is *binding*: every one of the 30 components in
`web/src/components/` uses `<style scoped>`, and a large fraction re-specifies
values the tokens already define. Measured on 2026-08-11:

| Signal | Off-system | On-system | Verdict |
|---|---|---|---|
| `border-radius` | 188 hardcoded px | 108 via `--radius*` | 63% off-system |
| `font-size` | 191 hardcoded px | 264 via `--text-*` | 42% off-system |
| Hex colours in scoped styles | 106 | — | all off-system |
| Distinct button class names in markup | 47 | — | no shared vocabulary |
| Components referencing `.theme-light` | 4 of 30 | — | see §5 |

Two of those rows are functional bugs, not taste:

- **Hardcoded `font-size` breaks the font-scale setting.** The type tokens are
  `calc(11px * var(--font-scale))`, driven by Settings → Appearance. A hardcoded
  `font-size: 12px` does not scale, so a user who raises the scale gets a UI that
  grows unevenly. 191 places currently behave that way.
- **Hardcoded hex breaks the light theme and the workspace hue.** `:root.theme-light`
  redefines tokens, not literals, and `data-workspace-color` redefines `--accent`.
  A literal `#4caf50` responds to neither.

So the system's job is not to invent a look. The look exists. Its job is to make
the existing look *reachable* so components stop reinventing it.

---

## 2. The four layers

Work at the highest layer that can express what you need. Never reach past a
layer to hardcode what the layer below provides.

```
L3  Patterns    compositions with rules      lanes, action dock, settings card list
L2  Components  own a meaning, not a look    ChatSignals, TierLabel, CountTile
L1  Primitives  global, unscoped, in App.vue .btn-small, .card, .badge, inputs
L0  Tokens      App.vue :root                colour, type, space, radius, motion
```

### L0 — Tokens (`web/src/App.vue`, the `:root` block)

The single source of raw values. Already complete; do not add near-duplicates.

- Surface: `--bg`, `--bg2`, `--bg3`, `--bg-elev`
- Text: `--fg`, `--fg2`, `--fg3`
- Accent: `--accent`, `--accent-strong`, `--accent2`
- Edges: `--border`, `--border-strong`
- Status: `--success`, `--warning`, `--error`
- Geometry: `--radius`, `--radius-sm`, `--radius-lg`, `--touch`, `--space-1…6`
- Type: `--font-mono`, `--font-sans`, `--text-xs|sm|base|lg`, `--font-scale`
- Motion: `--ease`

Overridden in two places, both already wired: `:root.theme-light` for the light
theme, and `[data-workspace-color="…"]` for the five workspace accents in
`web/src/lib/workspaceColors.ts`.

**Rule L0.1** — No hex, rgb, or hsl literal in any component style. If a value is
missing from the tokens, add a token.
**Rule L0.2** — No hardcoded `font-size`. Use `--text-*`, or `calc(N * var(--font-scale))`
if you genuinely need an off-scale size.
**Rule L0.3** — No hardcoded `border-radius` or spacing. Use `--radius*` and `--space-*`.
**Rule L0.4** — Do not write a token fallback that differs from the token's real
value. `var(--warning, #b7791f)` in `SettingsView.vue` is wrong twice over: the
token is `#ff9800`, and the fallback can never fire.

### L1 — Primitives (`web/src/App.vue`, the global `<style>` block)

`App.vue`'s style block is **not scoped**, so it is the app's global sheet. This
is where shared primitives live. `.btn-small`, `.card` and friends are already
here.

**Rule L1.1 — one name, one look.** A primitive may not be re-specified by a
container. These currently violate it and should collapse into primitive
variants:

```
.critique-picker-header .btn-small     SettingsView.vue:5808
.settings-card-header-actions .btn-small        :6084
.asset-actions .btn-small                       :6200
.workspace-actions .btn-small                   :6384, :6569
.font-scale-row .btn-small                      :6760
```

If a container needs a different button, it needs a *variant*
(`.btn-small.btn-small--compact`), declared once, globally.

**Rule L1.2** — 47 button class names is not a vocabulary. New buttons use a
primitive plus a modifier. The sanctioned set:

| Class | Use |
|---|---|
| `.btn-primary` | the one affirmative action in a view |
| `.btn-small` | default action button; `.danger` modifier for destructive |
| `.btn-icon` | icon-only, must carry `aria-label` and meet `--touch` |
| `.btn-chip` | inline toggle inside a row or header |

Anything else (`.fv-btn-sm`, `.pfp-btn-sm`, `.csv-tool-btn`, `.fix-btn`, …) is a
migration target, not a precedent. Do not add to that list.

**Rule L1.3** — Interactive elements meet `--touch` (44px) on touch devices, per
`CLAUDE.md`. Current known failure: the queued-message glyph buttons in
`ChatPanel.vue` (`▲ ▼ ✎ ✓ ✕ ×`).

### L2 — Components

A component at this layer owns a **meaning**, and every surface that expresses
that meaning must use it. This is the layer that makes the system "used
everywhere" rather than merely documented.

`ChatSignals.vue` is the first and the model to copy: it owns "what state is this
chat in", takes a `density` prop, and every surface showing chat state renders it
instead of its own marks.

**Rule L2.1** — If two surfaces render the same fact, that fact gets a component.
No exceptions for "it's only a dot".

Existing and wanted:

| Component | Owns | Status |
|---|---|---|
| `ChatSignals` | chat state marks (needs-you / working / unread / loop / quiet) | **exists, all 4 call sites** |
| `AppIcon` | the SVG icon set (see Rule S4) | **exists** |
| `TierLabel` | a priority tier heading with its hairline rule | wanted |
| `CountTile` | one number + label, urgency-ordered (project stats, lane summary) | wanted — pattern now used in 2 places |
| `KeyBadge` | the `1`–`9` workspace shortcut badge | wanted — currently drawn in 3 places |
| `SectionCard` | titled section with optional actions slot | wanted — see §4 |
| `EmptyNote` | the `// nothing here` convention | wanted — string built ad hoc in ~8 places |

### L3 — Patterns

Named compositions with rules, documented where they live. Current: workspace
lanes and priority tiers (`docs/plans/HOME_LANES_REDESIGN_PLAN.md`). Proposed:
the chat context bar and action dock.

---

## 3. The semantic rules

These are what make the system coherent rather than merely tidy. They come from
the home-screen work and apply everywhere.

**Rule S1 — Fill is state. Hue is ownership.**
A solid fill means, and only ever means, *this needs the user*. Because that one
property is reserved, hue is free to mean "which workspace" at every density and
any workspace count. Never spend a filled treatment on something non-blocking —
that includes new-chat buttons, which is why they are ghost/dashed now.

**Rule S2 — Density is the priority channel.**
Urgency is how much room a thing gets: full card → dimmed card → single row →
collapsed disclosure. Not colour, not weight alone, not a badge. This is what
survives 60 items, colour blindness, and the light theme.

**Rule S3 — Semantic colour is not the accent.**
`--success` / `--warning` / `--error` express outcome. `--accent` expresses
workspace identity. A workspace whose accent is pink must not make errors
invisible; never use `--accent` to mean "bad" or `--error` to mean "Work".

**Rule S4 — Emoji are not icons.**
Emoji cannot inherit `currentColor`, so they can never carry a workspace hue or
dim with a tier — they are the one glyph type that cannot join the system. They
also render differently per platform. Use `AppIcon.vue`, which holds the set in
the house style: 24 viewBox, `stroke-width: 2`, `stroke-linecap: square`, miter
joins, `currentColor`. Add a named icon there rather than inlining an SVG.
Text glyphs that *do* inherit colour (`↻` `↳` `▾` `›`) are fine and are already
part of the signal spec.

**Rule S8 — Background work that produces a decision needs two surfaces.**
A transient *working* mark where the work originates, and a persistent
*needs you* count where its output waits. One without the other either hides
that anything happened, or hides that something is now pending. See
`docs/plans/MEMORY_VISIBILITY_PLAN.md` for the worked example — the insights and
memory-proposal pipeline currently has neither.

**Rule S5 — Counts are real counts.**
`chatUnread()` returns 0 or 1 by design. Never render it as a digit; a per-chat
badge would always read `1`. Chat-level unread is carried by **title weight**
(`--fg` at 600 vs `--fg2` at 400). Digits belong to `projectUnread` and
`workspaceUnread`, which sum those binary values into real numbers.

**Rule S6 — Absence is reported, not implied.**
An empty tier or lane says `// nothing needs you here` rather than vanishing. A
shorter list is not an answer.

**Rule S7 — One meaning per key.**
Unmodified `1`–`9` mean `switchWorkspace`, in every view. Do not fork a shortcut
per screen; give the screen a visible reaction instead.

---

## 4. The settings page

Settings is, unexpectedly, **the most internally consistent surface in the app**.
28 `.card` blocks and 25 `.section-title` labels, one dialect used throughout. It
is the closest thing to an existing pattern library and should be the basis for
`SectionCard`, not a rewrite target.

Three real problems:

### 4a. Three dialects for "titled section"

The same concept is structured three ways:

| Surface | Structure | Count |
|---|---|---|
| `SettingsView.vue` | `.card` > `.section-title` (a `<p>`) | 30 |
| `ProjectView.vue` | `.card` > `.card-header` > `<h3>` + `.card-actions` | 6 |
| `ChatPanel.vue` | `.label-eyebrow` inside a popup section | 3 |

Settings' version has no heading element at all — a `<p>` styled as a title,
which costs the document its outline and screen-reader navigation. ProjectView's
is semantically right.

**Resolution** — one `SectionCard` primitive: a real `<h3>` visually styled as
the current eyebrow label, plus an `actions` slot. Migrate ProjectView first (6
sites), then settings incrementally per tab. Do not big-bang 28 cards.

### 4b. It is a 7,098-line monolith

`SettingsView.vue` is the largest file in the app: a 2,144-line template and
2,900 lines of scoped CSS covering seven routed tabs. The routes already exist
(`/settings/providers`, `/settings/workspaces`, `/settings/models`,
`/settings/context`, `/settings/skills`, `/settings/automations`), so the tabs
are separable with no router change — each becomes its own SFC. This is also
where most of the L1.1 violations live, and splitting makes them visible.

Not urgent, but no *new* tab should be added to this file.

### 4c. Off-system values

`#4caf50` and `#2ea44f` (GitHub's green) are hardcoded, so neither flips in the
light theme; `--success` exists and is the correct token. Plus the dead
`var(--warning, #b7791f)` fallback from Rule L0.4.

Also: the sidebar's settings nav labels `/settings/skills` as **assets**. Route
and label should agree — rename the route or the label.

---

## 5. Theming

`:root.theme-light` in `App.vue` redefines tokens. Components should therefore
need **no** theme-specific CSS: style through tokens and both themes work.

Only 4 of 30 components mention `.theme-light`, and that is the *correct* number
— the other 26 rely on tokens, which is the intended design. The problem is not
the missing 26; it is the 106 hex literals that silently opt out of theming.

**Rule T1** — Do not add `.theme-light` overrides to a component. If a component
needs one, a token is missing or a literal is being used.
**Rule T2** — Before merging UI work, view it in both themes. A pink-accent
workspace and a light theme together are the case that breaks.

---

## 6. Live migration state

Read this before assuming a component is on-system.

`41c1506 feat(pwa): add workspace lanes to home` landed steps 1–3 of the home
plan: `ChatSignals.vue`, `lib/relativeTime.ts`, `lib/homeLanes.ts`, lanes and
tiers in `HomeRecentChats.vue`, `ChatSignals` adopted in `ProjectSidebar.vue`,
`workspaceNeedsInput` in the store. 145 tests pass across 25 files.

A follow-up commit closed the two gaps that commit left open — both were exactly
the drift this document exists to prevent, and they are recorded here because the
shape recurs:

1. **`ChatSignals` was adopted in 2 of 4 call sites.** `ProjectView.vue` kept its
   own `spinner-dot` / `needs-input-badge` / `badge` chain (and printed
   `chatUnread` as a digit, violating Rule S5); `ChatPanel.vue` rendered the chain
   as *prose* in the subchat banner. Both now use the component. A half-adopted L2
   component is worse than none — the same chat renders two ways depending on the
   view.
2. **Two relative-time dialects coexisted.** `lib/relativeTime.ts` was created
   compact while `ProjectView.vue` kept a private `formatRelative`. The shared
   helper now takes `{ suffix, absoluteAfterDays }` so prose and file-listing
   forms come from one implementation, and the private copy is gone.

**Current state:** all four `ChatSignals` call sites migrated; one
`formatRelative`. `ChatPanel.vue` and `ProjectView.vue` are otherwise still
off-system (see §3 Rule S4 on emoji, and the chat-page proposals) — the state
chain is fixed, the rest of those views is not.

### Test suite: Node floor (resolved)

There was never a broken dependency here, and an earlier draft of this document
said there was. The real cause: **jsdom 29 requires Node
`^20.19.0 || ^22.13.0 || >=24.0.0`** — its CJS dependency chain does `require()`
on an ESM module, and `require(esm)` only landed in Node 20.19.0. Nothing in the
repo declared that floor, so on an older local Node every jsdom test file failed
to start its worker while vitest still printed
`Test Files 25 passed` for the files that *did* run. 17 of 42 files silently
never executed, and the summary looked green.

CI was always fine — `.github/workflows/ci.yml` uses Node 22.

Fixed by declaring the constraint and making violations loud:

- `engines` in `web/package.json`, mirroring jsdom's supported range
- `.nvmrc` pinning 22 to match CI
- `web/scripts/check-node.mjs`, run as the first step of `npm test`, which exits 1
  with an explanation rather than letting a partial run report success

On a supported Node the full suite is 42 files / 345 tests green.

**Rule V1** — Never trust a green vitest summary without checking the file count
against `find web/src -name '*.test.ts' | wc -l`. A worker that fails to start is
reported as an error, not a failure, and does not fail the file count.

---

## 7. Checklist for any UI change

- [ ] No hex/rgb/hsl literal; no hardcoded `font-size`, `border-radius`, or spacing
- [ ] No container re-specifying a primitive (Rule L1.1)
- [ ] Reused an L2 component if the fact it owns is being shown
- [ ] Filled treatment used only for "needs the user" (Rule S1)
- [ ] Semantic colour kept distinct from accent (Rule S3)
- [ ] No new emoji as an icon (Rule S4)
- [ ] Counts are real counts (Rule S5)
- [ ] Empty states say so (Rule S6)
- [ ] Verified in dark **and** light theme, and at a raised font scale
- [ ] Touch targets meet `--touch`
- [ ] `npm run test`, `npm run build`, `npm run lint` pass from `web/`
