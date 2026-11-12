// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { computed, nextTick, ref } from 'vue'
import type { RailItem } from '../../lib/titleRail'
import { markRailArrival, resetRailState, takeRailArrival, useTitleRail } from '../useTitleRail'

const KEY = 'test-rail'

function entry(id: string): RailItem {
  return { id, title: id, subtitle: '', mark: null, titlePending: false, label: id }
}

function setup(ids: string[], current: string, navigate = vi.fn(async (_id: string) => {})) {
  const list = ref(ids)
  const cur = ref(current)
  const scope = ref('ws')
  const entries = computed(() => list.value.map(entry))
  const rail = useTitleRail(KEY, { scope: () => scope.value, currentId: () => cur.value, entries, navigate })
  return { list, cur, scope, rail, navigate, order: () => rail.items.value.map(i => i.id) }
}

describe('useTitleRail', () => {
  afterEach(() => resetRailState(KEY))

  it('is empty for a lone item or an item not in the list', () => {
    expect(setup(['a'], 'a').rail.items.value).toEqual([])
    expect(setup(['a', 'b'], 'z').rail.items.value).toEqual([])
  })

  it('walks the list in order with no wrap-around', () => {
    const { rail } = setup(['a', 'b', 'c'], 'a')
    expect(rail.neighbours.value).toEqual({ index: 0, previous: null, next: 'b' })
    expect(rail.step(-1)).toBe(false)
  })

  it('navigates on a step and records the side the next item arrives from', async () => {
    const { rail, navigate } = setup(['a', 'b', 'c'], 'b')
    expect(rail.step(1)).toBe(true)
    await nextTick()
    expect(navigate).toHaveBeenCalledWith('c')
    expect(takeRailArrival(KEY)).toBe(1)
    expect(takeRailArrival(KEY)).toBe(0)
  })

  it('keeps its order frozen while the live list reorders', () => {
    const { list, order } = setup(['a', 'b', 'c'], 'a')
    list.value = ['c', 'a', 'b']
    expect(order()).toEqual(['a', 'b', 'c'])
    list.value = ['d', 'c', 'a', 'b']
    expect(order()).toEqual(['a', 'b', 'c', 'd'])
  })

  it('a host that stays mounted reuses the travel order when the rail moves it, and starts fresh otherwise', async () => {
    const { list, cur, rail, order } = setup(['a', 'b', 'c'], 'a')
    list.value = ['c', 'b', 'a']
    await rail.select('b')
    cur.value = 'b'
    expect(order()).toEqual(['a', 'b', 'c'])
    // An arrival the rail did not make (a sidebar click) takes the live order.
    cur.value = 'c'
    expect(order()).toEqual(['c', 'b', 'a'])
  })

  it('does not reuse a travel order from another scope', async () => {
    const { list, cur, scope, rail, order } = setup(['a', 'b', 'c'], 'a')
    list.value = ['c', 'b', 'a']
    await rail.select('b')
    scope.value = 'other'
    cur.value = 'b'
    expect(order()).toEqual(['c', 'b', 'a'])
  })

  it('clears the travel order and arrival when the switch fails', async () => {
    const navigate = vi.fn(async () => { throw new Error('nope') })
    const { rail } = setup(['a', 'b'], 'a', navigate)
    await expect(rail.select('b')).rejects.toThrow('nope')
    expect(takeRailArrival(KEY)).toBe(0)
  })

  it('ignores a second switch while one is in flight', async () => {
    let release: () => void = () => {}
    const navigate = vi.fn(() => new Promise<void>(r => { release = r }))
    const { rail } = setup(['a', 'b', 'c'], 'b', navigate)
    const first = rail.select('c')
    await rail.select('a')
    expect(navigate).toHaveBeenCalledTimes(1)
    release()
    await first
  })

  it('keeps each rail\'s arrival separate', () => {
    markRailArrival(KEY, -1)
    markRailArrival('other-rail', 1)
    expect(takeRailArrival(KEY)).toBe(-1)
    expect(takeRailArrival('other-rail')).toBe(1)
    resetRailState('other-rail')
  })
})
