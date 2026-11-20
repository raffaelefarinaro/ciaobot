// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProjectStore } from '../projects'
import type { ChatMessage } from '../../lib/types'

// The live timeline is private to the store; these cases read the history rows a chat
// reopened mid-run carries. The pure rules (live over history, running only) are in lib/runningWork.test.ts.
describe('projects store runningWorkFor (history rows)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('reads a reloaded running subagent and background shell, and skips settled ones', () => {
    const store = useProjectStore()
    store.messages['c1'] = [
      { role: 'system', content: 'Subagent: Explore', tool_name: '_subagent', subagent: { tool_use_id: 'a1', agent_type: 'Explore', description: 'Explore', state: 'running', step_count: 0, steps: [] } },
      { role: 'system', content: 'npm run dev', tool_name: '_shell', shell: { tool_use_id: 's1', command: 'npm run dev', background: true, state: 'running', exit_code: null, output_tail: '' } },
      { role: 'system', content: 'Subagent: Old', tool_name: '_subagent', subagent: { tool_use_id: 'a2', agent_type: '', description: 'Old', state: 'done', step_count: 0, steps: [] } },
      { role: 'system', content: 'ls', tool_name: '_shell', shell: { tool_use_id: 's2', command: 'ls', background: false, state: 'running', exit_code: null, output_tail: '' } },
    ] as ChatMessage[]

    const work = store.runningWorkFor('c1')
    expect(work.agents.map(row => row.tool_use_id)).toEqual(['a1'])
    expect(work.shells.map(row => row.tool_use_id)).toEqual(['s1'])
    expect(work.agents[0].started_at).toBeUndefined()
  })

  it('is empty for a chat with no rows', () => {
    const store = useProjectStore()
    expect(store.runningWorkFor('nobody')).toEqual({ agents: [], shells: [] })
  })
})
