import { describe, expect, it } from 'vitest'
import { taskRailLabel, taskRailMark } from './taskBoard'

describe('taskRailMark', () => {
  it('reads the agent band: needs you, working, and a result to review as news', () => {
    expect(taskRailMark({ status: 'in_progress', attempt_state: 'needs_you' })).toBe('needs')
    expect(taskRailMark({ status: 'in_progress', attempt_state: 'running' })).toBe('working')
    expect(taskRailMark({ status: 'in_review', attempt_state: 'ready_for_review' })).toBe('unread')
    expect(taskRailMark({ status: 'backlog', attempt_state: '' })).toBeNull()
    expect(taskRailMark({ status: 'in_progress', attempt_state: 'failed' })).toBeNull()
    // A done task carries no band, so no mark.
    expect(taskRailMark({ status: 'done', attempt_state: 'running' })).toBeNull()
  })
})

describe('taskRailLabel', () => {
  it('names the title, the lane and the state', () => {
    expect(taskRailLabel('Plan', 'To do', null)).toBe('Plan, To do')
    expect(taskRailLabel('Plan', 'In progress', 'needs')).toBe('Plan, In progress, needs you')
    expect(taskRailLabel('Plan', 'In review', 'unread')).toBe('Plan, In review, ready to review')
  })
})
