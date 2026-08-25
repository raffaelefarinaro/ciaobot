# Workspace isolation: per-workspace agent roots

Status: **discussion + plan, nothing implemented**
Written: 2026-08-19
Scope of the eventual change: `ciao/config.py`, `ciao/provider_service.py`,
`ciao/web/project_chats.py`, `ciao/control_plane.py`,
`ciao/context/entity_tagger.py`, `ciao/os_audit.py`, `ciao/memory_tool.py`,
`ciao/sync_skills.py`, `ciao/transcripts.py`, `ciao/insights.py`,
`ciao/subagent_tracking.py`, `ciao/schedules.py`, plus a new re-root migration.

Related: [`PER_WORKSPACE_MEMORY_CURATION_PLAN.md`](PER_WORKSPACE_MEMORY_CURATION_PLAN.md)
(step 0 "Blocker: the bounded memory regions are global" is the question this
plan answers), [`VAULT_VOCABULARY_PLAN.md`](VAULT_VOCABULARY_PLAN.md), and
[`OPERATOR_ACTIONS_PLAN.md`](OPERATOR_ACTIONS_PLAN.md) — **which owns the surface
this plan's repair mechanism ships on.** See [§11](#11-the-repair-mechanism);
that plan is a prerequisite for the cut, not an optional companion.

