// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { isLoopbackHostname, isLoopbackPage } from './loopback'

describe('isLoopbackHostname', () => {
  it('accepts every loopback spelling, however it is written', () => {
    expect(isLoopbackHostname('localhost')).toBe(true)
    expect(isLoopbackHostname('LocalHost')).toBe(true)
    expect(isLoopbackHostname('127.0.0.1')).toBe(true)
    expect(isLoopbackHostname('::1')).toBe(true)
    expect(isLoopbackHostname('[::1]')).toBe(true)
  })

  it('rejects a host that merely looks loopback-ish', () => {
    expect(isLoopbackHostname('127.0.0.1.example.com')).toBe(false)
    expect(isLoopbackHostname('192.168.1.20')).toBe(false)
    expect(isLoopbackHostname('phone.lan')).toBe(false)
    expect(isLoopbackHostname('')).toBe(false)
  })
})

describe('isLoopbackPage', () => {
  it('reads the hostname of the origin serving the page', () => {
    expect(isLoopbackPage()).toBe(isLoopbackHostname(window.location.hostname))
  })
})
