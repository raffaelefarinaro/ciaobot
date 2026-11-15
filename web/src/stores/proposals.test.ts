// @vitest-environment jsdom
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
import type { ProposalHistoryRow, ProposalPreview, ProposalRow, ProposalsResponse } from '../lib/types'
const reservationHeaders = { headers: { 'X-Ciao-Proposal-Reservation': `${'a'.repeat(32)}.${'b'.repeat(32)}` } }

vi.mock('../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), postReservation: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

beforeEach(() => {
  localStorage.clear()
  vi.mocked(api.postReservation).mockReset().mockImplementation(async (url: string, body?: unknown) => {
    if (url.endsWith('/seal')) return { sealed: true }
    const scope = body as { kind?: string; workspace?: string; destination?: string; targets?: string[] }
    return { id: `${'a'.repeat(32)}.${'b'.repeat(32)}`, ...(scope.kind === 'sweep'
      ? { scope: { ...scope, workspaces: ['personal', 'work'] } }
      : scope.kind === 'undo' ? { scope: { ...scope, workspace: scope.workspace || 'personal' } }
      : scope.kind === 'batch_accept'
        ? { scope: { ...scope, destinations: Object.fromEntries(useProposalsStore().rows
          .filter(row => scope.targets?.includes(row.id) && row.kind === 'rehome')
          .map(row => [row.id, scope.workspace || 'work'])) } }
      : scope.kind === 'batch_dismiss' ? { scope }
      : scope.kind === 'row_accept' && scope.destination !== undefined
        ? { scope: { ...scope, destination: scope.destination || 'work' } } : {}) }
  })
})

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

describe('partial settlement guards', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api.post).mockReset()
    vi.mocked(api.get).mockReset()
  })

  it('defers a batch seal until every unresolved sibling is inspected, including across reload', async () => {
    let store = useProposalsStore()
    store.rows = [row({ id: 'a' }), row({ id: 'b', workspace: 'work' }), row({ id: 'c', workspace: 'work' })]
    vi.mocked(api.post).mockResolvedValueOnce({ ok: true, results: [
      { id: 'a', action: 'accept', dismissed: true },
      { id: 'b', action: 'accept', dismissed: false, queue_settled: false },
      { id: 'c', action: 'accept', dismissed: false, queue_settled: false },
    ] })
    vi.mocked(api.get).mockResolvedValue({ rows: [] })
    await store.batch(['a', 'b', 'c'], 'accept', 'work')
    expect(api.postReservation).toHaveBeenCalledWith('/api/proposals/reservations', {
      kind: 'batch_accept', workspace: 'work', targets: ['a', 'b', 'c'], workspaces: ['personal', 'work'],
    })
    expect(api.post).toHaveBeenCalledWith('/api/proposals/batch', expect.anything(), reservationHeaders)
    setActivePinia(createPinia())
    store = useProposalsStore()
    expect(store.partialWarnings.a).toBeDefined()
    expect(store.partialWarnings.b).toBeDefined()
    expect(store.partialWarnings.c).toBeDefined()
    await store.acknowledgePartial('b')
    expect(store.partialWarnings.b).toBeDefined()
    expect(store.partialWarnings.c).toBeDefined()
    expect(vi.mocked(api.postReservation).mock.calls.some(([url]) => url.endsWith('/seal'))).toBe(false)
    setActivePinia(createPinia())
    store = useProposalsStore()
    expect(store.partialWarnings.b).toBeDefined()
    await store.acknowledgePartial('c')
    expect(store.partialWarnings.a).toBeUndefined()
    expect(store.partialWarnings.b).toBeUndefined()
    expect(store.partialWarnings.c).toBeUndefined()
    expect(vi.mocked(api.postReservation).mock.calls.filter(([url]) => url.endsWith('/seal'))).toHaveLength(1)
  })

  it('retains all batch markers when the shared operation is active or sealing is lost', async () => {
    let store = useProposalsStore()
    store.rows = [row({ id: 'a' }), row({ id: 'b', workspace: 'work' })]
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('response lost'))
    await store.batch(['a', 'b'], 'dismiss')
    setActivePinia(createPinia())
    store = useProposalsStore()
    await store.acknowledgePartial('a')
    expect(store.partialWarnings.a).toBeDefined()
    expect(store.partialWarnings.b).toBeDefined()
    vi.mocked(api.postReservation).mockRejectedValueOnce(new Error('active'))
    await store.acknowledgePartial('b')
    expect(store.intentLockError).toContain('could not be confirmed')
    vi.mocked(api.postReservation).mockRejectedValueOnce(new TypeError('seal response lost'))
    await store.acknowledgePartial('b')
    expect(store.partialWarnings.a).toBeDefined()
    expect(store.partialWarnings.b).toBeDefined()
    await store.acknowledgePartial('b')
    expect(store.partialWarnings.a).toBeUndefined()
    expect(store.partialWarnings.b).toBeUndefined()
  })

  it('fails closed when a batch inspection cannot be persisted', async () => {
    let store = useProposalsStore()
    store.rows = [row({ id: 'a' }), row({ id: 'b' })]
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('response lost'))
    await store.batch(['a', 'b'], 'dismiss')
    await store.acknowledgePartial('a')
    const write = vi.spyOn(localStorage, 'setItem').mockImplementationOnce(() => { throw new Error('quota') })
    await store.acknowledgePartial('b')
    write.mockRestore()
    expect(store.intentStorageUnavailable).toBe(true)
    expect(vi.mocked(api.postReservation).mock.calls.some(([url]) => url.endsWith('/seal'))).toBe(false)
    store = useProposalsStore(createPinia())
    expect(store.partialWarnings.a?.reviewed).toBe(true)
    expect(store.partialWarnings.b?.reviewed).toBeUndefined()
    expect(store.canAct('a')).toBe(false)
    expect(store.canAct('b')).toBe(false)
  })

  it('keeps remaining batch markers blocked if local cleanup fails after sealing', async () => {
    let store = useProposalsStore()
    store.rows = [row({ id: 'a' }), row({ id: 'b' })]
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('response lost'))
    await store.batch(['a', 'b'], 'dismiss')
    await store.acknowledgePartial('a')
    const remove = vi.spyOn(localStorage, 'removeItem').mockImplementationOnce(() => { throw new Error('storage') })
    await store.acknowledgePartial('b')
    remove.mockRestore()
    expect(store.intentStorageUnavailable).toBe(true)
    store = useProposalsStore(createPinia())
    expect(store.partialWarnings.a?.reviewed).toBe(true)
    expect(store.canAct('a')).toBe(false)
    vi.mocked(api.postReservation).mockRejectedValueOnce(new Error('sealed token evicted'))
    await store.acknowledgePartial('a')
    expect(store.partialWarnings.a).toBeDefined()
    expect(store.canAct('a')).toBe(false)
  })

  it('never dispatches a batch if any row marker fails to persist', async () => {
    const store = useProposalsStore()
    store.rows = [row({ id: 'a' }), row({ id: 'b' })]
    const original = localStorage.setItem.bind(localStorage)
    const write = vi.spyOn(localStorage, 'setItem')
      .mockImplementationOnce(original)
      .mockImplementationOnce(() => { throw new Error('quota') })
    try {
      await store.batch(['a', 'b'], 'dismiss')
      expect(api.post).not.toHaveBeenCalled()
      expect(store.intentStorageUnavailable).toBe(true)
      expect(api.postReservation).toHaveBeenCalledWith('/api/proposals/reservations/seal', {
        id: `${'a'.repeat(32)}.${'b'.repeat(32)}`,
        scope: { kind: 'batch_dismiss', workspace: '', targets: ['a', 'b'], workspaces: ['personal'] },
        inspected: false,
      })
    } finally {
      write.mockRestore()
    }
  })

  it('reserves, persists and dispatches a scoped single action before sealing it', async () => {
    const store = useProposalsStore()
    store.rows = [row()]
    const id = `${'a'.repeat(32)}.${'b'.repeat(32)}`
    vi.mocked(api.post).mockImplementation(async (url: string, ...args: unknown[]) => {
      expect(url).toBe('/api/proposals/r1/dismiss')
      expect(args[1]).toEqual({ headers: { 'X-Ciao-Proposal-Reservation': id } })
      expect(Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i)).some(key => key?.includes(':v2:'))).toBe(true)
      return { ok: true, result: { id: 'r1', action: 'dismiss', dismissed: true } }
    })
    vi.mocked(api.get).mockResolvedValue({ rows: [] })
    expect((await store.act('r1', 'dismiss')).ok).toBe(true)
    expect(localStorage.length).toBe(0)
  })

  it('retains a v2 warning after a lost response until server sealing succeeds', async () => {
    let store = useProposalsStore()
    store.rows = [row()]
    const id = `${'a'.repeat(32)}.${'b'.repeat(32)}`
    let seal = false
    vi.mocked(api.postReservation).mockImplementation(async (url: string) => {
      if (url === '/api/proposals/reservations/seal') {
        if (!seal) throw new Error('active or unknown')
        return { sealed: true }
      }
      return { id }
    })
    vi.mocked(api.post).mockImplementation(async () => {
      throw new TypeError('response lost')
    })
    await store.act('r1', 'dismiss')
    setActivePinia(createPinia())
    store = useProposalsStore()
    expect(store.partialWarnings.r1).toBeDefined()
    await store.acknowledgePartial('r1')
    expect(store.partialWarnings.r1).toBeDefined()
    expect(store.intentLockError).toContain('could not be confirmed')
    seal = true
    await store.acknowledgePartial('r1')
    expect(store.partialWarnings.r1).toBeUndefined()
    expect(localStorage.length).toBe(0)
  })

  it('does not dispatch when reservation or marker persistence fails', async () => {
    const store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.postReservation).mockRejectedValueOnce(new TypeError('reservation lost'))
    expect((await store.act('r1', 'dismiss')).ok).toBe(false)
    expect(api.post).not.toHaveBeenCalled()
    expect(vi.mocked(api.postReservation).mock.calls.map(call => call[0])).toEqual(['/api/proposals/reservations'])
  })

  it('never dispatches after reservation if the marker cannot be persisted', async () => {
    const store = useProposalsStore()
    store.rows = [row()]
    const write = vi.spyOn(localStorage, 'setItem').mockImplementationOnce(() => { throw new Error('quota') })
    try {
      expect((await store.act('r1', 'dismiss')).ok).toBe(false)
      expect(api.post).not.toHaveBeenCalled()
      expect(api.postReservation).toHaveBeenCalledWith('/api/proposals/reservations/seal', {
        id: `${'a'.repeat(32)}.${'b'.repeat(32)}`,
        scope: { kind: 'row_dismiss', workspace: 'personal', targets: ['r1'] },
        inspected: false,
      })
      expect(store.intentStorageUnavailable).toBe(true)
    } finally {
      write.mockRestore()
    }
  })

  it('does not dispatch when marker persistence and best-effort seal both fail', async () => {
    const store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.postReservation).mockImplementation(async url => {
      if (url.endsWith('/seal')) throw new TypeError('seal lost')
      return { id: `${'a'.repeat(32)}.${'b'.repeat(32)}` }
    })
    const write = vi.spyOn(localStorage, 'setItem').mockImplementationOnce(() => { throw new Error('quota') })
    try {
      expect((await store.act('r1', 'dismiss')).ok).toBe(false)
      expect(api.post).not.toHaveBeenCalled()
      expect(api.postReservation).toHaveBeenCalledTimes(2)
      expect(store.intentStorageUnavailable).toBe(true)
    } finally {
      write.mockRestore()
    }
  })

  it('retains the marker when a successful mutation loses its seal response', async () => {
    let store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockResolvedValueOnce({ ok: true, result: { id: 'r1', action: 'dismiss', dismissed: true } })
    vi.mocked(api.postReservation).mockImplementation(async (url: string) => {
      if (url.endsWith('/seal')) throw new TypeError('seal response lost')
      return { id: `${'a'.repeat(32)}.${'b'.repeat(32)}` }
    })
    expect((await store.act('r1', 'dismiss')).ok).toBe(false)
    setActivePinia(createPinia())
    store = useProposalsStore()
    expect(store.partialWarnings.r1).toBeDefined()
    await store.acknowledgePartial('r1')
    expect(store.partialWarnings.r1).toBeDefined()
    expect(store.intentLockError).toContain('could not be confirmed')
  })

  it.each(['single', 'batch', 'skill'])('records install-wide scope for %s', async (kind) => {
    const store = useProposalsStore()
    store.rows = [row({ id: 'global', workspace: '', kind: kind === 'skill' ? 'skill' : 'memory' })]
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('response lost'))
    if (kind === 'single') await store.act('global', 'dismiss')
    else if (kind === 'batch') await store.batch(['global'], 'dismiss')
    else await store.acceptSkill('global')
    expect(api.post).toHaveBeenCalledTimes(1)
    setActivePinia(createPinia())
    expect(useProposalsStore().partialWarnings.global?.workspace).toBe('')
  })

  it('accepts the flat skill-file dismissal response as settled', async () => {
    const store = useProposalsStore()
    store.rows = [row({ id: 'skill', kind: 'skill', workspace: '' })]
    vi.mocked(api.post).mockResolvedValueOnce({ id: 'skill', action: 'dismiss', dismissed: true })
    vi.mocked(api.get).mockResolvedValue({ rows: [] })
    expect((await store.act('skill', 'dismiss')).ok).toBe(true)
    setActivePinia(createPinia())
    expect(useProposalsStore().partialWarnings.skill).toBeUndefined()
  })

  it('clears only a matching explicitly pre-write accept refusal', async () => {
    const store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockRejectedValueOnce(Object.assign(new Error('fold refused'), {
      status: 409, payload: { id: 'r1', no_effect: true, error: 'fold refused' },
    }))
    const result = await store.act('r1', 'accept')
    expect(result.ok).toBe(false)
    expect(store.partialWarnings.r1).toBeUndefined()
    expect(store.canAct('r1')).toBe(true)
  })

  it('clears a late missing-bullet refusal only after matching proof and a confirmed seal', async () => {
    const store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockRejectedValueOnce(Object.assign(new Error('already resolved'), {
      status: 409, payload: { id: 'r1', no_effect: true, error: 'already resolved' },
    }))
    await store.act('r1', 'accept')
    expect(store.partialWarnings.r1).toBeUndefined()
    expect(api.postReservation).toHaveBeenCalledWith('/api/proposals/reservations/seal', expect.objectContaining({
      scope: { kind: 'row_accept', workspace: 'personal', targets: ['r1'] }, inspected: true,
    }))
    expect(useProposalsStore(createPinia()).partialWarnings.r1).toBeUndefined()
  })

  it.each([400, 503])('accepts an explicit matching no-effect refusal with status %i', async status => {
    const store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockRejectedValueOnce(Object.assign(new Error('refused'), {
      status, payload: { id: 'r1', no_effect: true },
    }))
    await store.act('r1', 'accept')
    expect(store.partialWarnings.r1).toBeUndefined()
    expect(api.postReservation).toHaveBeenCalledWith('/api/proposals/reservations/seal', expect.objectContaining({
      scope: { kind: 'row_accept', workspace: 'personal', targets: ['r1'] }, inspected: true,
    }))
  })

  it('clears a batch preflight refusal only for the exact marked ids', async () => {
    const store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockRejectedValueOnce(Object.assign(new Error('journal unavailable'), {
      status: 503, payload: { ids: ['r1'], no_effect: true },
    }))
    await store.batch(['r1'], 'accept')
    expect(store.partialWarnings.r1).toBeUndefined()
    vi.mocked(api.post).mockRejectedValueOnce(Object.assign(new Error('unknown'), {
      status: 503, payload: { ids: ['other'], no_effect: true },
    }))
    await store.batch(['r1'], 'accept')
    expect(store.partialWarnings.r1).toBeDefined()
  })

  it.each([
    { payload: { id: 'r1', no_effect: true }, clear: true },
    { payload: { id: 'other', no_effect: true }, clear: false },
    { payload: { id: 'r1' }, clear: false },
  ])('single admitted recheck only clears an exact proof: $payload', async ({ payload, clear }) => {
    const store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockRejectedValueOnce(Object.assign(new Error('scope changed'), { status: 409, payload }))
    await store.act('r1', 'accept')
    expect(Boolean(store.partialWarnings.r1)).toBe(!clear)
    expect(vi.mocked(api.postReservation).mock.calls.some(([url]) => url.endsWith('/seal'))).toBe(clear)
    expect(Boolean(useProposalsStore(createPinia()).partialWarnings.r1)).toBe(!clear)
  })

  it.each([
    { ids: ['r2', 'r1'], clear: true },
    { ids: ['r1'], clear: false },
    { ids: ['r1', 'r1'], clear: false },
    { ids: ['r1', 'other'], clear: false },
    { ids: ['r1', 'r2', 'other'], clear: false },
  ])('batch admitted recheck only clears the exact ID set: $ids', async ({ ids, clear }) => {
    const store = useProposalsStore()
    store.rows = [row(), row({ id: 'r2' })]
    vi.mocked(api.post).mockRejectedValueOnce(Object.assign(new Error('scope changed'), {
      status: 409, payload: { ids, no_effect: true },
    }))
    await store.batch(['r1', 'r2'], 'accept')
    const reloaded = useProposalsStore(createPinia())
    for (const id of ['r1', 'r2']) {
      expect(Boolean(store.partialWarnings[id])).toBe(!clear)
      expect(Boolean(reloaded.partialWarnings[id])).toBe(!clear)
    }
    expect(vi.mocked(api.postReservation).mock.calls.some(([url]) => url.endsWith('/seal'))).toBe(clear)
  })

  it('keeps the sweep marker when its applied receipt is unconfirmed', async () => {
    const store = useProposalsStore()
    vi.mocked(api.post).mockRejectedValueOnce(Object.assign(new Error('receipt unavailable'), {
      status: 503, payload: { queue_settled: false, queue_state: 'replaced', queue_receipt_confirmed: false },
    }))
    await store.dismissOlderThan('2026-01-01')
    expect(store.partialWarnings.__sweep__?.queueState).toBe('replaced')
    expect(store.partialWarnings.__sweep__?.queueReceiptConfirmed).toBe(false)
    const reloaded = useProposalsStore(createPinia())
    expect(reloaded.partialWarnings.__sweep__?.queueReceiptConfirmed).toBe(false)
  })

  it('rehydrates a lost sweep and refuses acknowledgement until server sealing', async () => {
    let store = useProposalsStore()
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('response lost'))
    await store.dismissOlderThan('2026-01-01')
    expect(api.postReservation).toHaveBeenCalledWith('/api/proposals/reservations', {
      kind: 'sweep', workspace: '', targets: ['2026-01-01'],
    })
    expect(api.post).toHaveBeenCalledWith('/api/proposals/dismiss-older-than?date=2026-01-01',
      undefined, reservationHeaders)
    setActivePinia(createPinia())
    store = useProposalsStore()
    vi.mocked(api.postReservation).mockRejectedValueOnce(new Error('active or old epoch'))
    await store.acknowledgePartial('__sweep__')
    expect(store.partialWarnings.__sweep__).toBeDefined()
    await store.acknowledgePartial('__sweep__')
    expect(store.partialWarnings.__sweep__).toBeUndefined()
    expect(api.postReservation).toHaveBeenLastCalledWith('/api/proposals/reservations/seal', {
      id: `${'a'.repeat(32)}.${'b'.repeat(32)}`,
      scope: { kind: 'sweep', workspace: '', targets: ['2026-01-01'], workspaces: ['personal', 'work'] },
      inspected: true,
    })
  })

  it.each(['implement', 'undo'])('retains a lost %s until the server can seal it', async kind => {
    let store = useProposalsStore()
    store.rows = [row({ id: 'skill', kind: 'skill' })]
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('response lost'))
    if (kind === 'implement') await store.acceptSkill('skill')
    else await store.undoReceipt('receipt', 'personal')
    const id = kind === 'implement' ? 'skill' : '__undo__:receipt'
    expect(api.post).toHaveBeenCalledWith(
      kind === 'implement' ? '/api/proposals/skill/implement' : '/api/memory/receipts/receipt/undo?workspace=personal',
      undefined, reservationHeaders,
    )
    setActivePinia(createPinia())
    store = useProposalsStore()
    expect(store.partialWarnings[id]).toBeDefined()
    vi.mocked(api.postReservation).mockRejectedValueOnce(new Error('active'))
    await store.acknowledgePartial(id)
    expect(store.partialWarnings[id]).toBeDefined()
    await store.acknowledgePartial(id)
    expect(store.partialWarnings[id]).toBeUndefined()
  })

  it('seals and clears a sweep that confirms nothing was removed', async () => {
    const store = useProposalsStore()
    vi.mocked(api.post).mockResolvedValueOnce({ ok: true, removed: 0 })
    vi.mocked(api.get).mockResolvedValue({ rows: [] })
    await store.dismissOlderThan('2026-01-01')
    expect(store.partialWarnings.__sweep__).toBeUndefined()
    expect(localStorage.length).toBe(0)
    expect(api.postReservation).toHaveBeenCalledWith('/api/proposals/reservations/seal', expect.anything())
  })

  it('keeps a sweep warning after earlier removals and a later failure', async () => {
    const store = useProposalsStore()
    vi.mocked(api.post).mockRejectedValueOnce(Object.assign(new Error('later queue failed'), {
      status: 503, payload: { removed: 2, workspace: 'work', queue_settled: false, queue_state: 'unknown' },
    }))
    await store.dismissOlderThan('2026-01-01')
    expect(store.partialWarnings.__sweep__?.queueState).toBe('unknown')
    setActivePinia(createPinia())
    expect(useProposalsStore().partialWarnings.__sweep__).toBeDefined()
  })

  it('does not infer no effect from an unmarked matching 409', async () => {
    const store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockRejectedValueOnce(Object.assign(new Error('unknown'), {
      status: 409, payload: { id: 'r1', conflict: true },
    }))
    await store.act('r1', 'accept')
    expect(store.partialWarnings.r1).toBeDefined()
  })

  it('clears a batch busy result only with its explicit no-effect contract', async () => {
    const store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockResolvedValueOnce({
      ok: true, action: 'accept', results: [{ id: 'r1', action: 'accept', dismissed: false, error: 'busy', no_effect: true }],
    })
    await store.batch(['r1'], 'accept')
    expect(store.partialWarnings.r1).toBeUndefined()
  })

  it.each(['single', 'batch'])('retains the chosen rehome target after a lost %s response', async (kind) => {
    let store = useProposalsStore()
    store.rows = [row({ id: 'move', workspace: 'personal', kind: 'rehome' })]
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('response lost'))
    if (kind === 'single') await store.act('move', 'accept', 'work')
    else await store.batch(['move'], 'accept', 'work')
    setActivePinia(createPinia())
    store = useProposalsStore()
    expect(store.partialWarnings.move?.workspace).toBe('personal')
    expect(store.partialWarnings.move?.target).toBe('work')
  })

  it('retains the server-derived rehome destination after a lost response and seals that scope', async () => {
    let store = useProposalsStore()
    store.rows = [row({ id: 'move', workspace: 'personal', kind: 'rehome' })]
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('response lost'))
    await store.act('move', 'accept')
    setActivePinia(createPinia())
    store = useProposalsStore()
    expect(store.partialWarnings.move?.target).toBe('work')
    await store.acknowledgePartial('move')
    expect(api.postReservation).toHaveBeenLastCalledWith('/api/proposals/reservations/seal', {
      id: `${'a'.repeat(32)}.${'b'.repeat(32)}`,
      scope: { kind: 'row_accept', workspace: 'personal', targets: ['move'], destination: 'work' },
      inspected: true,
    })
  })

  it('retains each server-derived batch destination across a lost response and reload', async () => {
    let store = useProposalsStore()
    store.rows = [row({ id: 'move', kind: 'rehome' }), row({ id: 'plain' }),
      row({ id: 'move2', kind: 'rehome', workspace: 'work' })]
    const scope = { kind: 'batch_accept', workspace: '', targets: ['move', 'plain', 'move2'],
      workspaces: ['personal', 'work'], destinations: { move: 'work', move2: 'personal' } }
    vi.mocked(api.postReservation).mockImplementation(async url => url.endsWith('/seal')
      ? { sealed: true } : { id: `${'a'.repeat(32)}.${'b'.repeat(32)}`, scope })
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('response lost'))
    await store.batch(['move', 'plain', 'move2'], 'accept')
    store = useProposalsStore(createPinia())
    expect(store.partialWarnings.move?.target).toBe('work')
    expect(store.partialWarnings.move2?.target).toBe('personal')
    expect(store.partialWarnings.plain?.target).toBeUndefined()
    await store.acknowledgePartial('move')
    expect(vi.mocked(api.postReservation).mock.calls.some(([url]) => url.endsWith('/seal'))).toBe(false)
    await store.acknowledgePartial('plain')
    await store.acknowledgePartial('move2')
    expect(api.postReservation).toHaveBeenLastCalledWith('/api/proposals/reservations/seal', {
      id: `${'a'.repeat(32)}.${'b'.repeat(32)}`, scope, inspected: true,
    })
    expect(store.partialWarnings.move2).toBeUndefined()
  })

  it.each([
    { move: 'work', other: 'work' }, {}, { move: '' }, { move: 'personal' },
  ])('refuses an invalid server batch destination map: %o', async destinations => {
    const store = useProposalsStore()
    store.rows = [row({ id: 'move', kind: 'rehome' }), row({ id: 'plain' })]
    vi.mocked(api.postReservation).mockResolvedValueOnce({
      id: `${'a'.repeat(32)}.${'b'.repeat(32)}`,
      scope: { kind: 'batch_accept', workspace: '', targets: ['move', 'plain'], workspaces: ['personal'], destinations },
    })
    await store.batch(['move', 'plain'], 'accept')
    expect(api.post).not.toHaveBeenCalled()
  })

  it('records an accept partial and blocks later decisions and the all-workspace sweep', async () => {
    const store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockRejectedValueOnce(Object.assign(new Error('incomplete'), {
      payload: { queue_settled: false, queue_state: 'unknown' },
    }))
    await store.act('r1', 'accept')
    expect(store.partialWarnings.r1?.workspace).toBe('personal')
    await store.act('r1', 'accept')
    await store.batch(['r1', 'other'], 'dismiss')
    await store.dismissOlderThan('2026-01-01')
    expect(api.post).toHaveBeenCalledTimes(1)
    await store.acknowledgePartial('r1')
    vi.mocked(api.post).mockResolvedValueOnce({ removed: 0 })
    vi.mocked(api.get).mockResolvedValue({ rows: [] })
    await store.dismissOlderThan('2026-01-01')
    expect(api.post).toHaveBeenCalledTimes(2)
    expect(store.partialWarnings.r1).toBeUndefined()
  })

  it.each(['partial', 'lost'])('rehydrates a v2 %s accept until server seal', async (outcome) => {
    let store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockRejectedValueOnce(outcome === 'partial'
      ? Object.assign(new Error('incomplete'), { payload: { queue_settled: false, queue_state: 'unknown' } })
      : new TypeError('connection lost'))
    await store.act('r1', 'accept')
    expect(api.post).toHaveBeenCalledTimes(1)
    const key = Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i))
      .find(item => item?.startsWith('ciao:proposal-intent:v2:personal:r1:'))!
    expect(localStorage.getItem(key)).not.toContain('Remember the thing')
    setActivePinia(createPinia())
    store = useProposalsStore()
    store.rows = [row(), row({ id: 'other', workspace: 'work' })]
    expect(store.partialWarnings.r1?.workspace).toBe('personal')
    await store.act('r1', 'dismiss')
    await store.batch(['r1', 'other'], 'accept')
    expect(api.post).toHaveBeenCalledTimes(1)
    await store.acknowledgePartial('r1')
    vi.mocked(api.post).mockResolvedValueOnce({ ok: true, result: { id: 'r1', action: 'dismiss', dismissed: true } })
    vi.mocked(api.get).mockResolvedValue({ rows: [] })
    expect((await store.act('r1', 'dismiss')).ok).toBe(true)
    expect(api.post).toHaveBeenCalledTimes(2)
    setActivePinia(createPinia())
    expect(useProposalsStore().partialWarnings.r1).toBeUndefined()
  })

  it('refuses before POST when intent storage is full', async () => {
    const store = useProposalsStore()
    store.rows = [row()]
    const write = vi.spyOn(localStorage, 'setItem').mockImplementationOnce(() => { throw new Error('quota') })
    expect((await store.act('r1', 'accept')).ok).toBe(false)
    expect(api.post).not.toHaveBeenCalled()
    expect(store.intentStorageUnavailable).toBe(true)
    expect(api.postReservation).toHaveBeenLastCalledWith('/api/proposals/reservations/seal', {
      id: `${'a'.repeat(32)}.${'b'.repeat(32)}`, inspected: false,
      scope: { kind: 'row_accept', workspace: 'personal', targets: ['r1'] },
    })
    write.mockRestore()
  })

  it('fails closed on malformed persisted intent', async () => {
    localStorage.setItem('ciao:proposal-intent:v1:personal:r1', '{')
    const store = useProposalsStore()
    store.rows = [row()]
    expect(store.intentStorageUnavailable).toBe(true)
    await store.act('r1', 'dismiss')
    await store.batch(['r1', 'other'], 'accept')
    expect(api.post).not.toHaveBeenCalled()
  })

  it('keeps the warning scope but blocks other operations without server proof', async () => {
    let store = useProposalsStore()
    store.rows = [row(), row({ id: 'other', workspace: 'work' })]
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('lost'))
    await store.act('r1', 'accept')
    setActivePinia(createPinia())
    store = useProposalsStore()
    store.rows = [row(), row({ id: 'other', workspace: 'work' })]
    vi.mocked(api.post).mockResolvedValueOnce({ ok: true, result: { id: 'other', action: 'dismiss', dismissed: true } })
    vi.mocked(api.get).mockResolvedValue({ rows: [] })
    expect((await store.act('other', 'dismiss')).ok).toBe(false)
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(store.partialWarnings.r1?.workspace).toBe('personal')
    expect(store.partialWarnings.other).toBeUndefined()
  })

  it('retains accounted-for and unsettled batch rows after reload', async () => {
    let store = useProposalsStore()
    store.rows = [row(), row({ id: 'other', workspace: 'work' })]
    vi.mocked(api.post).mockResolvedValueOnce({
      ok: true,
      results: [
        { id: 'r1', action: 'dismiss', dismissed: true },
        { id: 'other', action: 'dismiss', dismissed: false, queue_settled: false, queue_state: 'replaced',
          queue_receipt_confirmed: true, queue_receipt_id: 'receipt-42', decision_confirmed: false },
      ],
    })
    vi.mocked(api.get).mockResolvedValue({ rows: [] })
    await store.batch(['r1', 'other'], 'dismiss')
    setActivePinia(createPinia())
    store = useProposalsStore()
    expect(store.partialWarnings.r1?.reviewed).toBe(true)
    expect(store.partialWarnings.other?.workspace).toBe('work')
    expect(store.partialWarnings.other?.queueState).toBe('replaced')
    expect(store.partialWarnings.other?.queueReceiptId).toBe('receipt-42')
    expect(store.partialWarnings.other?.decisionConfirmed).toBe(false)
    store.rows = [row({ id: 'other', workspace: 'work' })]
    await store.act('other', 'accept')
    expect(api.post).toHaveBeenCalledTimes(1)
  })

  it('keeps the pre-request marker when recording partial details hits quota', async () => {
    let store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockImplementationOnce(async () => {
      vi.spyOn(localStorage, 'setItem').mockImplementationOnce(() => { throw new Error('quota') })
      throw Object.assign(new Error('incomplete'), { payload: { queue_settled: false, queue_state: 'replaced' } })
    })
    await store.act('r1', 'accept')
    expect(store.intentStorageUnavailable).toBe(true)
    vi.restoreAllMocks()
    setActivePinia(createPinia())
    store = useProposalsStore()
    store.rows = [row()]
    expect(store.partialWarnings.r1?.queueState).toBe('unknown')
    await store.act('r1', 'accept')
    expect(api.post).toHaveBeenCalledTimes(1)
  })

  it('keeps a lost sweep blocked until its reservation is sealed', async () => {
    let store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('lost'))
    await store.dismissOlderThan('2026-01-01')
    setActivePinia(createPinia())
    store = useProposalsStore()
    store.rows = [row()]
    await store.act('r1', 'dismiss')
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(store.partialWarnings.__sweep__).toBeDefined()
    await store.acknowledgePartial('__sweep__')
    vi.mocked(api.post).mockResolvedValueOnce({ ok: true, result: { id: 'r1', action: 'dismiss', dismissed: true } })
    vi.mocked(api.get).mockResolvedValue({ rows: [] })
    expect((await store.act('r1', 'dismiss')).ok).toBe(true)
    expect(api.post).toHaveBeenCalledTimes(2)
  })

  it.each(['batch', 'sweep'])('refuses %s before POST when storage is full', async (kind) => {
    const store = useProposalsStore()
    store.rows = [row()]
    const write = vi.spyOn(localStorage, 'setItem').mockImplementationOnce(() => { throw new Error('quota') })
    if (kind === 'batch') await store.batch(['r1'], 'dismiss')
    else await store.dismissOlderThan('2026-01-01')
    expect(api.post).not.toHaveBeenCalled()
    expect(store.intentStorageUnavailable).toBe(true)
    expect(api.postReservation).toHaveBeenLastCalledWith('/api/proposals/reservations/seal', {
      id: `${'a'.repeat(32)}.${'b'.repeat(32)}`, inspected: false,
      scope: kind === 'batch'
        ? { kind: 'batch_dismiss', workspace: '', targets: ['r1'], workspaces: ['personal'] }
        : { kind: 'sweep', workspace: '', targets: ['2026-01-01'], workspaces: ['personal', 'work'] },
    })
    write.mockRestore()
  })

  it.each(['skill', 'undo'])('refuses %s before POST when storage is full', async (kind) => {
    const store = useProposalsStore()
    store.rows = [row({ id: 'skill', kind: 'skill' })]
    const write = vi.spyOn(localStorage, 'setItem').mockImplementationOnce(() => { throw new Error('quota') })
    if (kind === 'skill') await store.acceptSkill('skill')
    else await store.undoReceipt('receipt', 'personal')
    expect(api.post).not.toHaveBeenCalled()
    expect(store.intentStorageUnavailable).toBe(true)
    expect(api.postReservation).toHaveBeenLastCalledWith('/api/proposals/reservations/seal', {
      id: `${'a'.repeat(32)}.${'b'.repeat(32)}`, inspected: false,
      scope: kind === 'skill'
        ? { kind: 'implement', workspace: 'personal', targets: ['skill'] }
        : { kind: 'undo', workspace: 'personal', targets: ['receipt'] },
    })
    write.mockRestore()
  })

  it('retains an unknown undo across reload and blocks proposal decisions', async () => {
    let store = useProposalsStore()
    store.rows = [row()]
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('lost'))
    await store.undoReceipt('receipt', 'personal')
    setActivePinia(createPinia())
    store = useProposalsStore()
    store.rows = [row()]
    await store.act('r1', 'accept')
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(store.partialWarnings['__undo__:receipt']?.workspace).toBe('personal')
    await store.acknowledgePartial('__undo__:receipt')
    vi.mocked(api.post).mockResolvedValueOnce({ ok: true, result: { id: 'r1', action: 'dismiss', dismissed: true } })
    vi.mocked(api.get).mockResolvedValue({ rows: [] })
    expect((await store.act('r1', 'dismiss')).ok).toBe(true)
    expect(api.post).toHaveBeenCalledTimes(2)
  })

  it.each([
    { status: 409, payload: { id: 'receipt', conflict: true } },
    { status: 400, payload: { id: 'receipt', undoable: false } },
  ])('clears the undo marker for a proved no-effect refusal: %o', async (failure) => {
    const store = useProposalsStore()
    vi.mocked(api.post).mockRejectedValueOnce(Object.assign(new Error('refused'), failure))
    expect((await store.undoReceipt('receipt', 'personal')).ok).toBe(false)
    expect(store.partialWarnings['__undo__:receipt']).toBeUndefined()
    setActivePinia(createPinia())
    expect(useProposalsStore().partialWarnings['__undo__:receipt']).toBeUndefined()
  })
})

