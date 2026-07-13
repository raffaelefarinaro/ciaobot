# Weekly Self-Improvement Review

Review the past 7 days (archived chat insights under the vault's `Logs/Chats/` and recent schedule runs) for concrete setup improvements.

Use the **memory** subagent for item 3; handle item 4 (prompt hygiene) directly — no dedicated subagent ships for it.

## Checklist

1. **Instruction quality** — repeated user corrections (CLAUDE.md/memory edits), skills that errored or needed workarounds, and redundant or contradictory instructions across CLAUDE.md, agents, commands, and schedule prompts.
2. **Schedule health** — `.runtime/schedules.json` `last_triggered_on`; flag missed runs and prompts that produced poor results.
3. **Memory and vault** — review `Workspace/Learnings.md` Active as a lifecycle queue: promote entries that recurred 3+ times and were encoded in canonical guidance; resolve entries only when their own evidence shows the fix shipped, the behavior was removed or superseded, or the rule was encoded in a canonical skill/doc; move promoted/resolved entries to Promoted / Resolved while preserving the learning and recording the evidence or destination; prune low-confidence entries older than 30 days unless reinforced, but never retire medium/high-confidence entries solely because of age. Then clear promoted/rejected memory proposals; fix `MEMORY.md` drift; run `ciao vault-lint` and apply low-risk fixes (dead wikilinks, orphans, near-duplicates); flag stale entity pages (frontmatter `updated` >90 days) and attribute contradictions.
4. **Prompt hygiene** — review AGENTS.md, CLAUDE.md, canonical `subagents/*.md` and `commands/*.md`, their generated `.claude/` / `.agents/skills/` mirrors, and prompts in `.runtime/schedules.json` for contradictions, bloat, and outdated paths. File + short before/after per issue.
5. **Decision log** (optional) — if the vault has `Workspace/Decision-Log.md`, flag "revisit when" items that are now ripe.
6. **Workspace extension** (optional) — if the vault has `Workspace/Weekly-Review-Template.md`, run its additional bullets too (e.g. connection discovery or other local checks).

## Rules

- Apply concrete low-risk fixes directly (prompt/doc/vault edits, dead-link fixes, schedule prompt rewrites). Ask before external actions, destructive changes, or anything touching auth, schemas, or data migrations.
- Never restart the Ciaobot service from inside a run; apply code changes and tell the user to use Restart in Settings.

## Output format

- For each finding (max 10, by impact): problem (with reference), fix applied or proposed (file + before/after), why it helps.
- End with a "Checked and fine:" section listing areas reviewed that need no changes.
