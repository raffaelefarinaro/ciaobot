/**
 * Human-readable text for a caught value.
 *
 * Exists because `strict` makes a caught value `unknown`, so reading a
 * `.message` off it stops type-checking once the old `: any` annotation on the
 * catch binding is gone. The behaviour is deliberately identical to the
 * expression this replaced: a truthy `message` property wins, anything else
 * falls back.
 *
 * @param error the caught value, of any shape
 * @param fallback text when there is no usable `message`; defaults to
 *   `String(error)`, matching the old `|| e` tail
 */
export function errorMessage(error: unknown, fallback?: string): string {
  if (error && typeof error === 'object' && 'message' in error) {
    const message = (error as { message?: unknown }).message
    if (message) return String(message)
  }
  return fallback ?? String(error)
}

/**
 * The parsed JSON body an `ApiError` carried, when it has one.
 *
 * For the handful of callers that branch on a specific server flag
 * (`peer_unreachable`, `blockers`, ...) rather than just showing text. Returns
 * undefined for anything that is not an object-shaped payload, so a caller can
 * optional-chain the key it cares about.
 */
export function errorPayload(error: unknown): Record<string, unknown> | undefined {
  if (error && typeof error === 'object' && 'payload' in error) {
    const payload = (error as { payload?: unknown }).payload
    if (payload && typeof payload === 'object') {
      return payload as Record<string, unknown>
    }
  }
  return undefined
}

/**
 * A `string[]` field from an error payload, or undefined when absent or empty.
 *
 * The preflight routes answer with `blockers` / `warnings` lists that callers
 * join into an alert. Returning undefined for an empty list keeps the existing
 * `if (payload?.blockers)` truthiness checks behaving the same way.
 */
export function errorPayloadList(error: unknown, key: string): string[] | undefined {
  const value = errorPayload(error)?.[key]
  if (!Array.isArray(value) || value.length === 0) return undefined
  return value.map((item) => String(item))
}

/**
 * Like {@link errorMessage}, but prefers the server's own `payload.error`.
 *
 * `ApiError` (lib/api.ts) carries the parsed JSON body, which usually says
 * something more specific than the generic message built from the status line.
 * Matches the old `e?.payload?.error || e.message || fallback` chain. Checked
 * structurally rather than with `instanceof ApiError` so this module stays free
 * of an import cycle with `api.ts`.
 */
export function apiErrorMessage(error: unknown, fallback?: string): string {
  if (error && typeof error === 'object' && 'payload' in error) {
    const payload = (error as { payload?: unknown }).payload
    if (payload && typeof payload === 'object' && 'error' in payload) {
      const detail = (payload as { error?: unknown }).error
      if (detail) return String(detail)
    }
  }
  return errorMessage(error, fallback)
}
