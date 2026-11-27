import type { UnsureItem, UnsureList } from '../lib/unsureList'

/**
 * Thirteen Unsure lines shaped like a real list (names, projects and ids are
 * invented), as `GET /api/memory/unsure` answers them: parsed by the engine's
 * own `ciao.unsure_list.parse_list`, so titles, reasons and sources are what
 * the page really gets. One line is the old machine-written kind with no plain
 * first sentence, one is ticked, and every kind is present. Shared by the
 * stories and the Playwright spec.
 */
export const UNSURE_FIXTURE_ITEMS: UnsureItem[] = [
  {
    "id": "1595d4b902c9b287",
    "raw": "- [ ] Is the live Northwind deployment unaffected by the December 11 model retirement? — unsure because the review checked local code and configuration, not the production environment, where a model override could still select a retiring ID; source: chat-7e11530b; 2026-11-27",
    "checked": false,
    "kind": "",
    "qualifier": "",
    "title": "Is the live Northwind deployment unaffected by the December 11 model retirement?",
    "text": "Is the live Northwind deployment unaffected by the December 11 model retirement?",
    "reason": "the review checked local code and configuration, not the production environment, where a model override could still select a retiring ID",
    "source": "chat-7e11530b",
    "date": "2026-11-27",
    "malformed": false
  },
  {
    "id": "db7523f800669f0c",
    "raw": "- [ ] Does the November 27 summary of the voice feature reflect a changed decision or just an imprecise summary? — unsure because the [Northwind note](../projects/active/Northwind/Northwind.md#audio) records the decision to keep the current default while credit lasts; the later review reports no new decision and does not verify the deployed default; sources: chat-30605c3a and chat-7e11530b; 2026-11-27",
    "checked": false,
    "kind": "",
    "qualifier": "",
    "title": "Does the November 27 summary of the voice feature reflect a changed decision or just an imprecise summary?",
    "text": "Does the November 27 summary of the voice feature reflect a changed decision or just an imprecise summary?",
    "reason": "the [Northwind note](../projects/active/Northwind/Northwind.md#audio) records the decision to keep the current default while credit lasts; the later review reports no new decision and does not verify the deployed default",
    "source": "chat-30605c3a and chat-7e11530b",
    "date": "2026-11-27",
    "malformed": false
  },
  {
    "id": "ae52338c77ab7db1",
    "raw": "- [ ] Should Sam Rivera's note say he moved from the platform team to the payments team? — unsure because two notes disagree and neither names a date; source: People/Sam Rivera.md; 2026-11-26",
    "checked": false,
    "kind": "",
    "qualifier": "",
    "title": "Should Sam Rivera's note say he moved from the platform team to the payments team?",
    "text": "Should Sam Rivera's note say he moved from the platform team to the payments team?",
    "reason": "two notes disagree and neither names a date",
    "source": "People/Sam Rivera.md",
    "date": "2026-11-26",
    "malformed": false
  },
  {
    "id": "3dbbb1383fdd682f",
    "raw": "- [ ] observation: You prefer a short status update before a long explanation. — seen in: chat-1a2b3c4d, chat-5e6f7a8b — unsure because it has shown up in two chats so far; source: chat-5e6f7a8b; 2026-11-25",
    "checked": false,
    "kind": "observation",
    "qualifier": "",
    "title": "You prefer a short status update before a long explanation.",
    "text": "You prefer a short status update before a long explanation. — seen in: chat-1a2b3c4d, chat-5e6f7a8b",
    "reason": "it has shown up in two chats so far",
    "source": "chat-5e6f7a8b",
    "date": "2026-11-25",
    "malformed": false
  },
  {
    "id": "84b0576a499c94af",
    "raw": "- [ ] drop memory: Remove the reminder about the old staging password rotation from what every chat loads. \"Staging credentials rotate every Friday; ask before rotating\" — unsure because the rotation may still be in place; source: care run; 2026-11-25",
    "checked": false,
    "kind": "drop",
    "qualifier": "memory",
    "title": "Remove the reminder about the old staging password rotation from what every chat loads.",
    "text": "Remove the reminder about the old staging password rotation from what every chat loads. \"Staging credentials rotate every Friday; ask before rotating\"",
    "reason": "the rotation may still be in place",
    "source": "care run",
    "date": "2026-11-25",
    "malformed": false
  },
  {
    "id": "2591ae6f5758b7fd",
    "raw": "- [ ] restore to profile: Put back that you work from Lisbon on Mondays. \"Works from the Lisbon office on Mondays\" — removed because no typed turn of yours said it; source: chat-9c8d7e6f; 2026-11-24",
    "checked": false,
    "kind": "restore",
    "qualifier": "profile",
    "title": "Put back that you work from Lisbon on Mondays.",
    "text": "Put back that you work from Lisbon on Mondays. \"Works from the Lisbon office on Mondays\"",
    "reason": "no typed turn of yours said it",
    "source": "chat-9c8d7e6f",
    "date": "2026-11-24",
    "malformed": false
  },
  {
    "id": "b98bbd4469d88305",
    "raw": "- [ ] category: Add a category for recipes? Three notes about cooking have no category that fits. — unsure because no category in VOCABULARY.md covers them; source: chat-0f1e2d3c; 2026-11-24",
    "checked": false,
    "kind": "category",
    "qualifier": "",
    "title": "Add a category for recipes?",
    "text": "Add a category for recipes? Three notes about cooking have no category that fits.",
    "reason": "no category in VOCABULARY.md covers them",
    "source": "chat-0f1e2d3c",
    "date": "2026-11-24",
    "malformed": false
  },
  {
    "id": "5b3eada211e50395",
    "raw": "- [ ] repo ~/repos/northwind-api: Run the API tests with `uv run pytest -n auto` instead of plain pytest. — unsure because only one chat used that command; source: chat-4b5c6d7e; 2026-11-23",
    "checked": false,
    "kind": "repo",
    "qualifier": "~/repos/northwind-api",
    "title": "Run the API tests with `uv run pytest -n auto` instead of plain pytest.",
    "text": "Run the API tests with `uv run pytest -n auto` instead of plain pytest.",
    "reason": "only one chat used that command",
    "source": "chat-4b5c6d7e",
    "date": "2026-11-23",
    "malformed": false
  },
  {
    "id": "f35ec7f92ae8d440",
    "raw": "- [ ] rule flip memory/stale-days: Re-check notes every 45 days instead of every 30? 30 -> 45 — unsure because the operator has not approved the change; source: care run; 2026-11-23",
    "checked": false,
    "kind": "rule",
    "qualifier": "memory/stale-days",
    "title": "Re-check notes every 45 days instead of every 30?",
    "text": "Re-check notes every 45 days instead of every 30? 30 -> 45",
    "reason": "the operator has not approved the change",
    "source": "care run",
    "date": "2026-11-23",
    "malformed": false
  },
  {
    "id": "b78024e6100587d9",
    "raw": "- [ ] Atlas renewal: contract owner listed as procurement in Projects/Atlas.md but the 2026-11-12 thread names finance as owner, both cited, neither superseded, kept both pending confirmation, also budget line differs (EUR 40k vs EUR 42k) between the brief and the kickoff deck — unsure because two sources conflict; source: Projects/Atlas.md; 2026-11-22",
    "checked": false,
    "kind": "",
    "qualifier": "",
    "title": "Atlas renewal: contract owner listed as procurement in Projects/Atlas.md but the 2026-11-12 thread names finance as owner, both cited, neither superseded, kept both pending confirmation, also budget line differs (EUR 40k vs EUR 42k) between the brief and the kickoff deck",
    "text": "Atlas renewal: contract owner listed as procurement in Projects/Atlas.md but the 2026-11-12 thread names finance as owner, both cited, neither superseded, kept both pending confirmation, also budget line differs (EUR 40k vs EUR 42k) between the brief and the kickoff deck",
    "reason": "two sources conflict",
    "source": "Projects/Atlas.md",
    "date": "2026-11-22",
    "malformed": false
  },
  {
    "id": "9df23e4e51f8aa65",
    "raw": "- [x] Is Dana Okafor the new contact for the Northwind invoices? — unsure because only an agent message says so; source: Tasks/2026-11-20-invoices.md; 2026-11-21",
    "checked": true,
    "kind": "",
    "qualifier": "",
    "title": "Is Dana Okafor the new contact for the Northwind invoices?",
    "text": "Is Dana Okafor the new contact for the Northwind invoices?",
    "reason": "only an agent message says so",
    "source": "Tasks/2026-11-20-invoices.md",
    "date": "2026-11-21",
    "malformed": false
  },
  {
    "id": "57e8995d8422e516",
    "raw": "- [ ] Does the weekly sync still happen on Thursdays at 10:00? — unsure because the calendar note is from last spring; source: Projects/Weekly sync.md; 2026-11-20",
    "checked": false,
    "kind": "",
    "qualifier": "",
    "title": "Does the weekly sync still happen on Thursdays at 10:00?",
    "text": "Does the weekly sync still happen on Thursdays at 10:00?",
    "reason": "the calendar note is from last spring",
    "source": "Projects/Weekly sync.md",
    "date": "2026-11-20",
    "malformed": false
  },
  {
    "id": "1b810829470a24d2",
    "raw": "- [ ] Should the travel note keep the old frequent-flyer number? — unsure because the airline account was closed in the October chat; source: chat-2c3d4e5f; 2026-11-19",
    "checked": false,
    "kind": "",
    "qualifier": "",
    "title": "Should the travel note keep the old frequent-flyer number?",
    "text": "Should the travel note keep the old frequent-flyer number?",
    "reason": "the airline account was closed in the October chat",
    "source": "chat-2c3d4e5f",
    "date": "2026-11-19",
    "malformed": false
  }
]

/** The list for `workspace`, optionally with other items. */
export function unsureFixtureList(workspace: string, items: UnsureItem[] = UNSURE_FIXTURE_ITEMS): UnsureList {
  const ticked = items.filter(item => item.checked).length
  return {
    workspace,
    path: `memory-vault/${workspace}/Workspace/Unsure.md`,
    found: true,
    items,
    counts: { waiting: items.length - ticked, ticked },
  }
}
