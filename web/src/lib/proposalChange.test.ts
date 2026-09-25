import { describe, expect, it } from 'vitest'
import { changeFor } from './proposalChange'
import type { ProposalPreview, ProposalRow } from './types'

function row(overrides: Partial<ProposalRow> = {}): ProposalRow {
  return {
    id: 'r', kind: 'memory', text: 'A fact', source: '', workspace: 'work',
    path: 'work/Workspace/Memory-Proposals.md', line: 1, ...overrides,
  }
}

function preview(overrides: Partial<ProposalPreview> = {}): ProposalPreview {
  return {
    id: 'r', workspace: 'work', kind: 'memory', text: 'A fact', source: '',
    action: 'edit_region', operation: 'add', destination: 'ciao:memory', destination_path: '/w/AGENTS.md',
    revision: 'rev', before: '- old', after: '- old\n§\n- A fact', exact: true, truncated: false,
    can_accept: true, reason: '', separator: '\n§\n', ...overrides,
  }
}

describe('changeFor', () => {
  it('reads a people note that does not exist yet as a new note', () => {
    const c = changeFor(row({ kind: 'people' }), preview({
      action: 'write_people_note', operation: 'add', destination: 'People/Mo.md', before: '', after: '---\n# Mo\n',
    }))
    expect(c).toMatchObject({ type: 'new', label: 'New note', verb: 'Create note', destination: 'People/Mo.md', qualifier: 'does not exist yet' })
  })

  it('reads an append to a bounded region as adding an entry to the always-loaded guide', () => {
    const c = changeFor(row(), preview())
    expect(c).toMatchObject({ type: 'add', label: 'Add to a note', verb: 'Add entry', destination: 'work/AGENTS.md' })
    expect(c.qualifier).toBe('Agent memory, always loaded')
    // An empty region is still an add to the guide, not a new file.
    expect(changeFor(row(), preview({ before: '' })).type).toBe('add')
    expect(changeFor(row({ kind: 'profile' }), preview({ destination: 'ciao:profile' })).qualifier)
      .toBe('User profile, always loaded')
  })

  it('reads an append to an existing file as adding a line, and to an empty one as a new note', () => {
    const learnings = { action: 'append_learnings', destination: 'Workspace/Learnings.md', separator: '\n' }
    expect(changeFor(row({ kind: 'learnings' }), preview({ ...learnings, before: '## Active\n' })))
      .toMatchObject({ type: 'add', verb: 'Add line', qualifier: '' })
    expect(changeFor(row({ kind: 'learnings' }), preview({ ...learnings, before: '' })).type).toBe('new')
  })

  it('tells an exact update from a merge a model decides', () => {
    expect(changeFor(row({ kind: 'learnings' }), preview({
      action: 'append_learnings', operation: 'update', destination: 'Workspace/Learnings.md', exact: true,
    }))).toMatchObject({ type: 'update', label: 'Update a line', verb: 'Update line' })
    expect(changeFor(row({ kind: 'project' }), preview({
      action: 'fold_doc', operation: 'update', destination: 'Projects/Ciao.md', exact: false,
    }))).toMatchObject({ type: 'merge', label: 'Merge into a note', verb: 'Merge', qualifier: 'a model folds it in when you accept' })
  })

  it('reads a re-home as a move to the destination workspace', () => {
    const c = changeFor(
      row({ kind: 'rehome', rehome: { note: 'personal/People/Mo.md', destination: 'work', candidates: [], justified: true, reason: '' } }),
      preview({ action: 'move_file', operation: 'move', destination: 'work', exact: false, revision: '' }),
    )
    expect(c).toMatchObject({ type: 'move', verb: 'Move to work', destination: 'personal/People/Mo.md', qualifier: 'to the work workspace' })
  })

  it('reads a duplicate as already saved, and a refused row as blocked', () => {
    expect(changeFor(row(), preview({ operation: 'none' }))).toMatchObject({ type: 'none', label: 'Already saved', verb: 'Clear' })
    expect(changeFor(row(), preview({ operation: 'none', can_accept: false }))).toMatchObject({ type: 'blocked', verb: '' })
    expect(changeFor(row({ kind: 'project' }), preview({ operation: '', can_accept: false, action: 'fold_doc' })).type).toBe('blocked')
  })

  it('gives rows with no preview their own types', () => {
    expect(changeFor(row({ kind: 'skill', path: 'Workspace/skill-proposals/x.md' }), undefined).type).toBe('skill')
    expect(changeFor(row({ kind: 'review' }), undefined, { canAccept: false, fallbackQualifier: 'Needs you' }))
      .toMatchObject({ type: 'decide', qualifier: 'Needs you' })
    expect(changeFor(row(), undefined).type).toBe('pending')
  })
})
