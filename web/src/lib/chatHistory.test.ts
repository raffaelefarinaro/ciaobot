import { describe, expect, test } from 'vitest'
import {
  dropSupersededLiveTail,
  groupIntoTurns,
  historySignature,
  isLiveTraceRow,
  isSettledHistoryRow,
  mergeMessageFields,
  mergeMetadata,
  normalizeMessages,
  queuedTextAlreadyRendered,
  sanitizeInjectedContext,
  stripImageManifest,
  stripLegacyContextPrefix,
  toChatMessage,
  toolIcon,
  userMessageIncludesQueuedText,
} from './chatHistory'
import type { ChatMessage } from './types'

function msg(partial: Partial<ChatMessage> & { role: ChatMessage['role'] }): ChatMessage {
  return { content: '', timestamp: '', ...partial }
}

describe('context stripping', () => {
  test('drops the wrapped context envelope', () => {
    const raw = '[CIAO_CONTEXT_BEGIN]\nproject notes\n[CIAO_CONTEXT_END]\n\nwhat is this?'
    expect(sanitizeInjectedContext(raw)).toBe('what is this?')
  })

  test('drops legacy bracket prefixes', () => {
    expect(stripLegacyContextPrefix('[CONTEXT: x]\n\nhello')).toBe('hello')
    expect(stripLegacyContextPrefix('[Chat: "Ideas"]\n\nhello')).toBe('hello')
  })

  test('drops a multi-line PWA interface preamble', () => {
    const raw = '[PWA interface: first line\nsecond line in the workspace.]\n\nhello'
    expect(stripLegacyContextPrefix(raw)).toBe('hello')
  })

  test('keeps text that only looks like a prefix', () => {
    expect(stripLegacyContextPrefix('just a message')).toBe('just a message')
  })

  test('never returns empty: a context-only message keeps its original text', () => {
    const raw = '[CONTEXT: only]\n'
    expect(stripLegacyContextPrefix(raw)).toBe(raw)
    expect(sanitizeInjectedContext(raw)).toBe(raw.trim())
  })

  test('strips the incoming-images manifest the SDK persisted', () => {
    expect(stripImageManifest('look at this\n\n[INCOMING IMAGES]\n1. shot.png\n')).toBe('look at this')
  })

  test('a manifest-only message keeps its text rather than vanishing', () => {
    const raw = '[INCOMING IMAGES]\n1. shot.png\n'
    expect(stripImageManifest(raw)).toBe(raw)
  })
})

describe('normalizeMessages', () => {
  test('trims, sanitises user turns and drops empty rows', () => {
    const out = normalizeMessages([
      msg({ role: 'user', content: '[CIAO_CONTEXT_BEGIN]\nctx\n[CIAO_CONTEXT_END]\n\n  hi  ' }),
      msg({ role: 'assistant', content: '   ' }),
    ])
    expect(out).toHaveLength(1)
    expect(out[0].content).toBe('hi')
  })

  test('drops cached host-unreachable bubbles from older clients', () => {
    const out = normalizeMessages([
      msg({ role: 'system', content: 'Error: Host WS unreachable: timeout' }),
      msg({ role: 'system', content: 'a real notice' }),
    ])
    expect(out.map(m => m.content)).toEqual(['a real notice'])
  })

  test('keeps an activity row only when it has content', () => {
    const out = normalizeMessages([
      msg({ role: 'system', tool_name: '_activity', content: '' }),
      msg({ role: 'system', tool_name: '_activity', content: 'Read file.md' }),
    ])
    expect(out).toHaveLength(1)
  })

  test('keeps a file card only when its path is plausible', () => {
    const out = normalizeMessages([
      msg({ role: 'system', tool_name: '_filecard', content: 'x', file_path: '' }),
      msg({ role: 'system', tool_name: '_filecard', content: 'x', file_path: 'notes/a.md' }),
    ])
    expect(out.map(m => m.file_path)).toEqual(['notes/a.md'])
  })
})

