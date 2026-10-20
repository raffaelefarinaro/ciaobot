import { describe, expect, it } from 'vitest'
import { isChatCodeModeOn, isCodeModeLocked } from './codeMode'
import type { ProjectRepo } from './types'

const repo: ProjectRepo = { path: '/r/app', branch: 'main', dirty: false, missing: false }

describe('codeMode', () => {
  it('is on only with a linked repo', () => {
    expect(isChatCodeModeOn({ code_mode: true }, null)).toBe(false)
    expect(isChatCodeModeOn({ code_mode: true }, repo)).toBe(true)
    expect(isChatCodeModeOn({}, repo)).toBe(false)
  })

  it('locks only when on and the chat has messages', () => {
    expect(isCodeModeLocked({ code_mode: true, user_turn_count: 1 })).toBe(true)
    expect(isCodeModeLocked({ code_mode: true, user_turn_count: 0 })).toBe(false)
    expect(isCodeModeLocked({ code_mode: true })).toBe(false)
    expect(isCodeModeLocked({ code_mode: false, user_turn_count: 4 })).toBe(false)
    expect(isCodeModeLocked(null)).toBe(false)
    expect(isCodeModeLocked(undefined)).toBe(false)
  })
})
