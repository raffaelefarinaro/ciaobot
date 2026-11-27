import { describe, expect, it } from 'vitest'
import { highlightSegments, plainText } from './searchText'

describe('plainText', () => {
  it('keeps link text and drops the url', () => {
    expect(plainText('see [PR #254](https://example.com/pull/254) now')).toBe('see PR #254 now')
    expect(plainText('![chart](a.png) text')).toBe('chart text')
  })
  it('removes emphasis, code, heading, quote and list markers', () => {
    expect(plainText("**haven't** __done__ `x` ~~y~~")).toBe("haven't done x y")
    expect(plainText('# Title\n> quoted\n- one\n2. two\n* three')).toBe('Title quoted one two three')
  })
  it('collapses whitespace and leaves markup characters inside words alone', () => {
    expect(plainText('a\n\n  b\t c')).toBe('a b c')
    expect(plainText('snake_case and 2*3')).toBe('snake_case and 2*3')
    expect(plainText('<b>x</b>')).toBe('<b>x</b>')
  })
})

describe('plainText cap', () => {
  it('caps long input and stays fast on pathological brackets', () => {
    const start = Date.now()
    const out = plainText('['.repeat(100000))
    expect(out.length).toBeLessThanOrEqual(2000)
    expect(Date.now() - start).toBeLessThan(500)
  })
})

describe('highlightSegments', () => {
  it('flags each term case-insensitively and keeps the original casing', () => {
    expect(highlightSegments('Fix the Login bug', 'login fix')).toEqual([
      { text: 'Fix', match: true }, { text: ' the ', match: false },
      { text: 'Login', match: true }, { text: ' bug', match: false },
    ])
  })
  it('treats regex characters literally', () => {
    expect(highlightSegments('a.b (c) a+b', 'a.b (c')).toEqual([
      { text: 'a.b', match: true }, { text: ' ', match: false },
      { text: '(c', match: true }, { text: ') a+b', match: false },
    ])
  })
  it('ignores terms shorter than two characters', () => {
    expect(highlightSegments('a bc a', 'a bc')).toEqual([
      { text: 'a ', match: false }, { text: 'bc', match: true }, { text: ' a', match: false },
    ])
  })
  it('returns one plain run for an empty query or empty text', () => {
    expect(highlightSegments('abc', '  ')).toEqual([{ text: 'abc', match: false }])
    expect(highlightSegments('', 'a')).toEqual([{ text: '', match: false }])
  })
})
