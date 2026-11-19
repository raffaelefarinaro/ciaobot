import { describe, expect, it } from 'vitest'
import { subagentLabel } from './subagentIds'

describe('subagentIds', () => {
  it('labels a running subagent by description, else short id', () => {
    expect(subagentLabel({ agent_id: 'a1', description: ' Sweep ' })).toBe('Sweep')
    expect(subagentLabel({ agent_id: 'agent-0123456789abcdef' })).toBe('01234567…')
  })
})
