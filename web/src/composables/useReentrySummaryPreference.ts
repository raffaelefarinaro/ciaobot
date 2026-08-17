import { ref, type Ref } from 'vue'

export const REENTRY_SUMMARY_STORAGE_KEY = 'ciao-reentry-summary-enabled'

type PreferenceStorage = Pick<Storage, 'getItem' | 'setItem'>

function defaultStorage(): PreferenceStorage | null {
  if (typeof localStorage === 'undefined') return null
  return localStorage
}

export function readReentrySummaryEnabled(
  storage: PreferenceStorage | null = defaultStorage(),
): boolean {
  if (!storage) return true
  try {
    // Default to enabled when the key has never been written, so first-run
    // users see the same Apple Intelligence orientation note the rest of
    // the app is tuned around.
    return storage.getItem(REENTRY_SUMMARY_STORAGE_KEY) !== 'false'
  } catch {
    return true
  }
}

export function writeReentrySummaryEnabled(
  enabled: boolean,
  storage: PreferenceStorage | null = defaultStorage(),
): void {
  if (!storage) return
  try {
    storage.setItem(REENTRY_SUMMARY_STORAGE_KEY, String(enabled))
  } catch {
    // A blocked preference store must not prevent reading the chat.
  }
}

export function useReentrySummaryPreference(): {
  reentrySummaryEnabled: Ref<boolean>
  setReentrySummaryEnabled: (enabled: boolean) => void
} {
  const reentrySummaryEnabled = ref(readReentrySummaryEnabled())
  function setReentrySummaryEnabled(enabled: boolean): void {
    reentrySummaryEnabled.value = enabled
    writeReentrySummaryEnabled(enabled)
  }
  return { reentrySummaryEnabled, setReentrySummaryEnabled }
}
