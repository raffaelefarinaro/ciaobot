# Operator actions: the home-screen housekeeping strip

Status: **proposed**
Written: 2026-08-19
Scope: `ciao/operator_actions.py` (new), `ciao/vault_migration.py`,
`ciao/os_audit.py`, `ciao/web/routes_api.py`, `ciao/web/app.py`,
`web/src/components/HousekeepingStrip.vue` (new),
`web/src/stores/housekeeping.ts` (new), `web/src/components/ChatLayout.vue`

Visual reference: <https://claude.ai/code/artifact/1b64fd79-9663-4bb5-bd3a-51421f6c51b4>

Turns conditions Ciaobot already detects about its own install into tiles on the
home screen that either run the fix, open a chat that talks it through, or both —
and clear themselves by re-detection rather than by anyone declaring success.

Related: [`VAULT_VOCABULARY_PLAN.md`](VAULT_VOCABULARY_PLAN.md) — supplies the
first non-update action, and the migration this plan gives a trigger to.
[`HOME_LANES_REDESIGN_PLAN.md`](HOME_LANES_REDESIGN_PLAN.md) — owns the home
screen's signal vocabulary, which this strip must not spend twice.
[`WORKSPACE_ISOLATION_PLAN.md`](WORKSPACE_ISOLATION_PLAN.md) — **depends on this
one.** Its per-workspace-agent-root migration is a hard cut with no per-install
opt-out, and this strip is where its repair actions surface, so steps 1–4 of the
Order below are a prerequisite for that cut. It adds five detectors
(`workspace-unmigrated`, `workspace-root-missing`, `workspace-assets-stale`,
`skill-triage-pending`, `legacy-env-ignored`) and sharpens open question 4.

---

## Problem

Ciaobot already knows several things the operator has to do, and tells nobody who
is looking.

**`audit_upgrade_notices()`** (`ciao/os_audit.py:848`) exists precisely for this.
Its docstring states the goal outright — "A release note only works if someone
reads it and then remembers to act. These are the same conditions stated as facts
about *this* machine, so the PWA can show them and the operator can act without
consulting a changelog" — and each notice carries a `remedy` already written as
instructions for an agent to follow. The report is served at
`/api/agent-assets/audit` (`ciao/web/app.py:291`). **Nothing under `web/src/`
reads the `upgrade_notices` key.** The only way to see it is to run `ciao
os-audit` in a terminal, which is exactly the audience that did not need telling.

**`ciao/vault_migration.py`** is finished and wired to nothing. `migrate_if_needed()`
documents itself as "Called from the install/upgrade path"; no caller exists.
`audit_upgrade_notices` was already given a `runtime_dir` parameter for it and
does not yet read it. So the vocabulary check that `vault-lint` now enforces will
ship against unmigrated vaults, which is the failure its own module docstring
predicts: "an upgrade that shipped the check without this would hand every
existing install a permanently unhealthy audit."

**Updates** are detected (`ciao/package_version.py:142`) and one click away
(`POST /api/package/update`), but the home screen's only trace of them is a 6px
dot on the settings nav icon (`web/src/components/ProjectSidebar.vue:109`) plus a
once-per-version toast. Both are dismissable-by-ignoring, and both require the
user to know that Settings is where updates live.

The common shape: **a detected condition, a known remedy, and no surface where
the person who has to act will encounter it.** Settings already proves the
interaction — a health card with a `Fix issues` button
(`POST /api/workspace-health/fix`) beside a `Fix issues in chat` button that
seeds a chat with a prompt (`SettingsView.vue:4599`). What is missing is the
generalisation of that pattern, and its promotion to the screen people open.

### Non-goals

- Not a health dashboard. Settings keeps the exhaustive audit; this strip carries
  only the subset that is *actionable, upgrade-shaped, and clearable*.
- Not a task list. The user cannot add to it, reorder it, or check things off.
- Not chat state. "A chat is waiting for you" already has a home in the lanes.

---

## Design

### 1. The detector contract

One module, `ciao/operator_actions.py`, holding a registry of detectors. A
detector is a pure function from a context to zero or more actions.

