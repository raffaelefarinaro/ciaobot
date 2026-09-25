# Insights sandbox (#594)

A dev harness that answers one question: **does a full Ciaobot chat session do
a better end-of-conversation memory pass than today's one-shot extraction?**

It is not shipped product code. Nothing here is imported by `ciao/`, and no
setting, route or PWA surface changes because of it.

## What it does

The #586 comparison put a *read-only* agent next to today's one-shot
extraction, and the agent barely used its tools — a median of one call, spent
reading the transcript. It had no `ciao` CLI, no skills, and no way to check
what memory already held, so it could not do the thing a chat actually does at
the end of a conversation: **write**.

This harness measures that properly, by letting both arms write for real:

| arm | what it is |
|---|---|
| `oneshot` | today's production chain, in process: `insights.extract_and_append(..., text_mode=True)` → insights, project-doc fold, memory proposals with auto-promote |
| `agent` | a normal **attended** chat on a second Ciaobot server, given the transcript and asked to do the pass the way it normally would |

Both arms process the same archived chats, read from the #586 cache
(`.runtime/insights_compare/<run-id>/`), with the same model per chat. The
report is a **git diff**: what each arm really wrote, per chat, side by side.

A full agent can write, so the dry run is enforced by *where* it runs, not by
which tools it has. A restricted agent is not the behaviour under test.

## Safety

Three APFS clones (`cp -c -R`, instant) of the live workspace live under
`<sandbox-root>/<run-id>/`: `base`, `oneshot`, `agent`.

Before anything touches a clone, `sandbox.prepare_clone` neutralises every
thing in it that reaches outside:

- the git `origin` remote is removed — otherwise `_branch_backup_loop` pushes
  every 30 s;
- `.runtime/push_subscriptions.json` is deleted — no phone notifications;
- every user and system schedule is disabled, including the packaged
  `system-*` ids and their per-workspace fan-out — otherwise a tick fires
  memory curation and skill evolution mid-run;
- `.runtime/startup_triage.json` is stamped — no startup triage chat;
- `.runtime/background/` is emptied — no orphaned task is woken on boot;
- on the **agent** clone, `insights_enabled` and `trajectories_enabled` are
  set false — the harness's own chats must not trigger the archive pipeline;
- every `.mcp.json` is deleted, `.env` is reduced to `CIAO_*` and `PWA_*` keys
  *minus any whose value is a path* (an absolute or `~`-prefixed location, or
  one containing `..`; the dropped keys are named in `prep.log`), and each
  workspace's `allowed_mcp_servers`, `claude_ai_mcps` and `gws_profile` are
  cleared — no integrations to reach through.

`--live` must be a **main repository**, not a linked git worktree. A linked
worktree's `.git` is a text file pointing at the source repository's git
directory, and `cp` copies it verbatim, so the copy is not a repository of its
own: the first `git remote remove` would strip the *source's* remote and every
per-chat commit would land in the source's metadata. The copy is refused
before any git command runs, and deleted rather than left behind.

The harness then refuses to run on the live path: every clone goes through
`sandbox.assert_sandbox_path`, and the one-shot arm re-asserts that its own
resolved `config.workspace_root` *is* the clone before the first model call.
`--live` is only ever read.

It also refuses to run on an install whose **paths point outside the clone**.
An absolute `CIAO_VAULT_ROOT`, an absolute per-workspace `vault_root`, and a
project `vault_doc_path` outside the workspace are all supported
configurations, and `CiaoConfig` deliberately preserves them — so a clone of
such an install would have both arms writing the original vault. Before
anything runs, each arm builds the config it is about to use and passes
`config.vault_root`, every selected `workspace_vault_root` and `agent_root`,
every selected archive and every resolved project doc through
`sandbox.assert_contained`. A path outside the clone stops the run with a
`SandboxError` naming it. That is a refusal, not a rebasing: the pilot is
refused on an install the clone cannot honestly measure, and the operator
points `--live` at a workspace with relative roots instead.

