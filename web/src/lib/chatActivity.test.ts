import { describe, expect, it } from 'vitest'
import {
  buildTurnParts,
  collectTraceOutputs,
  findFinalAnswerIndex,
  formatTokenUsage,
  isAnswerBubble,
  isProgressCommentary,
  traceSummaryMeta,
} from './chatActivity'
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

  it('appends the context-window occupancy when present', () => {
    expect(formatTokenUsage({ input_tokens: '2', output_tokens: '1079', context_pct: '42.3%' }))
      .toBe('Tokens <span class="token-number">2</span> in · <span class="token-number">1,079</span> out · <span class="context-pct">42.3%</span> ctx')
  })

  it('renders context_pct alone when token sides are missing', () => {
    expect(formatTokenUsage({ context_pct: '8.4%' }))
      .toBe('Tokens <span class="context-pct">8.4%</span> ctx')
    expect(formatTokenUsage({ context_pct: '' }))
      .toBe('')
  })
})

describe('isProgressCommentary', () => {
  it('folds Claude progress narration like the screenshot case', () => {
    expect(isProgressCommentary('Now let me make the edits.')).toBe(true)
    expect(isProgressCommentary('Now the script side:')).toBe(true)
    expect(isProgressCommentary('Now the CSS: make the chip body a real button and update the stale comment.')).toBe(true)
    expect(isProgressCommentary('Now let me typecheck and run the tests.')).toBe(true)
    expect(isProgressCommentary("I'll dig into the ciaobot code for each of these.")).toBe(true)
    expect(isProgressCommentary('Let me look at how chat comments work today before giving an opinion.')).toBe(true)
  })

  it('keeps real mid-turn answers, blockers, and answer-shaped openings', () => {
    expect(isProgressCommentary('Done. Option 2 is implemented in ChatPanel.vue.')).toBe(false)
    expect(isProgressCommentary(
      "The classifier is blocking edits to permission-bypass logic (fittingly). I'll continue with the rest.",
    )).toBe(false)
    expect(isProgressCommentary(
      'Both moves are right in the same way: a pending comment is staged input, not a chat-level control.',
    )).toBe(false)
    // Short substantive plan — must stay visible (5c6410f regression guard).
    expect(isProgressCommentary('the plan')).toBe(false)
    // Long enough mid-turn update.
    expect(isProgressCommentary('x'.repeat(200))).toBe(false)
  })
})

describe('findFinalAnswerIndex', () => {
  it('prefers the last non-progress text over a trailing narration line', () => {
    const buffer = [
      text('Done. Option 2 is implemented with the chip popover.'),
      text('Now update the docs:'),
      activity('Edit README.md'),
    ]
    expect(findFinalAnswerIndex(buffer)).toBe(0)
  })

  it('falls back to the last text when the whole turn is progress narration', () => {
    const buffer = [
      text('Now let me make the edits.'),
      activity('Edit a.vue'),
      text('Now the script side:'),
    ]
    expect(findFinalAnswerIndex(buffer)).toBe(2)
  })

  it('respects Codex final_answer phase', () => {
    const buffer = [
      text('working', { phase: 'commentary' }),
      text('Done.', { phase: 'final_answer' }),
      text('Now tidy up:'),
    ]
    expect(findFinalAnswerIndex(buffer)).toBe(1)
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

  it('folds Claude progress narration into Activity and keeps the final answer', () => {
    // Screenshot shape: short "Now…" notes between tool batches, then Done.
    const buffer = [
      text('Now let me make the edits.'), activity('Edit ChatPanel.vue'),
      text('Now the script side:'), filecard('ChatPanel.vue'),
      text('Now let me typecheck and run the tests.'), activity('Bash'),
      text('Done. Option 2 is implemented in ChatPanel.vue.'),
    ]
    const finalIdx = findFinalAnswerIndex(buffer)
    expect(finalIdx).toBe(6)
    expect(buildTurnParts(buffer, finalIdx)).toEqual([
      { kind: 'trace', steps: [buffer[0], buffer[1], buffer[2], buffer[3], buffer[4], buffer[5]] },
    ])
  })

  it('keeps substantive mid-turn answers as bubbles between activity', () => {
    const buffer = [
      text('the plan'), activity('Read doc.md'),
      text('Done. Aligned the backend to the confirmed contract.'),
    ]
    expect(buildTurnParts(buffer, 2)).toEqual([
      { kind: 'assistant', msg: buffer[0] },
      { kind: 'trace', steps: [buffer[1]] },
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

  it('keeps a mid-turn blocker visible even when it starts like progress', () => {
    const blocker =
      "The classifier is blocking edits to permission-bypass logic (fittingly). I'll continue with the rest."
    const buffer = [
      text('Now wire it into the provider:'),
      activity('Edit provider.py'),
      text(blocker),
      activity('Edit routes.py'),
      text("All restored. Now the one piece I couldn't apply — I need your call on it:"),
    ]
    const finalIdx = findFinalAnswerIndex(buffer)
    expect(finalIdx).toBe(4)
    expect(buildTurnParts(buffer, finalIdx)).toEqual([
      { kind: 'trace', steps: [buffer[0], buffer[1]] },
      { kind: 'assistant', msg: buffer[2] },
      { kind: 'trace', steps: [buffer[3]] },
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


