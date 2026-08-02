import type { SlashCommand } from './types'

export type { SlashCommand } from './types'

export const BUILTIN_PLAN_COMMAND: SlashCommand = {
  name: 'plan',
  description: 'Toggle plan mode for this chat.',
  argument_hint: '',
  source: 'builtin',
  path: '',
}

/** Add the UI-owned /plan entry and let it win over a disk-backed collision. */
export function includeBuiltinPlanCommand(commands: readonly SlashCommand[]): SlashCommand[] {
  return [BUILTIN_PLAN_COMMAND, ...commands.filter((command) => command.name !== BUILTIN_PLAN_COMMAND.name)]
}

/** Return the mode to persist for an exact /plan command, or null otherwise. */
export function planCommandTargetMode(text: string, currentMode: string): 'plan' | 'normal' | null {
  if (text.trim() !== '/plan') return null
  return currentMode === 'plan' ? 'normal' : 'plan'
}
