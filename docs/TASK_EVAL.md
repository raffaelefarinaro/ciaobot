# Agentic task eval

The agentic task eval measures whether Ciaobot actually completes a task, and
what that costs. It is the executed half of the behavioral evaluation: where
`ciao eval run` asks a model to describe the tool calls it would make
(`docs/BEHAVIORAL_EVAL.md`), this harness lets Ciaobot act. Each run boots its
own engine on a synthetic fixture vault, sends one real chat turn (or
delegates one task), grades the resulting workspace state with deterministic
checks, and records tokens, tool calls, ciao-call errors, cost and wall time.

Implementation: `ciao/task_eval.py`. Catalog: `ciao/stock/evals/tasks/`.
CLI: `ciao eval tasks`.

## Cost warning

`ciao eval tasks run` makes real model calls, one per task per repeat, and
each run boots a full engine. Nothing runs in PR CI. The tests in
`tests/test_task_eval.py` never boot an engine and never call a model.

## Running it

```bash
# Catalog: id, tier, category, requirements.
ciao eval tasks list
ciao eval tasks list --json

# Run the easy tier, three repeats, into an explicit report file.
ciao eval tasks run --provider claude --model haiku --label baseline \
    --tier easy --repeats 3 --out /tmp/base.jsonl

# Summarize one or more reports (pass rate, medians, tool errors, timeouts).
ciao eval tasks report /tmp/base.jsonl

# Compare two runs task by task.
ciao eval tasks compare --baseline /tmp/base.jsonl --candidate /tmp/cand.jsonl
```

`run` first checks that the `ciao` an agent would run is this checkout's package
(`cli_package_mismatch`). The engine's sandbox only reads the package running
it, so on a mismatch `run` prints the two paths and the remedy to stderr and
exits 2 before anything boots. The remedy is to run the eval from an install of
the checkout under test, e.g. `uv venv .venv-eval && uv pip install -p .venv-eval -e .`
in that checkout, then `.venv-eval/bin/ciao eval tasks run ...`.

`run` prints the summary table and exits 0 unless every run errored. Without
`--out`, the report goes to `.runtime/evals/tasks/<UTC stamp>-<label>.jsonl`
(`CIAO_RUNTIME_ROOT` moves the `.runtime` root, as for the behavioral report).
`--include` takes comma-separated task ids; `--tier` takes comma-separated
tiers. `--keep-workdir` keeps each run's throwaway workspace on disk for
debugging, and the path is printed to stderr.

`--model` is required and never defaulted. `--jobs` boots that many engines
at once; each is a full engine, so keep it low.

## How a run works

1. A temporary directory `T` is made. `setup_workspace(T/ws, ...)` runs with
   every `CIAO_*`, `PWA_*` and `GWS_*` variable removed from the environment
   and the working directory set to `T`. The engine's storage roots
   (`CIAO_MEMORY_DIR`, `CIAO_LAUNCH_AGENTS_DIR`) point into `T`, so nothing
   reaches the live install's index or LaunchAgent. The queue-lock directory
   keeps its per-user temp default: lock names hash the throwaway vault path,
   and the agent's sandbox can write there (it cannot write into `T`).
2. The fixture vault `ciao/stock/evals/tasks/_fixture/` is copied over
   `T/ws/personal/memory-vault/`. The task's optional `overlay/` tree is copied
   over `T/ws/personal/`. The workspace defaults to the requested provider, and
   its `personal` registry entry pins the requested model for that provider
   (`provider_overrides`), so delegated chats run on the requested pair too.
   Steps 1 and 2 are `prepare_workspace`, which runs without booting an engine.
3. The engine is started with `ciao run` (cwd `T/ws`, the same environment
   scrub), and the harness waits until `/api/status` answers 401. It then
   signs in with the run's own token and creates a "Task eval host" project
   to hold the eval chat.
4. Setup steps run (`task`, `project`), then the harness takes a snapshot:
   task ids, schedule ids, project ids, the relative paths of every file under
   the agent root (`T/ws/personal/`), and the bytes of any file an `unchanged`
   check names. The snapshot is taken after the host project exists, so its
   scaffold files are never reported as new.
