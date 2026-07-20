import { describe, expect, it } from 'vitest'
import { buildTurnParts, collectTraceOutputs, formatTokenUsage, isAnswerBubble, traceSummaryMeta } from './chatActivity'
import type { ChatMessage } from './types'

// Minimal ChatMessage factory for the grouping tests.
const msg = (over: Partial<ChatMessage>): ChatMessage => ({
  role: 'assistant',
  content: '',
  timestamp: '',
  ...over,
})
const text = (content: string, over: Partial<ChatMessage> = {}) =>
  msg({ role: 'assistant', content, ...over })
const activity = (content: string) => msg({ role: 'system', tool_name: '_activity', content })
const thinking = (content: string) => msg({ role: 'system', tool_name: '_thinking', content })
const filecard = (file_path: string) =>
  msg({ role: 'system', tool_name: '_filecard', file_path, content: file_path })

describe('collectTraceOutputs', () => {
  it('returns each file path once in first-seen order', () => {
    expect(collectTraceOutputs([
      { tool_name: '_activity', content: 'Edit draft.md' },
      { tool_name: '_filecard', file_path: 'draft.md', content: '' },
      { tool_name: '_filecard', file_path: 'draft.md', content: '' },
      { tool_name: '_filecard', file_path: 'brief.md', content: '' },
    ])).toEqual([{ file_path: 'draft.md' }, { file_path: 'brief.md' }])
  })

  it('falls back to file-card content when the path field is absent', () => {
    expect(collectTraceOutputs([
      { tool_name: '_filecard', content: 'notes.md' },
    ])).toEqual([{ file_path: 'notes.md' }])
  })

  it('preserves created/edited action labels for Outputs chips', () => {
    expect(collectTraceOutputs([
      { tool_name: '_filecard', file_path: 'new.csv', content: '', action: 'created' },
      { tool_name: '_filecard', file_path: 'notes.md', content: '', action: 'edited' },
    ])).toEqual([
      { file_path: 'new.csv', action: 'created' },
      { file_path: 'notes.md', action: 'edited' },
    ])
  })

  it('drops implausible bare words that are not file paths', () => {
    expect(collectTraceOutputs([
      { tool_name: '_filecard', file_path: 'There', content: '', action: 'created' },
      { tool_name: '_filecard', file_path: 'guests.csv', content: '', action: 'created' },
    ])).toEqual([{ file_path: 'guests.csv', action: 'created' }])
  })
})

describe('formatTokenUsage', () => {
  it('spells out input and output token labels with thousands separators and styled spans', () => {
    expect(formatTokenUsage({ input_tokens: '2', output_tokens: '1079' }))
      .toBe('Tokens <span class="token-number">2</span> in · <span class="token-number">1,079</span> out')
  })

  it('omits a missing side without hiding the available value', () => {
    expect(formatTokenUsage({ output_tokens: '1079' }))
      .toBe('Tokens <span class="token-number">1,079</span> out')
    expect(formatTokenUsage({ input_tokens: '2' }))
      .toBe('Tokens <span class="token-number">2</span> in')
  })

  it('handles numbers, zero values, and empty values correctly', () => {
    expect(formatTokenUsage({ input_tokens: 0, output_tokens: 0 }))
      .toBe('Tokens <span class="token-number">0</span> in · <span class="token-number">0</span> out')
    expect(formatTokenUsage({ input_tokens: null, output_tokens: undefined }))
      .toBe('')
    expect(formatTokenUsage({ input_tokens: '', output_tokens: '' }))
      .toBe('')
    expect(formatTokenUsage(undefined))
      .toBe('')
  })
})

describe('isAnswerBubble', () => {
  it('accepts substantive assistant text', () => {
    expect(isAnswerBubble(text('hello'))).toBe(true)
    expect(isAnswerBubble(text('final', { phase: 'final_answer' }))).toBe(true)
  })

  it('rejects markers, commentary, and non-assistant roles', () => {
    expect(isAnswerBubble(activity('Read x'))).toBe(false)
    expect(isAnswerBubble(thinking('hmm'))).toBe(false)
    expect(isAnswerBubble(filecard('a.md'))).toBe(false)
    expect(isAnswerBubble(text('narration', { phase: 'commentary' }))).toBe(false)
    expect(isAnswerBubble(msg({ role: 'user', content: 'hi' }))).toBe(false)
    expect(isAnswerBubble(msg({ role: 'system', content: 'sys' }))).toBe(false)
  })

  it('rejects a marker even if it carries the assistant role', () => {
    // Older/looser payloads sometimes tag activity rows role:assistant.
    expect(isAnswerBubble({ role: 'assistant', tool_name: '_thinking' })).toBe(false)
  })
})