describe('queued-text matching', () => {
  test('matches an exact echo and one paragraph of a joined send', () => {
    expect(userMessageIncludesQueuedText('do it', 'do it')).toBe(true)
    expect(userMessageIncludesQueuedText('first\n\ndo it', 'do it')).toBe(true)
    expect(userMessageIncludesQueuedText('first\ndo it', 'do it')).toBe(false)
  })

  test('empty queued text never matches', () => {
    expect(userMessageIncludesQueuedText('anything', '   ')).toBe(false)
  })

  test('only user rows count as a rendering of the queued text', () => {
    const rows = [msg({ role: 'assistant', content: 'do it' })]
    expect(queuedTextAlreadyRendered(rows, 'do it')).toBe(false)
    expect(queuedTextAlreadyRendered([...rows, msg({ role: 'user', content: 'do it' })], 'do it')).toBe(true)
  })
})

describe('historySignature', () => {
  test('ignores thinking rows and turn metadata', () => {
    const a = [msg({ role: 'assistant', content: 'x', usage: { input: '1' } })]
    const b = [
      msg({ role: 'assistant', content: 'x' }),
      msg({ role: 'system', tool_name: '_thinking', content: 'reasoning' }),
    ]
    expect(historySignature(a)).toBe(historySignature(b))
  })

  test('changed content changes the signature', () => {
    expect(historySignature([msg({ role: 'assistant', content: 'x' })]))
      .not.toBe(historySignature([msg({ role: 'assistant', content: 'y' })]))
  })
})

describe('mergeMessageFields', () => {
  test('fills only what the server row is missing', () => {
    const server = msg({ role: 'assistant', content: 'a', effective_model: 'srv' })
    const local = msg({
      role: 'assistant', content: 'a', timestamp: 'T', effective_model: 'loc',
      usage: { input: '5' }, quota: { used: 1 }, is_error: false, turn_index: 3,
      duration_ms: 90, unattended: true,
    })
    const merged = mergeMessageFields(server, local)
    expect(merged.effective_model).toBe('srv')
    expect(merged.usage).toEqual({ input: '5' })
    expect(merged.quota).toEqual({ used: 1 })
    expect(merged.is_error).toBe(false)
    expect(merged.turn_index).toBe(3)
    expect(merged.duration_ms).toBe(90)
    expect(merged.unattended).toBe(true)
    expect(merged.timestamp).toBe('T')
  })
})

describe('groupIntoTurns / mergeMetadata', () => {
  test('a turn is a user row plus everything until the next one', () => {
    const turns = groupIntoTurns([
      msg({ role: 'assistant', content: 'preamble' }),
      msg({ role: 'user', content: 'q1' }),
      msg({ role: 'assistant', content: 'a1' }),
      msg({ role: 'user', content: 'q2' }),
    ])
    expect(turns).toHaveLength(3)
    expect(turns[0].user).toBeNull()
    expect(turns[1].responses.map(r => r.content)).toEqual(['a1'])
  })

  test('overlays local metadata onto matching server turns', () => {
    const server = [msg({ role: 'user', content: 'q' }), msg({ role: 'assistant', content: 'a' })]
    const local = [
      msg({ role: 'user', content: 'q' }),
      msg({ role: 'system', tool_name: '_activity', content: 'Read x' }),
      msg({ role: 'assistant', content: 'a', usage: { input: '7' } }),
    ]
    const merged = mergeMetadata(server, local)
    expect(merged.map(m => m.tool_name || m.role)).toEqual(['user', '_activity', 'assistant'])
    expect(merged[2].usage).toEqual({ input: '7' })
  })

  test('a turn the local copy does not match is taken from the server verbatim', () => {
    const server = [msg({ role: 'user', content: 'other' }), msg({ role: 'assistant', content: 'a' })]
    const merged = mergeMetadata(server, [msg({ role: 'user', content: 'q' })])
    expect(merged.map(m => m.content)).toEqual(['other', 'a'])
  })
})

describe('toChatMessage', () => {
  test('maps a server row, defaulting the timestamp and dropping a false marker', () => {
    const out = toChatMessage({ role: 'assistant', content: 'a' })
    expect(out.timestamp).toBe('')
    expect(out.unattended).toBeUndefined()
  })

  test('carries the fields the footer and dedup depend on', () => {
    const out = toChatMessage({
      role: 'user', content: 'q', sent_at: 'T', turn_index: 4, unattended: true,
      usage: { input: '1' }, quota: { used: 2 }, effective_model: 'm', i: 9, lazy: true, full_length: 400,
    })
    expect(out).toMatchObject({
      timestamp: 'T', turn_index: 4, unattended: true, effective_model: 'm', i: 9, lazy: true, full_length: 400,
    })
  })
})

