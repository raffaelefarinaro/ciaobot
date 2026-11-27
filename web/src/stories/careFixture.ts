/**
 * Workspace care fixture routes: the declared `GET /api/care-report` read for
 * the synthetic workspace, answered with one of the shared synthetic reports,
 * and the care schedule's run-now. Synthetic values only.
 */
import type { CareReport } from '../lib/careReport'
import { CARE_PROPOSAL_PATH, CARE_WORKSPACE, careReports } from '../lib/careReportFixture'
import type { FixtureRoute } from './fixtureBoundary'
import { held, json } from './homeWindowsFixture'

export { careReports, CARE_PROPOSAL_PATH, CARE_WORKSPACE }

export const careReportPath = `/api/care-report?workspace=${CARE_WORKSPACE}`
export const careRunPath = `/api/schedule-run/system-memory-curation@${CARE_WORKSPACE}`

/**
 * The care read, answering `report`, never answering, or failing. A list
 * answers its reports in turn, the last one repeating (a run that starts).
 */
export function careRoute(report: CareReport | CareReport[] | 'held' | 'failed'): FixtureRoute {
  const queue = Array.isArray(report) ? [...report] : null
  return {
    method: 'GET',
    path: careReportPath,
    respond: () => {
      if (queue) return json(queue.length > 1 ? queue.shift() : queue[0])
      return report === 'held' ? held() : report === 'failed'
        ? json({ error: 'Synthetic care read failure' }, 503)
        : json(report)
    },
  }
}

/** The care schedule's run-now, accepted, and the automations re-read that follows it. */
export function careRunRoutes(): FixtureRoute[] {
  return [
    { method: 'POST', path: careRunPath, respond: () => json({ schedule_id: `system-memory-curation@${CARE_WORKSPACE}`, status: 'ok' }, 201) },
    { method: 'GET', path: '/api/schedules', respond: () => json([]) },
  ]
}

/** The synthetic proposal the needs-you report points at, as its file reads. */
export const CARE_PROPOSAL_FILE = [
  '---',
  'type: guide-edit',
  'target: AGENTS.md',
  'title: Trim the guide',
  'summary: Removes an outdated status section and a repeated folder path from the guide.',
  'rationale: |',
  '  The status section names a synthetic tool nobody uses now.',
  '  The chat-logs folder is written twice, once with an old name.',
  'evidence:',
  '  - Workspace/Care-Report.md',
  '  - https://example.com/synthetic/issue/1',
  '  - chat-synthetic-1',
  'proposed_change: |',
  '  Delete the `## Tool status` section.',
  '',
  '  Keep one line for the chat-logs folder: `Logs/Chats/`.',
  'created: 2026-11-26',
  'status: draft',
  '---',
  '',
  'Both lines are loaded into every chat, so each costs a little on every turn.',
  '',
].join('\n')

export const careProposalFilePath = `/api/workspace-file?exact=1&path=${encodeURIComponent(`memory-vault/${CARE_WORKSPACE}/${CARE_PROPOSAL_PATH}`)}`

/** The proposal's read and the file viewer's save path, which Reject uses. */
export function careProposalRoutes(text = CARE_PROPOSAL_FILE): FixtureRoute[] {
  return [
    { method: 'GET', path: careProposalFilePath, respond: () => new Response(text, { status: 200, headers: { 'Content-Type': 'text/markdown' } }) },
    { method: 'POST', path: '/api/workspace-file', respond: () => json({ ok: true, snapshot: null }) },
  ]
}