describe('cross-tab proposal admission', () => {
  beforeEach(() => {
    vi.mocked(api.post).mockReset()
    vi.mocked(api.get).mockReset()
    vi.mocked(api.get).mockResolvedValue({ rows: [] })
  })

  function stores() {
    const first = useProposalsStore(createPinia())
    const second = useProposalsStore(createPinia())
    first.rows = [row({ workspace: '' })]
    second.rows = [row({ workspace: '' })]
    return [first, second] as const
  }

  it('never acknowledges a legacy marker without server completion proof', async () => {
    const token = 'c'.repeat(32)
    localStorage.setItem(`ciao:proposal-intent:v1::r1:${token}`,
      JSON.stringify({ version: 1, workspace: '', id: 'r1', action: 'dismiss', token }))
    const [, second] = stores()
    await second.acknowledgePartial('r1')
    expect(localStorage.length).toBe(1)
    expect(second.partialWarnings.r1).toBeDefined()
    expect(second.intentLockError).toContain('cannot verify')
    second.rows = [row({ id: 'another' })]
    expect((await second.act('another', 'dismiss')).ok).toBe(false)
    expect(api.post).not.toHaveBeenCalled()
  })

  function heldPost() {
    let reject!: (reason: Error) => void
    let started!: () => void
    const entered = new Promise<void>(resolve => { started = resolve })
    vi.mocked(api.post).mockImplementationOnce(() => {
      started()
      return new Promise((_resolve, fail) => { reject = fail })
    })
    return { entered, fail: () => reject(new TypeError('lost response')) }
  }

  it.each(['row', 'sweep', 'undo', 'batch', 'skill'])(
    'refuses a concurrent %s operation while a row POST is in flight', async kind => {
      const [first, second] = stores()
      const barrier = heldPost()
      const pending = first.act('r1', 'dismiss')
      await barrier.entered
      if (kind === 'row') await second.act('r1', 'dismiss')
      if (kind === 'sweep') await second.dismissOlderThan('2026-01-01')
      if (kind === 'undo') await second.undoReceipt('receipt', '')
      if (kind === 'batch') await second.batch(['r1'], 'dismiss')
      if (kind === 'skill') await second.acceptSkill('r1')
      expect(api.post).toHaveBeenCalledTimes(1)
      expect(second.intentLockError).toContain('in progress')
      barrier.fail()
      await pending
    },
  )

  it('hydrates another tab, blocks active acknowledgement, then seals after inspection', async () => {
    const [first, second] = stores()
    const barrier = heldPost()
    const pending = first.act('r1', 'dismiss')
    await barrier.entered
    const key = localStorage.key(0)!
    window.dispatchEvent(new StorageEvent('storage', { key, newValue: localStorage.getItem(key) }))
    expect(second.partialWarnings.r1?.workspace).toBe('')
    await second.acknowledgePartial('r1')
    expect(localStorage.getItem(key)).not.toBeNull()
    expect(second.partialWarnings.r1).toBeDefined()
    barrier.fail()
    await pending
    await second.acknowledgePartial('r1')
    expect(localStorage.getItem(key)).toBeNull()
    expect(second.partialWarnings.r1).toBeUndefined()
  })

  it('refuses a row while a global sweep is in flight', async () => {
    const [first, second] = stores()
    const barrier = heldPost()
    const pending = first.dismissOlderThan('2026-01-01')
    await barrier.entered
    expect((await second.act('r1', 'dismiss')).ok).toBe(false)
    expect(api.post).toHaveBeenCalledTimes(1)
    barrier.fail()
    await pending
  })

  it('persists the server-resolved workspace for an unscoped undo', async () => {
    const [store] = stores()
    vi.mocked(api.post).mockRejectedValueOnce(new TypeError('lost response'))
    await store.undoReceipt('receipt', '')
    expect(useProposalsStore(createPinia()).partialWarnings['__undo__:receipt'].workspace).toBe('personal')
  })

  it('fails closed with an explanation when Web Locks are unavailable', async () => {
    const descriptor = Object.getOwnPropertyDescriptor(navigator, 'locks')!
    Object.defineProperty(navigator, 'locks', { configurable: true, value: undefined })
    try {
      const [store] = stores()
      expect((await store.act('r1', 'dismiss')).ok).toBe(false)
      await store.acknowledgePartial('r1')
      expect(api.post).not.toHaveBeenCalled()
      expect(store.intentLockError).toContain('Web Locks are unavailable')
    } finally {
      Object.defineProperty(navigator, 'locks', descriptor)
    }
  })
})

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

  describe('needs-you filter', () => {
    function decided() {
      const store = useProposalsStore()
      store.rows = [
        row({ id: 'you', decider: 'you' }),
        row({ id: 'none' }),
        row({ id: 'night', decider: 'nightly' }),
        row({ id: 'w-you', workspace: 'work', decider: 'you' }),
      ]
      return store
    }

    it('needs-you shows undecided and decide:you rows, hides decide:nightly', () => {
      const store = decided()
      store.decideFilter = 'needs-you'

      expect(store.visibleRows('personal').map(r => r.id)).toEqual(['you', 'none'])
    })

    it('needsYouCount counts over the workspace scope', () => {
      const store = decided()
      store.decideFilter = 'needs-you'

      expect(store.needsYouCount('personal')).toBe(2)
      expect(store.needsYouCount('work')).toBe(1)
    })

    it('resetFilters and revealRow clear the needs-you filter', () => {
      const store = decided()
      store.decideFilter = 'needs-you'
      store.resetFilters()
      expect(store.decideFilter).toBe('all')

      store.decideFilter = 'needs-you'
      store.revealRow('night')
      expect(store.decideFilter).toBe('all')
    })

    it('needs-you does not hide history rows', () => {
      const store = decided()
      store.historyRows = [historyRow({ id: 'h1', workspace: 'personal' })]
      const before = store.visibleHistory('personal').map(r => r.id)
      store.decideFilter = 'needs-you'

      expect(store.visibleHistory('personal').map(r => r.id)).toEqual(before)
      expect(store.historyFiltersActive).toBe(false)
    })
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
    vi.mocked(api.post).mockResolvedValue({ ok: true, result: { id: 'p1', action: 'edit_region', dismissed: true } } as never)

    const store = useProposalsStore()
    store.rows = [row({ id: 'p1' })]
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
    vi.mocked(api.post).mockResolvedValue({ ok: true, result: { id: 'p1', action: 'edit_region', dismissed: true } } as never)

    const store = useProposalsStore()
    store.rows = [row({ id: 'p1' })]
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

describe('queue load errors', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset()
  })

  it('records a first-load failure in loadError, not the action error slot', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('proposals are unreachable'))
    const store = useProposalsStore()

    await store.fetch()

    expect(store.loadError).toBe('proposals are unreachable')
    expect(store.error).toBe('')
    expect(store.loaded).toBe(false)
    expect(store.loading).toBe(false)
  })

  it('preserves the last good snapshot when a refresh fails', async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ rows: [row({ id: 'a' })] } as never)
    const store = useProposalsStore()
    await store.fetch()
    expect(store.loaded).toBe(true)

    vi.mocked(api.get).mockRejectedValueOnce(new Error('refresh broke'))
    await store.fetch({ force: true })

    expect(store.rows.map(r => r.id)).toEqual(['a'])
    expect(store.loadError).toBe('refresh broke')
    // `loaded` stays true: there IS a snapshot, it is just stale.
    expect(store.loaded).toBe(true)
  })

  it('clears loadError on the next successful load', async () => {
    vi.mocked(api.get).mockRejectedValueOnce(new Error('nope'))
    const store = useProposalsStore()
    await store.fetch()
    expect(store.loadError).toBe('nope')

    vi.mocked(api.get).mockResolvedValueOnce({ rows: [row({ id: 'a' })] } as never)
    await store.fetch({ force: true })

    expect(store.loadError).toBe('')
    expect(store.rows.map(r => r.id)).toEqual(['a'])
  })

  it('does not let a list refresh erase an unread action failure', async () => {
    // The risk named in the ticket: refresh used to clear `error`, hiding an
    // accept failure the operator had not read. The two slots are separate now.
    vi.mocked(api.get).mockResolvedValue({ rows: [row({ id: 'a' })] } as never)
    vi.mocked(api.post).mockRejectedValueOnce(new Error('accept refused'))
    const store = useProposalsStore()
    store.rows = [row({ id: 'a' })]

    await store.act('a', 'accept')
    expect(store.error).toBe('accept refused')

    await store.fetch({ force: true })
    expect(store.error).toBe('accept refused')
    expect(store.loadError).toBe('')
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
    vi.mocked(api.post).mockResolvedValue({ ok: true, result: { id: 'p1', action: 'dismiss', dismissed: true } } as never)

    const store = useProposalsStore()
    await store.fetch()
    store.rows = [row({ id: 'p1' })]
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
    vi.mocked(api.post).mockResolvedValue({ ok: true, result: { id: 'p1', action: 'dismiss', dismissed: true } } as never)

    const store = useProposalsStore()
    await store.fetch()
    store.rows = [row({ id: 'p1' })]
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
    vi.mocked(api.post).mockResolvedValue({ ok: true, result: { id: 'p2', action: 'dismiss', dismissed: true } } as never)

    const store = useProposalsStore()
    await store.fetch()
    store.rows = [row({ id: 'p2' })]
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
describe('batch conflicts stay guarded', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  function preview(id: string, revision: string): ProposalPreview {
    return {
      id, workspace: 'personal', kind: 'memory', text: 't', source: '',
      operation: 'add', destination: 'ciao:memory', destination_path: '/g',
      revision, before: '', after: 't', exact: true, truncated: false,
      can_accept: true, reason: '', separator: '\n§\n',
    }
  }

  it('keeps the conflict flag and refreshes the preview so a retry stays guarded', async () => {
    const store = useProposalsStore()
    store.rows = [row({ id: 'r1' }), row({ id: 'r2' })]
    store.previews = { r1: preview('r1', 'rev-1'), r2: preview('r2', 'rev-2') }
    vi.mocked(api.post).mockResolvedValueOnce({
      ok: true, action: 'accept', summary: [],
      results: [
        { id: 'r1', action: 'edit_region', dismissed: false, conflict: true, no_effect: true },
        { id: 'r2', action: 'edit_region', dismissed: true, promoted: true },
      ],
    })
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url.includes('/preview')) return { preview: preview('r1', 'rev-1-fresh') }
      return { rows: [row({ id: 'r1' })] }
    })

    await store.batch(['r1', 'r2'], 'accept')

    // The first batch sends both revisions.
    expect(vi.mocked(api.post).mock.calls[0][1]).toMatchObject({
      revisions: { r1: 'rev-1', r2: 'rev-2' },
    })
    // The conflicted row keeps its flag and is re-previewed onto fresh
    // state; the accepted row's preview is gone. Before the fix, dropPreview
    // erased both, and the retry below sent no revisions at all.
    expect(store.conflictIds.has('r1')).toBe(true)
    expect(store.previews['r1']?.revision).toBe('rev-1-fresh')
    expect(store.previews['r2']).toBeUndefined()

    vi.mocked(api.post).mockResolvedValueOnce({
      ok: true, action: 'accept', summary: [],
      results: [{ id: 'r1', action: 'edit_region', dismissed: true, promoted: true }],
    })
    await store.batch(['r1'], 'accept')
    expect(vi.mocked(api.post).mock.calls[1][1]).toMatchObject({
      revisions: { r1: 'rev-1-fresh' },
    })
  })
})

