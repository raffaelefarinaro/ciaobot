import { describe, expect, it } from 'vitest'
import { elapsedLabel, runningWorkFrom, type WorkCard } from './runningWork'
import type { ShellCard } from './shellCard'
import type { SubagentCard } from './subagentCard'

const subagent = (id: string, state: SubagentCard['state'], description = 'Explore repo', agent_type = 'Explore'): WorkCard => ({
  kind: 'subagent',
  card: { tool_use_id: id, agent_type, description, state, step_count: 0, steps: [] },
  started_at: 1_000,
})

const shell = (id: string, state: ShellCard['state'], background: boolean, command = 'npm run dev'): WorkCard => ({
  kind: 'shell',
  card: { tool_use_id: id, command, background, state, exit_code: null, output_tail: '' },
  started_at: 2_000,
})

describe('runningWorkFrom', () => {
  it('counts running subagents as agents and running background shells as shells', () => {
    const work = runningWorkFrom([subagent('a1', 'running'), shell('s1', 'running', true)])
    expect(work.agents).toEqual([{ tool_use_id: 'a1', label: 'Explore repo', detail: 'Explore', started_at: 1_000 }])
    expect(work.shells).toEqual([{ tool_use_id: 's1', label: 'npm run dev', detail: '', started_at: 2_000 }])
  })

  it('never counts a settled card, done or failed', () => {
    const work = runningWorkFrom([
      subagent('a1', 'done'),
      subagent('a2', 'failed'),
      shell('s1', 'done', true),
      shell('s2', 'failed', true),
    ])
    expect(work).toEqual({ agents: [], shells: [] })
  })

  it('counts a foreground shell only while backgrounded: a running foreground shell is the turn, not background work', () => {
    const work = runningWorkFrom([shell('s1', 'running', false)])
    expect(work.shells).toEqual([])
  })

  it('reads a reloaded running card from history, with no start time', () => {
    // A history row carries no start time: the store stamps only live cards.
    const history: WorkCard[] = [
      { ...subagent('a9', 'running'), started_at: undefined },
      { ...shell('s9', 'running', true), started_at: undefined },
    ]
    const work = runningWorkFrom([], history)
    expect(work.agents.map(row => row.tool_use_id)).toEqual(['a9'])
    expect(work.shells.map(row => row.tool_use_id)).toEqual(['s9'])
    expect(work.agents[0].started_at).toBeUndefined()
  })

  it('takes the live card over a history row for the same call, so a card settled live is not counted again', () => {
    const live = [subagent('a1', 'done')]
    const history = [subagent('a1', 'running')]
    expect(runningWorkFrom(live, history).agents).toEqual([])
  })

  it('falls back to a placeholder label when a card has no description or command', () => {
    const work = runningWorkFrom([
      { kind: 'subagent', card: { tool_use_id: 'a', agent_type: '', description: '', state: 'running', step_count: 0, steps: [] } },
      { kind: 'shell', card: { tool_use_id: 's', command: '', background: true, state: 'running', exit_code: null, output_tail: '' } },
    ])
    expect(work.agents[0].label).toBe('Subagent')
    expect(work.shells[0].label).toBe('Shell')
  })
})

describe('elapsedLabel', () => {
  const start = Date.parse('2026-11-20T10:00:00Z')

  it('reads seconds, minutes and hours, and is empty without a start', () => {
    expect(elapsedLabel(start, start + 12_900)).toBe('12s')
    expect(elapsedLabel(start, start + 184_000)).toBe('3m 4s')
    expect(elapsedLabel(start, start + 3_720_000)).toBe('1h 2m')
    expect(elapsedLabel(undefined, start)).toBe('')
  })

  it('never reads negative when the clock is behind the start', () => {
    expect(elapsedLabel(start, start - 5_000)).toBe('0s')
  })
})
