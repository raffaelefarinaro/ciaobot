import { describe, expect, it } from 'vitest'
import { diffWithContext, headingAbove, lineChanges } from './textDiff'

describe('lineChanges', () => {
  it('names only the contiguous edit', () => {
    expect(lineChanges('a\nb\n', 'a\nb\nc\n')).toEqual([{ op: 'added', text: 'c' }])
    expect(lineChanges('a\nx\nc', 'a\ny\nc')).toEqual([
      { op: 'removed', text: 'x' },
      { op: 'added', text: 'y' },
    ])
  })
})

describe('diffWithContext', () => {
  it('keeps one line either side of the edit, with positions', () => {
    const before = '## Decisions\n- one\n- two\n## Later\n'
    const after = '## Decisions\n- one\n- two\n- three\n## Later\n'
    expect(diffWithContext(before, after)).toEqual([
      { op: 'context', text: '- two', line: 3 },
      { op: 'added', text: '- three', line: 4 },
      { op: 'context', text: '## Later', line: 5 },
    ])
  })

  it('numbers a removed line in the old body and its replacement in the new', () => {
    expect(diffWithContext('a\nx\nc', 'a\ny\nc', '\n', 0)).toEqual([
      { op: 'removed', text: 'x', line: 2 },
      { op: 'added', text: 'y', line: 2 },
    ])
  })

  it('treats a whole new body as added lines', () => {
    expect(diffWithContext('', '---\n# Mo\n', '\n', 1).map(l => l.op)).toEqual(['added', 'added'])
  })

  it('splits a bounded region on its entry separator', () => {
    const sep = '\n§\n'
    expect(diffWithContext('one', `one${sep}two`, sep, 1)).toEqual([
      { op: 'context', text: 'one', line: 1 },
      { op: 'added', text: 'two', line: 2 },
    ])
  })

  it('returns nothing when nothing changes', () => {
    expect(diffWithContext('same', 'same')).toEqual([])
  })
})

describe('headingAbove', () => {
  it('finds the nearest heading at or above a line', () => {
    const body = '# Title\n\n## Decisions\n- a\n- b'
    expect(headingAbove(body, 5)).toBe('## Decisions')
    expect(headingAbove(body, 1)).toBe('# Title')
    expect(headingAbove('- a\n- b', 2)).toBe('')
  })

  it('does not take a comment inside fenced code for a heading', () => {
    const body = '## Setup\n```sh\n# install first\nnpm i\n```\n- added'
    expect(headingAbove(body, 6)).toBe('## Setup')
  })
})