5. The entry runs: `prompt` sends the task's prompt in a new chat, `delegate`
   hands an existing task to the agent.
6. The run is settled when `/api/admin/restart-blockers` is empty on two
   consecutive polls two seconds apart, at least five seconds after the prompt
   was accepted. This also covers chats spawned by a delegation. On timeout the
   chat is stopped, the run is marked `timed_out`, and it is still graded.
7. Checks grade the state. The engine receives SIGTERM (after 20 s, a kill),
   and `T` is deleted unless `--keep-workdir` is given.

A run never raises. A failure is recorded as `status: "error"` with the
exception text. A task whose `requires` names a capability the harness does
not have is recorded as `skipped` and the engine is not booted.

## Records

Each run is one JSON object per line:

| Field | Meaning |
| --- | --- |
| `schema` | `ciao-task-eval/1` |
| `label`, `task_id`, `tier`, `category`, `repeat` | the run's identity |
| `provider`, `model` | what was requested |
| `code_revision` | short HEAD of the checkout, plus `-dirty` when the tree is not clean |
| `provider_version` | first line of `<provider> --version` |
| `catalog_sha256` | hash over the task catalog bytes |
| `status` | `passed`, `failed`, `skipped` or `error` |
| `answer` | the last 2000 characters of the answer text (all turns of all chats, most recent last) |
| `checks` | per check: `kind`, `ok`, `detail` |
| `tokens` | `input`, `output`, `cache_read`, `cache_write` and `context_total` (input + cache read + cache write), summed over every turn of every chat in the run |
| `tool_calls`, `tool_errors` | from the turns' tool-call lists |
| `ciao_calls`, `ciao_errors` | from `mcp_tool_calls.jsonl`; an error is any status other than `ok` |
| `cost_usd` | the engine's recorded cost, or null |
| `wall_s`, `timed_out`, `chat_ids` | timing and the chats measured |

`summarize` groups rows by label, provider, model, code revision and task. Its
pass rate and medians exclude skipped runs. `compare` joins two reports by task
id and reports the pass-rate difference and the percentage change in context,
wall time and cost.

## The catalog

Each task is `ciao/stock/evals/tasks/<task_id>/task.json`:

```json
{
  "id": "E1-task-capture",
  "tier": "easy",
  "category": "task_capture",
  "prompt": "Add a task: renew the bike insurance by Friday",
  "entry": {"kind": "prompt"},
  "setup": [],
  "requires": [],
  "timeout_s": 600,
  "checks": [
    {"kind": "new_tasks", "count": 1, "title_contains": ["insurance"], "due": "weekday:friday"},
    {"kind": "new_files", "glob": "memory-vault/projects/**", "max": 0}
  ]
}
```

- `id` must equal the directory name. `tier` is `easy`, `medium` or `hard`.
- `entry` is `{"kind": "prompt"}`, which needs `prompt`, or
  `{"kind": "delegate", "task_title": "..."}`, which needs a setup task with
  that exact title.
- `setup` steps are `{"kind": "task", "title", "body"?, "due"?}` and
  `{"kind": "project", "name", "context"?}`.
