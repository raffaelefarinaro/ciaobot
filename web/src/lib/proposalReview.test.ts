import { describe, expect, it } from 'vitest'
import { approveDraft, parseProposal, proposalIsOpen, proposalKindWords, withProposalStatus } from './proposalReview'

const FILE = [
  '---',
  'type: note-retire',
  'target: People/Tom.md',
  'title: Retire the duplicate note for Tom',
  'summary: His facts are now in Tomas Berg.md.',
  'rationale: >',
  '  Two notes for one person',
  '  split his facts.',
  'evidence:',
  '  - People/Tomas Berg.md',
  'proposed_change: |',
  '  Delete People/Tom.md.',
  '  Point links at People/Tomas Berg.md.',
  'created: 2026-11-26',
  'status: ready  # set by care',
  '---',
  '',
  'Body text.',
].join('\n')

describe('proposal files', () => {
  it('reads every field the checker requires, block scalars included', () => {
    const p = parseProposal(FILE)!
    expect(p.kind).toBe('note-retire')
    expect(p.target).toBe('People/Tom.md')
    expect(p.rationale).toBe('Two notes for one person split his facts.')
    expect(p.proposedChange).toBe('Delete People/Tom.md.\nPoint links at People/Tomas Berg.md.')
    expect(p.evidence).toEqual(['People/Tomas Berg.md'])
    expect(p.status).toBe('ready')
    expect(p.body).toBe('Body text.')
    expect(proposalKindWords(p.kind)).toBe('Retire a note')
    expect(parseProposal('no front matter')).toBeNull()
    expect(parseProposal(FILE.replace('note-retire', 'surprise'))!.kind).toBe('')
  })

  it('sets the status and leaves every other byte alone', () => {
    const next = withProposalStatus(FILE, 'rejected')!
    expect(next).toBe(FILE.replace('status: ready  # set by care', 'status: rejected'))
    // A body line that looks like a status line is not front matter.
    const tricky = FILE.replace('Body text.', 'status: draft')
    expect(withProposalStatus(tricky, 'rejected')!.endsWith('status: draft')).toBe(true)
    // No status line yet: one is added at the end of the front matter.
    const none = FILE.replace('status: ready  # set by care\n', '')
    expect(withProposalStatus(none, 'rejected')).toBe(none.replace('created: 2026-11-26\n', 'created: 2026-11-26\nstatus: rejected\n'))
    expect(withProposalStatus('plain text', 'rejected')).toBeNull()
  })

  it('knows which statuses still wait on the person', () => {
    expect(['draft', 'ready', ''].map(proposalIsOpen)).toEqual([true, true, true])
    expect(['applied', 'rejected'].map(proposalIsOpen)).toEqual([false, false])
  })

  it('drafts an approval that asks before filing upstream', () => {
    expect(approveDraft('skill-edit', 'v/Workspace/Proposals/a.md')).toBe(
      'I approve this proposal: v/Workspace/Proposals/a.md. Make exactly the change it describes, set its status to applied, run ciao proposal-check on it, and tell me what changed.',
    )
    expect(approveDraft('upstream-issue', 'p.md')).toContain('never file it without my yes')
  })
})
