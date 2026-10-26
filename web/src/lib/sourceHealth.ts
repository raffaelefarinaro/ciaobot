import { apiErrorMessage } from './errorMessage'

/**
 * Whether a best-effort source could be read, as opposed to what it said.
 *
 * `ready` means the last read worked (an empty result included), `stale` that a
 * later read failed and the rows are from the last success, `unavailable` that
 * no read has ever worked in this scope, and `checking` that none has finished
 * yet. Transport only: whether a successful answer is itself healthy is the
 * consumer's call.
 */
export type SourceHealthStatus = 'checking' | 'ready' | 'stale' | 'unavailable'

export interface SourceHealth {
  status: SourceHealthStatus
  /** ISO time of the last successful read in this scope; empty before one. */
  lastSuccessAt: string
  /** Why the last read failed; empty unless it did. */
  error: string
}

export function checkingHealth(): SourceHealth {
  return { status: 'checking', lastSuccessAt: '', error: '' }
}

export function readyHealth(now: Date = new Date()): SourceHealth {
  return { status: 'ready', lastSuccessAt: now.toISOString(), error: '' }
}

/** A failed read: `stale` when this scope has succeeded before, else `unavailable`. */
export function failedHealth(prev: SourceHealth, error: unknown): SourceHealth {
  return {
    status: prev.lastSuccessAt ? 'stale' : 'unavailable',
    lastSuccessAt: prev.lastSuccessAt,
    error: apiErrorMessage(error, 'The check could not be completed'),
  }
}