describe('isLiveTraceRow', () => {
  test('every assistant row and the three system trace rows', () => {
    expect(isLiveTraceRow(msg({ role: 'assistant' }))).toBe(true)
    for (const tool of ['_activity', '_thinking', '_filecard']) {
      expect(isLiveTraceRow(msg({ role: 'system', tool_name: tool }))).toBe(true)
    }
    expect(isLiveTraceRow(msg({ role: 'system', content: 'notice' }))).toBe(false)
    expect(isLiveTraceRow(msg({ role: 'user' }))).toBe(false)
  })
})

describe('isSettledHistoryRow', () => {
  // The session file is written as the turn runs, so a /messages response can
  // land while its last row is still provider commentary or a tool trace.
  // Those rows are what the client renders from live events; settling on one
  // ended the post-result retries before the answer was persisted, and the
  // reply stayed blank until Activity forced a refetch (#630).
  test('only real answer or error output can close a turn', () => {
    const cases: Array<[string, ChatMessage | undefined, boolean]> = [
      ['a final answer', msg({ role: 'assistant', content: 'Here is the summary.', phase: 'final_answer' }), true],
      ['a phase-less answer', msg({ role: 'assistant', content: 'legacy answer' }), true],
      ['an explicit assistant error', msg({ role: 'assistant', content: 'Error: the run failed', is_error: true }), true],
      ['provider commentary', msg({ role: 'assistant', content: 'Now let me check the numbers.', phase: 'commentary' }), false],
      ['thinking', msg({ role: 'system', content: 'planning the answer', tool_name: '_thinking' }), false],
      ['activity', msg({ role: 'system', content: 'Read offer.md', tool_name: '_activity' }), false],
      ['a file card', msg({ role: 'system', content: 'offer.md', tool_name: '_filecard', file_path: 'notes/offer.md' }), false],
      ['any other tool-backed system row', msg({ role: 'system', content: '3 hits', tool_name: 'Grep' }), false],
      ['an ordinary terminal system error', msg({ role: 'system', content: "Error: a message didn't reach the engine" }), true],
      ['a user row', msg({ role: 'user', content: 'summarise the offer' }), false],
      ['an empty assistant row', msg({ role: 'assistant', content: '   ' }), false],
      ['an empty system notice', msg({ role: 'system', content: '' }), false],
      ['no row at all', undefined, false],
    ]
    for (const [label, row, settled] of cases) {
      expect(isSettledHistoryRow(row), label).toBe(settled)
    }
  })
})

