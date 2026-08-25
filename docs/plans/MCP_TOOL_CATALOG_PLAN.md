# MCP tool catalog: keep / move / consolidate

Status: **draft for discussion** — no code changed.

## 1. Context

Ciaobot embeds an authenticated MCP server (`ciao/mcp_server.py`) exposing a
44-tool catalog. The catalog is the default control surface for both the
Claude and Codex providers (`docs/MCP.md`). This plan reviews what belongs in
the live MCP catalog versus the CLI / PWA, and whether the schedule / loop /
project verbs should be consolidated into fewer, wider tools.

## 2. The core question

The user can ask the agent to do almost anything conversationally. The real
question is not "can the agent do it" but **"what is the best mechanism for the
agent to do it mid-turn."** Three mechanisms exist:

| Mechanism | Shape | Best for |
|---|---|---|
| **MCP tool** | typed schema, structured return, token-scoped, approval-gated | conversational, mid-turn, needs validation + structured result |
| **CLI (`ciao ...`)** | run via the agent's existing Bash tool | batch / setup / maintenance, one-shot or scheduled |
| **PWA button** | REST route in `routes_api.py` | user is already in the UI |

### A trap to avoid

A generic "MCP tool that runs a CLI command" is the wrong shape. The agent
already has Bash and `ciao` is on PATH, so `Bash(ciao schedules list)` works
today with zero MCP surface. Wrapping it in an MCP tool adds text-parsing risk,
approval noise, and loses the typed contract — strictly worse than either a
typed tool or plain Bash. **Do not add a generic CLI-runner tool.**

The one defensible hybrid is a *read-only structured* dispatcher that returns
the CLI's `--json` output for a small set of rarely-needed reads. Not worth it
until telemetry shows a real need.

## 3. What the telemetry shows

`.runtime/mcp_tool_calls.jsonl` currently holds only 4 records, all probe/status
calls (`context_get`, `memory_status`, `memory_proposals_list`, `chats_list`).
Notably `memory_proposals_list` appears in telemetry but is **already gone** from
the catalog — it was moved to `ciao memory-proposals` /
`ciao memory-proposal-dismiss`. That is the established precedent: review verbs
live in the CLI, not as live agent tools.

The eval suite (`evals/release.json`) only hard-requires `projects_list` and
`vault_search` (plus the `researcher` subagent). So the catalog is not
eval-constrained beyond those two reads.

## 4. Proposed split

### The real discriminator

The first draft drew the line as "create = MCP, mutate/delete = CLI/PWA." That
line does not hold. Users ask the agent to **mutate** things mid-turn just as
naturally as they ask it to create them: "rename that chat," "move this chat to
the Backend project," "mark that project complete," "change the schedule to
run at 9am." These are conversational mutations, not maintenance.

The real discriminator is **conversational vs admin**:

> **Conversational** (the user asks the agent mid-turn) → MCP tool. The agent
> needs a typed mechanism, and with the sole exception of `ciao create-chat`
> no CLI twins exist for these verbs — "move to CLI" would mean building new
> server-talking commands, strictly more work than keeping the tool.
>
> **Admin** (the user does it in the PWA settings, rarely asks the agent) →
> CLI / PWA. The PWA already has REST twins; the agent doesn't need a typed
> tool because the user won't ask for it mid-turn.

A secondary check: **does the agent already have a cheaper mechanism?** If a
skill covers the same ground (`adversarial_review` vs the `ciao-command-critique`
skill) or a native tool does the job (`project_files_list` vs Glob), the MCP
tool is redundant and should go.

### Keep as MCP — conversational core (34 after consolidation)

**Reads (13):** `context_get`, `memory_status`, `vault_search`,
`projects_list`, `project_get`, `workspaces_list`, `chats_list`, `chat_get`,
`schedules_list`, `loops_list`, `delegates_list`, `background_run_status`,
`file_surface`.

**Conversational writes — creates + mutations the user asks for mid-turn (15):**
`memory_update`, `chat_create`, `chat_send`, `chat_continue`, `chat_retry`,
`chat_handover`, `chat_fork`, `chat_archive`, `chat_update`, `delegate_spawn`,
`background_run_start`, `workspace_create`, `schedule` *(folded: preview/create/update)*,
`loop` *(folded: create/update)*, `project` *(folded: create/update/restore)*.

**Conversational destructive — stops/deletes/completes (6):** `chat_stop`,
`chat_delete`, `schedule_action`, `loop_action`, `project_action`
*(folded: complete + delete)*, `background_run_cancel`.

Rationale: these are the verbs a user asks the agent to do mid-turn. The PWA
has REST twins for most (`PATCH /api/chats/{id}`, `DELETE /api/chats/{id}`,
`POST /api/projects/{id}/complete`, etc.), but **the agent cannot call REST
routes** — it only has MCP tools and Bash. With no CLI twins (only
`ciao create-chat` exists), removing the MCP tool removes the agent's only
mechanism. Keep them.

### Move to CLI / PWA — admin, not conversation (4)

`workspace_update`, `workspace_delete`, `adversarial_review`,
`project_files_list`.

Rationale:

- **`workspace_update` / `workspace_delete`** — workspace config (provider keys,
  env vars, model defaults) is settings-UI territory. The PWA has
  `PATCH/DELETE /api/workspaces/{name}`. The user configures this in Settings,
  not by asking the agent mid-turn.
- **`adversarial_review`** — the `ciao-command-critique` skill and
  `/code-review --fix` already cover this surface. The MCP tool is redundant
  with the skill; the skill is the better mechanism (model-picked, not a
  flat tool call).
- **`project_files_list`** — the agent has native Glob/Read. A typed MCP
  wrapper around "list files in a project" adds nothing the agent can't already
  do with Glob. Drop as redundant.

