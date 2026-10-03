import { computed, reactive } from 'vue'

// Presentation only. Durable dismissals still belong to the housekeeping and
// device-setup owners. Keeping this in memory lets a closed window stay closed
// while navigating away from Home, but a reload offers unfinished work again.
const closed = reactive(new Set<string>())
const available = reactive(new Map<string, string[]>())

const closedCount = computed(() => {
  const keys = new Set([...available.values()].flat())
  return [...keys].filter(key => closed.has(key)).length
})

export function useHomeNoticeWindows() {
  return {
    closedCount,
    isClosed: (key: string) => closed.has(key),
    close: (key: string) => { closed.add(key) },
    reopenAll: () => { closed.clear() },
    setAvailable: (owner: string, keys: string[]) => { available.set(owner, keys) },
    clearAvailable: (owner: string) => { available.delete(owner) },
  }
}

/** Test isolation for the module-local, per-tab presentation state. */
export function resetHomeNoticeWindows() {
  closed.clear()
  available.clear()
}
