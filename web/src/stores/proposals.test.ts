/**
 * The review queue's filter state.
 *
 * It lives in the store because the sidebar renders the controls and the panel
 * renders the list — the same split the memory map uses. The scope rule is here
 * for the same reason: the sidebar's chip counts and the list must not be able
 * to disagree about what is in scope.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProposalsStore } from './proposals'
import type { ProposalRow } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

function row(overrides: Partial<ProposalRow> = {}): ProposalRow {
  return {
    id: 'r1',
    kind: 'memory',
    text: 'Remember the thing',
    source: '',
    workspace: 'personal',
    path: 'personal/Workspace/Memory-Proposals.md',
    line: 3,
    ...overrides,
  }
}

describe('proposals store filters', () => {
  beforeEach(() => setActivePinia(createPinia()))

  function seeded() {
    const store = useProposalsStore()
    store.rows = [
      row({ id: 'p-mem' }),
      row({ id: 'p-skill', kind: 'skill', text: '2026-08-09-defuddle' }),
      row({ id: 'w-mem', workspace: 'work', text: 'Work thing' }),
      row({ id: 'w-rehome', workspace: 'work', kind: 'rehome', text: 'Re-home Oliver' }),
      row({ id: 'global', workspace: '', text: 'Applies everywhere', path: 'Workspace/Install.md' }),
    ]
    return store
  }

  it('scopes to a workspace and keeps install-wide rows', () => {
    const store = seeded()

    expect(store.scopedRows('personal').map(r => r.id)).toEqual(['p-mem', 'p-skill', 'global'])
    expect(store.scopedRows('work').map(r => r.id)).toEqual(['w-mem', 'w-rehome', 'global'])
  })

  it('shows everything when no workspace is active yet', () => {
    const store = seeded()

    expect(store.scopedRows('')).toHaveLength(5)
  })

  it('filters by kind within the scope', () => {
    const store = seeded()
    store.kindFilter = 'skill'

    expect(store.visibleRows('personal').map(r => r.id)).toEqual(['p-skill'])
    expect(store.visibleRows('work')).toEqual([])
  })

  it('searches text and path', () => {
    const store = seeded()
    store.search = 'oliver'

    expect(store.visibleRows('work').map(r => r.id)).toEqual(['w-rehome'])

    store.search = 'Workspace/Memory-Proposals'
    expect(store.visibleRows('personal').map(r => r.id)).toEqual(['p-mem', 'p-skill'])
  })

  it('counts kinds over the scope, not the filtered list', () => {
    const store = seeded()
    store.kindFilter = 'skill'

    // Clicking a chip must not renumber the other chips, or the counts move
    // under the pointer as you use them.
    expect(store.kindCounts('personal')).toEqual([
      { kind: 'memory', count: 2 },
      { kind: 'skill', count: 1 },
    ])
  })

  it('resets both filters together', () => {
    const store = seeded()
    store.kindFilter = 'skill'
    store.search = 'oliver'

    store.resetFilters()

    expect(store.kindFilter).toBe('all')
    expect(store.search).toBe('')
    expect(store.visibleRows('personal')).toHaveLength(3)
  })
})
