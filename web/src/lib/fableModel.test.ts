import { describe, it, expect } from 'vitest'
import {
  CODEX_FABLE_LEVEL,
  CODEX_FABLE_PSEUDO_MODEL,
  CODEX_FABLE_REAL_MODEL,
  isFableSelection,
  selectableThinkingLevels,
} from './fableModel'

const SOL_LEVELS = ['low', 'medium', 'high', CODEX_FABLE_LEVEL]

describe('isFableSelection', () => {
  it('matches the pseudo-model name', () => {
    expect(isFableSelection(CODEX_FABLE_PSEUDO_MODEL, '')).toBe(true)
    expect(isFableSelection(CODEX_FABLE_PSEUDO_MODEL, null)).toBe(true)
  })

  it('matches the stored real-model and level pair', () => {
    expect(isFableSelection(CODEX_FABLE_REAL_MODEL, CODEX_FABLE_LEVEL)).toBe(true)
  })

  it('leaves the real model on any other level alone', () => {
    expect(isFableSelection(CODEX_FABLE_REAL_MODEL, 'high')).toBe(false)
    expect(isFableSelection(CODEX_FABLE_REAL_MODEL, '')).toBe(false)
    expect(isFableSelection('gpt-5.6-terra', CODEX_FABLE_LEVEL)).toBe(false)
    expect(isFableSelection(undefined, undefined)).toBe(false)
  })
})

describe('selectableThinkingLevels', () => {
  it('drops the level fable encodes, so the chips cannot become a dead end', () => {
    const levels = selectableThinkingLevels(CODEX_FABLE_REAL_MODEL, SOL_LEVELS)
    expect(levels).toEqual(['low', 'medium', 'high'])
    // Whatever a user can pick here must leave the chips visible.
    for (const level of levels) {
      expect(isFableSelection(CODEX_FABLE_REAL_MODEL, level)).toBe(false)
    }
  })

  it('leaves every other model untouched', () => {
    expect(selectableThinkingLevels('gpt-5.6-terra', SOL_LEVELS)).toEqual(SOL_LEVELS)
    expect(selectableThinkingLevels('claude-opus-5', ['low', 'high'])).toEqual(['low', 'high'])
  })

  it('is a no-op on an empty list', () => {
    expect(selectableThinkingLevels(CODEX_FABLE_REAL_MODEL, [])).toEqual([])
  })
})
