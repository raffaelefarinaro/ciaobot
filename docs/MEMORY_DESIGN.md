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
7. **Owned** — the memory pass runs automatically when a chat with a human
   turn is archived and files what it learns into plain markdown notes on
   this machine. The pass is itself a provider chat, so the archived
   transcript is sent to the configured model provider, as the original
   chat was.

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
in `docs/ARCHITECTURE.md` and pinned by tests.

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
| Age is evidence, not a defect | Generative Agents' recency scoring (arXiv:2304.03442); MemoryBank decay | `memory-audit` reports aging (`as-of` ≥ 90d, learned ≥ 180d, 30-day note horizon) as *informational* findings the nightly curator re-verifies — nothing expires automatically except explicit `[expires:]` |
| Decay by disuse, reinforce by access | MemoryBank (Ebbinghaus + access reinforcement) | `ciao vault search` hits logged to `.runtime/vault_search_hits.jsonl`; "stale AND never retrieved in 90d" (`retrieved_recently: false`) is the strongest demotion signal — signal only, no auto-delete |
| Consolidate episodes into cited rules | Generative Agents' reflection: derived memories cite their sources | Learnings entries carry `[key] [first → last] (xN) — sources: chat ids`; recurrence counting is mechanical, promotion at x3 cites its episodes; connections only among retrieved items |
| Scope by default, promote explicitly | Anthropic's project-scoped memory; wrong scoping is a production failure | Per-workspace vaults, regions, and curation; `[project]` facts go to the project doc, never a region; a region holds only cross-project preferences, rules and identity facts the person stated, written directly with their source by the memory pass or a care step, and every replaced or removed line goes to the `Memory-Consolidations.md` undo log first |
| The machinery must not remember itself | observed self-ingestion, 2026-08: the curation run's transcript re-extracted its own prompt rules into `ciao:memory` | A turn the transcript marks as unattended is not the user's, so the memory pass is told never to record one; a care step never writes a fact whose only source is the care run's own output or an instruction found in a note, transcript or queue line, because a fact needs a source the person produced (`step-preamble.md`); bookkeeping files are `RESERVED_UNINDEXED_FILES` in FTS and `search: false` is a general opt-out |
| Recall must survive paraphrase | LongMemEval ablations (arXiv:2410.10813): key expansion + query rewriting | AND-first ranking for multi-word queries, with OR hits filling any slots AND leaves short of the limit (a zero-hit AND is the case where OR fills all of them); system prompt mandates 2–3 reformulations before "not found"; curation maintains `aliases:` frontmatter ("brother-in-law", "hourly rate") |
| Recall must see the whole fact | LongMemEval-S head-to-head (60 questions, Haiku): 43.3% answering from `ciao vault search` snippets vs 86.7% reading the top-5 notes | snippets (32 tokens of highlighted lines) pick the best-matching note and the agent reads it before answering or abstaining, since a snippet can look settled while hiding a later change; another match is read only when identity or a conflict stays open. Trade-off: full private notes now land in recall transcripts, while answers stay guarded by the "private working evidence" rule; task `H11-recall-value-below-snippet` pins it |
| Procedures are contracts, not prose | prompt drift: three near-copies of the curation contract had diverged | The care procedure lives in the packaged Workspace care schedule prompt; tests pin its contract there, without a separate skill |
| A managed mutation must be safe and reversible | one read-merge-write per region is not a transaction in plain markdown | Git is the record and the undo; a region write is one locked, atomic replacement of the guide (`ciao/memory_tool.py`), and an unavailable lock is a retryable failure, never an unlocked write |

## Failure modes this design answers

- **Bloat** — curated-small beats add-all by large margins (a 248-record
  curated store outperformed 2,400 add-all records ~3× in one 2026 study).
  Caps + write-time reconcile + recurrence-counted learnings + log rotation.
