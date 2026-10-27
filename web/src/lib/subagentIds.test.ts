import { describe, expect, it } from 'vitest'
import { isShellRow, shellTaskId, subagentLabel } from './subagentIds'

describe('subagentIds', () => {
  it('labels a running subagent by description, else short id', () => {
    expect(subagentLabel({ agent_id: 'a1', description: ' Sweep ' })).toBe('Sweep')
    expect(subagentLabel({ agent_id: 'agent-0123456789abcdef' })).toBe('01234567…')
  })

  it('treats shell and monitor rows as processes, never agents', () => {
    expect(isShellRow({ subagent_type: 'shell' })).toBe(true)
    expect(isShellRow({ subagent_type: 'monitor' })).toBe(true)
    expect(isShellRow({ subagent_type: 'Explore' })).toBe(false)
    expect(isShellRow({})).toBe(false)
  })

  it('strips the task: / shell: prefix to the id the stop route takes', () => {
    expect(shellTaskId('task:abc')).toBe('abc')
    expect(shellTaskId('shell:sh_1')).toBe('sh_1')
    expect(shellTaskId('abc')).toBe('abc')
  })
})
