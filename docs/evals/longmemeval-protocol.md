# LongMemEval-S harness protocol

`scripts/memory-eval-longmemeval.py` is the harness behind the LongMemEval-S
numbers in [memory-comparison-2026-11.md](memory-comparison-2026-11.md). It
indexes one question's chat haystack with Ciaobot's real search path
(`ciao.fts_search`) and answers the question from what search returned, either
model-free (retrieval only) or with an answer model and a judge.

This page is the protocol: the dataset, the notes that get indexed, the three
answering modes, the exact prompts, the sampling rule, cost, and the limits.
Read [MEMORY_EVAL.md](../MEMORY_EVAL.md) for the deterministic suite that runs in
CI; this is the benchmark that needs a model and the dataset.

## What the benchmark is

[LongMemEval](https://github.com/xiaowu0162/LongMemEval) (Wu et al., ICLR 2025)
gives a chat assistant a long history and asks one question about it. Each of its
500 questions comes with a haystack of roughly 40–50 user/assistant sessions and
the ids of the sessions that hold the answer. The five abilities are information
extraction (single-session user/assistant), multi-session reasoning, knowledge
updates, temporal reasoning, and abstention (the answer is not in the history).

Ciaobot is measured as it actually recalls: Markdown notes, SQLite FTS5 keyword
search with BM25 ranking, no embeddings. The comparison in the November report
also runs an oracle (gold sessions only) and full-context reference, other local
memory systems, and a real-vault probe. This harness covers only the Ciaobot
rows; the other systems have their own harnesses.

## Dataset

The official cleaned snapshot, `longmemeval_s_cleaned.json`, from
`xiaowu0162/longmemeval-cleaned` on Hugging Face:

- URL: <https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json>
- sha256: `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`

The dataset is never bundled, never downloaded by the harness, and never
committed. Pass the file with `--dataset`. The harness refuses any other hash
unless you pass `--allow-other-dataset`; then the actual hash is written into
every JSONL row (`dataset_sha256`) and printed in the run header, so a report
can never be mistaken for the documented snapshot.

The file is a JSON array (the loader also accepts JSONL). The fields this harness
uses are `question_id`, `question_type`, `question`, `answer`, `question_date`,
`haystack_session_ids`, `haystack_dates`, `haystack_sessions` and
`answer_session_ids`. A question is an abstention question when its id ends in
`_abs`; it has no answer sessions.

> The hash above is the one the November report records. This repository does not
> contain the file, so the harness's own test cannot re-verify it; it pins the
> refusal behaviour against a synthetic file instead. Check the hash yourself
> before a run.

Treat the dataset as untrusted input: keep it in its own directory and run the
harness from the project venv (the harness itself does not use `-I`, because it
imports `ciao`).

## One question at a time

Every question is indexed alone, in a fresh temporary directory, and the
directory is deleted afterwards. The harness sets `CIAO_MEMORY_DIR` to a
throwaway path for that question (it wins over the install-owned
`<runtime>/vault-fts.db` in `fts_search.get_db_path`) and restores the previous
value afterwards. A run therefore cannot read or rebuild a live vault or the
live FTS database.

For one question:

1. Coalesce repeated session ids. The cleaned set has a handful of questions
   where the same session id appears twice with identical content; indexing it
   twice would let one session occupy several of the five result slots.
2. Write one Markdown note per unique session, named
   `<sanitized-date>-chat-sNN.md`. Session ids in the note are neutral (`s00`,
   `s01`, …, in haystack order) because LongMemEval's real ids reveal which
   sessions hold the answer. The date in the filename is sanitized to
   `[0-9A-Za-z-]`; the original date string is kept in the note's `updated`
   frontmatter and in the body.
3. Frontmatter carries `type: chat-session`, the title and the date; the body is
   one paragraph per turn, `role: content`. LongMemEval's per-turn `has_answer`
   flag is dropped — it would point the search at the answer.
4. Index with `ciao.fts_search.index_vault` (Porter-stemmed FTS5) and search the
   question text with `search_vault(limit=10)`, scoped to that question's stored
   key prefix. Map each returned note path back to the session it was written
   for, and score the **first five distinct sessions**.

The November protocol searched 10 and scored the first 5; this harness does the
same, and nothing uses results 6–10. They are not a second retrieval attempt and
do not reach the answer model.

## The three answering modes

The same top-5 retrieval feeds three configurations:

| `--mode` | What the answer model sees |
|---|---|
| `snippets` | The title and FTS-highlighted snippet for each of the top 5 notes. This is what `ciao vault search` prints. |
| `notes` (default) | The full Markdown text of each of the top 5 notes. |
| `agentic` | The full notes first, then up to three more searches the model asks for. Initial search counts as one, so at most four searches in all. |

The snippets/notes context uses LongMemEval's own wrapper:

```
### Session 1:
Session Date: <haystack date>
Session Content:
<the title + snippet, or the full note>
```

Sessions are sorted by date before rendering, as `run_generation.py` does. The
question date is passed as "Current Date".

In `agentic` mode each turn is one invocation of the answer command. The model
may reply with exactly `SEARCH: <query>` (the harness searches the same index,
shows the new notes in full, and marks already-shown notes instead of repeating
them) or `FINAL ANSWER: <answer>`. A reply with no marker is taken as the answer.
If the fourth search is used and the model still asks for more, the run records
an error for that question and does not invent an answer.

The agentic wrapper (the "search again" instruction) is **not** an official
LongMemEval prompt — the benchmark only ships one-shot answering. It is the
November rule written down: lexical search, two or three reformulations before
abstaining, at most four searches. It is defined in `AGENTIC_INSTRUCTIONS` in the
script; treat it as part of this protocol.

## Prompts

The answer prompt and the five judge prompts are copied verbatim from LongMemEval,
so a run is comparable to the benchmark's own evaluation.

### Answer prompt

From `prepare_prompt` in `src/generation/run_generation.py`, the chain-of-thought
template with retrieval (`cot=True`, no fact expansion). The source uses
positional `{}` slots; they are named here for readability, and the script's
constant keeps the text itself byte-identical.

```
I will give you several history chats between you and a user. Please answer the question based on the relevant chat history. Answer the question step by step: first extract all the relevant information, and then reason over the information to get the answer.


History Chats:

{history}

Current Date: {question_date}
Question: {question}
Answer (step by step):
```

### Judge prompts

From `get_anscheck_prompt` in `src/evaluation/evaluate_qa.py`. A question counts
as correct when the judge reply contains `yes` (the official evaluator's rule is
`"yes" in response.lower()`); a reply containing neither `yes` nor `no` is a
missing verdict, counted and reported, and makes the run exit non-zero.

Single-session user, single-session assistant and multi-session share one
template:

```
I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no.

Question: {question}

Correct Answer: {answer}

Model Response: {response}

Is the model response correct? Answer yes or no only.
```

Temporal reasoning adds the off-by-one-days allowance:

```
I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct.

Question: {question}

Correct Answer: {answer}

Model Response: {response}

Is the model response correct? Answer yes or no only.
```

Knowledge update:

```
I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.

Question: {question}

Correct Answer: {answer}

Model Response: {response}

Is the model response correct? Answer yes or no only.
```

Single-session preference uses the benchmark's `answer` field as a rubric:

```
I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.

Question: {question}

Rubric: {answer}

Model Response: {response}

Is the model response correct? Answer yes or no only.
```

Abstention (`question_id` ends in `_abs`):

```
I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.

Question: {question}

Explanation: {answer}

Model Response: {response}

Does the model correctly identify the question as unanswerable? Answer yes or no only.
```

The script's constants keep the source whitespace, including the trailing space
before a blank line in the shared and temporal templates.

### The commands that run the models

`--answer-cmd` and `--judge-cmd` are shell templates with two placeholders:
`{prompt_file}` (the prompt is written there) and `{out_file}` (a template may
write the reply there; stdout also works). The defaults are the November
invocation:

```
claude -p --model claude-haiku-5-5 --tools "" --setting-sources "" \
  --strict-mcp-config --no-session-persistence \
  --system-prompt "You are a helpful assistant." "$(cat {prompt_file})" > {out_file}
```

and for the judge:

```
claude -p --model claude-sonnet-5-5 --tools "" --setting-sources "" \
  --strict-mcp-config --no-session-persistence \
  --system-prompt "You are a helpful assistant." "$(cat {prompt_file})" > {out_file}
```

Every environment variable whose name ends in `_API_KEY` is removed before each
command runs, as in the November run: a subscription CLI stays a subscription
CLI. The commands run with no tools, no settings sources, no MCP servers and no
session persistence. Any other answerer or judge works — `opencode run`, a local
model wrapper, a script — as long as it reads the prompt file and writes or
prints the reply. The model names above are the ones the published numbers used;
the script does not check that the CLI has them.

## Sampling

The 60-question QA sample is stratified, seeded and reproducible:

- Strata: `question_type`, except that an id ending in `_abs` is its own
  `abstention` stratum regardless of its nominal type.
- Sizes, in draw order: knowledge-update 10, temporal-reasoning 12, multi-session
  12, single-session-user 6, single-session-assistant 6, single-session-preference
  6, abstention 8.
- Within a stratum, questions are sorted by id and drawn with
  `random.Random(20261108).sample(...)`, one `sample` call per stratum in the
  order above. The same RNG object is reused across strata.

`--sample N` scales the same proportions for other sizes; `N=60` reproduces the
published table exactly. A stratum with too few questions is a hard error, not a
quiet redistribution. `--questions PATH` overrides sampling entirely and runs
exactly the ids in the JSON array, in that order; `--questions-out PATH` writes
the drawn ids so a later run can be replayed with `--questions`.

The report groups abstention separately (the November table has an `Abs`
column). The benchmark's own `evaluate_qa.py` groups `_abs` questions under their
nominal `question_type`, so its per-type numbers are not directly comparable to
this table's.

## Output and what counts as incomplete

`--out` is JSONL, one row per question, written as the question finishes. A row
carries the question id, type and stratum, the question and its date, the
dataset hash, the mode, gold and retrieved session ids, the strict/fractional/
any-hit values, the answer, the judge verdict and raw reply, estimated context
tokens, per-phase latency, and `error`.

- Accuracy is correct / n overall and per type. An incomplete question is in the
  denominator and shown separately, never dropped; per type, `judged < n` means
  some questions in that type did not produce a usable verdict.
- Retrieval is strict@5 (all gold sessions in the top 5), fractional@5 (share of
  gold sessions in the top 5) and any-hit@5 (at least one), over answerable
  questions only. Abstention questions have no gold sessions and are excluded
  from all three denominators.
- "Judge missing or failed" counts questions without a usable verdict, including
  ones whose answer command failed and the judge was skipped.
- The process exits non-zero if any question errored, and prints each one with
  its reason.

Latency is wall time around the index, search, answer and judge steps. Context
tokens are an estimate, `characters / 4`; the harness does not see the provider's
token counts. For `agentic`, context characters are all retrieved history
accumulated across the searches, and answer latency is the sum of the calls.

## Reproducing a published number

The only published LongMemEval-S QA numbers are the November 60-question ones:
86.7% for top-5 full notes and 90.0% for agentic, with the per-type breakdown in
[memory-comparison-2026-11.md](memory-comparison-2026-11.md). No full
500-question QA run has been published.

Set up:

```bash
mkdir -p ~/longmemeval
curl -L -o ~/longmemeval/longmemeval_s_cleaned.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
shasum -a 256 ~/longmemeval/longmemeval_s_cleaned.json
# expect d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442
```

60-question stratified reproduction, top-5 full notes:

```bash
.venv/bin/python scripts/memory-eval-longmemeval.py \
  --dataset ~/longmemeval/longmemeval_s_cleaned.json \
  --sample 60 --seed 20261108 \
  --mode notes \
  --out runs/longmemeval-ciao-notes-60.jsonl \
  --questions-out runs/longmemeval-sample-60.json
```

The same sample in agentic mode (it reads the id list the first command wrote):

```bash
.venv/bin/python scripts/memory-eval-longmemeval.py \
  --dataset ~/longmemeval/longmemeval_s_cleaned.json \
  --questions runs/longmemeval-sample-60.json \
  --mode agentic \
  --out runs/longmemeval-ciao-agentic-60.jsonl
```

Both use the default `claude -p` commands above. Compare the `TOTAL` line, the
per-type table and the retrieval block with the published file.

A full run over all 500 questions (470 answerable plus 30 abstention), which has
not been published before:

```bash
.venv/bin/python scripts/memory-eval-longmemeval.py \
  --dataset ~/longmemeval/longmemeval_s_cleaned.json \
  --mode notes \
  --out runs/longmemeval-ciao-notes-500.jsonl
```

The model-free retrieval benchmark over every answerable question, no model
calls:

```bash
.venv/bin/python scripts/memory-eval-longmemeval.py \
  --dataset ~/longmemeval/longmemeval_s_cleaned.json \
  --retrieval-only \
  --out runs/longmemeval-retrieval-500.jsonl
```

This indexes all 500 questions and scores the 470 with answer sessions. It is
the command that produces the 470-question strict@5 / fractional@5 / any-hit@5
table in the November file.

### If the reproduction does not match

The original harness was never committed. This one follows the written protocol.
The details below are not recoverable from the report and are therefore *defined
here* rather than reproduced:

1. **Snippet layout.** The November harness's exact line layout for
   `--mode snippets` is unknown. This harness prints `<title>: <snippet>`; the
   full-note modes are unaffected.
2. **Note filenames.** The report says `<date>-chat-sNN.md`, but LongMemEval dates
   contain `/`, spaces and parentheses. This harness sanitizes the date instead
   of creating directories, and keeps the original date in the note.
3. **RNG call order across strata.** This harness draws in the order listed
   above. The report names the strata and sizes but not the order; a different
   order yields different question ids from the same seed.

Check `--questions-out` against the ids in the published run first. If the ids
differ, the sample differs and the accuracy number cannot be expected to match.
If the ids match but the accuracy does not, the likely causes are the answer
model actually behind `claude -p`, the CLI's own defaults, and the snippet/note
formatting. Say which of those changed rather than presenting a re-run as the
same number.

Do not compare against a run whose `dataset_sha256` is not the documented value.

## Cost

The model-free retrieval run costs nothing beyond CPU: one index build per
question (the November run measured a median of 25 ms per haystack) and one
query (median 2.8 ms). A 500-question retrieval run is minutes, not hours, plus
the cost of writing and deleting 500 temporary vaults.

For QA, the November report measured:

| Mode | Context per question | What that means for a 60-question run |
|---|---|---|
| `notes` | 15.1k tokens (characters / 4) | About 0.9M input tokens for the answer calls, plus 60 judge calls |
| `agentic` | 50.8k input tokens, all calls | About 3.0M input tokens, plus 60 judge calls |

The judge prompt is the question, the reference answer and the model response —
small next to the retrieved context, but it is one call per question. Output
tokens are the answer plus a one-line verdict.

Dollar cost is the token counts multiplied by the current price of whatever
model `--answer-cmd` and `--judge-cmd` point at. The script does not estimate
prices, and this page does not restate vendor pricing; the model ids in the
November run are the ones to price if you want to reproduce its bill.

## Curated ingest

Everything above is `--ingest raw`, the default and the November protocol: every
session becomes a note, so the benchmark measures search over a full dump of the
history. Ciaobot does not keep a dump. When a chat is archived, a memory pass
reads the transcript and decides what becomes a note, what becomes an entry in
the bounded `ciao:memory` / `ciao:profile` regions of `AGENTS.md`, what waits in
the proposal queue for a person, and what is dropped. `--ingest curated` replays
each haystack session through that step and answers over what is left, so the
score credits a pass that kept the right facts and penalises one that dropped
them.

### What runs

For one question, in a fresh temporary directory:

1. A throwaway install: a `CiaoConfig` rooted in the temp directory, the
   workspace seed every agent root gets at startup
   (`sync_workspace_skills(..., refresh_upstream=False)`: the stock `AGENTS.md`
   with empty memory regions, the stock skills and commands), a
   `ProjectChatManager`, the transcript store, and the agent control plane
   (`CiaoMcpService` + `CiaoControlPlane`) served on an ephemeral loopback port
   with the engine's own `/agent/v1/{op}` handler. The pass's `ciao memory
   update`, `ciao vault search` and other agent commands reach this install and
   nothing else.
2. Sessions in date order (repeated ids coalesced, as in raw ingest). Each one is
   recorded as a chat in the workspace's General project, one turn per
   user/assistant exchange; only `role` and `content` are read, so `has_answer`
   never reaches the pass. The chat title is neutral (`Chat session s03`).
3. The chat is archived the way the PWA's archive route does it:
   `ProjectChatManager.archive_chat` writes the Markdown transcript, then
   `run_archive_postprocess` decides whether a pass is due and queues it. The
   pass chat starts with the engine's own prompt
   (`MemoryPassCoordinator._prompt_for`: `MEMORY_PASS_PROMPT` plus its
   observation, skill-review and rules sections) behind the engine's own context
   capsule, and settles through `on_turn_finished` (archived when clean, left in
   `attention` when not).
4. `--proposals` is applied to whatever the pass queued, through the PWA's batch
   route (`proposals_batch`), before the next session is archived.
5. When every session is done, the workspace vault is indexed with
   `fts_search.index_vault`. That index excludes `Logs/`, so the archived
   transcripts are not searchable: answering reads only the notes the passes
   wrote (plus the `Workspace/` queue and observation files, which the real
   index includes too), and the two memory regions, which every real chat
   loads. The regions are prepended to the answer context as
   `### Workspace memory (loaded in every chat):`; the retrieved notes follow in
   the same LongMemEval wrapper as raw ingest, with an empty `Session Date`
   (a note is not a session).

Two seams are not engine code:

- **The provider.** The engine would start a Claude or OpenCode session for the
  pass. Here every chat's provider is a `CommandProvider` that runs
  `--extract-cmd` with the prompt, environment and working directory the engine
  built for that turn — the same operator-pointed mechanism as `--answer-cmd`.
  It is the narrowest replaceable point: `start_stream`, the request builder,
  the capsule and the settle logic around it all run as they do in a chat.
- **The clock.** `ciao.transcripts._now_iso` and the capsule's `datetime` are
  frozen at the session's LongMemEval date while it is recorded and its pass
  runs, so the transcript and the pass's `today=` line say when the
  conversation happened. Other engine stamps keep the wall clock: a proposal
  batch heading in `Memory-Proposals.md` and the learned-at stamp an accepted
  `[memory]` fact gets (`[2026-11-11]` on a run made that day).

Any one-shot model call the engine makes on its own (`ciao.providers.oneshot`,
used by a `[project]` doc fold, an opt-in reconcile, or an LLM chat title) is
refused during curated ingest and counted; a question where that happened is
reported incomplete, because the store would then depend on a model the
operator did not choose.

### Flags

- `--ingest curated` — off by default. Raw ingest output is unchanged by this
  mode: `tests/fixtures/longmemeval_raw_golden.jsonl` pins every raw row and
  every raw answer prompt, byte for byte, as the harness wrote them before
  curated ingest existed.
- `--extract-cmd` — required. A shell command with placeholders, substituted
  literally and shell-quoted like `--answer-cmd`'s:
  - `{prompt_file}` — the capsule and the pass prompt, exactly as the engine
    built them;
  - `{out_file}` — where the reply may be written (stdout also works);
  - `{system_prompt_file}` — the Ciaobot system instructions the Claude
    provider appends to Claude Code's own system prompt;
  - `{disallowed_tools}` — the comma-separated tools the engine denies this turn.

  It runs in the temp install's agent root (where the seeded `AGENTS.md` and
  `.claude/skills` are), with the turn's environment: `CIAO_WORKSPACE`,
  `CIAO_VAULT_ROOT`, `CIAO_AGENT_URL` and `CIAO_AGENT_TOKEN` point at the temp
  install, the engine's own `ciao` leads `PATH`, every inherited `CIAO_*` is
  removed, and so is every `*_API_KEY`, as for the answer and judge commands.
  It has to be an agent that can run shell commands and edit files: the pass
  reads the transcript, writes notes and calls `ciao`. A Claude Code
  invocation along these lines should work, but **it has not been run** for
  this page:

  ```bash
  --extract-cmd 'claude -p --model claude-haiku-5-5 --permission-mode bypassPermissions \
    --strict-mcp-config --no-session-persistence \
    --append-system-prompt "$(cat {system_prompt_file})" \
    --disallowedTools {disallowed_tools} "$(cat {prompt_file})" > {out_file}'
  ```

