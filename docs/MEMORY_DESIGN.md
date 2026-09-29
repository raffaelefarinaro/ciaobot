# Memory system design

Why Ciaobot's memory works the way it does. The mechanics live in
`docs/ARCHITECTURE.md` ("Memory, insights, and self-improvement"); this
document records the goals, the research the design leans on, and the
reasoning behind each choice — so a future change argues with the rationale,
not just the code. Written 2026-08; sources reflect the 2025–26 state of the
art in agent memory.

## Goals

A personal assistant's memory must be, with **zero user configuration**:

1. **Valuable** — it keeps what will matter in a future session, not trivia.
2. **Current** — a changed fact replaces its predecessor; stale facts are
   findable but never assert themselves as fresh.
3. **Connected without inventing** — links between facts are grounded in
   retrieved evidence, never generated free-form.
4. **Recallable** — the model can actually find a fact when it needs it,
   including from a paraphrase.
5. **Non-bloating** — no surface grows without bound; the machinery never
   pollutes the store with its own paperwork.
6. **Legible** — plain markdown the user can read, edit, and diff. User
   correction is the only reliable fix for extraction errors.
7. **Private by choice** — the automatic memory pass is on by default, but the
   Session insights switch in Settings → General stops it.

## Architecture: two layers, verbatim long tail

**A small always-loaded core + a searchable long tail** is the pattern three
independent lines of work converged on: MemGPT/Letta's core-vs-archival
split (arXiv:2310.08560), Claude Code's capped MEMORY.md index over topic
files, and ChatGPT's injected profile beside on-demand history search. In
Ciaobot the core is the two fenced regions in each agent root's `AGENTS.md`
(`ciao:memory` ~3000 chars, `ciao:profile` ~1375; an install that still has
only the legacy `CLAUDE.md` keeps them there), loaded natively by every
session; the long tail is the vault notes behind `ciao vault search` (SQLite
FTS5) plus the archived transcripts under `Logs/`, which that search does not
cover (`Logs/` is excluded from the vault index).

The core is deliberately small because an always-injected memory has a real cost:
it spends context on every turn and, worse, it asserts itself before the
user says a word — Simon Willison's critique of ChatGPT's dossier (casual
experiments leaking into serious work) is the canonical failure. Small and
legible beats large and clever here. Its character budget is *advisory* rather
than enforced: a write always goes through and reports `over_cap`, because a
refused write does not shrink the region, it only hides the fact — bounding is
the job of consolidation during memory curation. The same policy is stated
once in `ciao/memory_policy.py` and pinned by tests.

The long tail keeps **verbatim transcripts as the store of record**, with what
the memory pass derived from them as an index over them — never a replacement. The two
strongest empirical results of 2025–26 both say lossy distillation is the
enemy: verbatim conversation chunks beat LLM-extracted artifacts by 15–22
points on LoCoMo/LongMemEval (arXiv:2601.00821), and goal-directed agentic
search over raw logs beats every compression-based memory system
(SUMER, arXiv:2511.21726). A capable model with a search tool over sources
outperforms any pipeline that summarizes those sources away.

## Principle → mechanism

| Principle | Source | Mechanism in Ciaobot |
| --- | --- | --- |
| Extract state, not events | consolidation surveys (arXiv:2603.07670); "user prefers X" is durable, "user said X on Tuesday" is not | A bullet states the standing rule as a present-tense `Durable rule:` clause; `memory_audit.find_event_shaped` rejects event-shaped text at promotion and flags it as rot in regions |
| Write-time dedup: ADD/UPDATE/NOOP against neighbors | Mem0 (arXiv:2504.19413); entity resolution at write beats query-time cleanup | `reconcile_region_fact`: one model call per region (a capped region fits whole in a prompt) decides add / covered / update-entry-N; fail-soft to plain append |
| Bi-temporal stamps; invalidate, don't delete | Zep/Graphiti (arXiv:2501.13956); ChatGPT's per-insight date ranges | `[as-of:]`/`[expires:]` (world time) on a bounded fact; trailing learned-at `[YYYY-MM-DD]` (system time) on every promotion; replaced entries go to the `Memory-Consolidations.md` undo log, so "current truth" and history both survive |
| Age is evidence, not a defect | Generative Agents' recency scoring (arXiv:2304.03442); MemoryBank decay | `memory-audit` reports aging (`as-of` ≥ 90d, learned ≥ 180d, per-type note horizons) as *informational* findings the nightly curator re-verifies — nothing expires automatically except explicit `[expires:]` |
| Decay by disuse, reinforce by access | MemoryBank (Ebbinghaus + access reinforcement) | `ciao vault search` hits logged to `.runtime/vault_search_hits.jsonl`; "stale AND never retrieved in 90d" (`retrieved_recently: false`) is the strongest demotion signal — signal only, no auto-delete |
| Consolidate episodes into cited rules | Generative Agents' reflection: derived memories cite their sources | Learnings entries carry `[key] [first → last] (xN) — sources: chat ids`; recurrence counting is mechanical, promotion at x3 cites its episodes; connections only among retrieved items |
| Scope by default, promote explicitly | Anthropic's project-scoped memory; wrong scoping is a production failure | Per-workspace vaults, regions, and curation; `[project]` facts go to the project doc, never a region; promoting a NEW region fact always takes an explicit act — a REVIEWER action, whether that is a user or agent accepting a queued proposal or the attended memory pass writing one itself — and the unattended curator never promotes a new region fact: it may only consolidate what is already there, under the undo-log rule |
| The machinery must not remember itself | observed self-ingestion, 2026-08: the nightly curator's transcript re-extracted its own prompt rules into `ciao:memory` | A turn the transcript marks as unattended is not the user's, so the memory pass is told never to record one and the unattended run defers instead; bookkeeping files are `RESERVED_UNINDEXED_FILES` in FTS and `search: false` is a general opt-out |
| Recall must survive paraphrase | LongMemEval ablations (arXiv:2410.10813): key expansion + query rewriting | AND→OR fallback for zero-hit multi-word queries; system prompt mandates 2–3 reformulations before "not found"; curation maintains `aliases:` frontmatter ("brother-in-law", "hourly rate") |
| Procedures are contracts, not prose | prompt drift: three near-copies of the curation contract had diverged | The nightly procedure lives in the packaged Workspace care schedule prompt; tests pin its contract there, without a separate skill |
| A managed mutation must be safe and reversible | one read-merge-write per region, plus a queue bullet and a decision record, is not one transaction in plain markdown | `ciao/memory_receipts.py`: every managed region write, queue resolution and prune records a stable-id receipt (`Workspace/Memory-Receipts.jsonl`) with actor/source, revisions and before/after images; the guide lock is required (unavailable = retryable failure, never an unlocked write), preview/apply/undo compare revisions so an external edit is a conflict, startup recovery reconciles an interrupted receipt from its images, and undo refuses a changed destination. External direct edits are conflicts, not audited writes |

