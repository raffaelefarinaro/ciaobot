import { computed, reactive, readonly, ref } from 'vue'

// Presentation only. Durable dismissals still belong to the housekeeping and
// device-setup owners. Keeping this in memory lets a closed window stay closed
// while navigating away from Home, but a reload offers unfinished work again.
const closed = reactive(new Set<string>())
const available = reactive(new Map<string, string[]>())
// Module-level so a strip remount (Home alerts move between the main column and
// the rail) does not lose a reveal that is still waiting for its source rows.
const pendingReveal = ref('')

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
    reopen: (key: string) => closed.delete(key),
    pendingReveal: readonly(pendingReveal),
    requestReveal: (key: string) => { pendingReveal.value = key },
    clearReveal: () => { pendingReveal.value = '' },
    setAvailable: (owner: string, keys: string[]) => { available.set(owner, keys) },
    clearAvailable: (owner: string) => { available.delete(owner) },
  }
}

/** Test isolation for the module-local, per-tab presentation state. */
export function resetHomeNoticeWindows() {
  closed.clear()
  available.clear()
  pendingReveal.value = ''
}