describe('row preview prefetch', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api.get).mockReset()
  })

  it('runs at most three at a time, once per id, and keeps failures per row', async () => {
    const store = useProposalsStore()
    let inFlight = 0
    let peak = 0
    const release: Array<() => void> = []
    vi.mocked(api.get).mockImplementation((url: string) => {
      inFlight += 1
      peak = Math.max(peak, inFlight)
      const id = /proposals\/([^/]+)\/preview/.exec(url)![1]
      return new Promise((resolve, reject) => {
        release.push(() => {
          inFlight -= 1
          if (id === 'bad') reject(new Error('unreadable'))
          else resolve({ ok: true, preview: { id, revision: `rev-${id}` } } as never)
        })
      })
    })

    const done = store.prefetchPreviews(['a', 'b', 'bad', 'c', 'd'])
    await Promise.resolve()
    expect(inFlight).toBe(3)
    while (release.length) {
      release.shift()!()
      await new Promise(resolve => setTimeout(resolve, 0))
    }
    await done
    expect(peak).toBe(3)
    expect(Object.keys(store.previews).sort()).toEqual(['a', 'b', 'c', 'd'])
    expect(store.previewErrors.bad).toBe('unreadable')

    // Asked again: nothing is refetched, not even the failure (it has a retry).
    vi.mocked(api.get).mockClear()
    await store.prefetchPreviews(['a', 'bad', 'c'])
    expect(api.get).not.toHaveBeenCalled()
  })
})

