// Mirror of `is_rate_limit_telemetry` in ciao/rate_limits.py — keep the two in
// sync. Transient rate-limit status lines (allowed / warning / rejected) are
// usage telemetry for Settings, not conversation, so they're dropped from the
// chat stream; a genuine hard "Rate limit exceeded" still surfaces as an error.
export function isRateLimitTelemetry(message: string): boolean {
  return message.includes('Rate limit') && !message.startsWith('Rate limit exceeded')
}
