import { describe, expect, it } from 'vitest'
import { lineChanges } from '../textDiff'

describe('lineChanges', () => {
  it('names an appended entry and nothing else', () => {
    // The common case: a fact accepted into a bounded region. Returning the
    // unchanged entries as context would bury the one line that is new.
    const before = '- Older fact.\n- Another older fact.'
    const after = '- Older fact.\n- Another older fact.\n- A new fact [2026-09-19]'
    expect(lineChanges(before, after)).toEqual([
      { op: 'added', text: '- A new fact [2026-09-19]' },
    ])
  })

  it('names a replaced line as a removal and an addition', () => {
    // A learning whose recurrence count went up: one line in place, which is
    // why the bullet's own text never describes the write.
    const before = 'head\n- [k] [a → a] (x1) fact\ntail'
    const after = 'head\n- [k] [a → b] (x2) fact\ntail'
    expect(lineChanges(before, after)).toEqual([
      { op: 'removed', text: '- [k] [a → a] (x1) fact' },
      { op: 'added', text: '- [k] [a → b] (x2) fact' },
    ])
  })

  it('returns nothing when the bodies are identical', () => {
    expect(lineChanges('same', 'same')).toEqual([])
  })

  it('handles an empty destination', () => {
    expect(lineChanges('', '- first fact')).toEqual([
      { op: 'added', text: '- first fact' },
    ])
  })

  it('names a removal with no replacement', () => {
    expect(lineChanges('a\nb\nc', 'a\nc')).toEqual([{ op: 'removed', text: 'b' }])
  })
})

describe('lineChanges trailing newline', () => {
  it('does not report a serialized body’s terminating newline as a change', () => {
    // `serialize_entries` ends the region body with a newline. Splitting on it
    // produced a final empty line, which rendered as a blank "+" row under
    // every real addition.
    expect(lineChanges('- Older fact.\n', '- Older fact.\n- A new fact.\n')).toEqual([
      { op: 'added', text: '- A new fact.' },
    ])
  })

  it('treats an empty destination gaining its first entry as one addition', () => {
    expect(lineChanges('', '- A new fact.\n')).toEqual([
      { op: 'added', text: '- A new fact.' },
    ])
  })
})

describe('lineChanges with a region separator', () => {
  it('treats a bounded region entry, not a line, as the unit', () => {
    // Region entries are joined by "\n§\n". Diffed as lines, appending one
    // entry showed a second added row reading "§", and a multi-line entry was
    // torn into unrelated rows.
    const before = 'First fact.\n§\nSecond fact.\n'
    const after = 'First fact.\n§\nSecond fact.\n§\nThird fact.\nwith a second line\n'
    expect(lineChanges(before, after, '\n§\n')).toEqual([
      { op: 'added', text: 'Third fact.\nwith a second line' },
    ])
  })

  it('reports a first entry in an empty region as one addition', () => {
    expect(lineChanges('', 'Only fact.\n', '\n§\n')).toEqual([
      { op: 'added', text: 'Only fact.' },
    ])
  })
})