describe('sibling previews after an accept', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset()
  })

  function preview(id: string, destination: string, revision: string): ProposalPreview {
    return { id, destination, revision } as ProposalPreview
  }

  it('re-previews other rows that write the same file, keeps the rest', async () => {
    // Accepting `a` changes Learnings.md, so `b`'s prefetched diff and pinned
    // revision describe a file that no longer exists; keeping them would show
    // the wrong lines and get `b`'s next accept refused as a conflict.
    const store = useProposalsStore()
    store.rows = [row({ id: 'a' })]
    store.previews = {
      a: preview('a', 'Workspace/Learnings.md', 'rev-1'),
      b: preview('b', 'Workspace/Learnings.md', 'rev-1'),
      c: preview('c', 'People/Ada.md', 'rev-9'),
    }
    vi.mocked(api.post).mockResolvedValue({ ok: true, result: { id: 'a', action: 'edit_region', dismissed: true } } as never)
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url === '/api/proposals') return Promise.resolve({ rows: [row({ id: 'b' }), row({ id: 'c' })] } as never)
      const id = /proposals\/([^/]+)\/preview/.exec(url)![1]
      return Promise.resolve({ ok: true, preview: preview(id, 'Workspace/Learnings.md', 'rev-2') } as never)
    })

    await store.act('a', 'accept', '', { expectedRevision: 'rev-1' })
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(store.previews.b?.revision).toBe('rev-2')
    expect(store.previews.c?.revision).toBe('rev-9')
    expect(vi.mocked(api.get).mock.calls.map(c => c[0])).not.toContain('/api/proposals/c/preview')
  })
})

