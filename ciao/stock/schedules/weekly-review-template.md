# Weekly Self-Improvement Review

Generic checklist for a Ciaobot workspace. Review the past 7 days (archived chat insights under the vault's `Logs/Chats/` and recent schedule runs) for concrete improvements to the setup.

## Dispatch strategy

- **memory subagent**: vault-facing bullets (5, 6, 7, 8).
- **doc-updater subagent**: bullet 10 (prompt hygiene on non-vault files).
- **All other bullets**: main session inline.

## Checklist

1. **Repeated corrections/misunderstandings** — propose CLAUDE.md or memory edits for anything the user had to correct more than once.
2. **Skills that errored or needed workarounds** — propose skill edits.
3. **Redundant/contradictory instructions** across CLAUDE.md, agents, commands, and schedule prompts.
4. **Schedule health** — `.runtime/schedules.json` `last_triggered_on`; flag missed runs and prompts that produced poor results.
5. **Learnings promotion** — review the `Workspace/Learnings.md` Active section in the vault. Promote high-confidence entries that recurred 3+ times into CLAUDE.md as rules (move the entry to a Promoted/Resolved section with a note). Prune low-confidence entries older than 30 days.
6. **Memory gaps** — facts the user re-explained this week that should be in the vault; check memory proposals and remove items after promotion or rejection.
7. **MEMORY.md drift** — index vs actual vault files; flag broken links.
8. **Vault lint pass** — run `ciao vault-lint` and apply fixes for dead wikilinks, orphan pages, and near-duplicates. Manually flag stale entity pages (frontmatter `updated` older than 90 days) and contradictions (same entity, conflicting attributes across pages).
9. **Connection discovery** — scan vault entity files for implicit relationships not yet surfaced: people mentioned together without cross-references, projects sharing people/tools/themes without links, ideas or resources related to active projects but unconnected. Propose specific wikilink additions with evidence.
10. **Prompt hygiene** — review AGENTS.md, CLAUDE.md, canonical `subagents/*.md` and `commands/*.md`, stock agents under `.claude/agents/` not yet promoted to `subagents/`, their generated `.claude/` / `.agents/skills/` mirrors, and the prompts inside `.runtime/schedules.json` for contradictions, bloat, and outdated paths. Give file + short before/after per issue.
11. **Decision log** — if the vault has a `Workspace/Decision-Log.md`, flag "revisit when" items that are now ripe.
12. **Workspace extension** — if the vault contains a `Workspace/Weekly-Review-Template.md`, run its additional workspace-specific bullets too.

## Rules

- Apply concrete low-risk fixes directly during the review (prompt/doc/vault edits, dead-link fixes, schedule prompt rewrites). Ask before external actions, destructive changes, or anything touching auth, schemas, or data migrations.
- Never restart the Ciaobot service from inside a run; apply code changes and tell the user to use Restart in Settings.

## Output format

- For each finding (max 10, by impact): problem (with reference), fix applied or proposed (file + before/after), why it helps.
- End with a "Checked and fine:" section listing areas reviewed that need no changes.
