import { describe, expect, it } from 'vitest'
import { quoteArg } from './shellQuote'

describe('quoteArg', () => {
  it('quotes only when needed', () => {
    expect(quoteArg('notion', 'posix')).toBe('notion')
    expect(quoteArg("it's", 'posix')).toBe("'it'\\''s'")
    expect(quoteArg('a b; rm -rf /', 'posix')).toBe("'a b; rm -rf /'")
    expect(quoteArg('', 'posix')).toBe("''")
    expect(quoteArg('https://a.com/a,b', 'posix')).toBe('https://a.com/a,b')
  })
})
