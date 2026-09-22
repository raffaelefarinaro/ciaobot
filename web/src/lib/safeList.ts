const PROTOTYPE_HAZARD_KEYS = new Set(['__proto__', 'constructor', 'prototype'])

/**
 * Checked element write for comment lists whose index comes from stored data.
 * Skips prototype-hazardous keys and out-of-range indices, and writes through
 * `splice` rather than property assignment, so even a guard bypass could not
 * turn the write into a `__proto__` assignment on the array or its prototype.
 */
export function setListIndex<T>(list: T[], key: number | string, value: T): void {
  if (typeof key === 'string' && PROTOTYPE_HAZARD_KEYS.has(key)) return
  const index = typeof key === 'number' ? key : Number(key)
  if (!Number.isInteger(index) || index < 0 || index >= list.length) return
  list.splice(index, 1, value)
}