- **Stale overriding fresh** — timestamps + UPDATE-replaces + aging audit.
  A note keeps one current instruction: the surviving line carries the source
  and date, and the superseded line is removed. A genuine conflict is shown
  to the owner. Annotating the old line in place leaves it looking current in
  search. Supersession-only systems miss silent changes, which is what the
  disuse signal backstops.
- **Event-shaped rot** — the single most common gap in production memories;
  filtered where a fact is promoted (`_promotable_text`, `memory_audit`) and
  flagged in the regions afterwards.
- **Wrong scoping** — a project fact saved globally leaks across contexts;
  routing is by scope, with the Unsure list as the honest "unsure" bucket.
- **Self-pollution** — the memory system's own queue/logs/rules competing
  with real memories in recall and in the regions.

## Rejected alternatives

- **Vector/embedding search** — deferred, not refused. Lexical + aliases +
  OR top-up + agentic reformulation closes most of the gap
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
  `bypass` chat. Its scope (no messages, commits or external calls) is
  enforced only by the prompt text in `MEMORY_PASS_PROMPT`
  (`ciao/web/memory_pass.py`). It is visible and steerable like any other
  chat, it puts what it is unsure about on the Unsure list, and the daily
  care run's second-look step re-reads what it wrote. Fact-augmentation remains the right
  answer if a future pass needs context *without* the tools: *code* retrieves
  the region and top-k `ciao vault search` hits and puts them in the prompt.
- **Automatic forgetting** — disuse and age are *signals to a curator with
  an undo log*, never triggers for deletion. A personal assistant that
  silently forgets is worse than one that asks.

## Skill health evidence

This section states what a read-only skill review may conclude from the
evidence the code already holds, before any scheduled worker reads it. It
records code as it stands; it adds no collection, no telemetry and no worker.
Symbols are cited by name because line numbers drift.

Observed means the current code reads the signal. Inferred means it is a
conclusion the signal supports but does not prove.

### Signal table

