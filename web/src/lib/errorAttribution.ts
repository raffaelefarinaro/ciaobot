type ErrorAttributionKind = 'auth' | 'quota' | 'timeout' | 'blocked' | 'remote-http' | 'provider' | 'unknown'

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

// A provider usage or rate limit. Same shapes as `_is_terminal_quota_error`
// and `_is_retryable_quota_error` in ciao/web/chat_service.py. Bare "quota"
// or "rate limit" is not enough: tools report those too ("disk quota
// exceeded", "Google API quota exceeded"), so they only match with a provider
// name or an exhaustion verb.
export function isUsageLimitError(errorText: string): boolean {
  const text = (errorText || '').toLowerCase()
  return /\b(usage limit|session limit|weekly limit|monthly limit|upgrade for higher limits|usage credits|out of (?:extra )?usage)\b/.test(text)
    || /\b(?:reached|hit) your\b[^.]*\blimit\b/.test(text)
    || /\brate limit (?:exceeded|reached|hit|exhausted)\b/.test(text)
    || /\b(?:anthropic|claude|codex|opencode|openai|ollama) quota (?:exceeded|reached|exhausted)\b/.test(text)
    // A 429 only counts with a limit noun, as in `_is_retryable_quota_error`.
    || ((/\b429\b/.test(text) || text.includes('too many requests'))
      && /\b(usage limit|rate limit|quota|session)\b/.test(text))
}

// Weekly and monthly limits get one retry a day for up to a week
// (`quota_terminal` in ciao/web/project_chats.py), not the hourly retry, so
// the card says the limit will not clear soon.
function isTerminalUsageLimit(text: string): boolean {
  return /\b(weekly usage limit|weekly limit|monthly limit|upgrade for higher limits)\b/.test(text)
}

export function classifyError(errorText: string): ErrorAttribution {
  const text = (errorText || '').toLowerCase()
  if (isProviderAuthError(text)) {
    return { kind: 'auth', label: 'Signed out', copy: 'The AI provider\'s login expired. Sign in again to continue.' }
  }
  if (isUsageLimitError(text)) {
    return isTerminalUsageLimit(text)
      ? { kind: 'quota', label: 'Usage limit reached', copy: 'The AI provider\'s usage limit is used up and won\'t reset for a while. Its message above says when, or how to upgrade or add credits.' }
      : { kind: 'quota', label: 'Usage limit reached', copy: 'The AI provider\'s usage limit was reached. Its message above says when it resets.' }
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
  if (/\b(provider|anthropic|claude|codex|opencode|model|rate limit|token)\b/.test(text)) {
    return { kind: 'provider', label: 'Provider error', copy: 'The selected AI provider could not complete the request.' }
  }
  return { kind: 'unknown', label: 'Unknown error', copy: 'The operation did not complete.' }
}

export type ErrorQuickAction = 'retry-hourly' | 'switch-model' | 'sign-in' | 'fix'

// The model is unknown to the provider, or cannot take what the turn sent.
// Picking another model is the fix; a diagnosis chat would only say so.
function isModelMismatchError(text: string): boolean {
  return /\bmodel not found\b|\b(?:unknown|invalid|unsupported) model\b|\bdoes not support image input/.test(text)
}

// The one action an error bubble offers next to Retry. Errors with a known
// cause get that cause's fix; only the rest open a "Fix this error" chat.
export function errorQuickAction(errorText: string): ErrorQuickAction {
  const text = (errorText || '').toLowerCase()
  if (isProviderAuthError(text)) return 'sign-in'
  if (isUsageLimitError(text)) return 'retry-hourly'
  if (isModelMismatchError(text)) return 'switch-model'
  return 'fix'
}
