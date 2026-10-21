/** Optional "why" for an operator's task decision (#1730). Pure helpers, no Vue. */

/** The server's cap (`MAX_REASON` in `ciao/task_decisions.py`); over-long is a 400. */
export const REASON_MAX = 1000

export const REASON_CHIPS = ['not mine', 'not a task', 'already done', 'wrong person', 'good as is'] as const

function items(text: string): string[] {
  return text.split(',').map((item) => item.trim()).filter(Boolean)
}

/** The quick reasons present in `text`, as the chip labels. */
export function chipsIn(text: string, chips: readonly string[] = REASON_CHIPS): string[] {
  const present = new Set(items(text).map((item) => item.toLowerCase()))
  return chips.filter((chip) => present.has(chip.toLowerCase()))
}

/** Add `chip` as a comma-joined item, or remove it when already there. */
export function toggleChip(text: string, chip: string): string {
  const wanted = chip.toLowerCase()
  const current = items(text)
  if (current.some((item) => item.toLowerCase() === wanted)) {
    return current.filter((item) => item.toLowerCase() !== wanted).join(', ')
  }
  const next = text.trim() ? `${text.trim()}, ${chip}` : chip
  return next.length > REASON_MAX ? text : next
}

export function cleanReason(text: string): string {
  return text.trim().slice(0, REASON_MAX)
}