For the agent arm that check is done twice, because the server and the
harness do not read the same configuration. The harness builds its config from
an explicit env dict; the spawned `ciao run` calls `CiaoConfig.from_env()` with
no arguments, which loads the clone's own `.env` on top — so a clone whose
`.env` says `CIAO_VAULT_ROOT=/path/to/live-vault` resolves
`<clone>/memory-vault` in the preflight and the live vault in the server,
which then refreshes its index on boot. So before `Popen`, the harness asks a
child process — with the exact env and cwd the server gets — to resolve the
same configuration the same way, and every path it reports
(`workspace_root`, `vault_root`, `runtime_root`, `sync_root`, and each
workspace's `workspace_vault_root` and `agent_root`) goes through
`sandbox.assert_contained` too. That same probe runs for the one-shot arm.

`sync_root` is the repository git sync and branch backup actually push, which
is the one containing the *vault*: with the vault outside the workspace in a
repository of its own, the install root's remote is not the only one. The
harness removes the remote from that repository too, before the server exists,
and refuses (rather than touching a remote) if it is not inside the clone.

## Running it

Full cost warning first: **an agent arm chat on opus runs roughly $0.50–$1
per chat.** A 50-chat agent arm is tens of dollars. Start with `--limit`.

```sh
# A cheap pilot: one arm, two chats.
PYTHONPATH=$PWD python scripts/insights-sandbox/run.py \
    --run-id pilot-1 --arms oneshot --limit 2
```

Then the agent arm, on the same two chats:

```sh
PYTHONPATH=$PWD python scripts/insights-sandbox/run.py \
    --run-id pilot-2 --arms agent --limit 2
```

Useful flags: `--live` (default `~/repos/ciao`), `--sandbox-root` (default
`~/ciao-sandbox`), `--arms oneshot,agent`, `--from-cache <dir>` (repeatable),
`--limit N`, `--turn-timeout` (default 900 s).

The agent-clone server takes `--port 0` by default, which means "pick a free
port", so two runs — or a run alongside an instance you already have open —
cannot collide. An explicit `--port N` is checked before the server starts and
refused if anything already accepts connections on `127.0.0.1:N`; if the login
is then answered by a server that rejects the harness token, the run stops and
says so rather than talking to a stranger's instance.

The harness refuses to start while the live instance has chats in flight, and
refuses to reuse a `--run-id` whose directory already exists.

## Where the output lands

Everything for a run is under `<sandbox-root>/<run-id>/`:

```
prep.log        what prepare_clone neutralised, and each clone's baseline sha
selection.json  the chats, their models and their project docs
report.md       totals per arm, then a per-chat table
diffs/<arm>/    one .diff and one .stat per chat per arm
agent-server.log
```

Per-chat attribution is only worth anything if nothing else falls in the
window, so the agent clone gets two commits of its own before chat 1:
`harness: server boot`, after boot has been quiet for ten seconds, and
`harness: project setup`, after the harness's own per-workspace projects are
created. Both would otherwise be charged to chat 1.

Token columns in the report are read from each chat's own Claude session
transcript (`message.usage`, deduped by `message.id`, because a transcript
carries the same assistant record several times), not estimated. A chat runs
with its workspace's agent root as its working directory, and that — not the
install root — is what Claude encodes into the session's project directory, so
the transcript is looked up under `config.agent_root(row.workspace)`. A chat on
a provider whose usage the harness cannot read, or whose transcript is missing
or entirely zero, shows `-` rather than a row of zeroes.

The report contains counts and diffs, and **no verdict**. Which arm's writes
are better is a judgement about real memory, and a summary that made it would
be the thing under test.

## Cleaning up

```sh
rm -rf ~/ciao-sandbox/<run-id>
```

The live workspace was never written to, so there is nothing to restore.
