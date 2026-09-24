// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { contentHref, deviceHref } from './originNavigation'

describe('loopback origin navigation', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/')
  })

  afterEach(() => {
    window.history.replaceState({}, '', '/')
  })

  it('keeps content navigation on localhost', () => {
    expect(contentHref('/settings')).toBe('http://localhost:3000/settings')
  })

  it('keeps device navigation on 127.0.0.1', () => {
    expect(deviceHref('/device')).toBe('http://127.0.0.1:3000/device')
  })

  it('rewrites the host while preserving a bounded path', () => {
    window.history.replaceState({}, '', '/device')
    expect(contentHref('/')).toBe('http://localhost:3000/')
    expect(deviceHref('/device')).toBe('http://127.0.0.1:3000/device')
  })

  it('accepts an absolute bridge URL only on the matching loopback peer', () => {
    expect(contentHref('http://127.0.0.1:3000/api/auth/bridge?token=opaque')).toBe(
      'http://localhost:3000/api/auth/bridge?token=opaque',
    )
    expect(() => contentHref('https://attacker.example/steal')).toThrow()
    expect(() => contentHref('//attacker.example/steal')).toThrow()
    expect(() => contentHref('http://127.0.0.1:3001/device')).toThrow()
  })
})
