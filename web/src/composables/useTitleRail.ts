import { computed, ref, watch, type ComputedRef } from 'vue'
import { railNeighbours, stableRailOrder, type RailItem } from '../lib/titleRail'

/**
 * The model behind a title rail (`TitleRail.vue`): the items in sidebar order, the
 * current item's neighbours, and the step that switches to one. Chats and
 * automations each have one rail, told apart by `key`.
 *
 * The order is frozen when the rail arrives at an item, so stepping through a list
 * that reorders itself (a chat jumping to the top on new activity) still visits
 * every item once. A switch the rail makes hands its travel order to the item it
 * lands on; any other arrival starts from the live order.
 */
interface RailState {
  snapshot: { scope: string; id: string; ids: string[] } | null
  // Which way the last switch went (1 = to the next item), read once by whatever
  // mounts or updates for it, so its body can slide in from that side.
  arrival: -1 | 1 | 0
  // True while a switch is in flight; a second swipe during it is ignored.
  switching: boolean
}

// Module-level so it survives remounts (ChatLayout keys ChatPanel by chat id).
const states = new Map<string, RailState>()
function stateFor(key: string): RailState {
  let state = states.get(key)
  if (!state) {
    state = { snapshot: null, arrival: 0, switching: false }
    states.set(key, state)
  }
  return state
}

/** For a switch the rail did not start (archive-to-next): the side the next item comes from. */
export function markRailArrival(key: string, direction: -1 | 1 | 0): void {
  stateFor(key).arrival = direction
}

export function takeRailArrival(key: string): -1 | 1 | 0 {
  const state = stateFor(key)
  const a = state.arrival
  state.arrival = 0
  return a
}

export function resetRailState(key: string): void {
  states.delete(key)
}

export interface TitleRail {
  items: ComputedRef<RailItem[]>
  neighbours: ComputedRef<{ index: number; previous: string | null; next: string | null }>
  select: (id: string) => Promise<void>
  step: (direction: -1 | 1) => boolean
}

export function useTitleRail(key: string, options: {
  /** The list the order belongs to (the workspace); a snapshot from another scope is not reused. */
  scope: () => string
  currentId: () => string
  /** The live list, in sidebar order. */
  entries: ComputedRef<RailItem[]>
  /** Open another item. Rejects when the switch fails. */
  navigate: (id: string) => Promise<unknown>
}): TitleRail {
  const state = stateFor(key)

  // The travel order is consumed on arrival, so only the item a rail switch lands
  // on reuses it.
  function arrive(id: string): string[] {
    const saved = state.snapshot
    state.snapshot = null
    const reuse = saved !== null && saved.scope === options.scope() && saved.id === id
    return reuse && saved ? saved.ids : options.entries.value.map(e => e.id)
  }
  const frozen = ref<string[]>(arrive(options.currentId()))
  // A host that stays mounted across items (the automation panel) arrives again
  // each time its current item changes.
  watch(options.currentId, id => { frozen.value = arrive(id) }, { flush: 'sync' })

  const items = computed<RailItem[]>(() => {
    const byId = new Map(options.entries.value.map(e => [e.id, e]))
    const order = stableRailOrder(options.entries.value.map(e => e.id), frozen.value)
    if (!byId.has(options.currentId()) || order.length < 2) return []
    return order.map(id => byId.get(id)!)
  })

  const neighbours = computed(() => railNeighbours(items.value.map(i => i.id), options.currentId()))

  async function select(id: string): Promise<void> {
    const current = options.currentId()
    if (state.switching || id === current) return
    state.switching = true
    try {
      const ids = items.value.map(i => i.id)
      state.snapshot = { scope: options.scope(), id, ids }
      state.arrival = ids.indexOf(id) > ids.indexOf(current) ? 1 : -1
      await options.navigate(id)
    } catch (err) {
      state.snapshot = null
      state.arrival = 0
      throw err
    } finally {
      state.switching = false
    }
  }

  function step(direction: -1 | 1): boolean {
    const target = direction < 0 ? neighbours.value.previous : neighbours.value.next
    if (!target) return false
    // select clears its snapshot on failure, and the host surfaces switch errors itself.
    select(target).catch(() => {})
    return true
  }

  return { items, neighbours, select, step }
}
