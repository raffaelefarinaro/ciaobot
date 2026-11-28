import type { SkillProposalItem, SkillProposalList } from '../lib/skillProposals'

/**
 * Three open skill-edit proposals shaped as `GET /api/memory/skill-proposals`
 * answers them (`ciao.skill_proposals.read_skill_proposals`); names, chats and
 * skills are invented. The first carries its change as a diff, the second as
 * plain proposed text, the third has a field the checker asks for missing.
 * Shared by the stories and the Playwright spec.
 */
export function skillFixtureItems(workspace: string): SkillProposalItem[] {
  const at = (slug: string) => ({
    path: `Workspace/Proposals/${slug}.md`,
    open_path: `memory-vault/${workspace}/Workspace/Proposals/${slug}.md`,
  })
  return [
    {
      ...at('weekly-summary-ask-range'),
      withheld: false,
      type: 'skill-edit',
      target: 'weekly-summary',
      title: 'Weekly summary: ask for the date range first',
      summary: 'The skill assumes last week; ask which dates to cover when the request does not say.',
      rationale: 'In both chats the person stopped the summary and said the range was wrong ("last week, Monday to Friday"). Asking first avoids a rerun.',
      proposed_change: ' ## Steps\n-1. Collect last week\'s notes and tasks.\n+1. Ask which date range to cover if the request does not say.\n+2. Collect notes and tasks in that range.\n 3. Group by project.',
      evidence: ['chat-a290cb48', 'chat-ef52bb90', 'Workspace/Care-Report.md'],
      created: '2026-11-27',
      status: 'ready',
      body: 'Both corrections came within the first minute of the run. The rest of the skill worked as written.',
      problems: [],
    },
    {
      ...at('email-drafts-five-lines'),
      withheld: false,
      type: 'skill-edit',
      target: 'email-drafts',
      title: 'Email drafts: keep it under five lines',
      summary: 'Drafts run long; the person trims them to five lines every time.',
      rationale: 'Three drafts this month were cut to five lines or fewer before sending.',
      proposed_change: 'Add to the style rules: "Keep a draft to five lines unless the person asks for more. Put the ask in the first line."',
      evidence: ['chat-3b1f9c20'],
      created: '2026-11-24',
      status: 'draft',
      body: '',
      problems: [],
    },
    {
      ...at('release-notes-group'),
      withheld: false,
      type: 'skill-edit',
      target: 'release-notes',
      title: 'Release notes: group by Heads up, New, Improved, Fixed',
      summary: '',
      rationale: 'The person regrouped the notes this way twice.',
      proposed_change: '+Group the items under Heads up, New, Improved and Fixed.',
      evidence: ['chat-8c40d9e2', 'chat-d9e21a07'],
      created: '2026-11-20',
      status: 'draft',
      body: '',
      problems: ['missing or empty front matter fields: summary'],
    },
  ]
}

export function skillFixtureList(workspace: string, items = skillFixtureItems(workspace)): SkillProposalList {
  return { workspace, found: true, items, counts: { waiting: items.length }, skipped: 0, truncated: false }
}
