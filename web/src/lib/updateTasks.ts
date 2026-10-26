import type { UpdateTaskRow } from './types'

/** The lifecycles where an attempt exists: a chat was opened, is waiting on the
 *  operator, or did not get going. Every state question below turns on this one
 *  list, because it is the difference between "nobody has started this" and
 *  "somebody already did". */
export const LIVE_ATTEMPT_STATES: readonly string[] = ['in_progress', 'waiting_review', 'failed']

export function hasLiveAttempt(status: string): boolean {
  return LIVE_ATTEMPT_STATES.includes(status)
}

/** Whether Home puts this update task in front of the operator.
 *
 * Three kinds of thing survive, and everything else is somebody else's history:
 *
 *  - a task the operator already decided about is not news. `dismissed` is
 *    hidden in this scope (and reopenable from Settings → Overview → Update task history),
 *    and `completed` is a verdict a completion check reached rather than
 *    something to re-offer.
 *  - an offer that *applies* here is new work, and so is one nobody could rule
 *    on yet (`unknown`): a detector that has not run has not established that
 *    there is nothing to do.
 *  - a live attempt (`in_progress`, `waiting_review`, `failed`) always stays.
 *    Its chat is open or needs a decision, which is not a thing to hide.
 *
 * What is deliberately gone is an offer the detector says does *not* apply.
 * Every install ships the whole catalog, so drawing those would put a permanent
 * "nothing to do" card on the Home of everyone who does not need the task — and
 * an install with no work would no longer have an empty Home. */
export function isVisibleUpdateTask(t: Pick<UpdateTaskRow, 'status' | 'applicability'>): boolean {
  if (t.status === 'completed' || t.status === 'dismissed') return false
  if (hasLiveAttempt(t.status)) return true
  return t.applicability !== 'not_applicable'
}

export function visibleUpdateTasks<T extends Pick<UpdateTaskRow, 'status' | 'applicability'>>(rows: readonly T[]): T[] {
  return rows.filter(isVisibleUpdateTask)
}
