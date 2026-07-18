import { describe, expect, it } from 'vitest'
import { collectTraceOutputs, formatTokenUsage, traceSummaryMeta } from './chatActivity'

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