describe('dropSupersededLiveTail', () => {
  test('keeps everything until the window settles the turn', () => {
    const rows = [
      msg({ role: 'user', content: 'q', i: 0 }),
      msg({ role: 'assistant', content: 'live' }),
      msg({ role: 'assistant', content: 'server', i: 1 }),
    ]
    expect(dropSupersededLiveTail(rows, 1, [rows[2]])).toBe(rows)
  })

  test('drops the live trace once a settled server row replaces it', () => {
    const server = msg({ role: 'assistant', content: 'server answer', i: 1, timestamp: 'T' })
    const rows = [
      msg({ role: 'user', content: 'q', i: 0 }),
      msg({ role: 'system', tool_name: '_activity', content: 'Read x' }),
      msg({ role: 'assistant', content: 'live answer' }),
      server,
    ]
    const out = dropSupersededLiveTail(rows, 1, [server])
    expect(out.map(m => m.content)).toEqual(['q', 'server answer'])
  })

  test('carries the live turn cost onto the server row that closes the turn', () => {
    const server = msg({ role: 'assistant', content: 'server', i: 1, timestamp: 'T' })
    const rows = [
      msg({ role: 'user', content: 'q', i: 0 }),
      msg({ role: 'assistant', content: 'live', usage: { input: '11' }, effective_model: 'm' }),
      server,
    ]
    const out = dropSupersededLiveTail(rows, 1, [server])
    expect(out).toHaveLength(2)
    expect(out[1].usage).toEqual({ input: '11' })
    expect(out[1].effective_model).toBe('m')
  })

  test('a client-side notice with no server counterpart survives', () => {
    const server = msg({ role: 'assistant', content: 'server', i: 1, timestamp: 'T' })
    const rows = [
      msg({ role: 'user', content: 'q', i: 0 }),
      msg({ role: 'system', content: "Error: a message didn't reach the engine" }),
      server,
    ]
    const out = dropSupersededLiveTail(rows, 1, [server])
    expect(out.map(m => m.role)).toEqual(['user', 'system', 'assistant'])
  })

  test('a follow-up turn already streaming keeps its own trace', () => {
    const server = msg({ role: 'assistant', content: 'server 1', i: 2, timestamp: 'T' })
    const rows = [
      msg({ role: 'assistant', content: 'older', i: 0 }),
      msg({ role: 'user', content: 'q1', i: 1 }),
      msg({ role: 'assistant', content: 'live 1' }),
      server,
      msg({ role: 'user', content: 'q2' }),
      msg({ role: 'assistant', content: 'live 2' }),
    ]
    const out = dropSupersededLiveTail(rows, 1, [server])
    expect(out.map(m => m.content)).toEqual(['older', 'q1', 'server 1', 'q2', 'live 2'])
  })

  test('one refresh can settle a turn and the queued turn after it', () => {
    const first = msg({ role: 'assistant', content: 'server 1', i: 2, timestamp: 'T' })
    const second = msg({ role: 'assistant', content: 'server 2', i: 4, timestamp: 'T' })
    const rows = [
      msg({ role: 'assistant', content: 'older', i: 0 }),
      msg({ role: 'user', content: 'q1', i: 1 }),
      msg({ role: 'system', tool_name: '_activity', content: 'live tools 1' }),
      first,
      msg({ role: 'user', content: 'q2', i: 3 }),
      msg({ role: 'system', tool_name: '_activity', content: 'live tools 2' }),
      second,
    ]
    const out = dropSupersededLiveTail(rows, 1, [first, second])
    expect(out.map(m => m.content)).toEqual(['older', 'q1', 'server 1', 'q2', 'server 2'])
  })

  test('nothing to drop when the window settles nothing', () => {
    const rows = [msg({ role: 'assistant', content: 'a', i: 0 })]
    expect(dropSupersededLiveTail(rows, 1, [])).toBe(rows)
  })

  test('delayed completion prunes earlier traces without mixing turn footers or dropping a live turn', () => {
    const first = msg({ role: 'assistant', content: 'answer 1', i: 2, timestamp: 'T' })
    const second = msg({ role: 'assistant', content: 'answer 2', i: 4, timestamp: 'T', usage: { input: '99' } })
    const rows = [
      msg({ role: 'user', content: 'q1', i: 0 }),
      msg({ role: 'assistant', content: 'live 1', usage: { input: '11' }, effective_model: 'm1' }),
      first,
      msg({ role: 'user', content: 'q2', i: 3 }),
      msg({ role: 'assistant', content: 'live 2', usage: { input: '22' }, effective_model: 'm2' }),
      second,
      msg({ role: 'user', content: 'q3', i: 5 }),
      msg({ role: 'system', content: 'working', tool_name: '_activity' }),
    ]
    const out = dropSupersededLiveTail(rows, 7, [first, second])
    expect(out.map(m => m.content)).toEqual(['q1', 'answer 1', 'q2', 'answer 2', 'q3', 'working'])
    expect(out[1].usage).toEqual({ input: '11' })
    expect(out[1].effective_model).toBe('m1')
    expect(out[3].usage).toEqual({ input: '99' })
    expect(out[3].effective_model).toBe('m2')
  })
})

describe('toolIcon', () => {
  test('known tools keep their glyph and unknown tools get the gear', () => {
    expect(toolIcon('Read')).toBe('\u{1F4D6}')
    expect(toolIcon('WebSearch')).toBe(toolIcon('WebFetch'))
    expect(toolIcon('Bash')).toBe('$')
    expect(toolIcon('SomethingElse')).toBe('⚙️')
  })
})
