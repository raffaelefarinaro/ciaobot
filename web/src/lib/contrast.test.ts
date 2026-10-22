import { describe, expect, it } from 'vitest'
import {
  checkCustomPair,
  contrastRatio,
  deriveCustomTokens,
  mix,
  parseHex,
  toHex,
} from './contrast'

describe('parseHex', () => {
  it('accepts #fff and #FFFFFF', () => {
    expect(parseHex('#fff')).toEqual({ r: 255, g: 255, b: 255 })
    expect(parseHex('#FFFFFF')).toEqual({ r: 255, g: 255, b: 255 })
  })
  it('rejects malformed values', () => {
    expect(parseHex('fff')).toBeNull()
    expect(parseHex('#12345')).toBeNull()
    expect(parseHex('#gggggg')).toBeNull()
  })
})

describe('toHex and mix', () => {
  it('clamps and rounds channels', () => {
    expect(toHex({ r: 300, g: -4, b: 127.6 })).toBe('#ff0080')
  })
  it('mixes channel-wise', () => {
    expect(mix('#000000', '#ffffff', 0.5)).toBe('#808080')
    expect(mix('#102030', '#102030', 0.7)).toBe('#102030')
  })
})

describe('contrastRatio', () => {
  it('is 21 for black on white and symmetric', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 2)
    expect(contrastRatio('#ffffff', '#000000')).toBeCloseTo(21, 2)
  })
  it('puts #777777 on white just under AA', () => {
    const ratio = contrastRatio('#777777', '#ffffff')
    expect(ratio).toBeCloseTo(4.48, 1)
    expect(ratio).toBeLessThan(4.5)
  })
  it('matches the default theme pairs', () => {
    expect(contrastRatio('#e8e8f0', '#1a1a2e')).toBeCloseTo(14.0, 1)
    expect(contrastRatio('#1a1a2e', '#f4f4fa')).toBeCloseTo(15.57, 1)
    expect(contrastRatio('#d2d2de', '#1a1a2e')).toBeGreaterThanOrEqual(11.3)
  })
  it('throws on an unparsable colour', () => {
    expect(() => contrastRatio('zzz', '#fff')).toThrow()
  })
})

describe('checkCustomPair', () => {
  it('accepts the default dark pair', () => {
    expect(checkCustomPair({ bg: '#1a1a2e', fg: '#e8e8f0' }).ok).toBe(true)
  })
  it('refuses dim text on dark and says lighter', () => {
    const result = checkCustomPair({ bg: '#1a1a2e', fg: '#5a5a70' })
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.reason).toContain('4.5:1')
      expect(result.reason).toContain('lighter')
    }
  })
  it('refuses pale text on white and says darker', () => {
    const result = checkCustomPair({ bg: '#ffffff', fg: '#cccccc' })
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toContain('darker')
  })
  it('refuses unparsable colours without a ratio', () => {
    const result = checkCustomPair({ bg: 'zzz', fg: '#fff' })
    expect(result).toEqual({ ok: false, reason: 'Enter colours like #1a1a2e.', ratio: null })
  })
  it('refuses a pair that passes the background but fails the hover surface', () => {
    let found: { bg: string; fg: string } | null = null
    for (let v = 0x60; v <= 0xa0 && !found; v++) {
      const bg = toHex({ r: v, g: v, b: v })
      const fg = '#000000'
      const ratio = contrastRatio(fg, bg)
      if (ratio >= 4.5 && contrastRatio(fg, deriveCustomTokens({ bg, fg })['--bg3']) < 4.5) {
        found = { bg, fg }
      }
    }
    expect(found).not.toBeNull()
    const result = checkCustomPair(found!)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toContain('hovered')
  })
})

describe('deriveCustomTokens', () => {
  const pairs = [
    { bg: '#1a1a2e', fg: '#e8e8f0' },
    { bg: '#f4f4fa', fg: '#1a1a2e' },
    { bg: '#000000', fg: '#ffffff' },
    { bg: '#ffffff', fg: '#000000' },
    { bg: '#101018', fg: '#d0d0d8' },
    { bg: '#fdf6e3', fg: '#2b2b2b' },
    { bg: '#002b36', fg: '#eee8d5' },
    { bg: '#222222', fg: '#f0f0f0' },
    { bg: '#e8f0e8', fg: '#102010' },
    { bg: '#2d2a2e', fg: '#fcfcfa' },
  ]
  it.each(pairs)('keeps secondary text readable for %o', (pair) => {
    expect(checkCustomPair(pair).ok).toBe(true)
    const t = deriveCustomTokens(pair)
    expect(contrastRatio(t['--fg2'], t['--bg'])).toBeGreaterThanOrEqual(4.5)
    expect(contrastRatio(t['--fg3'], t['--bg'])).toBeGreaterThanOrEqual(4.5)
    const c1 = contrastRatio(t['--fg'], t['--bg'])
    const c2 = contrastRatio(t['--fg2'], t['--bg'])
    const c3 = contrastRatio(t['--fg3'], t['--bg'])
    expect(c1).toBeGreaterThanOrEqual(c2)
    expect(c2).toBeGreaterThanOrEqual(c3)
    for (const value of Object.values(t)) expect(value).toMatch(/^#[0-9a-f]{6}$/)
  })
})
