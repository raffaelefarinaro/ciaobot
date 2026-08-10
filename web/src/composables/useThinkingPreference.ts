import { ref, type Ref } from 'vue'

export const THINKING_EXPANDED_STORAGE_KEY = 'ciao-thinking-expanded'

type PreferenceStorage = Pick<Storage, 'getItem' | 'setItem'>

function defaultStorage(): PreferenceStorage | null {
  if (typeof localStorage === 'undefined') return null
  return localStorage
}

export function readThinkingExpanded(storage: PreferenceStorage | null = defaultStorage()): boolean {
  if (!storage) return true
  try {
    return storage.getItem(THINKING_EXPANDED_STORAGE_KEY) !== 'false'
  } catch {
    return true
  }
}

export function writeThinkingExpanded(
  expanded: boolean,
  storage: PreferenceStorage | null = defaultStorage(),
): void {
  if (!storage) return
  try {
    storage.setItem(THINKING_EXPANDED_STORAGE_KEY, String(expanded))
  } catch {
    // A blocked preference store must not prevent reading the trace.
  }
}

export function useThinkingPreference(): {
  thinkingExpanded: Ref<boolean>
  toggleThinking: () => void
} {
  const thinkingExpanded = ref(readThinkingExpanded())
  function toggleThinking(): void {
    thinkingExpanded.value = !thinkingExpanded.value
    writeThinkingExpanded(thinkingExpanded.value)
  }
  return { thinkingExpanded, toggleThinking }
}
