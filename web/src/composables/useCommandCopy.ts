import { onBeforeUnmount, ref } from 'vue'
import { writeClipboard } from '../lib/codeCopy'

const REVERT_MS = 2500

export type CopyState = 'copied' | 'failed'

/** Per-row copy feedback for a command shown as text, reverting after a moment. */
export function useCommandCopy() {
  const states = ref<Record<string, CopyState>>({})
  const timers = new Map<string, ReturnType<typeof setTimeout>>()

  async function copy(key: string, text: string): Promise<void> {
    if (!text) return
    const ok = await writeClipboard(text)
    states.value = { ...states.value, [key]: ok ? 'copied' : 'failed' }
    clearTimeout(timers.get(key))
    timers.set(key, setTimeout(() => {
      timers.delete(key)
      const { [key]: _gone, ...rest } = states.value
      states.value = rest
    }, REVERT_MS))
  }

  function stateOf(key: string): CopyState | undefined {
    return states.value[key]
  }

  onBeforeUnmount(() => {
    for (const timer of timers.values()) clearTimeout(timer)
    timers.clear()
  })

  return { copy, stateOf }
}
