import { describe, expect, it } from 'vitest'
import { skillProposal, skillRowSub, skillTitle, type SkillProposalItem } from './skillProposals'

const item: SkillProposalItem = {
  path: 'Workspace/Proposals/ask-dates.md', open_path: 'v/Workspace/Proposals/ask-dates.md', withheld: false,
  type: 'skill-edit', target: 'weekly-summary', title: 'Ask for the dates', summary: 's', rationale: 'r',
  proposed_change: 'c', evidence: ['chat-0123abcd', 'People/Mo.md'], created: '2026-11-26', status: 'ready', body: 'b', problems: [],
}

describe('skill proposals', () => {
  it('reads an item as the shared proposal detail does', () => {
    expect(skillProposal(item)).toEqual({
      kind: 'skill-edit', target: 'weekly-summary', title: 'Ask for the dates', summary: 's', rationale: 'r',
      proposedChange: 'c', evidence: ['chat-0123abcd', 'People/Mo.md'], status: 'ready', created: '2026-11-26', body: 'b',
    })
  })

  it('titles and describes a row from what the file carries', () => {
    expect(skillTitle(item)).toBe('Ask for the dates')
    expect(skillTitle({ ...item, title: '' })).toBe('ask-dates.md')
    expect(skillRowSub(item)).toBe('weekly-summary · from 1 chat')
    expect(skillRowSub({ ...item, evidence: [] })).toBe('weekly-summary')
    expect(skillRowSub({ ...item, withheld: true })).toBe('Holds credential-shaped text: not shown')
  })
})
