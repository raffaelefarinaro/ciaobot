# Memory comparison and evals: November 2026

- **Dates:** 8–9 November 2026.
- **Engine:** the comparison runs and the vault probe used develop as of 8–9 November, before #2716. The recall prompt evals compare a baseline on `8b9315c8` plus the H11 task (an uncommitted tree) with candidate runs that span `6e1ee923` (63 runs) and `36477064` (15 runs). The search fix was re-measured on develop `618ed22a`, which includes #2716, #2737 and #2741.
- **Answering and judging:** `claude-haiku-5-5` answers and `claude-sonnet-5-5` judges, both through the Claude CLI (`claude -p`) on a subscription. No API keys were set for any system.
- **Machine:** one macOS host (Apple silicon). Every number is from a single run.

## Purpose

Ciaobot's memory is a folder of Markdown notes, searched with SQLite FTS5 (keyword search, BM25 ranking) and read by the agent. There are no embeddings and no extraction model. We wanted to know three things:

1. How does that compare with other local memory systems on a public benchmark, at a comparable cost?
2. Where does Ciaobot lose answers: in search, or in what the agent does with the results?
3. Do the results hold on a real vault, not only on a synthetic benchmark?

The runs found two problems in Ciaobot, both now fixed (see [What changed](#what-changed)).

## Setup

### Benchmark

[LongMemEval](https://github.com/xiaowu0162/LongMemEval)-S, the cleaned Hugging Face snapshot (`longmemeval_s_cleaned.json`, sha256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`). Each question comes with a haystack of about 48 chat sessions between a user and an assistant, and the ids of the sessions that hold the answer.

- **Retrieval:** all 470 answerable questions (the 30 abstention questions have no answer sessions). Model-free.
- **Question answering:** a stratified sample of 60 questions, drawn with `random.Random(20261108)`: 10 knowledge-update, 12 temporal-reasoning, 12 multi-session, 6 each of single-session-user, single-session-assistant and single-session-preference, and 8 abstention questions (the system should say the answer is not there).

### Answering and judging

Every system gets the same answering and judging:

- The answer model receives the system's retrieved context and LongMemEval's own chain-of-thought answer prompt (from `run_generation.py`), with the question date as the current date.
- The judge receives LongMemEval's official per-type judge prompts, copied verbatim from `src/evaluation/evaluate_qa.py`, including the abstention prompt. A question counts as correct when the judge answers yes.
- The Claude CLI runs with no tools, no settings, no MCP servers and no session persistence, with every `*_API_KEY` removed from the environment.

### The cost rule

We compared only systems whose memory can be built without a language-model call per session. A system that runs a language model over every conversation it stores pays for that on every write: it needs an API key, a second subscription, or a local model large enough to extract facts well. That rules out several well-known systems (see [Systems not run](#systems-not-run-and-why)). Embeddings computed by a small local model are allowed.

### Systems and their configurations

Each system indexes only one question's haystack at a time, in a fresh, isolated store. Session ids in the indexed text are neutral (`s00`, `s01`, …), because LongMemEval's real ids reveal the answer sessions.

| System | Version | Configuration we ran | Limits of this configuration |
|---|---|---|---|
| Ciaobot | develop, Nov 2026 | One Markdown note per session, indexed by `fts_search.index_vault`, searched by `fts_search.search_vault` (the `ciao vault search` path), top 5. Three ways of answering: from the top-5 snippets, from the top-5 full notes, and agentic (below). | Keyword search only: no stemming of synonyms, no embeddings. |
| Basic Memory | 1.0.0 | Fresh home per question, `bm reindex --search --embeddings` (FTS plus local FastEmbed bge-small), `bm tool search --mode hybrid`, first 5 distinct notes, read in full. | Its best mode we found. Indexing is slow on CPU. |
| OpenClaw memory-core | 2026.11.2 | Built-in engine. Sessions written into the daily notes `memory/YYYY-MM-DD.md` of their date, `openclaw memory index`, `openclaw memory search --max-results 5`. Two providers: none (keyword only) and local Ollama `nomic-embed-text` (hybrid). Defaults otherwise: MMR on, recency decay on with a 30-day half-life. Reported two ways: its native snippets, and its hits expanded to the full source sessions, as the others read. | Recency decay and MMR are on by default, and we found no setting to turn them off or anchor them to the question date. Decay is computed against the machine's clock, so on a 2023 dataset it cannot work as designed. |
| Cognee | 1.6.7 | Local-only: GLiNER graph extraction and fastembed embeddings, CPU, no LLM. One `remember()` per question over all sessions, then `recall()` with default routing (`HYBRID_COMPLETION`, context only), `top_k=5`. | Cognee's normal pipeline uses an LLM to build the graph and to answer. Without one, the context it returns is thin (about 2.9k tokens). This is not its flagship configuration. |
| Holographic (`hermes-plugin-holographic`) | 0.26.5 | Local SQLite FTS5 plus HRR, `auto_extract` on (rule-based, no model), embeddings off (default). Each session fed through `sync_turn` and `on_session_end`, then `prefetch(question)`. | `auto_extract` is off by default: Holographic's intended path is the agent calling `fact_store` itself. We measured only the rule-based extractor, on the first 20 sampled questions (10 knowledge-update, 10 temporal). It kept only preference sentences and stores no session dates. This is not a measure of Holographic as used. |

Two references bracket the results:

- **Oracle:** the answer model reads only the gold sessions. An upper bound for retrieval.
- **Full context:** the answer model reads the whole haystack (about 126k tokens). What you get with no memory system and a large enough context window.

**Ciaobot agentic** is closest to how Ciaobot recalls in a chat. Haiku sees the top-5 full notes for the question text, then may issue up to three more searches of its own wording (`SEARCH: <query>`), so up to four searches in all. Only Ciaobot got these extra searches: every other system answered from one search with the question text. Their own agents could also search again; we did not give them that loop. New hits are shown in full. It is told the search is lexical and to try two or three reformulations before saying the answer is not there.

## Results

### Question answering, 60 LongMemEval-S questions

Correct answers per question type. KU knowledge-update, TR temporal-reasoning, MS multi-session, SSU/SSA single-session user/assistant, Pref single-session preference, Abs abstention. *Index* is the mean time to build one question's index (about 48 sessions). *Context* is the mean retrieved context handed to the answer model, estimated as characters ÷ 4.

| System | Accuracy | KU /10 | TR /12 | MS /12 | SSU /6 | SSA /6 | Pref /6 | Abs /8 | Index | Context |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| *Oracle (gold sessions only)* | *93.3% (56)* | 10 | 12 | 11 | 6 | 6 | 6 | 5 | – | 7.3k |
| *Full context (whole haystack)* | *91.7% (55)* | 10 | 12 | 10 | 6 | 6 | 5 | 6 | – | 126k |
| **Ciaobot, agentic (up to four searches)** | **90.0% (54)** | 10 | 11 | 9 | 6 | 6 | 5 | 7 | 0.07 s | 19.1k |
| **Ciaobot, top-5 full notes (one search)** | **86.7% (52)** | 10 | 11 | 8 | 6 | 6 | 5 | 6 | 0.07 s | 15.1k |
| Basic Memory, hybrid | 83.3% (50) | 10 | 11 | 8 | 6 | 6 | 5 | 4 | 497 s | 15.2k |
| OpenClaw, embeddings, full sessions | 71.7% (43) | 6 | 7 | 5 | 6 | 6 | 6 | 7 | 49 s | 9.6k |
| Cognee, local | 48.3% (29) | 4 | 3 | 2 | 5 | 6 | 3 | 6 | 1,726 s | 2.9k |
| OpenClaw, embeddings, native snippets | 43.3% (26) | 4 | 2 | 2 | 3 | 4 | 4 | 7 | 49 s | 0.9k |
| Ciaobot, snippets only | 43.3% (26) | 7 | 4 | 1 | 4 | 1 | 1 | 8 | 0.07 s | 0.3k |
| OpenClaw, keyword, full sessions | 40.0% (24) | 5 | 5 | 1 | 3 | 1 | 2 | 7 | 14 s | 10.4k |
| OpenClaw, keyword, native snippets | 25.0% (15) | 2 | 1 | 1 | 2 | 1 | 1 | 7 | 14 s | 0.9k |

Not ranked: Holographic's rule-based extractor, run on the first 20 sampled questions only (10 knowledge-update, 10 temporal), answered 0/20, with an index time of 0.4 s and almost no context returned. That configuration is not how Holographic is meant to be used (see its row above), so it is left out of the table.

The agentic run's answer model read 50.8k input tokens per question on average, across all its calls. A single-pass system reads about its context plus the prompt. On one search each, Ciaobot's 86.7% is the like-for-like comparison with the other systems; the agentic 90.0% shows what the extra searches add.

**Index times are rough.** Basic Memory, OpenClaw and Cognee were indexed by parallel workers sharing the machine, while Ciaobot ran sequentially and in-process. The times include each tool's CLI start-up. Read them as orders of magnitude, not as benchmarks.

What stands out:

- **Reading the note matters more than the search engine.** The same Ciaobot search scored 43.3% when Haiku answered from snippets and 86.7% when it read the same five notes in full. OpenClaw's embedding search moved from 43.3% to 71.7% the same way. A snippet that holds the query words often leaves out the later change, negation or condition that decides the answer.
- **Retrieval reached the answer for most systems.** At least one gold session was among the retrieved sessions for 51 of 52 answerable questions with Ciaobot and Basic Memory, 52/52 with Cognee, 50/52 with OpenClaw embeddings and 36/52 with OpenClaw keyword search. Cognee's low score points to how little of those sessions its local context carries (about 2.9k tokens), not to finding the wrong ones.
- **Where Ciaobot agentic lost:** 5 of its 6 misses were questions whose answer is spread over 2–5 sessions and only part of it was retrieved. Two of those were caused by the search bug fixed in #2716. The other misses were reasoning errors, or a guess where it should have abstained.
- **Basic Memory** gave the closest result to Ciaobot's top-5 notes (83.3% against 86.7%). Its only clear gap is abstention (4/8). Its index took minutes per question on CPU in our parallel run, against well under a second for FTS5.
- **OpenClaw's keyword mode** reached a gold session for only 36 of 52 questions. Its embedding mode did much better. We found no setting to turn off its recency decay or anchor it to the question date (see above), which may cost it some knowledge-update and temporal questions.

### Search recall, all 470 answerable questions

Model-free. Each session is a document, the query is the question text, and we score the top 5:

- **Strict@5:** every gold session is in the top 5.
- **Fractional@5:** the share of gold sessions in the top 5.
- **Any-hit@5:** at least one gold session is in the top 5.

This is the protocol of agentmemory's published benchmark ([retrieval evaluation, 28 October 2026](https://github.com/rohitg00/agentmemory/blob/main/docs/benchmarks/2026-10-28-retrieval-evaluation.md), agentmemory 0.9.31, keyless BM25 component). Their numbers are theirs, from their harness, not re-run by us.

| Search | Strict@5 | Fractional@5 | Any-hit@5 |
|---|---:|---:|---:|
| Local embeddings (bge-small-en-v1.5) | 91.7 | 95.1 | 97.2 |
| Hybrid: FTS5 OR ranking + embeddings, RRF | 90.9 | 95.4 | 98.5 |
| **Ciaobot FTS5, after #2716** | **85.1** | **92.2** | **96.8** |
| agentmemory BM25 component (published) | 84.7 | 91.7 | 96.2 |
| Plain BM25 (rank_bm25 Okapi, no stemming) | 83.6 | 91.2 | 96.2 |
| Ciaobot FTS5, before #2716 | 78.7 | 88.4 | 96.0 |

Median Ciaobot query: 2.8 ms (p95 12.6 ms). Median index build for one haystack: 25 ms. The after-#2716 row was re-run on develop `618ed22a` with the merged code and matches the counterfactual we computed before the fix exactly.

Strict@5 by question type:

| Type | n | Ciaobot before | Ciaobot after | Plain BM25 | agentmemory BM25 | Hybrid (RRF) | Embeddings |
|---|---:|---:|---:|---:|---:|---:|---:|
| Knowledge update | 72 | 75.0 | 98.6 | 97.2 | 97.2 | 98.6 | 100 |
| Multi-session | 121 | 64.5 | 70.2 | 66.9 | 71.1 | 84.3 | 89.3 |
| Temporal reasoning | 127 | 75.6 | 78.0 | 78.0 | 78.0 | 83.5 | 84.3 |
| Single-session preference | 30 | 80.0 | 83.3 | 80.0 | 83.3 | 93.3 | 96.7 |
| Single-session user | 64 | 96.9 | 100 | 100 | 98.4 | 100 | 92.2 |
| Single-session assistant | 56 | 100 | 100 | 98.2 | 98.2 | 100 | 100 |

Most of the knowledge-update gap was the bug fixed in #2716. Ciaobot ran an AND-of-all-words query and fell back to OR only when AND found nothing. When AND matched one or two notes, it returned only those. The note with the old value and the note with the new value rarely share every query word, so one of them was dropped. Embeddings still lead clearly on multi-session questions.

## The real-vault probe

The benchmark is synthetic chat history. To check whether the findings hold on real notes, we ran a probe on the maintainer's own vault: 120 indexed notes, 88 questions (75 synthetic, 13 from real past chats). The synthetic questions were worded away from the notes' own words, so they do not simply repeat a note's title. 10 of the 88 have no answer in the vault and test abstention. It ran on a read-only copy. Only aggregate numbers are published here, with no topics, note names or question text.

Answer accuracy on the 78 answerable questions (Haiku answering, same judge):

| Condition | Accuracy | Real questions (13) |
|---|---:|---:|
| Snippets only (top 5) | 23.1% | 0.0% |
| Snippets + the top note in full | 48.7% | 15.4% |
| Top-3 full notes | 64.1% | 53.8% |
| Top-5 full notes | 74.4% | 61.5% |
| Agent with search and read tools, old rule ("answer from snippets") | 88.5% | 69.2% |
| Agent, "read the best-matching note" (≈ the rule merged in #2737) | 89.7% | 61.5% |
| Agent, "read up to three notes" | 92.3% | 76.9% |

All conditions abstained correctly on all 10 unanswerable questions, except "up to three notes" (9/10). The three agent rules are within a few questions of each other. "Up to three notes" won on 5 questions and lost on 3 (one of them the abstention miss) against the old rule, which is not a significant difference. On the 13 real questions, one question is 7.7 points.

Search recall (strict@5) on the 78 answerable questions:

| Search | All (78) | Two-note questions (15) | Real questions (13) |
|---|---:|---:|---:|
| Local embeddings (bge-small) | 82.1 | 60.0 | 76.9 |
| Hybrid: Ciaobot FTS5 + embeddings, RRF | 73.1 | 33.3 | 69.2 |
| Ciaobot FTS5 | 65.4 | 26.7 | 61.5 |
| Plain BM25 | 57.7 | 13.3 | 53.8 |

Embeddings find more on the real vault too, 17 points more than FTS5. The answers barely change, because the agent reformulates. In all 9 of the agent's wrong answers under the old rule, it had already seen the right note. Separately, embeddings would have changed retrieval in at most 2 of those 9 questions, so the expected gain is about 1–2 questions in 78. The cost is a local model of about 130 MB and a second index. We deferred embeddings and will revisit when keyword misses explain more than 3 of the agent's wrong answers.

## Prompt evals

These use Ciaobot's own task suite (`ciao eval tasks`, see [TASK_EVAL.md](../TASK_EVAL.md)). It runs a fresh, isolated engine on a synthetic vault and makes deterministic checks on the end state.

### Recall reads the best-matching note (#2737)

The old system-prompt rule told recall to answer from `ciao vault search` snippets, not full notes. The new rule uses the snippets to pick the best-matching note and reads it before answering. We added a task for the failure: **H11**, where a note's top line states an old value and a section further down states the current one. No query built from the question's words gets the current value into a snippet. A second, similar note acts as a distractor.

Full suite, `claude/haiku`: 22 tasks, 3 repeats each, with H1 and M6 extended to 9 runs.

| | Old prompt | New prompt |
|---|---:|---:|
| Passed (78 runs) | 63/78 | 65/78 |
| H11, value below the snippet | 0/3 | 3/3 |
| H1, multi-source synthesis (9 runs) | 8/9 | 7/9 |
| M6, memory pass, paraphrase of a decided fact (9 runs) | 3/9 | 2/9 |
| Median tool calls per run | 4 | 4 |

H1 and M6 fail the same way under both prompts. We read that as noise, not a regression. The prompt stays within its budget (11,980 → 11,975 characters, budget 12,000). The run files do not record model calls, so the table gives tool calls instead.

The baseline ran on `8b9315c8` plus the H11 task, from an uncommitted tree. The candidate runs span two commits on the #2737 branch: `6e1ee923` (63 runs) and `36477064` (15 runs).

The trade-off: recall now reads full private notes into the transcript, where a 32-token snippet used to keep unrelated private lines out. The prompt's rule that search results are private working evidence now names notes read as well.

### A lighter memory-pass prompt: rejected

Anthropic's ["Components of agent memory"](https://www.anthropic.com/research/components-of-agent-memory) (6 November 2026) reports that prescriptive memory-writing rules can hurt frontier models. We tested a lighter memory-pass prompt, 54% of the current length, that drops most of Ciaobot's exclusion rules. The test set is real archived chats with known accept/dismiss outcomes: 29 chats, with 21 facts the user had accepted and 21 they had dismissed. We counted how many accepted facts each prompt kept, and how many proposals of a kind the user had dismissed it queued.

| Run | Accepted facts kept (current / light) | Dismissed-kind proposals (current / light) |
|---|---:|---:|
| Haiku, run 1 | 3 / 5 | 4 / 11 |
| Haiku, run 2 | 4 / 4 | 2 / 13 |
| Sonnet | 2 / 0 | 1 / 0 (the light prompt queued nothing) |

With Haiku, the lighter prompt let dismissed-kind facts back in about four times as often (24 against 6 over two runs), for at most two more accepted facts. With Sonnet it queued nothing at all. We kept the current prompt. This does not test the finding for frontier models in general, only for Ciaobot's prompt on the models it runs by default.

### Multi-note recall baseline

Before changing anything else, we recorded a baseline on develop `4c4093dc` for tasks whose answer needs several notes: suite 53/63, multi-note tasks 16/18, single-note tasks 15/15. Multi-note tasks cost about 2 more model calls and 80k more context tokens per run. Following links from the matched note would have fixed neither of the 2 failures, so link expansion was not built.

## Systems not run and why

mem0, Hindsight, Honcho, OpenViking, ByteRover, Supermemory, RetainDB and memU all build memory by running a language model over each session (extraction, distillation or reflection). That fails the cost rule above.

mem0 can also store messages without extraction (`add(..., infer=False)`), but that skips the extraction its design and published scores rest on, so it would not measure mem0 as used, and we did not run it. Cognee is in the comparison without its language model because it ships a model-free extraction path (its GLiNER demo pipeline, with local embeddings); its row says what that costs it.

Several of these publish LongMemEval scores between about 83% and 94% (for example [mem0's research page](https://mem0.ai/research)). Those scores are self-reported and not verified by us, with answer and judge models of each vendor's choice rather than Haiku and Sonnet, so they cannot be placed in the tables above. A fair comparison would run them through the same answering and judging, with the extraction cost reported next to the accuracy.

## What changed

Merged into `develop`:

| PR | Change |
|---|---|
| [#2716](https://github.com/raffaelefarinaro/ciaobot/pull/2716) | Search tops up a partial "all words" result with "any word" results, keeping the AND hits first. Strict@5 78.7% → 85.1% on LongMemEval-S. |
| [#2737](https://github.com/raffaelefarinaro/ciaobot/pull/2737) | Recall reads the best-matching note instead of answering from snippets. Adds task H11. |
| [#2741](https://github.com/raffaelefarinaro/ciaobot/pull/2741) | `ciao eval tasks compare` pools every run of a task. It had kept only the first revision's runs, which showed H1 as 9/9 when the file held 7/9. |

Fixes found along the way, unrelated to memory quality:

| PR | Change |
|---|---|
| [#2745](https://github.com/raffaelefarinaro/ciaobot/pull/2745) | Deeply nested JSON bodies are rejected with an explicit depth limit, which also works on Python 3.14. |
| [#2749](https://github.com/raffaelefarinaro/ciaobot/pull/2749) | Ending a terminal session no longer drops typed input or large pastes. |
| [#2750](https://github.com/raffaelefarinaro/ciaobot/pull/2750) | A non-object frame on the chat websocket gets an error reply instead of closing the socket. |

Not changed: embeddings (deferred, see the vault probe) and the memory-pass prompt (the lighter one was worse).

Next:

- A two-note recall task, then test "read up to three notes" against the merged rule.
- A relevance floor for keyword search, which today never returns zero results.
- A re-run of the vault probe. Embedding-first search gets built if keyword misses explain more than 3 wrong answers.

## Read the numbers with care

- **Single runs on one machine.** No configuration was repeated, so a difference of one or two questions is within noise.
- **60 questions.** The QA sample is stratified and seeded, but small: one question is 1.7 points overall and up to 17 points within a six-question type. The 470-question retrieval numbers are steadier.
- **A synthetic benchmark.** LongMemEval-S chats are generated, and its questions may share more words with the answer session than real questions do, which would favour keyword search. The vault probe was built to check that, and it is still only one person's vault.
- **An LLM judge.** Sonnet with LongMemEval's prompts. It can be wrong in both directions, and we did not hand-check every verdict.
- **Haiku answering.** A small model. Larger models may close some gaps (snippets especially) and open others.
- **OpenClaw's recency decay** is computed against the machine's clock and could not be anchored to the question dates, so OpenClaw ran with a ranking signal that works against it on this dataset.
- **Other systems in one configuration each.** We ran each in the best configuration we found that fits the cost rule. Their maintainers might find a better one.

## Reproducing

The harness scripts are not in the repository yet. Until they are, this is the protocol, precise enough to rerun:

1. **Data.** Download `longmemeval_s_cleaned.json` from the LongMemEval Hugging Face dataset and check the sha256 above. Treat it as untrusted input: keep it in its own directory and run Python on it with `-I`.
2. **Retrieval (470 questions).** For each answerable question, coalesce repeated session ids (13 in the set, identical content) and write one Markdown note per session with front matter (`type`, `title`, `updated`) and the turns as `role: content` paragraphs. Index the notes with `ciao.fts_search.index_vault` into a temporary `CIAO_MEMORY_DIR`, search the question text with `search_vault(limit=10)` scoped to that vault, map paths back to session ids, and score strict, fractional and any-hit at 5 against `answer_session_ids`. Plain BM25 is `rank_bm25.BM25Okapi` (k1 1.5, b 0.75) over lower-cased `\w+` tokens of the whole session. Embeddings are `bge-small-en-v1.5` over turn-aligned chunks of up to 1,800 characters, max-pooled per session. Hybrid is reciprocal rank fusion with k = 60.
3. **QA sample.** Group questions by type, treat ids ending in `_abs` as an abstention stratum, and draw 10/12/12/6/6/6/8 (KU/TR/MS/SSU/SSA/Pref/Abs) with `random.Random(20261108)` from each stratum's questions sorted by id.
4. **Per-system context.** For each question, write one note per haystack session named `<date>-chat-sNN.md`, with neutral ids. Give each system a fresh, isolated store with no provider keys in the environment, index, query with the question text, and keep the top 5 (configurations in the table above). "Full sessions" variants replace a system's own snippets with the full notes of its first 5 distinct retrieved sessions.
5. **Answer and judge.** `claude -p --model claude-haiku-5-5 --tools "" --setting-sources "" --strict-mcp-config --no-session-persistence --system-prompt "You are a helpful assistant."`, with LongMemEval's CoT answer template. Judge each answer with `claude-sonnet-5-5` and the official `get_anscheck_prompt` for its type. Count "yes" as correct.
6. **Agentic Ciaobot.** Start from the top-5 full notes. Allow up to three `SEARCH: <query>` turns, each returning the top 5 from the same index, with notes already shown marked as such. Stop at `FINAL ANSWER:`. Judge as above.
7. **Prompt evals.** `ciao eval tasks run --provider claude --model haiku --repeats 3` on the baseline and candidate commits, each installed in its own venv, then `ciao eval tasks compare` (needs #2741 for pooled rounds). The memory-pass comparison replays archived chats from a private vault and cannot be published. The method is to run each prompt's memory pass on chats whose proposals the user already accepted or dismissed, and count both.
