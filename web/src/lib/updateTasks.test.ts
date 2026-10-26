import { describe, expect, it } from 'vitest'
import source from './updateTasks.ts?raw'
import { hasLiveAttempt, isVisibleUpdateTask, visibleUpdateTasks } from './updateTasks'

const STATUSES = ['offered', 'in_progress', 'waiting_review', 'failed', 'completed', 'dismissed', 'future-state']
const APPLICABILITY = ['applicable', 'not_applicable', 'unknown']
const LIVE = ['in_progress', 'waiting_review', 'failed']

function expected(status: string, applicability: string): boolean {
  if (status === 'completed' || status === 'dismissed') return false
  if (LIVE.includes(status)) return true
  return applicability !== 'not_applicable'
}

describe('updateTasks', () => {
  const matrix = STATUSES.flatMap(status => APPLICABILITY.map(applicability => [status, applicability] as const))

  it.each(matrix)('visibility_matrix %s x %s', (status, applicability) => {
    expect(isVisibleUpdateTask({ status, applicability })).toBe(expected(status, applicability))
  })

  it('visible_update_tasks_filters_and_keeps_order', () => {
    const rows = [
      { id: 'a', status: 'offered', applicability: 'applicable' },
      { id: 'b', status: 'completed', applicability: 'applicable' },
      { id: 'c', status: 'failed', applicability: 'not_applicable' },
      { id: 'd', status: 'offered', applicability: 'not_applicable' },
      { id: 'e', status: 'offered', applicability: 'unknown' },
    ]
    const snapshot = [...rows]
    const out = visibleUpdateTasks(rows)
    expect(out.map(r => r.id)).toEqual(['a', 'c', 'e'])
    expect(rows).toEqual(snapshot)
    expect(out).not.toBe(rows)
  })

  it('has_live_attempt', () => {
    for (const status of STATUSES) expect(hasLiveAttempt(status)).toBe(LIVE.includes(status))
  })

  it('lib_is_vue_free', () => {
    expect(source).not.toContain("from 'vue'")
    expect(source).not.toContain("'pinia'")
    expect(source).not.toContain('./api')
  })
})
