import { describe, expect, it } from 'vitest'
import {
  BUILTIN_PLAN_COMMAND,
  includeBuiltinPlanCommand,
  planCommandTargetMode,
  type SlashCommand,
} from './planCommand'

const customCommand: SlashCommand = {
  name: 'brief',
  description: 'Write a short brief.',
  argument_hint: '',
  source: 'project',
  path: 'commands/brief.md',
}

describe('plan command', () => {
  it('adds a synthetic /plan command before disk-backed commands', () => {
    expect(includeBuiltinPlanCommand([customCommand])).toEqual([
      BUILTIN_PLAN_COMMAND,
      customCommand,
    ])
  })

  it('does not duplicate /plan when the backend already returns one', () => {
    const diskPlan: SlashCommand = {
      ...BUILTIN_PLAN_COMMAND,
      source: 'project',
      path: 'commands/plan.md',
    }
    expect(includeBuiltinPlanCommand([customCommand, diskPlan])).toEqual([
      BUILTIN_PLAN_COMMAND,
      customCommand,
    ])
  })

  it('targets plan from normal mode for the exact /plan command', () => {
    expect(planCommandTargetMode('/plan', 'normal')).toBe('plan')
    expect(planCommandTargetMode('  /plan  ', 'normal')).toBe('plan')
  })

  it('targets normal from plan mode for the exact /plan command', () => {
    expect(planCommandTargetMode('/plan', 'plan')).toBe('normal')
  })

  it('does not intercept prefixes, arguments, or non-plan modes as a return target', () => {
    expect(planCommandTargetMode('/planner', 'normal')).toBeNull()
    expect(planCommandTargetMode('/plan extra', 'normal')).toBeNull()
    expect(planCommandTargetMode('please /plan', 'normal')).toBeNull()
  })
})
