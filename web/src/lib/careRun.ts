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
  if (scheduleId === CARE_ROUTINE) return true
  const prefix = `${CARE_ROUTINE}@`
  return scheduleId.startsWith(prefix) && scheduleId.length > prefix.length
}

export function careRunPath(scheduleId: string): string {
  return `/api/schedules/${encodeURIComponent(scheduleId)}/care-run`
}

export type CareRunCounts = Pick<CareRunOutcome, 'planned' | 'completed' | 'deferred' | 'failed'>

export function careRunCountsLine(run: CareRunCounts): string {
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

// -- The exact-dispatch report (#3648) ----------------------------------------

export type CareRunState = 'ok' | 'pending' | 'superseded' | 'ambiguous' | 'unavailable'
export type CareJobStatus = 'ok' | 'error' | 'skipped'

/** One item's folded outcome, as the engine stores it (note text never travels). */
export interface CareRunItem {
  item_key: string
  pass_id: string
  status: string
  detail: string
  receipt_ids: string[]
}

export interface CareRunReport {
  state: CareRunState
  care_run: CareRunOutcome | null
  /** False for a run stored before the report existed, or whose stored report was invalid. */
  report_available: boolean
  job_status?: CareJobStatus
  run_id?: string
  counts?: CareRunCounts
  outcomes?: CareRunItem[]
  weekly?: { marker: string }
}

const STATES: readonly string[] = ['ok', 'pending', 'superseded', 'ambiguous', 'unavailable']
const JOB_STATUSES: readonly string[] = ['ok', 'error', 'skipped']

export function careRunReportPath(scheduleId: string, dispatchId: string): string {
  return `${careRunPath(scheduleId)}?dispatch_id=${encodeURIComponent(dispatchId)}`
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === 'object' && !Array.isArray(value)

function validCounts(value: unknown): value is CareRunCounts {
  return isRecord(value) && (['planned', 'completed', 'deferred', 'failed'] as const)
    .every(key => typeof value[key] === 'number' && Number.isFinite(value[key]))
}

function validItem(value: unknown): value is CareRunItem {
  return isRecord(value)
    && typeof value.item_key === 'string'
    && typeof value.pass_id === 'string'
    && typeof value.status === 'string'
    && typeof value.detail === 'string'
    && Array.isArray(value.receipt_ids)
}

/** The report for exactly one schedule dispatch. A malformed body throws. */
export async function fetchCareRunReport(
  scheduleId: string,
  dispatchId: string,
  get: (path: string) => Promise<unknown> = api.get,
): Promise<CareRunReport> {
  const body = await get(careRunReportPath(scheduleId, dispatchId))
  if (!isRecord(body) || typeof body.state !== 'string' || !STATES.includes(body.state)
    || typeof body.report_available !== 'boolean') {
    throw new Error('unexpected care run response')
  }
  if (body.state !== 'ok') return body as unknown as CareRunReport
  if (typeof body.job_status !== 'string' || !JOB_STATUSES.includes(body.job_status) || !validCounts(body.counts)
    || (body.outcomes !== undefined && !(Array.isArray(body.outcomes) && body.outcomes.every(validItem)))
    || (body.weekly !== undefined && !(isRecord(body.weekly) && typeof body.weekly.marker === 'string'))) {
    throw new Error('unexpected care run response')
  }
  return body as unknown as CareRunReport
}

/** Outstanding work, in the order a reader should act on it. */
const OUTSTANDING: readonly { status: string; label: string }[] = [
  { status: 'failed', label: 'Failed' },
  { status: 'conflict', label: 'Conflict' },
  { status: 'incomplete', label: 'Incomplete' },
  { status: 'blocked', label: 'Blocked' },
  { status: 'unfinished', label: 'Unfinished' },
  { status: 'deferred_budget', label: 'Deferred by budget' },
  { status: 'queued', label: 'Queued for review' },
]

export interface OutstandingGroup {
  status: string
  label: string
  items: CareRunItem[]
}

/** The outstanding items grouped by reason; settled items are not listed. */
export function outstandingGroups(outcomes: readonly CareRunItem[]): OutstandingGroup[] {
  return OUTSTANDING
    .map(({ status, label }) => ({ status, label, items: outcomes.filter(item => item.status === status) }))
    .filter(group => group.items.length > 0)
}

const SETTLED: readonly { status: string; label: string }[] = [
  { status: 'applied', label: 'applied' },
  { status: 'done_unverified', label: 'done, not verified' },
  { status: 'no_change', label: 'no change needed' },
  { status: 'rolled_back', label: 'rolled back' },
  { status: 'undone', label: 'undone' },
]

/** "applied 2 · no change needed 1" for the settled items, or '' when there are none. */
export function settledLine(outcomes: readonly CareRunItem[]): string {
  return SETTLED
    .map(({ status, label }) => ({ label, n: outcomes.filter(item => item.status === status).length }))
    .filter(entry => entry.n > 0)
    .map(entry => `${entry.label} ${entry.n}`)
    .join(' · ')
}

/** True when every item reports no change: the run looked and found nothing to do. */
export function isNoOp(outcomes: readonly CareRunItem[]): boolean {
  return outcomes.length > 0 && outcomes.every(item => item.status === 'no_change')
}

export function weeklyLine(marker: string): string {
  if (marker === 'advanced') return 'Weekly pass: finished, the weekly marker advanced.'
  if (marker === 'not_advanced') return 'Weekly pass: not finished, the weekly marker did not advance.'
  return `Weekly marker: ${marker}`
}

const PROPOSAL_KEY = /^prv_[a-f0-9]{64}$/

/** The exact Memory Review link for an item that is a review question, else null. */
export function reviewLink(workspace: string, itemKey: string): string | null {
  if (!workspace || !PROPOSAL_KEY.test(itemKey)) return null
  return `/memory/review?show=suggested&workspace=${encodeURIComponent(workspace)}&proposal=${itemKey}`
}