## Failure modes this design answers

- **Bloat** — curated-small beats add-all by large margins (a 248-record
  curated store outperformed 2,400 add-all records ~3× in one 2026 study).
  Caps + write-time reconcile + recurrence-counted learnings + log rotation.
- **Stale overriding fresh** — timestamps + UPDATE-replaces + aging audit;
  supersession-only systems miss silent changes, which is what the disuse
  signal backstops.
- **Event-shaped rot** — the single most common gap in production memories;
  filtered where a fact is promoted (`_promotable_text`, `memory_audit`) and
  flagged in the regions afterwards.
- **Wrong scoping** — a project fact saved globally leaks across contexts;
  routing is by scope, with `[review]` as the honest "unsure" bucket.
- **Self-pollution** — the memory system's own queue/logs/rules competing
  with real memories in recall and in the regions.

## Rejected alternatives

- **Vector/embedding search** — deferred, not refused. Lexical + aliases +
  OR fallback + agentic reformulation closes most of the gap
  (LongMemEval's own ablations), with no model dependency and no index
  lifecycle in a zero-config product. Revisit if `docs/MEMORY_EVAL.md`
  probes show a persistent gap.
- **Knowledge-graph store (Zep/Graphiti-style)** — takes the bi-temporal
  *idea* without the graph: a KG replaces the legible-markdown substrate the
  product is built on, and no production assistant ships one to end users.
- **Doing ADD/UPDATE/NOOP inside the writing turn** — a separate small
  promotion-time call is testable, cheap (regions are tiny), and fail-soft;
  the writer never needs to reason about region contents twice.
- **A one-shot extractor (a single model call over the transcript instead of
  a chat with tools)** — this is the alternative the design *rejected* and
  then measured. Its case was real: an archived transcript is untrusted
  content, so a sandboxed one-shot (transcript in, markdown out, no tools)
  cannot turn every archived chat into a prompt-injection vector against
  memory, it runs on a cheap model, and parse/route/dedupe stay pure
  functions with tests. What it could not do was the judgment — deciding
  what is already in a note, what a changed fact supersedes, whether a
  person is genuinely new. The #594 experiment compared the two on 50 real
  conversations and the agentic chat won, so the one-shot pipeline was
  deleted outright (#627) rather than left as a mode. The memory pass is
  that agentic chat: it reads the archive with tools, so the injection
  surface it opens is answered by bounds instead of by a sandbox — it is a
  `bypass` chat scoped to memory and the vault (no messages, no commits, no
  external calls), it is visible and steerable like any other chat, and
  anything it is unsure about is queued rather than written. The guarded
  code the one-shot relied on is not gone either: it is the accept path, and
  it is what every write goes through now (event-shape filter, reconcile,
  provenance row, receipts, undo log). Fact-augmentation remains the right
  answer if a future pass needs context *without* the tools: *code* retrieves
  the region and top-k `ciao vault search` hits and puts them in the prompt.
- **Automatic forgetting** — disuse and age are *signals to a curator with
  an undo log*, never triggers for deletion. A personal assistant that
  silently forgets is worse than one that asks.

## Evaluation

Don't trust LoCoMo-style vendor numbers (misconfiguration-sensitive; string
metrics conflate memory with style). The abilities that matter are
LongMemEval's: knowledge updates, temporal reasoning, abstention.
`tests/test_memory_eval.py` pins those deterministically over a fixture
vault; `docs/MEMORY_EVAL.md` carries the sandboxed live-vault probe runbook.
Every recall failure observed in the wild becomes a fixture case first.

Retrieval is only half the question: the deterministic eval cannot tell
whether the assistant *uses* results well, and a prompt or provider change can
alter auto-saving, tool selection, unattended behavior, or source handling
while it stays green. `docs/BEHAVIORAL_EVAL.md` and `ciao/behavioral_eval.py`
add versioned behavioral evaluations for that half — synthetic scenarios with
baseline/candidate reports tied to exact prompt and provider versions, plus a
model-free contract check that carries the CI gate.
