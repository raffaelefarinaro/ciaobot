import { describe, expect, it } from 'vitest'

import { parseFrontmatter } from './markdownFrontmatter'

describe('parseFrontmatter', () => {
  it('leaves a document without an opening fence untouched', () => {
    const source = '# Title\n\nSome prose.\n'
    expect(parseFrontmatter(source)).toEqual({ frontmatter: null, body: source })
  })

  it('splits scalar and list frontmatter from the body', () => {
    const result = parseFrontmatter(
      '---\ntitle: My Note\ntags:\n  - work\n  - ideas\n---\n\nBody text.\n',
    )
    expect(result.frontmatter).toEqual({ title: 'My Note', tags: ['work', 'ideas'] })
    // The blank line right after the closing fence is part of the fence match.
    expect(result.body).toBe('Body text.\n')
  })

  it('accepts a BOM and blank lines before the opening fence', () => {
    const result = parseFrontmatter('\uFEFF\n\n---\nkey: value\n---\nrest')
    expect(result.frontmatter).toEqual({ key: 'value' })
    expect(result.body).toBe('rest')
  })

  it('handles CRLF line endings on both fences', () => {
    const result = parseFrontmatter('---\r\nkey: value\r\n---\r\nbody line\r\n')
    expect(result.frontmatter).toEqual({ key: 'value' })
    expect(result.body).toBe('body line\r\n')
  })

  it('skips whitespace-only lines before the opening fence without eating the fence itself', () => {
    const result = parseFrontmatter('  \n\t\n---\nkey: value\n---\nrest')
    expect(result.frontmatter).toEqual({ key: 'value' })
    expect(result.body).toBe('rest')
  })

  it('returns null when the closing fence is missing', () => {
    const source = '---\ntitle: unfinished\n'
    expect(parseFrontmatter(source)).toEqual({ frontmatter: null, body: source })
  })

  it('stays linear on long newline-only inputs (ReDoS regression)', () => {
    // The old `(?:\s*\r?\n)*` pattern backtracked exponentially once the
    // leading newline run outgrew ~35 characters; this input hangs forever
    // there and must parse in bounded time now.
    const started = performance.now()
    expect(parseFrontmatter('\n'.repeat(100_000)).frontmatter).toBeNull()
    expect(performance.now() - started).toBeLessThan(2000)
  })

  it('still parses valid frontmatter after a long newline run', () => {
    const source = `${'\n'.repeat(2_000)}---\nkey: value\n---\nbody`
    expect(parseFrontmatter(source).frontmatter).toEqual({ key: 'value' })
    expect(parseFrontmatter(source).body).toBe('body')
  })
})
