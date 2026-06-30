import { describe, expect, it } from 'vitest'
import { createTerminalDiffLines } from './terminalDiff'

describe('createTerminalDiffLines', () => {
  it('returns only changed lines when files differ', () => {
    const lines = createTerminalDiffLines('keep\nold\nkeep 2', 'keep\nnew\nkeep 2')

    expect(lines).toEqual([
      { kind: 'del', text: 'old' },
      { kind: 'ins', text: 'new' },
    ])
  })

  it('keeps separate change groups readable without dumping unchanged content', () => {
    const lines = createTerminalDiffLines('a\nb\nc\nd\ne', 'a\nB\nc\nD\ne')

    expect(lines).toEqual([
      { kind: 'del', text: 'b' },
      { kind: 'ins', text: 'B' },
      { kind: 'skip', text: '… 1 unchanged line …' },
      { kind: 'del', text: 'd' },
      { kind: 'ins', text: 'D' },
    ])
  })

  it('shows an explicit empty state when there are no changes', () => {
    expect(createTerminalDiffLines('same\nfile', 'same\nfile')).toEqual([
      { kind: 'empty', text: 'No differences.' },
    ])
  })
})