```python
@dataclass(frozen=True)
class OperatorAction:
    id: str            # stable, scope-suffixed: "vault-location:work"
    kind: str          # detector slug, unscoped: "vault-location"
    severity: int      # sort key only; not rendered
    title: str         # the condition as a fact about this machine
    detail: str        # what runs unattended vs. what needs a decision
    glyph: str         # one character
    workspace: str     # "" for install-wide
    run_label: str = ""      # empty means: nothing safe to run
    chat_label: str = ""     # empty means: nothing to discuss
    chat_prompt: str = ""    # the seeded prompt, verbatim
```

Four requirements, all enforceable in tests:

1. **It must be able to reach zero.** A condition the user may legitimately
   decline is not an action. The codebase already argues this:
   `superseded_state_candidates` is deliberately excluded from
   `memory_actionable_count` because "a finding that can never be cleared would
   hold the audit at needs_attention until people stop reading it"
   (`ciao/os_audit.py`). A tile is that failure mode with a button on it.
2. **It must be cheap.** Detection runs on a poll from the home screen. No
   `scan_vault()`, no `audit_memory()`, no directory walks over user content —
   only version comparisons, receipt reads, and `Path.is_dir()` checks. Expensive
   work happens when the button is pressed, never to decide whether to draw the
   button.
3. **It must be idempotent under re-detection.** Running the detector twice with
   nothing changed produces byte-identical actions, including `id`.
4. **It must offer at least one of `run_label` / `chat_prompt`.** An action with
   neither is a notice, and notices belong in the Settings audit.

`detect_actions(context) -> list[OperatorAction]` runs the registry, sorts by
`(severity, id)`, and `logger.info`s the count. No truncation: if the list
routinely exceeds five, that is the signal this should have been a page, and
silently dropping the tail would hide it.

### 2. The three launch detectors

| kind | detects from | run | chat |
|---|---|---|---|
| `package-update` | `package_version.check_status()` — already cached | install the release | — |
| `vault-vocabulary` | the migration receipt's counts | rename the aliased types | decide the unresolved ones |
| `vault-location` | `audit_upgrade_notices()`, refactored to build `OperatorAction`s | — | the existing `remedy` prose |

`vault-location` is the important refactor: the `remedy` string moves *into* the
registry, and `audit_upgrade_notices` renders its notices *from* it. One string,
two surfaces, no drift between what the CLI advises and what the tile does.

### 3. Making `vault-vocabulary` cheap: survey, then apply