describe('decision reasons', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset()
    vi.mocked(api.get).mockResolvedValue({ rows: [row({ id: 'p1' })] } as never)
    vi.mocked(api.post).mockImplementation(async (url) => ({
      ok: true, result: { id: 'p1', action: String(url).endsWith('/dismiss') ? 'dismiss' : 'edit_region', dismissed: true },
    }) as never)
  })

  it('act sends reason only when one is given', async () => {
    const store = useProposalsStore()
    store.rows = [row({ id: 'p1' })]
    await store.act('p1', 'dismiss', '', { reason: 'one-off' })
    expect(api.post).toHaveBeenLastCalledWith('/api/proposals/p1/dismiss', { reason: 'one-off' }, reservationHeaders)

    await store.act('p1', 'dismiss')
    expect(api.post).toHaveBeenLastCalledWith('/api/proposals/p1/dismiss', {}, reservationHeaders)

    await store.act('p1', 'dismiss', '', { reason: '' })
    expect(api.post).toHaveBeenLastCalledWith('/api/proposals/p1/dismiss', {}, reservationHeaders)
  })

  it('act with text, expectedRevision and reason sends all three', async () => {
    const store = useProposalsStore()
    store.rows = [row({ id: 'p1' })]
    await store.act('p1', 'accept', '', { expectedRevision: 'rev-1', text: 'Edited.', reason: 'not true' })
    expect(api.post).toHaveBeenLastCalledWith('/api/proposals/p1/accept', {
      expected_revision: 'rev-1',
      text: 'Edited.',
      reason: 'not true',
    }, reservationHeaders)
  })
})