describe('buildTurnParts', () => {
  it('promotes a pre-answer text block a tool call split off (the reported bug)', () => {
    // [plan] -> [Read] -> [one-liner]. The Read split the answer in two, so the
    // plan used to be demoted into the italic Activity trace.
    const buffer = [text('the plan'), activity('Read doc.md'), text('logged it')]
    expect(buildTurnParts(buffer, 2)).toEqual([
      { kind: 'assistant', msg: buffer[0] },
      { kind: 'trace', steps: [buffer[1]] },
    ])
  })

  it('interleaves every message with the activity that ran between them (agentic turn)', () => {
    // Real shape: each short narration is its own assistant text block followed
    // by an Edit. Every message shows as a bubble; the Edit between two messages
    // becomes the Activity trace bubble that sits between them.
    const buffer = [
      text('Now the geometry parser:'), filecard('parser.py'),
      text('Now storage and the runner.'), filecard('storage.py'),
      text('Done. Aligned the backend to the confirmed contract.'),
    ]
    expect(buildTurnParts(buffer, 4)).toEqual([
      { kind: 'assistant', msg: buffer[0] },
      { kind: 'trace', steps: [buffer[1]] },
      { kind: 'assistant', msg: buffer[2] },
      { kind: 'trace', steps: [buffer[3]] },
    ])
  })

  it('folds bookkeeping tools run after the final answer into the pre-answer trace', () => {
    const buffer = [text('the plan'), activity('Read doc.md'), text('logged it'), activity('TodoWrite')]
    expect(buildTurnParts(buffer, 2)).toEqual([
      { kind: 'assistant', msg: buffer[0] },
      { kind: 'trace', steps: [buffer[1], buffer[3]] },
    ])
  })

  it('keeps the classic single-answer turn as one trace (unchanged behavior)', () => {
    const buffer = [activity('Read'), filecard('a.md'), text('done')]
    expect(buildTurnParts(buffer, 2)).toEqual([
      { kind: 'trace', steps: [buffer[0], buffer[1]] },
    ])
  })

  it('groups everything into traces when the turn produced no answer bubble', () => {
    const buffer = [activity('Read'), thinking('hmm')]
    expect(buildTurnParts(buffer, -1)).toEqual([
      { kind: 'trace', steps: [buffer[0], buffer[1]] },
    ])
  })

  it('keeps Codex commentary folded in the trace, not promoted to a bubble', () => {
    const buffer = [text('narration', { phase: 'commentary' }), activity('Read'), text('final')]
    expect(buildTurnParts(buffer, 2)).toEqual([
      { kind: 'trace', steps: [buffer[0], buffer[1]] },
    ])
  })

  it('emits no trailing trace when adjacent text blocks precede the final answer', () => {
    const buffer = [text('t1'), text('t2')]
    expect(buildTurnParts(buffer, 1)).toEqual([
      { kind: 'assistant', msg: buffer[0] },
    ])
  })
})

describe('traceSummaryMeta', () => {
  it('returns "steps" when empty', () => {
    expect(traceSummaryMeta([])).toBe('steps')
  })

  it('correctly counts and pluralizes thoughts, notes, tool calls, files, and subagents', () => {
    expect(traceSummaryMeta([
      { role: 'assistant', tool_name: '_thinking', content: 'Hmm', timestamp: '' },
      { role: 'assistant', content: 'Note 1', timestamp: '' },
      { role: 'assistant', tool_name: '_activity', content: 'Tool A\nTool B', timestamp: '' },
      { role: 'assistant', tool_name: '_filecard', file_path: 'a.md', content: '', timestamp: '' },
    ], [
      { agent_id: 'sub-1', messages: [] }
    ])).toBe('1 thought · 1 note · 2 tool calls · 1 file · 1 subagent')
  })

  it('pluralizes multiple items correctly', () => {
    expect(traceSummaryMeta([
      { role: 'assistant', tool_name: '_thinking', content: 'Hmm', timestamp: '' },
      { role: 'assistant', tool_name: '_thinking', content: 'Hmm 2', timestamp: '' },
      { role: 'assistant', content: 'Note 1', timestamp: '' },
      { role: 'assistant', content: 'Note 2', timestamp: '' },
      { role: 'assistant', tool_name: '_filecard', file_path: 'a.md', content: '', timestamp: '' },
      { role: 'assistant', tool_name: '_filecard', file_path: 'b.md', content: '', timestamp: '' },
    ], [
      { agent_id: 'sub-1', messages: [] },
      { agent_id: 'sub-2', messages: [] }
    ])).toBe('2 thoughts · 2 notes · 2 files · 2 subagents')
  })
})


