/**
 * Synthetic Workspace care reports, one per state, shaped like
 * `GET /api/care-report`. Shared by the unit and component tests and the
 * Storybook stories. Synthetic values only: no real paths or vault text.
 */
import type { CareReport, CareStep } from './careReport'

export const CARE_WORKSPACE = 'storybook-alpha'

function step(id: string, state: CareStep['state'], what: string, evidence = '', verify = ''): CareStep {
  return { id, state, what, evidence, verify, step_file: state === 'open' ? '' : `/stock/references/${id}.md` }
}

function base(overrides: Partial<CareReport>): CareReport {
  const steps = overrides.steps ?? []
  return {
    status: 'ok',
    modified_at: '2026-11-27T03:12:00',
    updated: '2026-11-27',
    weekly_done: '2026-11-24',
    interrupted_ids: [],
    nothing_due: false,
    malformed: false,
    needs_you: [],
    workspace: CARE_WORKSPACE,
    report_path: `memory-vault/${CARE_WORKSPACE}/Workspace/Care-Report.md`,
    unsure_path: `memory-vault/${CARE_WORKSPACE}/Workspace/Unsure.md`,
    schedule: { id: `system-memory-curation@${CARE_WORKSPACE}`, enabled: true, last_status: 'ok', last_run_at: '2026-11-27T00:01:00+00:00' },
    running: null,
    ...overrides,
    steps,
    counts: {
      total: steps.length,
      done: steps.filter(s => s.state === 'done').length,
      blocked: steps.filter(s => s.state === 'blocked').length,
      open: steps.filter(s => s.state === 'open').length,
    },
  }
}

const fiveDone = [
  step('unsure', 'done', 'apply ticked Unsure lines', 'Filed 3 ticked lines into People and Projects', '0 ticked lines remain'),
  step('regions', 'done', 'consolidate regions', 'Trimmed profile region from 91% to 83%', 'ciao memory status -> pct 83'),
  step('learnings', 'done', 'promote learnings', 'Promoted 2 entries at (x3) into AGENTS.md', 'Learnings.md active 7'),
  step('stale', 'done', 're-check stale notes', 'Checked 4 notes, fixed 1', '3 of 4 still listed'),
  step('hygiene', 'done', 'vault index and audit', '2 dead links reported, none repaired', 'audit exit 1, 2 findings'),
]

export const careReports = {
  complete: base({ steps: fiveDone, weekly_done: '2026-11-27' }),
  needsYou: base({
    steps: fiveDone,
    weekly_done: '2026-11-27',
    needs_you: [
      { title: 'New line in Unsure', detail: '"Synthetic team moved": care could not tell if this replaces the older note.', action: 'unsure' },
      { title: 'Memory guide at 91%', detail: 'Care trimmed what it safely could. The rest needs your pick.', action: 'guide' },
    ],
  }),
  blocked: base({
    steps: [
      step('unsure', 'done', 'apply ticked Unsure lines', 'Nothing due'),
      step('regions', 'done', 'consolidate regions', 'memory 70%', 'pct 70'),
      step('learnings', 'done', 'promote learnings', 'Promoted 2 entries', 'Learnings.md active 7'),
      step('stale', 'done', 're-check stale notes', 'Checked 2 notes', '0 listed'),
      step('hygiene', 'blocked', 'vault index and audit', 'cannot write INDEX.md'),
      step('guide-review', 'blocked', 'review the guide body', 'depends on the index'),
    ],
    weekly_done: '2026-11-17',
    needs_you: [{ title: 'Hygiene is blocked', detail: 'The vault index command exited 2.', action: 'report' }],
  }),
  interrupted: base({
    steps: [
      step('unsure', 'done', 'apply ticked Unsure lines', 'applied 1', '0 left'),
      step('regions', 'done', 'consolidate regions', 'memory 72%', 'pct 72'),
      step('learnings', 'open', 'promote learnings (3 at x3)'),
      step('stale', 'open', 're-check stale notes (weekly due)'),
      step('hygiene', 'open', 'vault index (weekly due)'),
      step('guide-review', 'open', 'review the guide body (weekly due)'),
    ],
  }),
  running: base({
    steps: [
      step('unsure', 'done', 'apply ticked Unsure lines', 'applied 1', '0 left'),
      step('regions', 'done', 'consolidate regions', 'memory 72%', 'pct 72'),
      step('learnings', 'open', 'promote learnings (3 at x3)'),
      step('stale', 'open', 're-check stale notes (weekly due)'),
      step('hygiene', 'open', 'vault index (weekly due)'),
      step('guide-review', 'open', 'review the guide body (weekly due)'),
    ],
    running: { since: '2026-11-27T00:01:00+00:00', step: 'learnings', index: 3, total: 6 },
  }),
  never: base({
    status: 'missing', modified_at: '', updated: '', weekly_done: '', steps: [],
    schedule: { id: `system-memory-curation@${CARE_WORKSPACE}`, enabled: true, last_status: '', last_run_at: '' },
  }),
} satisfies Record<string, CareReport>
