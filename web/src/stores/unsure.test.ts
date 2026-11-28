// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { api } from '../lib/api'
import { useUnsureStore } from './unsure'
import type { UnsureList } from '../lib/unsureList'

vi.mock('../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
const apiGet = api.get as unknown as Mock
const apiPost = api.post as unknown as Mock

function listOf(raws: string[]): UnsureList {
  return {
    workspace: 'work', path: 'memory-vault/work/Workspace/Unsure.md', found: true,
    items: raws.map((raw, n) => ({
      id: `${String(n).repeat(16)}-0`, raw, checked: raw.startsWith('- [x]'), kind: '', qualifier: '',
      title: raw, text: raw, reason: '', source: '', date: '', destination: '', proposal: '', malformed: false,
    })),
    counts: { waiting: raws.length, ticked: 0 },
  }
}

describe('unsure store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiPost.mockReset()
  })

  it('puts a removed line back through the route and takes the list it answers', async () => {
    apiGet.mockResolvedValue(listOf(['- [ ] b']))
    const store = useUnsureStore()
    store.show('work')
    await vi.waitFor(() => expect(store.list?.items).toHaveLength(1))
    apiPost.mockResolvedValueOnce(listOf(['- [ ] a', '- [ ] b']))
    expect(await store.restore('- [ ] a', '1111111111111111-0')).toBe('')
    expect(apiPost).toHaveBeenCalledWith('/api/memory/unsure', { workspace: 'work', op: 'restore', line: '- [ ] a', before: '1111111111111111-0' })
    expect(store.list?.items.map(i => i.raw)).toEqual(['- [ ] a', '- [ ] b'])
  })

  it('answers a refused restore with the engine\'s words and keeps the list', async () => {
    apiGet.mockResolvedValue(listOf(['- [ ] b']))
    const store = useUnsureStore()
    store.show('work')
    await vi.waitFor(() => expect(store.list?.items).toHaveLength(1))
    apiPost.mockRejectedValueOnce(new Error('only an unticked item line can be put back'))
    expect(await store.restore('- [x] a')).toBe('only an unticked item line can be put back')
    expect(store.list?.items.map(i => i.raw)).toEqual(['- [ ] b'])
  })
})