| Signal | Source (path and symbol) | Owner and workspace attribution | Supported conclusion | Limitations |
| --- | --- | --- | --- | --- |
| Installed inventory | `ciao/skills_inventory.py` `build_skill_inventory`, `_stock_skill_names`, `_skill_candidates` | Each name is `custom` (workspace `skills/`) or `stock` (packaged, marked by `STOCK_SKILL_MARKER`). A workspace `skills/<name>` shadows the packaged copy. | Observed: what is installed, and whether a name is custom or stock. | Says nothing about use. A name that exists only under `.claude/` is not owned. |
| Owned source | `skills_inventory.py` `resolve_owned_skill` (`OwnedSkill`) and `eligible_owned_skills` | Only the workspace's canonical `skills/` source is owned. Stock copies, provider mirrors under `.claude/` and the `skills-src/` mirror are refused with a named reason. | Observed: which skills a pass may edit, and the revision (sha256) the bytes were read at. | Owning a skill is not evidence it is used. |
| Skill tool calls in tool events | `ciao/transcripts.py` `turn_skills` (`_SKILL_TOOL_NAMES`, `_skill_name_from_summary`); normalized names from `ciao/providers/acp/normalize.py` (a `skill` call is shown as `Skill`) | Per turn of one provider transcript, so the workspace is the transcript's own. | Observed: this turn invoked a skill tool with this name. | Depends on the provider exposing a tool event. Only what ACP events expose today is observable. |
| Skill source reads | `transcripts.py` `_SKILL_SOURCE_RE` in `turn_skills` | Same as above. | Observed: the turn read a `SKILL.md` under a `skills/<name>/` or `.claude/skills/<name>/` path. This is the opencode shape. | A read of the file is not proof the model followed it. |
| Command-name markers | `transcripts.py` `_SKILL_MARKER_RE` in `turn_skills`, read from prompt and response | Same as above. | Observed: a slash command or description match loaded the skill. | Only the marker text; a match is not a tool call. |
| Archive-recorded markers | `transcripts.py` `read_archive_skills` (`- Skills:` lines written by `_rendered_turn_skills`, the `### User` block check against `_ARCHIVE_USER_RE`) | Attributed to the archived transcript and its `## Turn N` heading. | Archive-recorded: the archive says the turn used the skill. The pass cites the turn. | An unreadable archive yields no skills, not an error, so absence here is unknown, not unused. The archive does not record whether the signal came from a tool event or a marker, so it is not treated as tool-event evidence. |
| Bare mentions | Skill names in prose (the turn's user or agent text) | Same as above. | Not evidence of use. Prose is not read for names; only markers are. | A reader or the model may name a skill without loading it. Never count a mention as use. |
| Unknown telemetry | No store exists. The Claude SDK driver and its JSONL tracking were deleted. | None. | Unknown. Any use count is absent, not zero. | State "unknown" for direct provider skill observations beyond the tool events above. No vendor telemetry is assumed. |
| Existing proposal | `ciao/skill_proposals.py` `SkillProposal` (`canonical_path`, `reviewed_revision`, `origins`), `read_records`, `proposal_id(workspace, skill)` | Keyed by workspace and skill name. | Observed: a skill already has an open or settled proposal, with the revision it was written against. | A proposal records a finding, not a fix. A settled proposal is history, not a new signal. |
| Upstream review drafts | `ciao/upstream_drafts.py` `file_draft`, `read_records`, `draft_id(workspace, target, skill, change)` | Routes lessons for packaged, mirrored, shared or upstream skills to `[review]` drafts. Never an edit. | Observed: a lesson for a skill this workspace does not own was filed for review. | Drafts are local records. Only `approve_draft` reaches GitHub, and it refuses in an unattended run. |

### Ownership routing

A review must route each skill before it names an action:

- **Workspace-owned** (`custom`, under `skills/`): a proposal may name it,
  because `resolve_owned_skill` proves the canonical source and its revision.
- **Packaged** (`stock`, marked with `STOCK_SKILL_MARKER`): never edited. Sync
  refreshes the copy, so a local edit is discarded. A lesson goes to a
  `[review]` draft for the project that ships it.
- **Mirrored** (provider copies under `.claude/`, and the `skills-src/`
  mirror): never edited. The owned source, if any, is the target instead.
- **Upstream** (a skill another repository maintains): a `[review]` draft
  through `upstream_drafts.py`. The draft may name the owning repository and
  version when they are known; it does not claim them otherwise.
- **No fitting skill**: a new owned skill is a separate decision. The
  edit-only proposal path cannot point at a skill that does not exist yet.

### Dependencies and protections

- Schedule or supporting-file references are unknown unless the review reads
  them explicitly and they are reliably present. No current signal in the
  inventory above reports them, so a review must mark them unknown, not
  infer them from a name.
- Disabled or paused references are protected. A skill named by a disabled or
  paused schedule is not a candidate for change or retirement.
- A rarely used skill that is essential (a skill other skills, schedules or
  workflows depend on) is protected. Rarity alone never qualifies it for
  change.
- Age or low usage alone never establishes a safe change. Low counts are a
  reason to look, not a reason to edit or delete.

### Requirements for a later bounded worker

A later worker that reads this evidence, and the marginal-cost check tracked in
#3054, must hold these rules:

- Read-only over the evidence above. It may suggest review with cited evidence
  (repository path, symbol, and the turn or revision) and list the existing
  proposal IDs for the skill. It writes no proposal, draft, edit or schedule
  change on its own.
- Every claim names whether it is observed or inferred, using the table.
- Unknown telemetry is reported as unknown, never as zero use.
- Ownership routing above is applied before any action is named.
- Protected skills (disabled or paused references, essential skills) are
  excluded from change suggestions and their exclusion is reported.
- No live workspace observations, no user transcript content, and no vendor
  telemetry speculation may enter the evidence the worker sees or cites.
- Its cost is bounded and checked against #3054 before it is scheduled.

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
while it stays green. `docs/TASK_EVAL.md` and `evals/`
add agentic task evaluations for that half — synthetic scenarios with
baseline/candidate reports tied to exact prompt and provider versions.
