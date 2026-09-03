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
import { api } from '../lib/api'
import { useProposalsStore } from './proposals'
import type { ProposalHistoryRow, ProposalRow, ProposalsResponse } from '../lib/types'

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

describe('post-mutation refresh', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset()
  })

  it('never reads the accepted row back out of a pre-mutation snapshot', async () => {
    // A plain fetch() is still in flight when the accept lands. Its refresh
    // used to be handed that same request back by the in-flight de-dup, so it
    // observed the list as it was *before* the POST: the accepted row was
    // written straight back into `rows` and sat in the queue as if nothing had
    // happened, until something else reloaded the panel.
    let releaseFirst!: (value: ProposalsResponse) => void
    const preMutation = new Promise<ProposalsResponse>(r => { releaseFirst = r })
    vi.mocked(api.get)
      .mockReturnValueOnce(preMutation as never)
      .mockResolvedValueOnce({ rows: [] } as never)
    vi.mocked(api.post).mockResolvedValue({} as never)

    const store = useProposalsStore()
    const reader = store.fetch()
    const accepting = store.act('p1', 'accept')

    releaseFirst({ rows: [row({ id: 'p1' })] })
    await reader
    await accepting

    expect(store.rows).toEqual([])
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(store.loading).toBe(false)
  })

  it('drops a pre-mutation response that lands after the refresh', async () => {
    // The other half of the same race: bypassing the de-dup is not enough if
    // the older, pre-mutation GET is still allowed to write `rows` when it
    // finally arrives — the accepted row would reappear a beat later.
    let releaseFirst!: (value: ProposalsResponse) => void
    const preMutation = new Promise<ProposalsResponse>(r => { releaseFirst = r })
    vi.mocked(api.get)
      .mockReturnValueOnce(preMutation as never)
      .mockResolvedValueOnce({ rows: [] } as never)
    vi.mocked(api.post).mockResolvedValue({} as never)

    const store = useProposalsStore()
    const reader = store.fetch()
    await store.act('p1', 'accept')
    expect(store.rows).toEqual([])

    releaseFirst({ rows: [row({ id: 'p1' })] })
    await reader

    expect(store.rows).toEqual([])
    expect(store.loading).toBe(false)
  })

  it('still de-dupes concurrent readers', async () => {
    // The de-dup is right for two panels mounting at once; only the refresh
    // that follows a mutation has to bypass it.
    vi.mocked(api.get).mockResolvedValue({ rows: [row({ id: 'p1' })] } as never)

    const store = useProposalsStore()
    await Promise.all([store.fetch(), store.fetch()])

    expect(api.get).toHaveBeenCalledTimes(1)
    expect(store.rows.map(r => r.id)).toEqual(['p1'])
  })
})


function historyRow(overrides: Partial<ProposalHistoryRow> = {}): ProposalHistoryRow {
  return {
    id: 'h1',
    ts: '2026-09-01T10:00:00+00:00',
    action: 'accepted',
    via: 'pwa',
    kind: 'memory',
    text: 'Remember the thing',
    source: '',
    workspace: 'personal',
    destination: 'ciao:memory',
    outcome: 'written',
    proposal_id: 'p1',
    ...overrides,
  }
}

