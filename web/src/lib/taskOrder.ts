/**
 * The task board's card order, as pure helpers.
 *
 * The server owns the baseline order (one id sequence per status); the client
 * never sorts. These helpers read that order, place a card by anchors in the
 * filtered (visible) list, and mirror the server's placement on an optimistic
 * copy. The filtered-insertion rule is the same one the server applies in
 * server's task-order placement (anchors `before_id` / `after_id` resolved
 * against the full sequence, hidden cards keeping their relative order); keep
 * the two textually aligned.
 */

import type { Task, TaskOrder, TaskStatus } from './types'

const STATUSES: readonly TaskStatus[] = ['backlog', 'in_progress', 'in_review', 'done']

export interface PlacementAnchors {
  before_id: string | null
  after_id: string | null
}

export const EMPTY_TASK_ORDER: TaskOrder = {
  version: 0,
  sequences: { backlog: [], in_progress: [], in_review: [], done: [] },
}

/** Read `order` off a `/api/tasks` payload; anything malformed degrades to empty. */
export function taskOrderFrom(json: unknown): TaskOrder {
  const raw = (json && typeof json === 'object' ? json : {}) as { version?: unknown; sequences?: unknown }
  const sequences = (raw.sequences && typeof raw.sequences === 'object' ? raw.sequences : {}) as Record<string, unknown>
  const out = { backlog: [], in_progress: [], in_review: [], done: [] } as Record<TaskStatus, string[]>
  for (const status of STATUSES) {
    const list = sequences[status]
    out[status] = Array.isArray(list) ? list.filter((id): id is string => typeof id === 'string') : []
  }
  const version = typeof raw.version === 'number' && Number.isInteger(raw.version) ? raw.version : 0
  return { version, sequences: out }
}

/** Rows in `sequence` order; unlisted rows follow, in input order. Does not mutate. */
export function orderTasks(tasks: Task[], sequence: readonly string[]): Task[] {
  const rank = new Map<string, number>()
  sequence.forEach((id, i) => {
    if (!rank.has(id)) rank.set(id, i)
  })
  const listed: Array<{ task: Task; rank: number }> = []
  const unlisted: Task[] = []
  for (const task of tasks) {
    const r = rank.get(task.id)
    if (r === undefined) unlisted.push(task)
    else listed.push({ task, rank: r })
  }
  listed.sort((a, b) => a.rank - b.rank)
  return [...listed.map((entry) => entry.task), ...unlisted]
}

function clamp(index: number, length: number): number {
  return Math.max(0, Math.min(length, index))
}

/**
 * Anchors for dropping `draggedId` at `index` among the visible ids (the dragged
 * id is removed first): the next visible card, else the previous, else append.
 */
export function placementAnchors(
  visibleIds: readonly string[],
  draggedId: string,
  index: number,
): PlacementAnchors {
  const visible = visibleIds.filter((id) => id !== draggedId)
  const at = clamp(index, visible.length)
  const before = visible[at]
  if (before !== undefined) return { before_id: before, after_id: null }
  const after = visible[at - 1]
  if (after !== undefined) return { before_id: null, after_id: after }
  return { before_id: null, after_id: null }
}

/** `sequence` with `id` placed by `anchors`; `null` when an anchor is not in it. */
export function applyPlacement(
  sequence: readonly string[],
  id: string,
  anchors: PlacementAnchors,
): string[] | null {
  const rest = sequence.filter((x) => x !== id)
  if (anchors.before_id !== null) {
    const at = rest.indexOf(anchors.before_id)
    if (at < 0) return null
    rest.splice(at, 0, id)
    return rest
  }
  if (anchors.after_id !== null) {
    const at = rest.indexOf(anchors.after_id)
    if (at < 0) return null
    rest.splice(at + 1, 0, id)
    return rest
  }
  rest.push(id)
  return rest
}

/**
 * The optimistic order after moving `id` from one status to another. `version`
 * is left as is; the server's response stamps the new one. `null` when an anchor
 * is missing from the destination.
 */
export function applyMoveToOrder(
  order: TaskOrder,
  id: string,
  _fromStatus: TaskStatus,
  toStatus: TaskStatus,
  anchors: PlacementAnchors,
): TaskOrder | null {
  const placed = applyPlacement(order.sequences[toStatus], id, anchors)
  if (placed === null) return null
  const sequences = {} as Record<TaskStatus, string[]>
  for (const status of STATUSES) {
    sequences[status] = status === toStatus
      ? placed
      : order.sequences[status].filter((x) => x !== id)
  }
  return { version: order.version, sequences }
}

/** Anchors for Move up (-1) or Move down (1) in the visible list; `null` at a boundary. */
export function stepAnchors(
  visibleIds: readonly string[],
  id: string,
  direction: -1 | 1,
): PlacementAnchors | null {
  const i = visibleIds.indexOf(id)
  if (i < 0) return null
  if (direction === -1) {
    return i === 0 ? null : { before_id: visibleIds[i - 1]!, after_id: null }
  }
  return i === visibleIds.length - 1 ? null : { before_id: null, after_id: visibleIds[i + 1]! }
}

export function canStep(visibleIds: readonly string[], id: string, direction: -1 | 1): boolean {
  return stepAnchors(visibleIds, id, direction) !== null
}

/** `ids` as a drag preview draws them: `draggedId` taken out and put at `index`. */
export function previewIds(ids: readonly string[], draggedId: string, index: number): string[] {
  const rest = ids.filter((id) => id !== draggedId)
  rest.splice(clamp(index, rest.length), 0, draggedId)
  return rest
}
