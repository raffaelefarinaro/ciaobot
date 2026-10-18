import { describe, expect, it } from 'vitest'
import {
  codeModeMarkerRow,
  codeModeMarkerText,
  interleaveCodeModeEvents,
  isChatCodeModeOn,
  isCodeModeMarker,
} from './codeMode'
import type { ChatMessage, CodeModeEvent, ProjectRepo } from './types'

const repo: ProjectRepo = { path: '/r/app', branch: 'main', dirty: false, missing: false }

function user(turn?: number): ChatMessage {
  return { role: 'user', content: `u${turn ?? 'x'}`, timestamp: '', ...(turn === undefined ? {} : { turn_index: turn }) }
}
function asst(n = 0): ChatMessage {
  return { role: 'assistant', content: `a${n}`, timestamp: '' }
}
function ev(after: number, on = true, at = '2026-01-01T09:00:00Z', actor: 'user' | 'agent' = 'agent'): CodeModeEvent {
  return { code_mode: on, actor, at, after_turn_index: after }
}
const texts = (rows: ChatMessage[]) => rows.map((r) => (isCodeModeMarker(r) ? `M:${r.content}` : r.content))

describe('codeMode', () => {
  it('words each switch', () => {
    expect(codeModeMarkerText(true, 'agent')).toBe('Switched to code mode · by Ciao')
    expect(codeModeMarkerText(true, 'user')).toBe('Switched to code mode · by you')
    expect(codeModeMarkerText(false, 'agent')).toBe('Left code mode · by Ciao')
    expect(codeModeMarkerText(false, 'user')).toBe('Left code mode · by you')
  })

  it('is on only with a linked repo', () => {
    expect(isChatCodeModeOn({ code_mode: true }, null)).toBe(false)
    expect(isChatCodeModeOn({ code_mode: true }, repo)).toBe(true)
    expect(isChatCodeModeOn({}, repo)).toBe(false)
  })

  it('returns the same rows without events', () => {
    const rows = [user(0), asst()]
    expect(interleaveCodeModeEvents(rows, undefined, { hasOlder: false })).toBe(rows)
    expect(interleaveCodeModeEvents(rows, [], { hasOlder: false })).toBe(rows)
  })

  it('puts -1 above the first turn', () => {
    const out = interleaveCodeModeEvents([user(0), asst()], [ev(-1)], { hasOlder: false })
    expect(texts(out)).toEqual(['M:Switched to code mode · by Ciao', 'u0', 'a0'])
  })

  it('anchors after a turn, before the next user row', () => {
    const out = interleaveCodeModeEvents([user(0), asst(0), user(1), asst(1)], [ev(0)], { hasOlder: false })
    expect(texts(out)).toEqual(['u0', 'a0', 'M:Switched to code mode · by Ciao', 'u1', 'a1'])
  })

  it('appends a switch made during the last turn', () => {
    const out = interleaveCodeModeEvents([user(0), asst(0), user(1), asst(1)], [ev(1)], { hasOlder: false })
    expect(texts(out).at(-1)).toBe('M:Switched to code mode · by Ciao')
    expect(out).toHaveLength(5)
  })

  it('orders same-anchor events by time', () => {
    const late = ev(0, false, '2026-01-01T10:00:00Z', 'user')
    const early = ev(0, true, '2026-01-01T09:00:00Z', 'agent')
    const out = interleaveCodeModeEvents([user(0), asst(0), user(1)], [late, early], { hasOlder: false })
    expect(texts(out)).toEqual([
      'u0', 'a0', 'M:Switched to code mode · by Ciao', 'M:Left code mode · by you', 'u1',
    ])
  })

  it('ignores rows without turn_index as anchors', () => {
    const out = interleaveCodeModeEvents([user(0), asst(0), user(), asst(1)], [ev(0)], { hasOlder: false })
    expect(texts(out)).toEqual(['u0', 'a0', 'ux', 'a1', 'M:Switched to code mode · by Ciao'])
  })

  it('drops events above an unloaded window', () => {
    const rows = [user(5), asst(5)]
    const events = [ev(2, true, '2026-01-01T08:00:00Z'), ev(4, false, '2026-01-01T09:00:00Z', 'user')]
    expect(texts(interleaveCodeModeEvents(rows, events, { hasOlder: true }))).toEqual([
      'M:Left code mode · by you', 'u5', 'a5',
    ])
    expect(texts(interleaveCodeModeEvents(rows, events, { hasOlder: false }))).toEqual([
      'M:Switched to code mode · by Ciao', 'M:Left code mode · by you', 'u5', 'a5',
    ])
  })

  it('builds a system marker row', () => {
    const e = ev(0, true, '2026-01-01T09:00:00Z', 'user')
    const row = codeModeMarkerRow(e)
    expect(row).toEqual({
      role: 'system',
      tool_name: '_code_mode',
      content: 'Switched to code mode · by you',
      timestamp: '2026-01-01T09:00:00Z',
    })
    expect(isCodeModeMarker(row)).toBe(true)
  })
})