- `requires` lists capability names. `gws_fake` is the only one (see
  [Capabilities](#capabilities)). A task that names a capability the platform
  lacks is recorded as skipped: `gws_fake` is skipped on Windows.
- An optional `overlay/` directory sits beside `task.json`. Its files are
  copied over `ws/personal/` before the engine starts, for example
  `overlay/memory-vault/People/x.md`.

The loader rejects an unknown check kind, a bad tier or entry kind, an id that
does not match its directory, and any file under the catalog that contains a
`behavioral_eval.PRIVATE_MARKERS` string. Directories starting with `_` are not
tasks; `_fixture/` is the base vault.

### Check kinds

Paths are relative to the agent root (`T/ws/personal/`), so vault notes start
with `memory-vault/`, for example `memory-vault/People/x.md`. Text
matching is case-insensitive.

| Kind | Fields | Passes when |
| --- | --- | --- |
| `answer_contains` | `all`, `any` | the final answer contains every `all` needle and at least one `any` needle |
| `answer_excludes` | `any` | the answer contains none of the needles |
| `abstains` | | the answer matches a clean refusal (`don't know`, `not recorded`, `could not find`, ...) |
| `new_tasks` | `count`, `title_contains`, `due` | exactly `count` tasks are new; every new task has the needles in its title and the due date. `weekday:friday` means that weekday, 1 to 7 days after the run date |
| `new_schedules` | `count`, `frequency`, `days_of_week`, `time`, `prompt_contains` | exactly `count` new non-system schedules match; days compare as 3-letter sets; `time` is the wall-clock `daily_time_utc` |
| `new_projects` | `count`, `name_contains` | exactly `count` new projects match the name needles |
| `file_contains` | `path`, `all`, `any` | the file holds the needles |
| `file_excludes` | `path`, `any` | the file holds none of the needles |
| `occurrences` | `path`, `text`, `count` | the text appears exactly `count` times |
| `new_files` | `glob`, `count` or `max` | files matching the glob that were not in the snapshot number `count`, or at most `max` |
| `frontmatter_valid` | `path` or `glob` | every matched file has a YAML frontmatter block with `type` and `updated`; a glob that matches nothing fails |
| `links_resolve` | `path` | every relative Markdown link in the note resolves to an existing file |
| `unchanged` | `path` | the file is byte-identical to its snapshot |
| `task_attempt` | `title_contains`, `outcome`, `count` | the matching task has exactly `count` attempts with that outcome (`done`, `blocked`, `needs_input`) |
| `command` | `argv`, `cwd`, `timeout_s` | the command exits 0; `{python}` in argv is `sys.executable` |
| `gws_calls` | `must`, `must_not`, `min_calls` | over the fake gws call log (see [Capabilities](#capabilities)): every `must` regex matches at least one call summary, no `must_not` regex matches any, and there are at least `min_calls` calls. Fails with "no gws fake in this run" when the task has no fake |

A check that raises is a failed check with the exception in its detail. It does
not stop grading of the other checks.

## Capabilities

A task lists the capabilities it needs in `requires`. `ciao eval tasks run`
passes the platform's capabilities (`available_capabilities()`), and a task
whose requirement is missing is recorded as skipped.

### `gws_fake`

Tasks that need Google Workspace state (M4, the email draft) set
`"requires": ["gws_fake"]` and a `gws_fake` block:

```json
"gws_fake": {
  "profile": "eval-sandbox",
  "account": "eval.user@example.com",
  "messages": [
    {"id": "19a2b7e4c9f60002", "threadId": "19a0f3c2b1d40001", "from": "Tomas Berg <tomas.berg@example.com>",
     "to": "eval.user@example.com", "subject": "Re: Team offsite: pick a day", "date": "Thu, 29 Oct 2026 16:40:00 +0100",
     "rfc822_id": "<offsite-2@mail.example.com>", "labelIds": ["INBOX", "UNREAD"], "body": "Can you confirm Thursday?"}
  ]
}
```

`profile` must be a profile slug. `messages` is a non-empty list; each message
has string `id`, `threadId`, `from`, `to`, `subject`, `date` (RFC 2822),
`rfc822_id` and `body`, and a `labelIds` list. Message ids are unique.

When a run boots, the harness does the following:

- Writes dummy credentials, a profile registry and a fresh token-health reading
  into the workspace. Nothing real is stored: the client and refresh tokens are
  placeholders. The profile is `ws/secrets/gws-<profile>/`.
- Writes the mailbox and an empty call log into `ws/secrets/gws-<profile>/eval-gws/`,
  together with a launcher named `gws`.
- Sets `CIAO_GWS_BIN` to that launcher in the engine's environment. `ciao gws` and
  the token health check run it and never fall back to the installed `gws`
  (`ciao/gws_auth.py`, `gws_bin_override`). It is applied after the `CIAO_*`
  variables are dropped, so it reaches the engine.
- Prepends the `eval-gws` folder to the engine's `PATH`, so a bare `gws` in the
  agent's shell also reaches the fake.
- After signing in, links the workspace to the profile with
  `PATCH /api/workspaces/personal`. That also resyncs the gws skills before the
  call returns, so the agent sees a connected account.

The fake answers the Gmail calls the M4 task and its neighbours use, from the
mailbox, and never contacts Google. The launcher is a POSIX shell script, so the
capability is POSIX only. Windows skips tasks that require it.

The fake and its call log live in a folder the agent can write, so the eval is
not tamper-proof: a deliberately adversarial agent could edit the log or the
launcher. The grading assumes an honest run.

Every call appends one JSON row to `ws/secrets/gws-<profile>/eval-gws/calls.jsonl`
before it is answered. A row has `ts`, `argv`, `op`, `effect`, `thread_id`,
`message_id`, `to`, `body` and `summary`. The `effect` is one of `read`,
`draft_created`, `sent`, `modified`, `dry_run` or `unknown`. The `summary` is a
single line:

```
op=gmail +reply effect=draft_created thread=19a0f3c2b1d40001 message=eval-draft-msg-3 to=tomas.berg@example.com body=Thursday works for me
```

`to` lists every To, Cc and Bcc address (and every `--to`, `--cc` and `--bcc`
flag), comma-joined. `body` comes last and keeps the first 2000 characters, so a
`.*` in a regex can reach it. The `gws_calls` check applies its regexes to these
summaries, case-insensitively.

`gmail +triage` prints JSON unless `--format table` is given, as the real tool
does. The fake covers `auth status`, `schema`, `gmail +triage`, `+read`, `+reply`,
`+reply-all`, `+forward` and `+send` (with `--draft` and `--dry-run`), and the
`gmail users` messages, threads, drafts and labels methods. An unknown command
is logged as `unknown` and exits 2. The M4 task's check is:

```json
{"kind": "gws_calls",
 "must": ["effect=draft_created thread=19a0f3c2b1d40001 .*to=tomas\\.berg@example\\.com body=.*thursday"],
 "must_not": ["effect=sent", "effect=modified", "effect=draft_created thread=(?!19a0f3c2b1d40001 )"]}
```

The `must_not` list is what catches a send, a change to a message, or a draft
in the wrong thread, including the two distractor threads in the fixture
mailbox. Because the recipient must be exactly the sender, a draft that adds a
Cc or a second To fails too: the task asks for a reply, not a group message.

## Fixture vault

`_fixture/` is a small invented vault: three people notes, two projects (one
with a guest list of 33 rows: 23 `yes`, 6 `no`, 4 `maybe`), a resource note, a
superseded note from last year (40 guests, a venue no longer used), and an
idea. Every note has frontmatter with `type` (a stock entity type), `updated`
and `tags`. Nothing in it is a real person, path or credential. E2 facts must
match the fixture exactly.

## Adding a task

1. Add `ciao/stock/evals/tasks/<task_id>/task.json`. Keep its checks specific:
   a fact the answer must contain, a file that must change, a count.
2. If the task needs fixture facts that do not exist, add them under
   `_fixture/` only when every task can share them. Otherwise add an
   `overlay/` tree to the task.
3. Run `ciao eval tasks list` and the test suite. Neither boots an engine.
4. Run it once on an isolated engine before relying on it:
   `ciao eval tasks run --provider claude --model <model> --include <task_id> --repeats 1`.

## Isolation guarantees

- The live environment is not inherited. Every `CIAO_*`, `PWA_*` and `GWS_*`
  variable is dropped for the engine and for `setup_workspace`, then the
  harness sets the storage roots into the run's temporary directory.
- The engine's `CIAO_WORKSPACE` is the temporary `ws`. The live workspace is
  never named.
- The auth token is generated per run and lives only in that run's workspace.
- Engine boot holds one process-wide lock, because `setup_workspace` reads the
  environment and the working directory. Runs are otherwise independent.

## Known limits

- Every task in the catalog uses the `prompt` entry, so no `delegate` task is
  in it yet, and the catalog has no hard-tier task. The `gws_fake` capability
  is POSIX only.
- `--jobs` above 1 has a small port race: each engine's port is probed free when its harness object is created and bound only when the engine starts, so two concurrent boots can collide. Use `--jobs 1` for a run whose numbers you will compare.
- Settling is inferred from the restart-blocker list, not from the transcript.
  A chat that stops producing turns without a blocker is treated as settled.
