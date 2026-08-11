# Surfacing memory extraction and proposals

Status: proposed, not started
Written: 2026-08-11
Scope: `web/` plus one read-only API addition

Answers the question: the insights → memory-proposal pipeline is one of the most
important things the system does, and it is currently close to invisible. Where
should it surface?

---

## What exists today

The pipeline is real and already tracked:

| Job (`ciao/job_runs.py` REGISTRY) | What it does |
|---|---|
| `insights` | Extracts durable insights from an archived session transcript |
| `memory_proposals` | Proposes durable facts from that session's insights |
| `trajectory` | Records a structured trajectory for skill mining |
| `backfill_insights` | Backfills missing insights on server startup |

`ciao/insights.py` appends a `## Session insights` section to each archived chat.
`ciao/memory_proposals.py` does a heuristic pass over that section and appends
bullets to `<workspace-vault>/Workspace/Memory-Proposals.md`. Auto-apply is
deliberately not the default — a human or the agent promotes them into the
`ciao:memory` / `ciao:profile` CLAUDE.md regions, then `memory_proposal_resolve`
dismisses them.

`/api/automation` returns per-job `last_run`, `recent`, and `stats`
(`total_runs`, `success_rate`, `avg_duration_ms`, `last_error`), rendered in
**Settings → Automation**.

## The actual gap

Nothing is wrong with the pipeline. The problem is that **its most valuable
output is its least visible**:

1. **No signal that extraction is happening.** Archiving a chat makes it vanish
   from home and the sidebar, and then two model-backed jobs run against it. The
   user gets no indication that work is in flight, or that it finished.
2. **No signal that proposals are waiting.** Memory proposals are, semantically,
   a *needs-you* state — the system is asking whether a fact should become
   durable. That is the same class of event as `chatNeedsInput`. Instead the
   output accumulates in a markdown file the user has to know to open.
3. **The only audit surface is an engineering one.** Settings → Automation
   answers "is the extractor healthy" (success rate, last error, duration). It
   does not answer "what did you learn, and what needs my call". Conflating those
   two is the same category error as the old project stat row mixing a date in
   with three counts.

This is the worked example behind **Rule S8** in `docs/DESIGN_SYSTEM.md`:
background work that produces a decision needs a transient *working* mark where
the work originates and a persistent *needs you* count where its output waits.
This pipeline currently has neither.

## Proposal

Three needs, three placements. Putting all of it in one place is the thing to
avoid.

### 1. Extraction in flight → the existing `working` mark

No new vocabulary. An archived chat whose `insights` or `memory_proposals` job is
running keeps a `ChatSignals` **working** ring for the duration, shown in the
archive list (`ProjectView`'s archived section, and the sidebar archive dialog).
Archiving stops looking like deletion and starts looking like handoff.

Needs: a per-chat "extraction running" flag. `job_runs` already records
`extra` per run, so the chat id can be carried there and exposed through the
existing automation payload rather than a new socket event.

### 2. Proposals waiting → a `needs you` count on home

Proposals are per-workspace (they write into that workspace's vault), which maps
onto the lane model — but they are **not chats**, so they must not be folded into
lane tier counts or they will distort "2 need you" into something that mixes
chats with facts.

Give them their own single row beneath the lanes:

```
↳ 3 memory proposals from 2 archived chats · personal          review
```

- One line, filled `needs you` treatment per Rule S1 when the count is above
  zero, because the user is genuinely the blocker.
- Absent entirely at zero rather than rendering "0 proposals" — but the memory
  destination stays reachable from Settings, per Rule S6's spirit (report the
  absence where the thing lives, not on the triage surface).
- Per workspace when more than one has proposals, matching the lane order.

Plus a notification, which is the surface that already exists for "something
happened while you were away". `NotificationBell` is the right home for the
event; the home row is the right home for the standing count.

### 3. What was learned → a real read surface

`Memory-Proposals.md` is a vault file, and the app already has both
`FileViewerModal` and the project canonical-doc pattern for exactly this. Make
the memory file a first-class destination opened from the home row, and render
proposals as reviewable rows — promote / dismiss — rather than raw markdown.
`memory_proposal_resolve` already exists as the dismiss path, so this is a
rendering change plus one write endpoint, not new pipeline logic.

### 4. Where not to put it

**Do not expand Settings → Automation.** It is the correct place for pipeline
health and should stay an engineering view. Adding "you have 3 facts to review"
there buries a decision inside a diagnostics table.

## One stat worth adding

Nothing currently measures whether extraction is *useful*, only whether it *ran*.
`success_rate` says the job did not crash. The honest measure is
**proposals promoted vs dismissed**, over time, per workspace. That is the number
that tells you whether the heuristic in `memory_proposals.py` is worth its model
spend, and it belongs in Settings → Automation next to the existing stats —
because *that* is a health question.

## Order

1. Proposal count API + the home row (delivers most of the value; read-only)
2. Notification on new proposals
3. The review surface (promote / dismiss)
4. `working` mark on archived chats mid-extraction
5. Promoted-vs-dismissed stat in Settings → Automation
