type ErrorAttributionKind = 'auth' | 'timeout' | 'blocked' | 'remote-http' | 'provider' | 'unknown'

export interface ErrorAttribution {
  kind: ErrorAttributionKind
  label: string
  copy: string
}

// The provider CLI is signed out or its login lapsed. Same shapes as
// `_is_retryable_auth_error` in ciao/web/chat_service.py, which retries them.
export function isProviderAuthError(errorText: string): boolean {
  const text = (errorText || '').toLowerCase()
  if (text.includes('oauth session expired')) return true
  if (text.includes('could not be refreshed') && (text.includes('failed to authenticate') || text.includes('session expired'))) return true
  return text.includes('not logged in') && text.includes('/login')
}

export function classifyError(errorText: string): ErrorAttribution {
  const text = (errorText || '').toLowerCase()
  if (isProviderAuthError(text)) {
    return { kind: 'auth', label: 'Signed out', copy: 'The AI provider\'s login expired. Sign in again to continue.' }
  }
  if (/\b(timeout|timed out|deadline exceeded)\b/.test(text)) {
    return { kind: 'timeout', label: 'Timed out', copy: 'The operation took too long to finish.' }
  }
  if (/\b(blocked|permission denied|not allowed|forbidden|approval required|access denied)\b/.test(text)) {
    return { kind: 'blocked', label: 'Blocked', copy: 'The operation was stopped by a permission or access rule.' }
  }
  if (/\b(?:http|status|status code)\s*[45]\d{2}\b|\b[45]\d{2}\s+(?:error|bad gateway|gateway timeout)\b/.test(text)) {
    return { kind: 'remote-http', label: 'Remote service error', copy: 'A remote service returned an HTTP error.' }
  }
  if (/\b(provider|anthropic|claude|codex|opencode|model|quota|rate limit|token)\b/.test(text)) {
    return { kind: 'provider', label: 'Provider error', copy: 'The selected AI provider could not complete the request.' }
  }
  return { kind: 'unknown', label: 'Unknown error', copy: 'The operation did not complete.' }
}
