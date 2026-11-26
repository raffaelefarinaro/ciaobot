import { describe, expect, it } from 'vitest'
import { parseUnsureLines } from './unsureList'

describe('parseUnsureLines', () => {
  it('reads open and ticked items and skips headings and prose', () => {
    const text = [
      '# Unsure',
      '',
      'Tick a line to say yes.',
      '- [ ] memory: prefers tea — unsure because one mention; 2026-11-01',
      '- [x] people: Ada is a client — source: chat-1; 2026-11-02',
      '- a plain bullet',
      '  - [X] drop memory: old fact — 2026-11-03',
    ].join('\r\n')
    expect(parseUnsureLines(text)).toEqual([
      { text: 'memory: prefers tea — unsure because one mention; 2026-11-01', ticked: false },
      { text: 'people: Ada is a client — source: chat-1; 2026-11-02', ticked: true },
      { text: 'drop memory: old fact — 2026-11-03', ticked: true },
    ])
  })

  it('skips fenced code and reads star bullets', () => {
    const text = ['* [ ] memory: star bullet', '```', '- [ ] inside a fence', '```', '- [x] after the fence'].join('\n')
    expect(parseUnsureLines(text)).toEqual([
      { text: 'memory: star bullet', ticked: false },
      { text: 'after the fence', ticked: true },
    ])
  })

  it('returns nothing for an empty list', () => {
    expect(parseUnsureLines('')).toEqual([])
  })
})
