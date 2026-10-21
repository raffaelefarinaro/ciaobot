import { describe, expect, it } from 'vitest'
import { isHostOrigin } from './hostOrigin'

describe('isHostOrigin', () => {
  it.each(['localhost', 'LOCALHOST', '127.0.0.1', '[::1]', '::1'])('is true for %s', (name) => {
    expect(isHostOrigin(name)).toBe(true)
  })
  it.each(['mini.tail1.ts.net', '192.168.1.5', 'example.com', 'localhost.evil.com'])('is false for %s', (name) => {
    expect(isHostOrigin(name)).toBe(false)
  })
})