Deliberately **not** related to [`OKF_ADOPTION_PLAN.md`](OKF_ADOPTION_PLAN.md);
see [Do not bundle with OKF](#do-not-bundle-with-okf).

---

## 1. Verdict up front

The diagnosis is right: five separate bugs, one disease — a global artifact with
per-workspace consumers. Every anchor in the handoff was re-checked against the
working tree and every one holds.

The proposed lever is **the highest-value single change available, and the claim
made for it is oversold.** Two corrections matter enough to change the plan:

1. **Per-workspace cwd fixes about half the disease, not all of it.** It is a
   complete structural fix for the four artifacts the provider CLI discovers
   itself. It does nothing for the artifacts *Ciaobot* reads and writes on the
   agent's behalf — the entity index, `memory_status`/`memory_update`,
   `os_audit`, the hygiene report chat. Those stay application logic.
   `_entity_visible_in_workspace` does not get deleted by this change; its
   jurisdiction shrinks.

2. **cwd is session identity, and the handoff does not mention it.** This is the
   cost that would ship a broken app if it is not sequenced first. Details in
   [§2.2](#22-the-cost-the-handoff-missed-cwd-is-session-identity).

Two structural recommendations follow from those:

- **Do not repurpose `workspace_root`.** Add a second, differently-named
  concept. There are 375 `workspace_root` references across ~40 modules and most
  of them correctly mean *the install root*. Changing what the existing name
  means is exactly how the app ends up half-rooted.
- **One piece of the value needs no migration.** Per-workspace bounded memory
  (step 4 of [§9](#9-sequencing)) closes the leak that caused the observed
  damage while every install stays on today's layout. Everything else —
  per-workspace skills, commands, agents, and above all MCP servers — requires
  per-root cwd. See [§10](#10-where-to-stop).

A third finding, beyond the handoff's five-row table: **there is a sixth leak,
and it is the sharpest.** `.mcp.json` is workspace-root-level, so every MCP
server is reachable from every workspace, scoped only by the `disallowed_tools`
**denylist** — fail-open by construction. See [§7.1](#71-mcpjson--already-in-not-a-choice-and-the-strongest-case-for-the-whole-change).

**Revisions, 2026-08-19.** Two decisions changed the shape of this plan after
the first draft:

1. **The shared layer is cut**, on evidence — see
   [§4](#4-why-there-is-no-shared-layer). Two layers, global and per-workspace,
   plus one non-workspace mirror source.
2. **Every deletable symbol is deleted and the shift is committed to**, rather
   than offered as an opt-in with a compatibility path. Those are the same
   decision mechanically: deleting the fallbacks makes an unrooted install
   fail-open, so the migration becomes mandatory and atomic
   ([§9.1](#91-why-deletion-forces-a-mandatory-migration)). What makes that
   survivable is the repair mechanism in [§11](#11-the-repair-mechanism), not a
   second code path.

---

## 2. Is per-workspace cwd the right lever?

### 2.1 What it does and does not reach

The seam is one line, as claimed. `ciao/provider_service.py` `_ensure_provider`:

```python
self._provider = factory(self._config.workspace_root, config=self._config)
```

and every provider spawns at that path (`claude.py` `cwd=str(self.workspace_root)`,
`codex.py` `cwd=self.workspace_root`, `opencode.py` likewise). `ProviderService`
is constructed per chat in `project_chats._get_provider(chat_id)`, which holds
`chat`, and `chat.project_id` → `project.workspace`. So the call site already
knows the logical workspace and throws it away. Confirmed.

Also confirmed: **nothing in Ciaobot injects the bounded memory regions in the
normal configuration.** `claude.py` calls `system_prompt_payload("", ...)`;
`opencode.py` calls `system_prompt_payload("", ...)`; `codex.py` builds a memory
block only `if not guides_share_file`, and `sync_skills._ensure_linked_workspace_guides`
symlinks `AGENTS.md` → `CLAUDE.md`, so they normally *are* the same file and the
block stays empty. The delivery mechanism really is the CLI's own file read from
cwd.

So the leak table splits cleanly, and not the way the handoff implies:

| Leak | Fixed by per-workspace cwd? |
|---|---|
| `CLAUDE.md` regions **injected into every session** | **Yes, completely, zero code** — the CLI reads cwd |
| `.claude/skills`, `.claude/agents`, `.claude/commands` **discovered** globally | **Yes** — `setting_sources=["user","project","local"]`, project scope is cwd |
| `.mcp.json` server set — **credentialed authority, and the sixth leak, not in the original table** | **Yes** (see [§7](#7-per-workspace-settingsjson-mcp-and-gws-profiles) — this happens whether you plan it or not) |
| `CLAUDE.md` **managed** by `memory_status` / `memory_update` / `audit_memory` / `ciao memory-audit` / curation | **No.** All four resolve `config.workspace_root / "CLAUDE.md"` themselves |
| one entity index (`INDEX.md`) | **No.** Injected by Ciaobot at prompt-build time, not read from cwd |
| one hygiene report chat | **No.** That is schedule routing |
| one skill-proposal queue | **Already fixed** — per-workspace queues landed today |
| the skill catalog's *source of truth* (`skill_evolution._resolve_skills_roots`, `skills_inventory`) | **No.** Those read `CIAO_WORKSPACE/skills` directly |

Net: cwd structurally eliminates the two highest-volume, highest-damage leaks
and the one nobody has noticed yet (`.mcp.json`). It leaves four that still need
the workspace threaded through application logic.

**So: right lever, wrong slogan.** "The isolation becomes the filesystem instead
of application logic" is true only of the CLI-discovered surface. Ciaobot remains
a multi-tenant server. One process serves every workspace, `_safe_relative` still
guards writes, `_entity_visible_in_workspace` still guards reads until its inputs
are re-rooted. Design for that rather than expecting the checks to disappear —
believing they are gone is more dangerous than knowing they are still there.

The counter-case is still strong enough to do it: the leaks cwd *does* fix are
the ones that produced the observed damage, it fixes them for **all three
providers at once** with one argument at one call site, and it fixes them for
artifacts Ciaobot does not and should not parse (a user's own skill, a user's own
`.claude/settings.json`). No amount of application logic can scope those,
because Ciaobot never sees them.

### 2.2 The cost the handoff missed: cwd is session identity

`ciao/transcripts.py`:

```python
def _claude_projects_dir(workspace_root: Path) -> Path:
    """Derive the Claude Code session directory for a workspace."""
    # Claude Code encodes workspace path: /Users/me/ciao → -Users-me-ciao
    slug = str(workspace_root).replace("/", "-").lstrip("-")
```

Changing cwd changes the slug, which relocates `~/.claude/projects/<slug>/`.
Consequences, all verified:

- **Every live chat session becomes unresumable.** `claude.py`
  `_validated_resume_session` calls `get_session_info(session_id, directory=str(self.workspace_root))`,
  gets `None`, logs "stale locally", and **starts fresh**. Silent amnesia, not an
  error. Codex is the same shape via `read_thread(workspace, ...)`; opencode too.
- **Six call sites read session JSONL by that slug** and would silently read an
  empty directory: `insights.py:305`, `insights.py:1077`,
  `subagent_tracking.py:188`, `routes_api.py:687`, `project_chats.py`,
  `transcripts.py:820`. That breaks Session insights, trajectory capture, CLI
  transcript archiving, and the background-subagent running-count signal that
  `_await_schedule_subagents` polls — which means schedule archiving would
  decide against a half-complete state.
- **The change is not reversible per chat.** You cannot flip one chat to a new
  root to try it; that chat's history is stranded at the old slug.

None of this is fatal. All of it has to be sequenced *before* the re-root, and
the session loss has to be handled deliberately rather than discovered. See
[§8.2 step 7](#82-case-b--upgrade-of-an-existing-install).

### 2.3 Where the idea fits the existing grain

Two facts make this less invasive than it sounds:

- `WorkspaceConfig` already carries per-workspace `default_provider`,
  `default_model`, `disallowed_tools`, `gws_profile`, `color`. A `root` field is
  the same shape as what is already there.
- `config._resolve_vault_root` **already accepts an absolute `vault_root`**
  ("absolute — preserved setup/external roots and pinned legacy vaults"). The
  registry already tolerates a workspace whose data lives outside
  `workspace_root`. Per-workspace roots generalise a precedent rather than
  inventing one.
- `sync_workspace_skills(workspace: Path)`, `git_sync.sync_workspace(workspace)`,
  `os_audit.audit_setup(workspace_dir, vault_root, runtime_dir)` and
  `_ensure_linked_workspace_guides(workspace)` are **already parameterised by
  root**. Calling them N times is the whole change on that side.

### 2.4 The naming collision is the real hazard

`docs/ARCHITECTURE.md` uses "workspace" for two different things in adjacent
paragraphs: the filesystem install root (line 163: "A **workspace** is a
separate directory") and the logical workspace (line 174:
`memory-vault/<workspace>/`). `os_audit` uses `workspace_dir` for the *install*
root. `sync_skills` uses `workspace: Path` for the install root while
`schedules.py` uses `workspace: str` for the logical name.

This refactor is exactly where that collision turns into bugs. **Fix the
vocabulary in a docs-only PR before writing any code.** Proposal:

| Term | Means | Type |
|---|---|---|
| `workspace_root` | the install root — unchanged, still global | `Path` |
| `workspace` | the logical workspace name (`personal`, `work`) — unchanged | `str` |
| **`agent_root`** | the directory a provider CLI runs in for a given logical workspace | `Path` |

`agent_root` is the new concept, it names what it is (the root the agent sees),
and it collides with nothing. Everything below uses it.

---

## 3. Layer boundaries

**Two layers, not three** — the shared layer is cut
([§4](#4-why-there-is-no-shared-layer)). Global and per-workspace, with one
non-workspace mirror source. Corrections to the original table, in descending
order of importance:

**`.runtime/migration/` receipts must not be flatly global.** The vault-vocabulary
receipt (`vault_migration.read_receipt(runtime_root)`) is a claim about *one
vault*. With N vaults and one global receipt, migrating workspace A marks B
migrated, and B's non-canonical types then fail `vault-lint` forever with no way
to trigger the fix. Same shape for `vault-links`. Either move these receipts
under each root, or key them by workspace inside the global file. Note the
distinction: the *rooting* migration receipt ([§8](#8-migration)) is
per-install and stays global, because it is one event about the whole install.

**Do not add a per-workspace `.env`.** It duplicates every secret N times and
multiplies the disclosure surface this whole change exists to reduce. The
registry (`.runtime/workspaces.json`) is already the per-workspace config
surface and should remain the only one. Credentials stay global.

**Add the registry to the table explicitly.** `.runtime/workspaces.json` becomes
the load-bearing artifact of the design: the map from logical name → `agent_root`.
It is global by necessity and it is the thing whose corruption breaks everything.
It needs the same fail-closed treatment `_entity_visible_in_workspace` has today.

**Add a layer you do not own.** `~/.claude/projects/<slug>/` is global-by-tool
and becomes N slugs. It has to be enumerated by the migration and by
`_claude_projects_dir`'s callers. It cannot be relocated.

**Drop "the app checkout" from the Global row.** It already lives outside every
workspace and is not part of this boundary.

Resulting layout, which also answers the separate-repos question:

```
<install-root>/                 # workspace_root — global, never a workspace
  .env                          # global: credentials, server config
  .runtime/                     # global: schedules, job_runs, state.json, FTS, background
    workspaces.json             #   name -> agent_root map
    migration/                  #   rooting receipt (per-install), keyed vocab receipts
  skills-src/                   # NOT a workspace: a sync source, mirrored into each root
  personal/                     # agent_root; independently git-init-able
    CLAUDE.md / AGENTS.md       # incl. this workspace's ciao:memory / ciao:profile
    .claude/{skills,agents,commands}/
    skills/ commands/ subagents/
    .mcp.json
    memory-vault/               # MEMORY.md, INDEX.md, VOCABULARY.md, People/, Logs/Chats/, Workspace/
  work/                         # same shape
```

The global layer is a **sibling** of every workspace, never a parent. That is
what makes "share the work workspace with a team" safe later: `.env` and
`.runtime/` are provably not inside any workspace tree, so a per-workspace repo
cannot carry them. Agreed with the handoff's instinct — separate *directories*
as the model, separate *repo* as a per-workspace choice. `git_sync.sync_workspace`
and the startup commit are already per-path, so N repos costs nothing structural.

---

## 4. Why there is no shared layer

An earlier draft of this plan made a third *shared* layer load-bearing: a
registered pseudo-workspace carrying cross-workspace entities and
user-authored skills. **Dropped, on evidence.** Three findings, in the order
they should have changed the conclusion.

### 4.1 The evidence

**It never had a single user.** There is no `shared/` directory in the vault at
all — only `personal/`, `work/`, `Logs/`, `Templates/`. The `shared/` prefix was
anticipated by an earlier design and used by nothing. Building a layer on it
meant designing for a requirement that was never expressed.

**The skills argument inverts once you count the catalog.** The case for shared
rested on user-authored skills drifting if copied N times. The real catalog is
20 skills and **16 are work-scoped**: `airtable-feedback`,
`airtable-opportunities`, `airtable-projects`, `jira-tickets`,
`zendesk-assistant`, `scandit-brand`, `scandit-slides`, `okr-writing`,
`kr-update`, `performance-review`, `release-slides`, `bigquery-data`,
`adoption-report`, `prd-writing`, `feature-brief`, `linkedin-writing`. Only
`diagnosing-bugs`, `codebase-architecture-review`, `excalidraw-diagram` and
`adversarial-review` are arguably general.

That is not a shared asset to preserve. **It is a misfiled asset — the same
disease as the 57 work contacts in `personal/People/`.** A shared layer would
have carried 4 of 20 and given the other 16 a reason to stay global.

**It was the riskiest part of the design.** Making shared reachable meant
widening `_safe_relative` from one root per principal to two. That single
invariant is why no cross-workspace *write* is currently possible. Relaxing it
for a layer with no users is a bad trade at any price.

The working tree already reached the same conclusion independently.
`_entity_visible_in_workspace` no longer carries a `shared/` branch, and its
docstring makes the argument from the same measurement: *"zero notes existed in
both trees, and every cross-workspace reference came from a note filed in the
wrong workspace. Workspaces are separate; a person belongs to whichever one you
deal with them in."*

### 4.2 What shared was carrying, and where it goes instead

**Skills, commands, subagents, and `.mcp.json` need a sync source — not a
workspace.** `sync_workspace_skills(workspace: Path)` already copies packaged
stock skills from package resources into each root. Add one more source:

```
<install>/skills-src/          # not an agent_root, not a workspace
  <name>/SKILL.md              # authored once, mirrored into every root
```

No agent root, no `INDEX.md`, no `VOCABULARY.md`, no lint pass, no promotion
tool, and — decisively — **no change to `_safe_relative`**. It is a mirror
source, exactly like `ciao.stock`, which is why it costs almost nothing: the
per-root install path already exists and already runs N times.

The same mechanism composes each root's `.mcp.json` from `skills-src/.mcp.json`
plus per-root overrides ([§7.1](#71-mcpjson--already-in-not-a-choice-and-the-strongest-case-for-the-whole-change)).

**Cross-workspace entities get no mechanism at all.** A person belongs to the
workspace you deal with them in. If the same name appears in both, that is two
notes about two relationships, and the misfiling that produced the observed
damage is fixed by per-workspace curation routing — which has already landed —
not by a visibility escape hatch.

### 4.3 The index, without shared

Each root writes its own `INDEX.md` covering only its own vault. Paths are
vault-relative with **no workspace prefix** — inside the subtree they already
are not prefixed. `find_entities` reads exactly one index: the active root's.
`_entity_index_root` becomes `agent_root / "memory-vault"`, and the comment on
it (currently explaining why it must *not* be the per-workspace subtree)
inverts.

Two bugs to fix on the way:

- **`_index_cache` is a single slot.** `get_index` replaces the whole cache when
  the path differs. Today the path never differs, so it is free; with per-root
  indexes it re-parses and re-compiles every regex on every workspace
  alternation. Make it a dict keyed by path.
- **`vault_index._workspace_of` becomes dead.** With no workspace segment left
  to infer, its `"personal"` default has nothing to disambiguate against
  `DIR_TYPE_MAP`. Remove it with the prefix.

`_entity_visible_in_workspace` still cannot be deleted immediately: its
`legacy_workspace` branch is the reason it exists, and it must keep failing
closed until no supported install has a prefixed cross-workspace index.


## 5. Does Option A become free?

Partly. Being precise about which parts, because "free" is what would make this
get under-scoped.

**Genuinely free (zero code):** delivery. Providers already pass an empty memory
block; the CLI reads `CLAUDE.md` from cwd; codex's symlink check works per root
unchanged. Per-root cwd means each session sees exactly its own regions. This is
the real prize and it is free.

**Cheap (a few lines each):** `control_plane.memory_status` and `memory_update`
already call `self._workspace(principal)` and discard the result. Threading it
into `config.agent_root(workspace) / "CLAUDE.md"` is a two-line change per
method, and `_workspace` already fails closed on a mismatched or unknown
workspace. The curation prompt's bounded-region clause comes back, scoped.

**Not free:** `os_audit.audit_memory` and `ciao memory-audit` assume one guide.
They become N, which changes the *report shape* — `memory_hygiene` grows a
per-guide dimension, and the Settings audit view and `GET /api/agent-assets/audit`
consumers change with it. Budget for the UI, not just the audit.

**A side effect worth naming:** `memory_char_limit` / `user_char_limit` are
global caps. Under per-root guides the effective total becomes N × the cap while
each session still pays only its own. That is a genuine win, not a regression —
but over-cap reporting must become per guide or the audit will read as if the
budget tripled.

### Are the legacy fallbacks deletable?

| Symbol | Verdict |
|---|---|
| `_entity_visible_in_workspace` | **Deleted in the cut.** Not deferrable: deleting it while an unrooted install exists is fail-open, which is why the cut is atomic ([§9.1](#91-why-deletion-forces-a-mandatory-migration)). |
| `config.legacy_entity_workspace()` | **Deletable, and this is the cleanest win of the design.** It exists only because setup historically pointed a logical workspace at `CIAO_VAULT_ROOT` itself, producing unprefixed index entries. Under per-root indexes those entries are unprefixed *and correct* — they are inside their own root. Deleted by the migration re-rooting that workspace, not by a code change. |
| `vault_index._workspace_of` | **Deletable.** No workspace segment left to infer, so its `"personal"` default has nothing to disambiguate against `DIR_TYPE_MAP`. |
| `config._legacy_workspaces()` | **Not deletable, and unrelated.** It is the config default for installs with no registry. Leave it. |
| `config.primary_workspace()` | **Not deletable.** Schedules, unscoped MCP principals, and CLI invocations still need a default. `_schedule_workspace_hint` resolving through it is correct. |
| `os_audit._per_workspace_vault_paths` | **Deletable.** Its whole job is scanning every child directory of the vault root because there is no registry lookup — which is why `Logs/` and `Templates/` are treated as workspaces. Under per-root layout it becomes "iterate the registry", which is what it should always have been. |

So: two of six deletable outright, one deletable last, one reduced, two staying.

---

## 6. Does workspace hygiene become per-workspace?

**Yes, and for the right reason** — but do not just flip the flag.

`PER_WORKSPACE_MEMORY_CURATION_PLAN.md`'s taxonomy says a routine wants fan-out
when inputs, subject, *and* outputs are all workspace-scoped. Hygiene is single
today because its subject is global: one `INDEX.md`, one skill catalog. Under
this design each root owns its own `INDEX.md`, `VOCABULARY.md`, `MEMORY.md`,
skills, and proposal queue. All three conditions become true, so fan-out is not a
workaround — the precondition genuinely changes. That resolves the outstanding
item about work findings surfacing in a personal chat, by routing rather than by
filtering.

**But `run_os_audit` is not uniformly per-root.** `audit_setup` validates the
global runtime dir; `job_runs_audit` reads the global `job_runs.jsonl`; the rule
audit reasons about the global tool denylist. Fanning out N ways would report
identical global findings N times, which reads as N problems. Split
`run_os_audit` into a global section and a per-root section: the fanned-out
routine reports its own root, and one global routine reports the global part.
`audit_setup(workspace_dir, vault_root, runtime_dir)` already has the right
signature for the per-root half.

**Skill evolution also becomes fan-out-eligible**, for the same reason — the
catalog stops being global. `_resolve_skills_roots()` reads
`CIAO_WORKSPACE/skills` and `.claude/skills` off an env var and caches the result
in a module-level `_DEFAULT_SKILLS_ROOTS` at import time; that has to become a
per-root argument. Shape: one pass per workspace over its own root. `skills-src/`
needs no pass of its own — it is a mirror source, and every skill in it is
already evaluated in each root it was mirrored into. The per-workspace
trajectory reading and per-workspace
queue writing landed today already point this way.

---

## 7. Per-workspace `settings.json`, MCP, and GWS profiles

### 7.1 `.mcp.json` — already in, not a choice, and the strongest case for the whole change

It lives at `workspace / ".mcp.json"` (`sync_skills` reads it there for the Codex
TOML and `opencode.json` projections), and Claude Code reads it from cwd. Per-root
cwd makes the MCP server set per-workspace whether or not you intend it.

**This is the sixth instance of the disease and the most consequential one,
because what leaks is authority rather than information.** Today's per-workspace
MCP scoping is `WorkspaceConfig.disallowed_tools` — the field is documented as
"Extra tools to deny (e.g. `mcp__n8n_mcp`, `Bash`)", resolved by
`config.disallowed_tools_for_workspace` and forwarded to
`ClaudeAgentOptions.disallowed_tools`. That is a **denylist**, so it is
**fail-open**: a server added to `.mcp.json` is reachable from every workspace
until somebody remembers to deny it per workspace. Every other leak in the table
discloses text. This one hands a personal session live credentialed access to
work systems — read a work Jira, post to a work Slack — with no per-workspace
entry ever having been written.

Per-root `.mcp.json` inverts that to fail-closed by construction: a server absent
from that root's file cannot be reached, and no denylist entry is required to keep
it out. Compare with `_entity_visible_in_workspace`, which had to be made to fail
closed deliberately; here the filesystem does it.

Two things to get right:

- **Composition.** The CLI reads exactly one file from one cwd, so common servers
  cannot merge at read time. `sync_workspace_skills` must compose each root's
  `.mcp.json` from `<install>/skills-src/.mcp.json` plus per-root overrides — the
  same mirror-source model packaged stock skills already use
  ([§4.2](#42-what-shared-was-carrying-and-where-it-goes-instead)). Without it,
  adding one server means editing N files. Note the asymmetry with skills: a
  genuinely common server is a real thing (search, docs), but it is not the
  default — anything credentialed is listed explicitly in the roots that should
  reach it, never mirrored.
- **Reachability is not authority.** Splitting `.mcp.json` scopes *which servers a
  session can call*, not *what those servers are allowed to see*. If work and
  personal both point at the same Atlassian or Slack account, the server still
  holds one account's full authority; the split only stops the wrong session from
  invoking it. Genuine separation needs separate accounts or profiles — which is
  what `gws_profile` already does for Google. Do not let the structural
  guarantee be over-trusted here; `disallowed_tools` remains useful as a second
  layer rather than being retired.

### 7.2 `.claude/settings.json` — out of scope to generate, in scope not to break

Ciaobot writes none today (verified). Per-root cwd plus
`setting_sources=["user","project","local"]` means a user-created per-root
settings file starts being honoured for free, which is a feature. Two follow-ups:
`os_audit`'s rule-clash audit should learn to read it, and the docs should say it
exists. Do not have Ciaobot manage it.

### 7.3 Skills and GWS profiles

**GWS profiles stay in the registry.** `WorkspaceConfig.gws_profile` is already
per-workspace and `_build_prompt_prefix` already resolves it per chat. Moving it
to a per-root file would buy nothing and would tempt OAuth tokens into N
locations. Tokens stay in the global credential store. Same answer for
`disallowed_tools`, `default_provider`, `default_model`, `color`: the registry is
the per-workspace config surface, the root is the per-workspace *content*
surface. Keep that line clean.

**Skills are not symmetric with MCP, and should not be treated as such.** A skill
is instructions; an MCP server is credentialed access. A work skill loaded in a
personal session discloses no personal data and grants no authority — its real
cost is context budget and mis-triggering, which `os_audit`'s skill budgets and
the 15KB cap already measure. Real, but a lesser cost.

So the defaults differ: **MCP is per workspace with nothing common by default** —
a server absent from a root's `.mcp.json` is unreachable, which is the point.
**Skills are per workspace with a mirror source for the genuinely general few**
([§4.2](#42-what-shared-was-carrying-and-where-it-goes-instead)); the measured
catalog says workspace-only is the common case at 16 of 20.

The same reasoning applies to commands and subagents.

---

## 8. Migration

**Revised for the hard cut ([§9](#9-sequencing--a-hard-cut)).** An earlier draft
made `root` default to unset so an un-migrated install stayed byte-identical to
today. The deletions rule that out: without `_entity_visible_in_workspace` an
unrooted install is fail-open, so "some workspaces rooted, some not" is not a
legal state. The invariant is now the opposite one:

> **`agent_root` is always derived — `workspace_root / name` — and every
> registered workspace re-roots together or none does.** Every consumer goes
> through one function, `config.agent_root(workspace)`. There is no unrooted
> configuration after the cut, and no half-rooted one at any point.

`root` therefore stops being an opt-in flag and becomes a recorded fact: an
explicit override for the external-vault case (§8.3), absent otherwise.

### 8.1 Case A — fresh install

`ciao setup` creates `<install>/<workspace>/` per registered workspace with its
own `CLAUDE.md` (+ `AGENTS.md` symlink), `.claude/{skills,agents,commands}`,
`skills/`, `commands/`, `subagents/`, `memory-vault/`; plus an empty
`<install>/skills-src/`; plus global `.env` and `.runtime/` at the install root.
`sync_workspace_skills` runs once per root. No migration path involved. This is
the easy case and it should be built and tested *first*, because it is the target
state the migration has to produce.

The existing rule that **Settings never accepts a filesystem path for a
workspace, and never follows a named-folder symlink outside the vault** must
survive intact. `agent_root` is *derived* (`workspace_root / name`), never
user-entered. The one exception is the preserved-external-vault case, and that
stays on `vault_root`, where it already lives.

### 8.2 Case B — upgrade of an existing install

The hard case, and after the hard-cut decision it runs **unattended at upgrade**
from `sync_workspace_skills` rather than on a user command. `ciao workspace-reroot`
remains as the manual entry point (dry-run by default, `--apply` to execute) for
re-running a refused migration and for `--repair` / `--undo` afterwards
([§11.1](#111-ciao-workspace-reroot---repair--deterministic-and-idempotent)).

Receipt-gated, all-or-nothing across workspaces, and every step below must either
complete for every registered workspace or leave the install on the old layout.

0. **Require a clean git tree, and refuse otherwise.** Setup already guarantees
   the workspace is a git repo. That repo is a far stronger undo than a
   hand-written reverse map, and refusing on a dirty tree is what makes it
   trustworthy. Tag the pre-migration commit and record it in the receipt.
1. **Create `<install>/<ws>/` per registered workspace**, plus an empty
   `<install>/skills-src/`.
2. **Move each vault with `git mv`** — `memory-vault/<ws>/` →
   `<install>/<ws>/memory-vault/` — so history follows the notes. Skip any
   workspace whose `vault_root` is absolute/external; see Case C.
3. **Split `CLAUDE.md`.** The unbounded body is install-wide guidance and is
   copied verbatim to every root. The bounded `ciao:memory` / `ciao:profile`
   regions must be **partitioned, not copied** — copying preserves exactly the
   leak this exists to fix.

   **Do not attempt to classify entries automatically.** Recommendation: write
   the regions into the primary workspace only, leave every other root's regions
   empty, and queue the full original region content into every other root's
   `Workspace/Memory-Proposals.md` for the user to promote. That is the only
   option that neither leaks nor loses data, and the proposals queue already
   exists for exactly this review step. A heuristic split would silently
   misattribute facts, which is the same failure mode as the 57 work contacts in
   `personal/People/`.
4. **Skills: triage the catalog, do not copy it.** The existing catalog
   (`CIAO_WORKSPACE/skills`, `.claude/skills` minus stock) is 20 skills of which
   16 are work-scoped. The migration **cannot** decide which is which, so it must
   not try: move the whole catalog into the primary workspace's root, write a
   `Workspace/Skill-Triage.md` listing every skill with its destination blank,
   and leave the split to the user. Copying all 20 into every root would
   reproduce exactly the global catalog this change exists to end.

   Stock skills repopulate each per-root `.claude/skills` from package resources
   as `sync_workspace_skills` already does, so nothing packaged needs migrating.
   Create `<install>/skills-src/` empty; the user promotes the general few into it
   as they triage.
5. **Rebuild each root's `INDEX.md` and `VOCABULARY.md`** from its own vault,
   dropping the workspace prefix. Keep `_entity_visible_in_workspace` in place
   through this step; it is harmless against unprefixed paths in a root-scoped
   index and it is the safety net if a root is missed.
6. **Drop and rebuild FTS.** It is derived state. Never migrate it.
7. **Sessions: accept the loss, and carry a summary rather than faking
   continuity.** Every live chat's provider session is stranded at the old cwd
   slug ([§2.2](#22-the-cost-the-handoff-missed-cwd-is-session-identity)).
   Enumerate open chats and set `handover_context_pending` on each, so the next
   turn starts a fresh provider session *with* the existing context capsule
   instead of silently forgetting. Say so in the migration output — a user whose
   long chat resets deserves to know why.

   Considered and rejected: symlinking `~/.claude/projects/<old-slug>` to the new
   slug. It is an undocumented SDK layout outside the workspace, it would break
   `list_sessions` for both slugs, and one workspace's old slug maps to N new
   ones anyway.
8. **Receipt** under the global `.runtime/migration/workspace-rooting.json`:
   the pre-migration git tag, per-workspace source → destination map, region-split
   decisions, session count invalidated, and — when the migration declined — the
   refusal reason, which `os_audit` reads back as `workspace_root_unmigrated`
   ([§11.2](#112-os_audit-rooting-drift-findings--so-nobody-has-to-look)). The map
   is what `--undo` reverses. Per-install, not per-root — it is one
   event about the whole install. (The *vocabulary* and *links* receipts are
   per-vault and move under each root; see [§3](#3-layer-boundaries).)

### 8.3 Case C — onboarding an existing non-Ciaobot vault

Today: `CIAO_VAULT_ROOT` points at an external folder (e.g. an Obsidian vault),
`vault_root` is `"."` or absolute, `legacy_entity_workspace()` owns its
unprefixed entries, and setup git-inits that folder separately.

**Constraint this imposes on the data model, and it is the one that most shapes
the design:** `agent_root` and `vault_root` must be **independent**. The
workspace gets a Ciaobot-created `agent_root` holding `CLAUDE.md`, `.claude/`,
`.mcp.json` — and its `vault_root` stays the absolute external path, unmoved.
The user's notes folder is never relocated and never absorbed.

If you instead require `vault_root` to live under `agent_root`, you break every
existing-folder install and every pinned legacy root. Do not do that.
`_resolve_vault_root` already supports absolute roots with a symlink guard; that
support becomes load-bearing rather than a compatibility wart.

For onboarding specifically: the external folder becomes one workspace's vault by
reference. Its notes have no workspace prefix, which under per-root indexes is
correct. `legacy_entity_workspace()` becomes unnecessary for it, which is the
mechanism by which that function dies.

---

## 9. Sequencing — a hard cut

**Revised 2026-08-19 on two decisions: everything marked deletable gets deleted,
and the shift is committed to rather than offered.** Those turn out to be the
same decision, for a mechanical reason worth stating plainly.

### 9.1 Why deletion forces a mandatory migration

Delete `_entity_visible_in_workspace` on an install that has *not* re-rooted and
the result is not "no filter needed" — it is **no filter at all** over an index
whose paths are still workspace-prefixed. Every entity in every workspace becomes
visible in every session. That is strictly worse than today: a fail-open
regression in the exact place the current code was deliberately made to fail
closed.

The same holds for `legacy_entity_workspace()`: it exists to stop unprefixed
legacy entries leaking into every workspace, and while an unrooted install exists
that job is still real.

So the compatibility invariant the earlier draft was built on — `root` unset
resolves to `workspace_root`, making "half-rooted" a legal state — **cannot
survive the deletions.** Deletion and re-rooting must land in the same release
and must be atomic per install. That is what committing to the full shift means
in practice, and it is the right reading of the instruction.

### 9.2 What that changes about how it ships

The migration now runs **at upgrade, unattended**, not on a user command. Its
home is `sync_workspace_skills`, which already hosts receipt-gated one-off
migrations (the legacy memory fold-in, the vault vocabulary pass) and already
runs per workspace root.

But note the risk class it joins. Those existing migrations are idempotent and
non-destructive. This one moves directories and invalidates every live provider
session. Two hard requirements follow:

- **It must be able to refuse, and the app must still boot after refusing.**
  Refusal conditions: dirty git tree, unreadable or absent registry, a workspace
  whose `vault_root` cannot be resolved, a destination that already exists with
  content, insufficient free space. On refusal, boot on the old layout, record the
  refusal reason in the receipt, and surface it as an `os_audit` finding with a
  remedy prompt (§11).
- **It must never boot half-rooted.** Either every registered workspace re-roots
  or none does. A partial move is the one state the deletions make unsafe.

### 9.3 The line to hold: Ciaobot's artifacts move, the user's content does not

`os_audit`'s existing `vault_outside_vault_root` notice carries a stated posture
in its own comment: *"rewriting a user's own notes is not a decision an upgrade
makes on their behalf, and this is the notice that makes the choice visible
instead of"* applying it. A mandatory automated re-root appears to contradict
that. It does not, if the line is drawn at **ownership** rather than at
automation:

| | Migrated automatically | Left to the user |
|---|---|---|
| **Ciaobot's own artifacts** — `CLAUDE.md` scaffold, `.claude/`, `.mcp.json`, `INDEX.md`, `VOCABULARY.md`, FTS, the registry | yes — Ciaobot wrote them, Ciaobot can move them | — |
| **The user's content** — vault notes, hand-authored skills | *moved* wholesale via `git mv`, which git can reverse | *never re-filed.* The 16-of-20 skill triage and any misfiled note stay decisions |

A move is reversible and preserves history. A re-filing is a judgement. The hard
cut applies to the first and never to the second — which is how the commitment and
the repo's posture both stay intact.

### 9.4 The sequence

| # | Step | Ships | Risk |
|---|---|---|---|
| 0 | **Vocabulary, docs only.** Fix `ARCHITECTURE.md`'s two meanings of "workspace"; establish `workspace_root` / `workspace` / `agent_root`. | alone | none |
| 1 | **`config.agent_root(name)`** — derived as `workspace_root / name`, with `_legacy_workspaces` entries deriving theirs too (§9.5). | alone | none |
| 2 | **Thread it through the seam.** `ProviderService(config, provider, agent_root=…)` from `_get_provider` via chat → project → workspace. | alone | low |
| 3 | **The six `_claude_projects_dir` call sites take an `agent_root`.** `insights` ×2, `subagent_tracking`, `routes_api`, `project_chats`, `transcripts`. | alone | low — the step that prevents §2.2's silent breakage |
| 4 | **Per-root memory management + the `mcp__*` allowlist.** `memory_status`, `memory_update`, `audit_memory`, `memory-audit` take an `agent_root`. Flip the per-workspace tool policy from denylist to allowlist for `mcp__*`. | alone — the last step that can | low |
| 5 | **`ciao workspace-reroot`, `--repair`, `--undo`, and the operator-action detectors** (§11) — requires steps 1–4 of [`OPERATOR_ACTIONS_PLAN.md`](OPERATOR_ACTIONS_PLAN.md) to have landed. Built and tested against a fresh install (§8.1) before anything migrates. | alone | medium |
| 6 | **THE CUT — one release, atomic per install.** Startup migration; per-root `INDEX.md` with no prefix; `_index_cache` as a dict keyed by path; `skills-src/` as a sync source; `system-vault-index` folded into hygiene (§9.6); `run_os_audit` split global/per-root; **and every deletion in §9.5**. | as one indivisible release | **high** |

Steps 0–4 are preparation and each is independently reviewable. **Step 6 is
deliberately not decomposable** — that is the cost of the hard cut, and pretending
otherwise is how a half-rooted install happens.

### 9.5 Deletions — all of them, in step 6

| Symbol | Why it goes |
|---|---|
| `context.entity_tagger._entity_visible_in_workspace` | Every index is now single-workspace. Nothing to filter. |
| `config.legacy_entity_workspace()` | Unprefixed entries are unprefixed *and correct* inside their own root. |
| `vault_index._workspace_of` | No workspace segment left to infer from a path. |
| `os_audit._per_workspace_vault_paths` | Replaced by iterating the registry — which is why `Logs/` and `Templates/` stop being mistaken for workspaces. |
| the workspace-prefix branch in `INDEX.md` writing | Paths are vault-relative within one root. |
| `web.project_chats._entity_index_root`'s special case | Becomes `agent_root / "memory-vault"`, unconditionally. |
| `config._legacy_workspaces()` | **Replaced, not merely removed** — see [§9.6](#96-legacy_workspaces-is-deletable-but-by-replacement). |
| `CiaoConfig.default_model_personal` / `default_model_work` | Dead once nothing manufactures `personal`/`work`. Env vars `CLAUDE_DEFAULT_MODEL_PERSONAL` / `_WORK` go with them. |
| `CiaoConfig.disallowed_tools_personal` / `disallowed_tools_work` | Same — the per-workspace denylist lives in the registry, and after step 4 it is an `mcp__*` allowlist anyway. |

**Kept, and why** — `config.primary_workspace()` stays: schedules, unscoped MCP
principals, and CLI invocations still need a default, and that need is unrelated
to rooting.

### 9.6 `_legacy_workspaces()` is deletable — but by replacement

An earlier revision of this plan said it stays and merely derives its roots. That
was wrong, and the reason it is deletable is not the one you would guess.

**It is misnamed.** It is not a legacy fallback — it is the **bootstrap default**.
Traced through: `ciao setup` writes `.runtime/workspaces.json` directly
(`cli.py`, three call sites). For an install that skipped setup, the registry is
absent, `_legacy_workspaces()` fires in `__post_init__`, and
`_normalize_workspace_vault_roots()` rewrites its one-segment `vault_root` values —
which sets `_workspace_registry_changed`, so `main.py` persists the result at first
boot. From boot two onward it never fires again. So it is the thing that seeds
every non-setup install, and simply deleting it leaves such an install with **zero
workspaces**.

**But it manufactures two, and the second one becomes actively harmful.** It
creates `personal` *and* `work` whether or not the user wanted a `work`
workspace. Today that is a harmless phantom registry entry pointing at a vault
subdirectory that may not exist. Once a workspace *is* a directory, the startup
migration would create a real `work/` tree — guide, `.claude/`, mirrored skills,
empty vault — on the disk of every install that never asked for it. That is a
regression the current layout does not have.

So: **delete `_legacy_workspaces()` and replace it with `_bootstrap_workspace()`
returning exactly one workspace**, named from the configured vault folder or
`"personal"` when there is nothing to name it from, with its root derived like
every other. One workspace is a defensible default; two is a guess that now costs
a directory tree.

The four legacy env-var fields feeding it (`CLAUDE_DEFAULT_MODEL_PERSONAL`,
`CLAUDE_DEFAULT_MODEL_WORK`, and the two `disallowed_tools_*`) die with it. That
is a breaking config change for anyone still setting them, so it needs a release
note — **and an operator action** that says so on the machine where it is true
rather than in a changelog ([§11](#11-the-repair-mechanism)): `legacy_env_ignored`,
detected by reading the env, cleared by removing the variables.

### 9.7 The other consequence the earlier draft missed

**`system-vault-index` has nothing global left to write.** Two stock routines
currently touch the index: `system-vault-index` (global, `per_workspace` unset)
and `system-workspace-hygiene` — which is **already** `per_workspace: true`. Under
per-root indexes the global one has no shared artifact to rebuild, so fold it into
hygiene rather than leaving a routine that regenerates a file no longer read.

And because hygiene is *already* fanned out, `run_os_audit` is **already**
reporting the global findings (`job_runs_audit`, the rule audit, `audit_setup`'s
runtime checks) once per workspace. Splitting it global/per-root is therefore a
present bug fix, not a prospective one.


## 10. What the commitment buys

No stop point any more — the decision is to go all the way. What that settles:

- **Per-workspace MCP servers.** The worst of the six leaks, and the only one
  where what leaks is credentialed authority rather than text. Reachable only with
  per-root `cwd`.
- **The misfiled skill catalog.** 16 of 20 skills are work-scoped and currently
  advertise themselves into every personal session. The `mcp__*` allowlist (step 4)
  closes the *authority* half of that; only re-rooting closes the *catalog* half,
  because policy cannot scope a directory the CLI reads by itself.
- **Per-workspace bounded memory**, i.e. Option A, delivered free at the delivery
  layer.
- **Six symbols deleted** and one whole class of check retired, rather than a
  second compatibility path maintained indefinitely.

The price, stated plainly: **step 6 is one indivisible release with a
destructive startup migration and no per-install opt-out.** Every live provider
session resets. If it is wrong, it is wrong for every install at once.

That is exactly why the repair mechanism below is not optional garnish — it is the
thing that makes an irreversible-by-design step survivable in practice. And it does
not need building from scratch: [`OPERATOR_ACTIONS_PLAN.md`](OPERATOR_ACTIONS_PLAN.md)
already specifies the surface, which makes it a **prerequisite** for the cut rather
than a follow-on.

## 11. The repair mechanism

**Revised: this ships on an existing planned surface rather than a new one.**
[`OPERATOR_ACTIONS_PLAN.md`](OPERATOR_ACTIONS_PLAN.md) already specifies exactly
what was being asked for — a home-screen strip of detected conditions, each with
a button that runs the fix, a button that opens a chat seeded with a prompt, or
both, clearing itself by re-detection rather than by anyone declaring success. An
earlier revision of this section invented a stock command plus new `os_audit`
findings. Delete that; register detectors instead.

That makes the operator-actions work a **prerequisite for the cut**, not a
companion to it. Steps 1–4 of its Order section (the dataclass, the registry,
`detect_actions`, the endpoints and dispatch) must land before step 6 here.

### 11.1 The mechanical half: `ciao workspace-reroot --repair`

Deterministic and idempotent. Re-derives the intended layout from the registry and
reconciles the filesystem to it; changes nothing when already correct. This is what
a tile's **run** button dispatches to.

| Drift | Repair |
|---|---|
| A registered workspace's root directory is missing | Create it, run `sync_workspace_skills` against it |
| `AGENTS.md` is not a symlink to its root's `CLAUDE.md` | Re-link via `_ensure_linked_workspace_guides` |
| A root's `.claude/skills` is missing packaged or `skills-src/` entries | Re-mirror |
| A root's `.mcp.json` is stale against `skills-src/` + overrides | Recompose |
| A root's `INDEX.md` is absent or still carries workspace prefixes | Rebuild with `vault-index --write` for that root |
| The FTS db references moved paths | Drop and reindex — derived state |
| A registry entry's root exists but holds no vault | **Report, do not guess** — routes to the chat half |

`--undo` reverses the whole cut from the receipt: the recorded pre-migration git
tag plus the per-workspace source → destination map. A real path back rather than a
best effort, precisely because §9.3 restricts the automated part to *moves*.

**`--undo` stays CLI-only and never becomes a tile.** "Revert the architecture" is
not a housekeeping button, and a run action must be safe to press without reading
anything.

### 11.2 The detectors

Five new entries in `ciao/operator_actions.py`, each obeying that plan's four
contract requirements:

| `kind` | Detected from | run | chat |
|---|---|---|---|
| `workspace-unmigrated` | the rooting receipt's `status` + refusal reason | re-run the cut once the precondition is fixed | explain the refusal, fix the dirty tree / disk / non-empty destination |
| `workspace-root-missing` | `Path.is_dir()` per registry entry | `--repair` | — |
| `workspace-assets-stale` | receipt `sync_generation` vs. the installed package version | `--repair` | — |
| `skill-triage-pending` | count of blank destinations in `Workspace/Skill-Triage.md` | — | walk the triage with the user (§11.3) |
| `legacy-env-ignored` | presence of `CLAUDE_DEFAULT_MODEL_PERSONAL` / `_WORK` / `disallowed_tools_*` in the env | — | name the dead variables and what replaced them (§9.6) |

Three points where that plan's contract bites, and how each is satisfied:

- **Detection must be cheap** (its requirement 2 and trap 1: no directory walks to
  decide whether to draw a button). So `workspace-assets-stale` compares a
  generation counter written into the receipt at sync time — it does **not** diff
  the mirrored skill trees. And `workspace-root-missing` is `is_dir()` per registry
  entry, which is bounded by workspace count. Nothing here reads `INDEX.md` to
  decide; the receipt records whether the cut completed.
- **`id` must be stable across identical passes** (requirement 3). Use the plan's
  scope-suffixed form: `workspace-root-missing:work`.
- **Every action needs `run_label` or `chat_prompt`** (requirement 4). All five
  have at least one; none is a bare notice.

**`skill-triage-pending` is the detector that most needs its path-to-zero argued**,
per that plan's trap 2. It reaches zero when no destination in
`Workspace/Skill-Triage.md` is blank, and the chat prompt's whole job is to get
there — but a user may legitimately never finish, which is the failure mode
requirement 1 warns about. Mitigation: the file is deleted on completion, and the
detector fires on the file's *existence with blanks*, so "I have decided to leave
these where they are" is expressible by filling every destination with the current
root. A user who wants it gone can make it gone without moving a file. Reviewers
should hold this one to that standard.

### 11.3 The chat half: what a script must not decide

The chat prompts follow the shape `os_audit`'s existing
`vault_outside_vault_root` remedy already established, which the operator-actions
plan moves into the registry so one string serves both the CLI and the tile:
**inspect both locations, ask before resolving conflicts, distinguish vault content
from Ciaobot runtime files, back up before moving anything, update the registry
atomically, restart as the final step, and verify before removing the backup.**

Its scope here:

- **A root moved or renamed by hand** — reconcile the registry with reality, or
  reality with the registry, after asking which was intended.
- **The skill triage.** Walk `Workspace/Skill-Triage.md` with the user, propose a
  destination per skill from what the skill actually does, move only what is
  confirmed. The one migration step that cannot be automated (§9.3) and the one
  most worth an agent.
- **A note filed in the wrong workspace** — propose, never move silently. The
  residue of the original bug, now a content decision rather than a structural one.
- **A refused migration** — read the receipt's reason, fix the precondition,
  re-run the cut.

Per that plan's §4, there is deliberately no `POST .../chat`: the prompt ships in
the `GET /api/housekeeping` payload and the client creates the chat with the flow it
already has.

### 11.4 First-run tiles — resolved

The cut makes the other plan's open question 4 common rather than rare: a fresh
post-migration boot produces `skill-triage-pending` on every install that had
hand-written skills, which is every install that had been used.

**Resolved 2026-08-19: that is fine and correct.** The conditions are true about
the machine, and stating them on arrival is what the strip is for; a detected
condition is not withheld for looking unwelcoming. Two implementation consequences
follow, recorded in that plan's question 4:

- **No first-run suppression** — no grace period, no message-count gate, no
  "seen once" threshold. Each would reintroduce exactly the stored per-action state
  that plan's derived-view rule removes, and the tile would then be misreporting
  the machine.
- **The copy carries the burden.** `skill-triage-pending` in particular may be the
  first thing a user sees after upgrading, so its `title` must read as a fact with a
  next step — *"20 skills need filing into workspaces"* — never as a failure.


## 12. Things I think are bad ideas

- **Repurposing `workspace_root` to mean the per-workspace directory.** 375
  references across ~40 modules; most correctly mean the install root. This is
  the single most likely way to end up half-rooted.
- **Auto-classifying the bounded regions during the split.** Same failure mode as
  the misfiled contacts. Partition to the primary workspace and queue the rest
  for review.
- **A per-workspace `.env`.** Duplicates secrets N times to solve a problem the
  registry already solves.
- **A shared layer at all.** Cut from this plan on evidence
  ([§4](#4-why-there-is-no-shared-layer)): zero notes ever lived in `shared/`,
  16 of the 20 user-authored skills are work-scoped rather than common, and
  reaching it would have meant widening `_safe_relative` from one root per
  principal to two — relaxing the invariant that makes cross-workspace writes
  impossible, for a layer with no users. `skills-src/` as a mirror source covers
  the only real need at a fraction of the risk.
- **Copying the skill catalog into every root during migration.** That
  reproduces the global catalog the change exists to end. Move it to one root and
  make the user triage it.
- **Requiring `vault_root` under `agent_root`.** Breaks every existing-folder and
  pinned-legacy install ([§8.3](#83-case-c--onboarding-an-existing-non-ciaobot-vault)).
- **Separate git repos per workspace as a requirement.** Agreed with the
  handoff's instinct, and the layout in [§3](#3-layer-boundaries) is what makes
  it safe as a later per-workspace *choice*: the global layer must be a sibling
  of every workspace, never a parent.
- **Leaving `run_os_audit` unsplit.** `system-workspace-hygiene` is *already*
  `per_workspace: true`, so the global findings are already being reported once per
  workspace. Present bug, not a prospective one
  ([§9.6](#96-two-consequences-the-earlier-draft-missed)).
- **Decomposing step 6 to make it feel safer.** The deletions make a partial
  re-root fail-open. A migration that can stop halfway is worse than one that
  refuses outright and boots on the old layout.
- **Automating the skill triage.** The migration cannot tell a general skill from a
  work one, and guessing reproduces the misfiling. Emit
  `Workspace/Skill-Triage.md` and hand it to the repair prompt
  ([§11.3](#113-the-chat-half-what-a-script-must-not-decide)).
- **Deleting `_legacy_workspaces()` without replacing it.** It is the bootstrap
  default, not a fallback — nothing else seeds a registry for an install that
  skipped `ciao setup`, so removing it outright yields zero workspaces
  ([§9.6](#96-_legacy_workspaces-is-deletable--but-by-replacement)).
- **Keeping it as-is either.** Manufacturing both `personal` *and* `work` costs a
  phantom registry entry today and a real unwanted directory tree after the cut.
- **Building a second repair surface.** A stock command plus new `os_audit`
  findings duplicates the housekeeping strip that
  [`OPERATOR_ACTIONS_PLAN.md`](OPERATOR_ACTIONS_PLAN.md) already specifies.
  Register detectors instead ([§11.2](#112-the-detectors)).

## Do not bundle with OKF

`OKF_ADOPTION_PLAN.md` (wikilinks → relative markdown links) is in flight and
touches `ciao/vault_index.py`, `ciao/vault_lint.py`, `ciao/web/routes_api.py`,
and `web/src/lib/vaultLinks.ts`. Steps 4, 5, and 7 above all want
`vault_index.py` and `routes_api.py`. Both changes rewrite link and path
resolution; interleaving them makes any regression un-bisectable, and both need
their own receipt-gated migration over the same files. Land OKF first, then step
0. Nothing in this plan was written against those four files and nothing in it
should edit them before OKF lands.