### The create verbs — the correction

Earlier drafts labeled schedule/loop/project **creation** as "PWA buttons." That
is wrong: **the agent creates schedules, loops, and projects conversationally.**
So creation is an agent action, not a UI action.

But documenting creation in the capabilities skill is necessary, not
sufficient. The skill tells the agent *when* to create and the confirmation
policy; it does not give a *mechanism*. The mechanism is either the MCP tool
(current) or a CLI command — and `ciao schedule create`, `ciao loop create`,
`ciao project create` **do not exist** (only `ciao create-chat` does). Building
them would mean new server-talking commands, strictly more work than keeping the
tool. **Keep the create verbs as MCP tools** (folded into the consolidated
`schedule` / `loop` / `project` tools per §5).

## 5. Consolidation: one tool per domain?

The user asked whether schedule/loop/project actions can be grouped into one
tool each (e.g. a single `schedule` tool with an `action` param covering
preview/create/update/pause/resume/run/delete).

### The pattern already exists

`chat_retry` folds set/stop/try-now into one `action`. `chat_handover` folds
new-session/handover. `schedule_action` and `loop_action` already fold
pause/resume/run/delete. So a consolidated tool is consistent with the codebase.

### The blocker: approval annotations

Every tool carries exactly one of `_READ` / `_WRITE` / `_DESTRUCTIVE`
(`docs/MCP.md` §Approval policy; enforced by `tests/test_mcp_server.py`):

- `_WRITE` → auto-approved in Auto mode (no prompt).
- `_DESTRUCTIVE` → prompts in Auto mode.
- `_READ` → read-only.

Today the split exists precisely because **create/update and delete/complete
need different approval**:

| Domain | create/update | delete/complete |
|---|---|---|
| schedule | `_WRITE` | `_DESTRUCTIVE` |
| loop | `_WRITE` | `_DESTRUCTIVE` |
| project | `_WRITE` (`create`/`update`) | `_DESTRUCTIVE` (`complete`/`delete`); `restore` is `_WRITE` |

A single consolidated tool can only carry **one** annotation, forcing a choice:

- Mark the whole tool `_DESTRUCTIVE` → every create/update now prompts in Auto
  mode (regression: creates become annoying).
- Mark it `_WRITE` → delete/complete become auto-approved (security regression:
  destructive ops no longer prompt).

**This is the real reason not to merge create/update with delete/complete into
one tool.**

### Recommended shape: two tools per domain

Preserve the approval boundary by keeping two tools per domain:

- `schedule` (`_WRITE`): `preview`, `create`, `update`
- `schedule_action` (`_DESTRUCTIVE`): `pause`, `resume`, `run`, `delete` *(already exists)*
- `loop` (`_WRITE`): `create`, `update`
- `loop_action` (`_DESTRUCTIVE`): `start`, `stop`, `run`, `delete` *(already exists)*
- `project` (`_WRITE`): `create`, `update`
- `project_action` (`_DESTRUCTIVE`): `complete`, `delete`

`project_restore` is `_WRITE` (non-destructive), so it folds into the `project`
tool, not `project_action`. This collapses 12 tools to 6 (schedule: 4→2,
loop: 3→2, project: 5→2) while keeping the approval semantics intact. The
`_action` tools already exist; the work is folding the create/update verbs
into their `_WRITE` siblings.

### Reads stay separate

`schedules_list`, `loops_list`, `projects_list`, `project_get` are `_READ` and
cheap. Folding them in would force the whole tool to `_READ` and break the
write/delete verbs. Keep them as-is.

## 6. Open questions for discussion

1. **Approval tradeoff.** Do we accept the two-tool-per-domain shape (keeps
   approval), or do we truly want one tool per domain and accept that either
   creates prompt or deletes auto-approve? (Recommend: two tools per domain.)

2. **Wide schemas.** The `_WRITE` tools (`schedule`, `loop`, `project`) carry
   the union of their actions' fields. The model sees every field on every call.
   Acceptable? (Consistent with `chat_retry`/`chat_handover`.)

3. **`project_restore`.** Kept as MCP (folds into the `project` `_WRITE` tool,
   non-destructive). It's the one admin-ish verb a user might plausibly ask the
   agent to do ("restore last week's project"). If telemetry later shows it's
   never used mid-turn, move it to `ciao project restore` (CLI) and drop from
   the catalog.

4. **`adversarial_review`.** Confirmed redundant with the `ciao-command-critique`
   skill and `/code-review --fix`. Move to the drop set unless telemetry shows
   real usage. (The skill is the better mechanism: model-picked, not a flat
   tool call.)

5. **`project_files_list`.** Confirmed redundant with native Glob/Read. Drop.

6. **Capabilities skill.** Should the "when to create a schedule/loop/project"
   guidance (confirmation policy, workspace/project routing) be added to the
   `ciao-capabilities` skill, so the skill owns the decision and MCP owns the
   mechanism? (Recommend: yes.)

7. **Telemetry.** The usage log is nearly empty. Should we instrument more
   before cutting, or is the catalog review enough to act on now?

## 7. Suggested implementation order

1. Add "when to create" guidance to `ciao-capabilities` skill.
2. Fold create/update into `_WRITE` siblings (`schedule`, `loop`, `project`);
   fold `project_restore` into `project`; fold `complete`/`delete` into
   `project_action`.
3. Remove the admin verbs from the catalog (`workspace_update`,
   `workspace_delete`, `adversarial_review`, `project_files_list`).
4. Prune orphaned control-plane methods.
5. Update `docs/MCP.md` catalog table and tool count (44 today → 34 after
   consolidation and removals).
6. Update `tests/test_mcp_server.py` approval-boundary assertions.
7. Run `pytest tests/` and `cd web && npm run build`.
