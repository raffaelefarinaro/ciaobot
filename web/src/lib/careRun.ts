// The derived outcome of a Workspace care schedule's last run (#3056 S3).
// Pure apart from the injectable GET: no Vue imports, so it is tested directly.

import { api } from './api'

/** The derived counts the engine stores on the care run that owns a chat. */
export interface CareRunOutcome {
  run_id: string
  planned: number
  completed: number
  deferred: number
  failed: number
  counts_complete: boolean
  reasons: string[]
}

const CARE_ROUTINE = 'system-memory-curation'

/** The nightly care routine, and its per-workspace copies (`system-memory-curation@<name>`). */
export function isCareSchedule(scheduleId: string): boolean {
  return scheduleId === CARE_ROUTINE || scheduleId.startsWith(`${CARE_ROUTINE}@`)
}

export function careRunPath(scheduleId: string): string {
  return `/api/schedules/${encodeURIComponent(scheduleId)}/care-run`
}

export function careRunCountsLine(run: CareRunOutcome): string {
  return `Planned ${run.planned} · completed ${run.completed} · deferred ${run.deferred} · failed ${run.failed}`
}

/** The outcome for the schedule's last run, or null when none is recorded for it. */
export async function fetchCareRun(
  scheduleId: string,
  get: (path: string) => Promise<unknown> = api.get,
): Promise<CareRunOutcome | null> {
  // The body is untrusted until its shape is checked here.
  const body = await get(careRunPath(scheduleId))
  if (!body || typeof body !== 'object' || !('care_run' in body)) {
    throw new Error('unexpected care run response')
  }
  return (body.care_run as CareRunOutcome | null) ?? null
}