- `--proposals accept|drop|leave` — required with `--ingest curated`; the run
  refuses to start without it. A proposal waits for a person, so the run has to
  say what that person does, after every pass:
  - `accept` — accept every queued row whose accept is a deterministic write:
    `[memory]`, `[profile]` (and legacy `[user]`), `[people]`, `[learnings]`.
    Everything else (`[project]` folds a doc with a model call; `[review]`,
    `[category]`, `[rehome]`, `[note_edit]`, `[repo]` and skill proposals need a
    person's choice) stays queued and is counted as `unaccepted` by kind. Rows
    marked `decide: you` are accepted too: this simulates an owner who says yes.
  - `drop` — dismiss every queued row.
  - `leave` — leave them queued. The queue is
    `Workspace/Memory-Proposals.md` in the vault, which the real index searches,
    so a left proposal is still findable by text; it is not in the regions.

  Observations (`ciao observation-add`) are never promoted by the harness; the
  nightly Workspace care run that would propose them after three sightings does
  not run.

### What it reports

Every curated row carries `ingest`, `proposals`, `retrieved_note_paths` and a
`curation` object:

- `sessions`, `haystack_sessions`, `passes_ok`, `passes_failed`,
  `passes_skipped` (the engine skipped the pass, e.g. an empty transcript),
  `sessions_with_writes`, `extract_latency_ms`, and a per-session `passes` list
  (status, latency, proposals filed, lines added; the error and stderr tail of a
  failed pass).
- `proposals` — filed, accepted, dropped, failed to resolve, unaccepted by kind,
  pending at the end.
- `store` and `baseline_store` (the fresh install before any session): notes,
  their characters and bytes; `Workspace/` files, characters and bytes; region
  characters and entry counts. `raw_chars` / `raw_bytes` are what raw ingest
  would have stored for the same haystack.
- `evidence_retained` — whether any line a gold answer session's pass wrote is
  still in the store at the end (notes, `Workspace/` files or regions). Lines
  are attributed by diffing the store before and after each session; blank
  lines, separators and bookkeeping frontmatter stamps (`updated:` and the like)
  are ignored, so re-stamping a note does not count. `evidence_sessions_retained`
  lists which gold sessions survived. `null` for abstention questions.
- `evidence_in_context` — whether such a line reached the answer context: in one
  of the top five notes, or in the regions.
- `retrieval` keeps raw ingest's shape (strict/fractional/any-hit @5), but a gold
  session counts as retrieved when one of the top five notes holds a line its
  pass wrote. It is not the same measurement as raw retrieval and should not
  share a column with it.
- `contradictions` — always `null`. Counting contradictory or superseded facts
  left behind needs a judgement about meaning, not a text diff, and is not
  measured.

The printed summary adds a `Curated ingest` block: pass counts, mean store size,
stored versus raw characters (with the fresh-install baseline subtracted for the
ratio), proposal totals, the retained-evidence and evidence-in-context rates over
answerable questions, and `knowledge-update` and abstention accuracy on their own
lines. Per-type accuracy is the same table as raw ingest.

A failed pass makes the question incomplete: the replay stops at that session
(later passes would spend model calls on a store Ciaobot would not hold), the
row's `error` starts with `curate:`, and nothing is answered or judged.

### Cost

Curated ingest runs one agent session per haystack session per question, before
any answering. LongMemEval-S has about 50 sessions per question, so a
60-question run is about 3,000 extraction runs, each reading a whole session
transcript plus whatever it reads from the vault. Sessions repeat across
questions, but every question replays its own haystack into its own install, so
nothing is reused. No curated run has been made against the dataset yet; the
only runs so far are the unit tests and a two-question smoke on the synthetic
haystack, both with the deterministic fake extractor in
`tests/fixtures/longmemeval_fake_extractor.py`.

### Limits

- **No sandbox.** In a workspace with workspace-only file access (the temp
  install is one), a real pass runs inside the provider's filesystem sandbox,
  limited to the workspace and a read-only grant on the archive. The harness
  cannot apply that to an arbitrary command; the capsule still tells the agent
  its file roots, but the command is trusted to stay in the temp install.
- **One agent, not the provider.** The engine's provider adds its own
  behaviour (the Claude SDK's preset system prompt, hooks, MCP wiring, retries).
  `--extract-cmd` replaces all of it; how close a run is to a real pass depends
  on how close that command is to the provider.
- **Every session is a separate, attended chat in the General project.** Real
  chats belong to projects with canonical docs, are often longer, and can be
  auto-titled; none of that is simulated.
- **No nightly curation.** The Workspace care run, which settles `decide:
  nightly` proposals and promotes recurring observations, does not run.
  `--proposals` stands in for its decisions and for the owner's.
- **Attribution is lexical.** A pass that rewrote a fact into a new line is
  credited to the session it was processing, even if the fact came from an
  older note it consolidated; a fact the pass kept by leaving an existing line
  untouched is credited to whoever wrote that line first.
- **Wall-clock stamps.** See the clock seam above: queue headings and accepted
  facts carry the run date, which a temporal question can trip over.

## What this cannot establish

- **Not a confidence interval.** Every published number is a single run on one
  machine. The 60-question sample is one question = 1.7 points overall and up to
  17 points inside a six-question type. Differences of one or two questions are
  noise. The 470-question retrieval numbers are steadier but still one run.
- **Model-dependent.** The published QA numbers use Haiku to answer and Sonnet to
  judge, both through the Claude CLI. A different answerer changes the accuracy;
  a different judge changes the labels. The retrieval numbers are model-free and
  do not have this problem.
- **The judge can be wrong.** Sonnet with LongMemEval's prompts is still an LLM
  judge. It can accept an answer that is wrong and reject one that is right; the
  November run did not hand-check every verdict.
- **Synthetic histories.** LongMemEval-S chats are generated. Its questions may
  share more words with the answer session than a person's real question would,
  which would favour keyword search. The real-vault probe in the November file
  was built to test that, and it is one person's vault, not a benchmark.
- **Retrieval is not answering.** Strict@5 says the right session was in the top
  5. It says nothing about whether the answer model read it, understood the
  update, or abstained correctly. The gap between the snippets and notes rows in
  the November table is exactly that distinction.
- **One engine commit.** Search results change with `ciao.fts_search`. A run on a
  different commit is a different measurement; record the commit with the output.
- **No full 500-question QA result exists yet.** The only published QA sample is
  60 questions. The 500-question command above produces a new result, not a
  reproduction of an old one.
- **Reconstructed harness.** The original script is gone. This harness implements
  the written protocol; the three unrecoverable details above can shift absolute
  numbers even when the question ids match.
- **Token estimates.** Context tokens are characters / 4, not the provider's
  count. Use them for relative comparison and for a rough bill, not for an exact
  invoice.

## Files

- `scripts/memory-eval-longmemeval.py` — this harness.
- `tests/test_longmemeval_harness.py` — synthetic three-session test: JSONL rows,
  strict/fractional/any-hit, incomplete counting, hash refusal, the raw-ingest
  golden, and curated ingest end to end through the real memory pass with a fake
  extractor. No network, no model, no real dataset.
- `tests/fixtures/longmemeval_raw_golden.jsonl` — raw-ingest rows as the harness
  wrote them before curated ingest existed.
- `tests/fixtures/longmemeval_fake_extractor.py` — the deterministic
  `--extract-cmd` the curated tests use.
- [memory-comparison-2026-11.md](memory-comparison-2026-11.md) — the published run
  and the comparison with other systems.