The detector cannot scan the vault, but the tile needs counts ("12 renames, 3
decisions"). Resolve it with a two-stage receipt, which `vault_migration.py`
almost supports already:

- **At boot, off the request path**, call `migrate_if_needed(..., apply=False)`.
  It writes a receipt with `status: "surveyed"` plus the `planned` and
  `unresolved` lists it already computes.
- **The detector reads that JSON only.** `len(planned)` and `len(unresolved)`
  give the tile its counts for free.
- **Pressing the button** calls `migrate_vault_vocabulary(apply=True)`, which
  rewrites frontmatter and writes `status: "migrated"` with the residual
  `unresolved`.
- **Residual `unresolved` becomes a second, chat-only action** — a different
  condition with a different remedy, so a different tile.

Required change to `vault_migration.py`: `migrate_if_needed` currently skips
whenever a receipt exists (`read_receipt(...) is not None`). It must skip only on
`status == "migrated"`, or a survey receipt permanently blocks the real
migration. Everything else in that module stands as written — it already applies
only aliased types and reports the rest, which is exactly the two-button split
the tile renders.

### 4. Endpoints

```
GET  /api/housekeeping                  → {"actions": [...]}
POST /api/housekeeping/{action_id}/run  → {"ok": bool, "summary": str, "actions": [...]}
```

`GET` is the detector pass. `POST .../run` dispatches through the registry,
performs the mechanical work, then **re-runs detection and returns the new list in
the same response** — so the client cannot render a stale strip between acting
and verifying. `package-update` delegates to the existing update path rather than
reimplementing it.

There is deliberately no `POST .../chat`: `chat_prompt` ships in the `GET`
payload and the client creates the chat with the flow it already has. That keeps
chat creation in one place, client-side, where `fixIssuesInChat` already lives.

### 5. Validation: the tile is a view, not a record

This is the part that decides whether the feature is trustworthy.

1. **Act** — button, or seeded chat.
2. **Re-detect** — the strip re-polls. For `run`, the response carries it. For
   the chat path, the seeded prompt ends by telling the agent to re-run the
   relevant check (`ciao vault-lint`, `ciao os-audit --json`) and report; the
   strip re-polls on interval and on window focus, so coming back to the home
   screen shows the truth regardless of what the agent claimed.
3. **Clear** — the action is gone from the detector output, so the tile is gone
   from the strip. A transient emerald confirmation row fades after ~4s.

No `done` flag exists anywhere. Nothing an agent writes can remove a tile except
the underlying condition changing.

**A failed action must stay visible as failed.** If a `run` returns and the same
`id` is still detected, the tile returns with its detail replaced by the failure
(`migrate_vault_vocabulary` already returns a `failed` list with per-file
reasons). Never silently re-offer the same button as though nothing happened.

### 6. Where it renders

`ChatLayout.vue` builds the home screen twice — desktop (~line 78) and mobile
(~line 165) — so `<HousekeepingStrip />` goes immediately before
`<HomeRecentChats>` in both, inside the existing `--home-max` column.

Order on the page: mascot status line → housekeeping strip → lanes. The strip
sits above the lanes because it is about the machine the lanes run on, and below
the status line because the status line is about the user's own work, which
outranks maintenance.

**When there are no actions the component renders nothing** — no wrapper, no
heading, no "all clear" row. Permanent furniture is what taught people to ignore
the settings dot.

### 7. Colour: attribution by label, not hue

The workspace palette has taken pink, cyan, amber, emerald and violet
(`web/src/App.vue:353-372`), and the home-lanes design record's governing rule is
one meaning per signal. The strip therefore gets **one hue, amber
(`--warning`)**, and never the workspace accent:

| signal | means |
|---|---|
| amber 3px left rule + glyph | the install or a vault needs an operator action |
| neutral rule, spinner glyph | running or re-checking; nothing for you to do |
| emerald rule, transient | just cleared; fades |

A per-workspace action names its workspace **in the title text** ("The Personal
vault is not in its standard folder"), because the hue channel is spent.

Known collision: a workspace whose colour is `amber` shares the strip's hue.
Position, shape and the `housekeeping` label still separate them; if that proves
too subtle in use, drop the strip to a neutral register with amber only on the
glyph.

### 8. Store and polling

New store `web/src/stores/housekeeping.ts` — `projects.ts` is already 196KB and
this is self-contained. Mirror `checkPackageStatus`'s pattern
(`projects.ts:788`): best-effort, failures leave the strip empty rather than
erroring. Refresh on bootstrap, on the existing `UPDATE_CHECK_INTERVAL_MS`
interval, on window focus, and after every `run`.

Once the strip carries the update action, `checkPackageStatus`'s once-per-version
toast is redundant noise for anyone on the home screen. Keep the toast (it fires
inside a chat too) but delete the settings nav dot only if the strip proves it
covers the case — that is a follow-up, not part of this change.

---

## Traps

1. **Do not poll `run_os_audit` from the home screen.** It calls `audit_memory`,
   `_vault_audit` and `audit_job_runs`, each of which walks user content. The
   detectors are a separate, cheap path; if a detector ever needs the audit,
   cache it the way `_cached_update_hint` does — "without ever blocking"
   (`ciao/web/routes_api.py:5441`).
2. **A detector that cannot reach zero poisons the strip.** See the contract
   above. Reviewers should reject any new detector without an argued path to
   zero.
3. **No snooze in v1.** It reintroduces exactly the stored per-action state this
   design removes. If it becomes necessary, make it client-side with a TTL, and
   never for `package-update`.
4. **The empty state is a hard requirement, not a nicety.** Test it explicitly.
5. **Both home templates.** A strip added only to the desktop branch of
   `ChatLayout.vue` is invisible on a phone, and the existing tests will not
   notice.
6. **Cross-workspace by nature.** `audit_upgrade_notices` iterates every
   registered workspace, so the strip shows actions for workspaces other than the
   active one. That is correct — an update is not per-workspace and a misplaced
   vault does not fix itself while you are elsewhere — but it means the strip must
   never be filtered by `activeWorkspace`.
7. **Survey receipt must not block migration.** The `migrate_if_needed` skip
   condition is the one behavioural change to a finished module; get it under
   test first.

---

## Order

1. `ciao/operator_actions.py`: dataclass, registry, `detect_actions`, plus the
   `package-update` detector. Unit-tested against a fake context.
2. `vault_migration.py`: receipt `status`, the survey pass, the boot call. Then
   the `vault-vocabulary` detector reading only the receipt.
3. Refactor `audit_upgrade_notices` to build actions from the registry and render
   its notices from them — one `remedy` string, two surfaces.
4. Endpoints + registry dispatch for `run`.
5. `housekeeping.ts`, `HousekeepingStrip.vue`, insertion into both `ChatLayout`
   branches.
6. Follow-up, separately: consider graduating the Settings workspace-health
   checks that are mechanically fixable into detectors, so `Fix issues` and the
   strip stop being two implementations of one idea.

---

## Tests

Backend — `tests/test_operator_actions.py`:

- empty registry output on a healthy fake install
- each detector fires on its condition and only on its condition
- `id` stability across two identical passes (contract 3)
- every registered action offers `run_label` or `chat_prompt` (contract 4)
- a detector raising does not sink the pass — it is logged and skipped
- no detector touches the vault: assert via a `scan_vault` spy that it is never
  called during `detect_actions`

`tests/test_vault_migration.py` (extend):

- survey receipt does not block a later `apply=True` run
- `status` transitions `surveyed → migrated`
- residual `unresolved` after apply produces the chat-only action

`tests/test_web_housekeeping.py`:

- `GET` shape; `POST .../run` returns the re-detected list
- a `run` whose condition persists returns the action with failure detail
- unknown `action_id` → 404, not 500

Frontend — `web/src/components/__tests__/HousekeepingStrip.test.ts`:

- renders nothing at all with zero actions (no wrapper element in the DOM)
- one/two tiles; run-only, chat-only, and both-buttons variants
- pressing run disables the button, shows the progress row, and re-renders from
  the response
- the chat button creates a chat with the prompt verbatim and navigates to it

`web/src/components/__tests__/ChatLayout.test.ts` (extend): the strip is present
in both the desktop and mobile home branches, and above the lanes in each.

---

## Open questions

1. **The label.** `housekeeping` is the mockup's choice — plain, low-alarm, and
   distinct from the lanes' "needs you". `maintenance` and `to do` were the
   alternatives; `to do` was rejected as it reads like the user's own task list.
2. **Does Settings keep its health card?** Yes for now — it covers findings the
   strip deliberately excludes. Worth revisiting once step 6 lands, or the two
   will drift.
3. **Should a chat-path action mark itself in-progress?** A tile whose chat is
   open still reads as untouched, which may prompt a second chat. A "discussed in
   *chat title*" line derived from a live chat lookup (not a stored flag) would
   fix it without breaking the derived-view rule.
4. **First-run interaction. Resolved 2026-08-19: tiles before the first message
   are fine and correct.** On a brand-new install the vault is created conformant
   and there is no update, so the strip is empty. An *adopted* existing vault
   (`CIAO_VAULT_MODE=existing`) may produce two tiles before the user has sent a
   single message, and that is the intended reading: the conditions are true about
   the machine, and stating them on arrival is the whole point of the strip. A
   detected condition is not withheld for looking unwelcoming.

   Two consequences for implementation rather than for design:

   - **No first-run suppression.** Do not add a grace period, a "seen once"
     threshold, or a message-count gate. Any of those reintroduces the stored
     per-action state that §5's derived-view rule removes, and the tile would then
     be lying about the machine.
   - **The copy carries the burden instead.** Because a tile may be the first thing
     a new user sees, each `title` must read as a fact plus a next step, never as an
     error. That is already the contract (`title` = the condition as a fact about
     this machine); this makes it load-bearing for onboarding, so it is worth
     reviewing the launch detectors' strings with a first-run reader in mind.

   The same answer covers the post-migration case that
   [`WORKSPACE_ISOLATION_PLAN.md`](WORKSPACE_ISOLATION_PLAN.md) introduces, where
   `skill-triage-pending` fires on every install that had hand-written skills.
