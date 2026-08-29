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

## Architecture: two layers, verbatim long tail

**A small always-loaded core + a searchable long tail** is the pattern three
independent lines of work converged on: MemGPT/Letta's core-vs-archival
split (arXiv:2310.08560), Claude Code's capped MEMORY.md index over topic
files, and ChatGPT's injected profile beside on-demand history search. In
Ciaobot the core is the two fenced regions in each agent root's `CLAUDE.md`
(`ciao:memory` ~3000 chars, `ciao:profile` ~1375), loaded natively by every
session; the long tail is the vault plus archived transcripts behind
`vault_search` (SQLite FTS5).

The core is hard-capped because an always-injected memory has a real cost:
it spends context on every turn and, worse, it asserts itself before the
user says a word — Simon Willison's critique of ChatGPT's dossier (casual
experiments leaking into serious work) is the canonical failure. Small and
legible beats large and clever here.

The long tail keeps **verbatim transcripts as the store of record**, with
extracted insights as an index over them — never a replacement. The two
strongest empirical results of 2025–26 both say lossy distillation is the
enemy: verbatim conversation chunks beat LLM-extracted artifacts by 15–22
points on LoCoMo/LongMemEval (arXiv:2601.00821), and goal-directed agentic
search over raw logs beats every compression-based memory system
(SUMER, arXiv:2511.21726). A capable model with a search tool over sources
outperforms any pipeline that summarizes those sources away.

## Principle → mechanism

| Principle | Source | Mechanism in Ciaobot |
| --- | --- | --- |
| Extract state, not events | consolidation surveys (arXiv:2603.07670); "user prefers X" is durable, "user said X on Tuesday" is not | Extraction demands a present-tense `Durable rule:` clause; `memory_audit.find_event_shaped` rejects event-shaped text at promotion and flags it as rot in regions |
| Write-time dedup: ADD/UPDATE/NOOP against neighbors | Mem0 (arXiv:2504.19413); entity resolution at write beats query-time cleanup | `plan_region_reconcile`: one model call per region (a capped region fits whole in a prompt) decides add / covered / update-entry-N; fail-soft to plain append |
| Bi-temporal stamps; invalidate, don't delete | Zep/Graphiti (arXiv:2501.13956); ChatGPT's per-insight date ranges | `[as-of:]`/`[expires:]` (world time) from extraction; trailing learned-at `[YYYY-MM-DD]` (system time) on every promotion; replaced entries go to the `Memory-Consolidations.md` undo log, so "current truth" and history both survive |
| Age is evidence, not a defect | Generative Agents' recency scoring (arXiv:2304.03442); MemoryBank decay | `memory-audit` reports aging (`as-of` ≥ 90d, learned ≥ 180d, per-type note horizons) as *informational* findings the nightly curator re-verifies — nothing expires automatically except explicit `[expires:]` |
| Decay by disuse, reinforce by access | MemoryBank (Ebbinghaus + access reinforcement) | `vault_search` hits logged to `.runtime/vault_search_hits.jsonl`; "stale AND never retrieved in 90d" (`retrieved_recently: false`) is the strongest demotion signal — signal only, no auto-delete |
| Consolidate episodes into cited rules | Generative Agents' reflection: derived memories cite their sources | Learnings entries carry `[key] [first → last] (xN) — sources: chat ids`; recurrence counting is mechanical, promotion at x3 cites its episodes; connections only among retrieved items |
| Scope by default, promote explicitly | Anthropic's project-scoped memory; wrong scoping is a production failure | Per-workspace vaults, regions, and curation; `[project]` facts go to the project doc, never a region; NEW region facts always pass a human review (proposals queue) — unattended runs may only consolidate, under the undo-log rule |
| The machinery must not remember itself | observed self-ingestion, 2026-08: the nightly curator's transcript re-extracted its own prompt rules into `ciao:memory` | System-schedule chats keep insights (audit trail) but cannot write memory; extraction prompts refuse machinery rules; bookkeeping files are `RESERVED_UNINDEXED_FILES` in FTS and `search: false` is a general opt-out |
| Recall must survive paraphrase | LongMemEval ablations (arXiv:2410.10813): key expansion + query rewriting | AND→OR fallback for zero-hit multi-word queries; system prompt mandates 2–3 reformulations before "not found"; curation maintains `aliases:` frontmatter ("brother-in-law", "hourly rate") |
| Procedures are contracts, not prose | prompt drift: three near-copies of the curation contract had diverged | The nightly procedure is one stock skill (`memory-curation`); the schedule prompt only dispatches; tests pin the contract to the skill file |

## Failure modes this design answers

- **Bloat** — curated-small beats add-all by large margins (a 248-record
  curated store outperformed 2,400 add-all records ~3× in one 2026 study).
  Caps + write-time reconcile + recurrence-counted learnings + log rotation.
- **Stale overriding fresh** — timestamps + UPDATE-replaces + aging audit;
  supersession-only systems miss silent changes, which is what the disuse
  signal backstops.
- **Event-shaped rot** — the single most common gap in production memories;
  filtered at three layers (prompt, `_is_durable`, region audit).
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
- **Doing ADD/UPDATE/NOOP inside the extraction call** — a separate small
  promotion-time call is testable, cheap (regions are tiny), and fail-soft;
  the extractor never needs region contents.
- **Agentic insight extraction (a full CLI session with tools instead of
  the one-shot)** — considered and kept two-tier on purpose. The one-shot
  extractor is deliberately *sandboxed*: transcript in, markdown out, no
  tools. That buys three things at once. (1) **Injection safety**: an
  archived transcript is untrusted content; an extraction agent with tools
  and write access turns every archived chat into a prompt-injection vector
  against memory, whereas the one-shot can only ever emit text that
  deterministic, guarded code then routes (event-shape filter, caps, dedupe,
  undo log). (2) **Cost and reach**: extraction runs on every archived chat
  and works on a cheap model or Apple's free on-device model — a tool-using
  session needs a capable cloud model and many calls per archive.
  (3) **Testability**: parse/route/dedupe are pure functions with tests.
  The judgment the agentic version would add already exists in the design,
  split into two cheaper places: the write-time reconcile (one sandboxed
  call that sees the region contents) and the nightly curation run, which
  IS a full agent session with tools and memory that re-judges everything
  queued. The middle path, if extraction ever needs more context, is
  fact-augmented extraction: *code* retrieves the region and top-k
  `vault_search` hits and puts them in the one-shot prompt — the model gets
  the context without getting the tools.
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
