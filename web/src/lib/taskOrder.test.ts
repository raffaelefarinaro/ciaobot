import { describe, expect, it } from 'vitest'
import {
  EMPTY_TASK_ORDER,
  applyMoveToOrder,
  applyPlacement,
  canStep,
  orderTasks,
  placementAnchors,
  previewIds,
  stepAnchors,
  taskOrderFrom,
} from './taskOrder'
import type { Task, TaskOrder } from './types'

const t = (id: string) => ({ id }) as Task
const ids = (tasks: Task[]) => tasks.map((x) => x.id)

describe('taskOrderFrom', () => {
  it('is defensive about malformed payloads', () => {
    expect(taskOrderFrom(null)).toEqual(EMPTY_TASK_ORDER)
    const order = taskOrderFrom({ version: 1.5, sequences: { backlog: ['a', 3, 'b'], done: 'x' } })
    expect(order.version).toBe(0)
    expect(order.sequences.backlog).toEqual(['a', 'b'])
    expect(order.sequences.done).toEqual([])
    expect(order.sequences.in_review).toEqual([])
  })
})

describe('orderTasks', () => {
  it('ranks by sequence and appends unlisted rows in input order', () => {
    expect(ids(orderTasks([t('x'), t('b'), t('y'), t('a')], ['a', 'b']))).toEqual(['a', 'b', 'x', 'y'])
  })

  it('ignores ids of tasks not in the rows and does not mutate input', () => {
    const rows = [t('b'), t('a')]
    expect(ids(orderTasks(rows, ['ghost', 'a', 'b']))).toEqual(['a', 'b'])
    expect(ids(rows)).toEqual(['b', 'a'])
  })
})

describe('placementAnchors', () => {
  it('uses the next visible item, else the previous, else append', () => {
    expect(placementAnchors(['A', 'B', 'C'], 'X', 1)).toEqual({ before_id: 'B', after_id: null })
    expect(placementAnchors(['A', 'B', 'C'], 'X', 3)).toEqual({ before_id: null, after_id: 'C' })
    expect(placementAnchors(['A', 'B', 'C'], 'X', 99)).toEqual({ before_id: null, after_id: 'C' })
    expect(placementAnchors([], 'X', 0)).toEqual({ before_id: null, after_id: null })
    expect(placementAnchors(['X'], 'X', 0)).toEqual({ before_id: null, after_id: null })
    expect(placementAnchors(['A', 'B', 'C'], 'C', 1)).toEqual({ before_id: 'B', after_id: null })
  })
})

describe('applyPlacement', () => {
  it('filtered insertion keeps hidden items relative order', () => {
    const anchors = placementAnchors(['A', 'B', 'C'], 'C', 1)
    expect(applyPlacement(['A', 'h', 'B', 'C'], 'C', anchors)).toEqual(['A', 'h', 'C', 'B'])
  })

  it('returns null for a missing anchor', () => {
    expect(applyPlacement(['A'], 'B', { before_id: 'zz', after_id: null })).toBeNull()
    expect(applyPlacement(['A'], 'B', { before_id: null, after_id: 'zz' })).toBeNull()
  })

  it('placement into an empty destination appends', () => {
    expect(applyPlacement([], 'A', { before_id: null, after_id: null })).toEqual(['A'])
  })
})

describe('applyMoveToOrder', () => {
  it('moves across statuses, no duplicates anywhere', () => {
    const order: TaskOrder = {
      version: 4,
      sequences: { backlog: ['a', 'b'], in_progress: ['c'], in_review: [], done: [] },
    }
    const next = applyMoveToOrder(order, 'a', 'backlog', 'in_progress', { before_id: 'c', after_id: null })!
    expect(next.sequences.backlog).toEqual(['b'])
    expect(next.sequences.in_progress).toEqual(['a', 'c'])
    expect(next.version).toBe(4)
    expect(order.sequences.backlog).toEqual(['a', 'b'])
    const all = Object.values(next.sequences).flat()
    expect(new Set(all).size).toBe(all.length)
    expect(applyMoveToOrder(order, 'a', 'backlog', 'done', { before_id: 'zz', after_id: null })).toBeNull()
  })
})

describe('stepAnchors', () => {
  it('first and last return null; middle returns the adjacent anchor', () => {
    const v = ['A', 'B', 'C']
    expect(stepAnchors(v, 'A', -1)).toBeNull()
    expect(stepAnchors(v, 'C', 1)).toBeNull()
    expect(stepAnchors(v, 'B', -1)).toEqual({ before_id: 'A', after_id: null })
    expect(stepAnchors(v, 'B', 1)).toEqual({ before_id: null, after_id: 'C' })
    expect(canStep(v, 'B', 1)).toBe(true)
    expect(canStep(v, 'A', -1)).toBe(false)
  })
})

describe('previewIds', () => {
  it('moves the dragged id to the clamped index', () => {
    expect(previewIds(['A', 'B', 'C'], 'A', 2)).toEqual(['B', 'C', 'A'])
    expect(previewIds(['A', 'B', 'C'], 'C', 0)).toEqual(['C', 'A', 'B'])
    expect(previewIds(['A', 'B'], 'A', 99)).toEqual(['B', 'A'])
  })
})
