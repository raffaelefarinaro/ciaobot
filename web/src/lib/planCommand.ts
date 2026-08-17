export type PlanReturnMode = 'auto' | 'normal' | 'bypass'

const PLAN_RETURN_MODE_STORAGE_PREFIX = 'ciao-plan-return-mode:'

function isPlanReturnMode(mode: string | null): mode is PlanReturnMode {
  return mode === 'auto' || mode === 'normal' || mode === 'bypass'
}

function planReturnModeStorageKey(chatId: string): string {
  return `${PLAN_RETURN_MODE_STORAGE_PREFIX}${chatId}`
}

/** Store the mode to restore after this chat leaves plan mode. */
export function rememberPlanReturnMode(chatId: string, mode: string): void {
  if (!isPlanReturnMode(mode)) return
  try {
    localStorage.setItem(planReturnModeStorageKey(chatId), mode)
  } catch {
    // Storage may be disabled or unavailable in an embedded browser.
  }
}

/** Return the remembered pre-plan mode, defaulting safely to auto. */
export function restorePlanReturnMode(chatId: string): PlanReturnMode {
  try {
    const mode = localStorage.getItem(planReturnModeStorageKey(chatId))
    return isPlanReturnMode(mode) ? mode : 'auto'
  } catch {
    return 'auto'
  }
}

/** Remove a saved return mode once it is no longer needed. */
export function clearPlanReturnMode(chatId: string): void {
  try {
    localStorage.removeItem(planReturnModeStorageKey(chatId))
  } catch {
    // Storage may be disabled or unavailable in an embedded browser.
  }
}