describe('proposal history', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset()
  })

  it('loads history rows and exposes truncation', async () => {
    vi.mocked(api.get).mockResolvedValue({
      rows: [historyRow()],
      total: 1,
      truncated: true,
    } as never)

    const store = useProposalsStore()
    await store.fetchHistory()

    expect(api.get).toHaveBeenCalledWith('/api/proposals/history?limit=200')
    expect(store.historyRows).toHaveLength(1)
    expect(store.historyLoaded).toBe(true)
    expect(store.historyTruncated).toBe(true)
  })

  it('drops a stale history response that lands after a forced refetch', async () => {
    let releaseFirst!: (value: unknown) => void
    const stale = new Promise((r) => { releaseFirst = r })
    vi.mocked(api.get)
      .mockReturnValueOnce(stale as never)
      .mockResolvedValueOnce({ rows: [historyRow({ id: 'h2' })], total: 1, truncated: false } as never)

    const store = useProposalsStore()
    const first = store.fetchHistory()
    const second = store.fetchHistory({ force: true })

    releaseFirst({ rows: [historyRow({ id: 'h1' })], total: 1, truncated: false })
    await first
    await second

    expect(store.historyRows.map(r => r.id)).toEqual(['h2'])
  })

  it('filters visibleHistory by workspace scope, kind, action, actor, and search', () => {
    const store = useProposalsStore()
    store.historyRows = [
      historyRow({ id: 'p-mem', workspace: 'personal' }),
      historyRow({ id: 'p-dismiss', workspace: 'personal', action: 'dismissed', via: 'agent', outcome: '' }),
      historyRow({ id: 'w-auto', workspace: 'work', via: 'auto', kind: 'learnings', text: 'Ship weekly' }),
      historyRow({ id: 'global', workspace: '', text: 'Applies everywhere' }),
    ]

    expect(store.visibleHistory('personal').map(r => r.id)).toEqual(['p-mem', 'p-dismiss', 'global'])

    store.historyActionFilter = 'dismissed'
    expect(store.visibleHistory('personal').map(r => r.id)).toEqual(['p-dismiss'])
    store.historyActionFilter = 'all'

    store.historyActorFilter = 'auto'
    expect(store.visibleHistory('work').map(r => r.id)).toEqual(['w-auto'])
    store.historyActorFilter = 'all'

    store.search = 'weekly'
    expect(store.visibleHistory('work').map(r => r.id)).toEqual(['w-auto'])
  })

  it('does not fetch history after a mutation while nothing is displaying it', async () => {
    vi.mocked(api.get).mockResolvedValue({ rows: [] } as never)
    vi.mocked(api.post).mockResolvedValue({} as never)

    const store = useProposalsStore()
    await store.fetch()
    store.historyLoaded = false

    await store.act('p1', 'dismiss')
    await Promise.resolve()

    expect(api.get).not.toHaveBeenCalledWith(expect.stringContaining('/api/proposals/history'))
  })

  it('refreshes a loaded ledger after a mutation even from the Queue tab', async () => {
    // The Queue tab's count badge renders off the loaded ledger, so dropping
    // `historyLoaded` without refetching made the badge vanish on every
    // accept/dismiss and stay gone until the History tab was reopened.
    vi.mocked(api.get).mockResolvedValue({ rows: [] } as never)
    vi.mocked(api.post).mockResolvedValue({} as never)

    const store = useProposalsStore()
    await store.fetch()
    store.view = 'queue'
    store.historyLoaded = true

    await store.act('p1', 'dismiss')
    await Promise.resolve()
    await Promise.resolve()

    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('/api/proposals/history'))
    expect(store.historyLoaded).toBe(true)
  })

  it('invalidates and re-fetches history after a mutation on the History tab', async () => {
    vi.mocked(api.get).mockResolvedValue({ rows: [] } as never)
    vi.mocked(api.post).mockResolvedValue({} as never)

    const store = useProposalsStore()
    await store.fetch()
    store.view = 'history'
    store.historyLoaded = true

    await store.act('p2', 'dismiss')
    await Promise.resolve()

    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('/api/proposals/history'))
  })

  it('does not invalidate history on a plain queue reload', async () => {
    // It used to flip the flag on every `fetch`, so each tab switch
    // re-downloaded the whole ledger even when nothing had changed.
    vi.mocked(api.get).mockResolvedValue({ rows: [] } as never)

    const store = useProposalsStore()
    store.historyLoaded = true
    await store.fetch({ force: true })

    expect(store.historyLoaded).toBe(true)
  })

  it('stops paging once the server caps the page', async () => {
    vi.mocked(api.get).mockResolvedValue({
      rows: [historyRow()], total: 1500, truncated: true, limit: 1000, at_max: true,
    } as never)

    const store = useProposalsStore()
    await store.fetchHistory({ limit: 1200 })

    expect(store.historyTruncated).toBe(true)
    expect(store.historyAtMax).toBe(true)
    expect(store.historyCanLoadMore).toBe(false)
    // The limit tracks what the server served, not what was asked for.
    expect(store.historyLimit).toBe(1000)

    vi.mocked(api.get).mockClear()
    await store.loadMoreHistory()
    expect(api.get).not.toHaveBeenCalled()
  })

  it('keeps paging while the server is still below its cap', async () => {
    vi.mocked(api.get).mockResolvedValue({
      rows: [historyRow()], total: 500, truncated: true, limit: 200, at_max: false,
    } as never)

    const store = useProposalsStore()
    await store.fetchHistory()

    expect(store.historyCanLoadMore).toBe(true)
    await store.loadMoreHistory()
    expect(api.get).toHaveBeenLastCalledWith('/api/proposals/history?limit=400')
  })

  it('does not latch the page cap when a newer request supersedes "show more"', async () => {
    // `fetchHistory` bails on its seq check before touching any state when a
    // newer request has started, so the superseded one leaves `historyRows` at
    // its previous length — indistinguishable from "nothing more to give". It
    // also clears `historyError`, so testing the error alone missed this and
    // "show more" was hidden permanently while more rows still existed.
    vi.mocked(api.get).mockResolvedValue({
      rows: [historyRow()], total: 500, truncated: true, limit: 200, at_max: false,
    } as never)
    const store = useProposalsStore()
    await store.fetchHistory()
    expect(store.historyCanLoadMore).toBe(true)

    // "Show more" is in flight, and never resolves before the newer one does.
    let release: (v: unknown) => void = () => {}
    vi.mocked(api.get).mockImplementationOnce(
      () => new Promise((r) => { release = r }) as never,
    )
    const slow = store.loadMoreHistory()

    // A queue mutation forces a fresh history read, which wins.
    vi.mocked(api.get).mockResolvedValue({
      rows: [historyRow()], total: 500, truncated: true, limit: 200, at_max: false,
    } as never)
    await store.fetchHistory({ force: true })

    release({ rows: [historyRow()], total: 500, truncated: true, limit: 400, at_max: false })
    await slow

    expect(store.historyError).toBe('')
    expect(store.historyAtMax).toBe(false)
    expect(store.historyCanLoadMore).toBe(true)
  })

  it('scopes the history request to a workspace and refetches when it changes', async () => {
    vi.mocked(api.get).mockResolvedValue({ rows: [historyRow()], total: 1, truncated: false } as never)

    const store = useProposalsStore()
    await store.ensureHistoryLoaded('personal')
    expect(api.get).toHaveBeenLastCalledWith('/api/proposals/history?limit=200&workspace=personal')

    // Same workspace, already loaded: no second request.
    vi.mocked(api.get).mockClear()
    await store.ensureHistoryLoaded('personal')
    expect(api.get).not.toHaveBeenCalled()

    // A different workspace is a different page on the server.
    await store.ensureHistoryLoaded('work')
    expect(api.get).toHaveBeenLastCalledWith('/api/proposals/history?limit=200&workspace=work')
  })

  it('keeps a history failure out of the queue error slot', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('history is unreachable'))

    const store = useProposalsStore()
    store.error = 'an unread accept failure'
    await store.fetchHistory()

    expect(store.historyError).toBe('history is unreachable')
    expect(store.error).toBe('an unread accept failure')
  })
})