import { computed, ref } from 'vue'
import { describe, expect, it } from 'vitest'
import { takeRailArrival, useTitleRail } from './useTitleRail'

const entries = computed(() => [
  { id: 'a', title: 'First', subtitle: '', mark: null, titlePending: false, label: 'First' },
  { id: 'b', title: 'Second', subtitle: '', mark: null, titlePending: false, label: 'Second' },
])

let testKey = 0
function createRail(
  navigate: (id: string) => Promise<unknown>,
  currentId = ref('a'),
  key = `use-title-rail-test-${testKey++}`,
) {
  return useTitleRail(key, {
    scope: () => 'workspace',
    currentId: () => currentId.value,
    entries,
    navigate,
  })
}

describe('useTitleRail navigation state', () => {
  it('shares the in-flight lock across a remounted rail and releases it when navigation finishes', async () => {
    const currentId = ref('a')
    let finishNavigation!: () => void
    const navigation = new Promise<void>(resolve => { finishNavigation = resolve })
    const navigate = async (id: string) => {
      await navigation
      currentId.value = id
    }
    const key = `use-title-rail-test-${testKey++}`
    const first = createRail(navigate, currentId, key)
    const pending = first.select('b')
    currentId.value = 'b'
    const remounted = createRail(navigate, currentId, key)

    expect(first.switching).toBe(remounted.switching)
    expect(remounted.switching.value).toBe(true)
    await remounted.select('a')
    expect(currentId.value).toBe('b')

    finishNavigation()
    await pending
    expect(first.switching.value).toBe(false)
  })

  it('releases the lock and rolls back when navigation rejects', async () => {
    const rail = createRail(async () => { throw new Error('navigation failed') })

    await expect(rail.select('b')).rejects.toThrow('navigation failed')
    expect(rail.switching.value).toBe(false)
  })

  it('releases the lock when navigation resolves without changing the current item', async () => {
    const key = `use-title-rail-test-${testKey++}`
    const rail = createRail(async () => false, undefined, key)

    await rail.select('b')
    expect(rail.switching.value).toBe(false)
    expect(takeRailArrival(key)).toBe(0)
  })
})
