import { describe, expect, it } from 'vitest'

import { isRateLimitTelemetry } from './rateLimit'

describe('rate-limit telemetry', () => {
  it('matches only transient status lines', () => {
    expect(isRateLimitTelemetry('Rate limit: allowed (five_hour)')).toBe(true)
    expect(isRateLimitTelemetry('  Rate limit: rejected (five_hour)')).toBe(true)
    expect(isRateLimitTelemetry('Rate limit exceeded (five_hour)')).toBe(false)
    expect(isRateLimitTelemetry('Error: Rate limit exceeded (five_hour)')).toBe(false)
    expect(isRateLimitTelemetry('A note about Rate limit behavior')).toBe(false)
  })
})
