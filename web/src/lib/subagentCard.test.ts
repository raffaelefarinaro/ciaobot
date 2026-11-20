import { describe, expect, it } from 'vitest'
import {
  SUBAGENT_STEP_LIMIT,
  addSubagentStep,
  isSubagentCallName,
  newSubagentCard,
  rememberSubagentCall,
  settleSubagentCard,
  subagentCardFromHistory,
  subagentFields,
  subagentTitle,
  type SubagentCard,
  type SubagentStep,
} from './subagentCard'

const activity = (line: string, tool_use_id?: string): SubagentStep => ({ kind: 'activity', line, tool_use_id })

describe('subagent call names and inputs', () => {
  it('recognises the subagent tool names, case-insensitively', () => {
    expect(['subagent', 'Agent', 'Task', 'AGENT'].map(isSubagentCallName)).toEqual([true, true, true, true])
    expect(['bash', 'Skill', 'agents'].map(isSubagentCallName)).toEqual([false, false, false])
  })

  it('reads the agent type and description from a [type] description input', () => {
    expect(subagentFields('[general] Sleep 5 then reply done')).toEqual({
      agent_type: 'general',
      description: 'Sleep 5 then reply done',
    })
  })

  it('takes a plain input as the description, and names nothing for a title-as-input', () => {
    expect(subagentFields('Scan the repo')).toEqual({ agent_type: '', description: 'Scan the repo' })
    expect(subagentFields('subagent')).toEqual({ agent_type: '', description: '' })
    expect(subagentFields('')).toEqual({ agent_type: '', description: '' })
  })

  it('titles the card by its description, or plainly when there is none', () => {
    expect(subagentTitle({ description: 'Scan' })).toBe('Subagent: Scan')
    expect(subagentTitle({ description: '' })).toBe('Subagent')
  })
})

describe('live card reducers', () => {
  it('starts running, and a re-announce with a description fills it in', () => {
    const card = newSubagentCard('sa1', 'subagent')
    expect(card).toMatchObject({ tool_use_id: 'sa1', state: 'running', description: '', step_count: 0 })
    rememberSubagentCall(card, '[general] Scan')
    expect(card).toMatchObject({ agent_type: 'general', description: 'Scan' })
    rememberSubagentCall(card, 'subagent')
    expect(card.description).toBe('Scan')
  })

  it('keeps a launch acknowledgement running, and settles on the terminal status', () => {
    const card = newSubagentCard('sa1', '[general] Scan')
    settleSubagentCard(card, { background: true })
    expect(card.state).toBe('running')
    settleSubagentCard(card, { subagent_status: 'completed' })
    expect(card.state).toBe('done')
  })

  it('treats a non-background result as the completion, and an error or a bad terminal status as failed', () => {
    const plain = newSubagentCard('a', '[general] x')
    settleSubagentCard(plain, {})
    expect(plain.state).toBe('done')

    const errored = newSubagentCard('b', '[general] x')
    settleSubagentCard(errored, { is_error: true })
    expect(errored.state).toBe('failed')

    for (const status of ['failed', 'interrupted']) {
      const terminal = newSubagentCard('c', '[general] x')
      settleSubagentCard(terminal, { background: true, subagent_status: status })
      expect(terminal.state).toBe('failed')
    }
  })

  it('replaces a child step its own call wrote again, and keeps a file step per file', () => {
    const card = newSubagentCard('sa1', '[general] x')
    addSubagentStep(card, activity('bash ls', 'k1'))
    addSubagentStep(card, activity('bash ls -la', 'k1'))
    expect(card.steps).toEqual([activity('bash ls -la', 'k1')])
    expect(card.step_count).toBe(1)

    const file = (path: string): SubagentStep => ({ kind: 'file', file_path: path, action: 'created', tool: 'Write', tool_use_id: 'k2' })
    addSubagentStep(card, file('/w/a.md'))
    addSubagentStep(card, file('/w/b.md'))
    addSubagentStep(card, file('/w/a.md'))
    expect(card.steps.map(step => (step.kind === 'file' ? step.file_path : step.line))).toEqual([
      'bash ls -la', '/w/a.md', '/w/b.md',
    ])
    expect(card.step_count).toBe(3)
  })

  it('never merges steps that carry no call id', () => {
    const card = newSubagentCard('sa1', '[general] x')
    addSubagentStep(card, activity('one'))
    addSubagentStep(card, activity('one'))
    expect(card.step_count).toBe(2)
  })
})

describe('history card normalisation', () => {
  it('reads a card the renderer wrote', () => {
    const wire = {
      tool_use_id: 'sa1', agent_type: 'general', description: 'Scan', state: 'done', step_count: 2,
      steps: [activity('bash ls', 'k1')],
    }
    expect(subagentCardFromHistory(wire)).toEqual(wire)
  })

  it('defaults the fields an older journal does not carry', () => {
    expect(subagentCardFromHistory({ tool_use_id: 'sa1' })).toEqual({
      tool_use_id: 'sa1', agent_type: '', description: '', state: 'running', step_count: 0, steps: [],
    })
    expect(subagentCardFromHistory({ tool_use_id: 'sa1', state: 'weird' })?.state).toBe('running')
  })

  it('rejects a value that is not a card, and drops malformed steps', () => {
    expect(subagentCardFromHistory(undefined)).toBeNull()
    expect(subagentCardFromHistory('subagent')).toBeNull()
    expect(subagentCardFromHistory({ description: 'no id' })).toBeNull()
    const card = subagentCardFromHistory({ tool_use_id: 'sa1', steps: [null, 'x', activity('ok')] }) as SubagentCard
    expect(card.steps).toEqual([activity('ok')])
  })

  it('bounds the steps it hands to the view and keeps the full count', () => {
    const steps = Array.from({ length: SUBAGENT_STEP_LIMIT + 5 }, (_, n) => activity(`cmd ${n}`, `k${n}`))
    const card = subagentCardFromHistory({ tool_use_id: 'sa1', step_count: steps.length, steps })
    expect(card?.steps).toHaveLength(SUBAGENT_STEP_LIMIT)
    expect(card?.step_count).toBe(SUBAGENT_STEP_LIMIT + 5)
  })
})