describe('accepting a skill proposal into a chat', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset()
  })

  function skillRow(overrides: Partial<ProposalRow> = {}): ProposalRow {
    return row({
      id: 'p-skill',
      kind: 'skill',
      text: 'defuddle',
      skill: 'defuddle',
      path: 'personal/Workspace/Skill-Proposals/defuddle.md',
      line: -1,
      lifecycle: 'pending',
      ...overrides,
    })
  }

  it('asks the server, and the server owns which chat', async () => {
    const store = useProposalsStore()
    store.rows = [skillRow()]
    vi.mocked(api.get).mockResolvedValue({ rows: [skillRow()] } as never)
    vi.mocked(api.post).mockResolvedValue({
      ok: true, chat_id: 'chat-x', project_id: 'p-1', created: true,
    } as never)

    const result = await store.acceptSkill('p-skill')

    expect(vi.mocked(api.post)).toHaveBeenCalledWith('/api/proposals/p-skill/implement', undefined, reservationHeaders)
    expect(result).toEqual({ ok: true, chatId: 'chat-x', created: true })
    // It re-read rather than patching the local copy: the row the server
    // returns is the one the panel has to render.
    expect(vi.mocked(api.get).mock.calls.map(c => c[0])).toContain('/api/proposals')
  })

  it('reports which path it took, so a reuse is not announced as a start', async () => {
    // A double tap, a retry after a dropped response and a second device all get
    // the live chat back with `created: false`. The panel pushes a "started"
    // toast, and it must not do that for work that was already running.
    const store = useProposalsStore()
    store.rows = [skillRow()]
    vi.mocked(api.get).mockResolvedValue({ rows: [skillRow()] } as never)
    vi.mocked(api.post).mockResolvedValue({
      ok: true, chat_id: 'chat-x', project_id: 'p-1', created: false,
    } as never)

    const result = await store.acceptSkill('p-skill')

    expect(result).toEqual({ ok: true, chatId: 'chat-x', created: false })
  })

  it('keeps the lifecycle across a reload, because the server row owns it', async () => {
    // The association used to live in this browser's localStorage, which a
    // reload threw away and a second device never had. A fresh store reading
    // the server's rows has to reach the same conclusion.
    vi.mocked(api.get).mockResolvedValue({
      rows: [skillRow({ lifecycle: 'implementing', chat_id: 'chat-x' })],
    } as never)
    vi.mocked(api.post).mockResolvedValue({
      ok: true, chat_id: 'chat-x', project_id: 'p-1', created: true,
    } as never)

    const before = useProposalsStore()
    before.rows = [skillRow()]
    await before.acceptSkill('p-skill')

    setActivePinia(createPinia())
    const after = useProposalsStore()
    await after.fetch({ force: true })

    const reloaded = after.rows.find(r => r.id === 'p-skill')!
    expect(reloaded.lifecycle).toBe('implementing')
    expect(reloaded.chat_id).toBe('chat-x')
  })

  it('surfaces a refusal instead of claiming a chat was started', async () => {
    const store = useProposalsStore()
    store.rows = [skillRow()]
    vi.mocked(api.get).mockResolvedValue({ rows: [skillRow()] } as never)
    vi.mocked(api.post).mockRejectedValue(new Error('no General project to host the chat'))

    const result = await store.acceptSkill('p-skill')

    expect(result.ok).toBe(false)
    expect(store.error).toContain('General project')
  })
})
