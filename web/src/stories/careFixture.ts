/**
 * Workspace care fixture routes: the declared `GET /api/care-report` read for
 * the synthetic workspace, answered with one of the shared synthetic reports,
 * and the care schedule's run-now. Synthetic values only.
 */
import type { CareReport } from '../lib/careReport'
import { CARE_WORKSPACE, careReports } from '../lib/careReportFixture'
import type { FixtureRoute } from './fixtureBoundary'
import { held, json } from './homeWindowsFixture'

export { careReports, CARE_WORKSPACE }

export const careReportPath = `/api/care-report?workspace=${CARE_WORKSPACE}`
export const careRunPath = `/api/schedule-run/system-memory-curation@${CARE_WORKSPACE}`

/** The care read, answering `report`, never answering, or failing. */
export function careRoute(report: CareReport | 'held' | 'failed'): FixtureRoute {
  return {
    method: 'GET',
    path: careReportPath,
    respond: () => report === 'held' ? held() : report === 'failed'
      ? json({ error: 'Synthetic care read failure' }, 503)
      : json(report),
  }
}

/** The care schedule's run-now, accepted, and the automations re-read that follows it. */
export function careRunRoutes(): FixtureRoute[] {
  return [
    { method: 'POST', path: careRunPath, respond: () => json({ schedule_id: `system-memory-curation@${CARE_WORKSPACE}`, status: 'ok' }, 201) },
    { method: 'GET', path: '/api/schedules', respond: () => json([]) },
  ]
}
