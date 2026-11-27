/**
 * Synthetic Workspace care reports, one per state, shaped like
 * `GET /api/care-report`. Shared by the unit and component tests and the
 * Storybook stories. Synthetic values only: no real paths or vault text.
 */
import type { CareNeed, CareReport, CareStep } from './careReport'

export const CARE_WORKSPACE = 'storybook-alpha'
/** When the fixture reports were written: a UTC instant, as `GET /api/care-report` sends it. */
export const CARE_WRITTEN_AT = '2026-11-27T07:12:00Z'

function step(id: string, state: CareStep['state'], what: string, evidence = '', verify = ''): CareStep {
  return { id, state, what, evidence, verify, step_file: state === 'open' ? '' : `/stock/references/${id}.md` }
}

/** A needs-you item as the route sends it: vault path, viewer path, proposal status. */
export function need(title: string, detail: string, path = '', proposalStatus = ''): CareNeed {
  const viewer = path && path !== 'AGENTS.md' ? `memory-vault/${CARE_WORKSPACE}/${path}` : ''
  return { title, detail, path, open_path: viewer, proposal_status: proposalStatus }
}

/** The proposal the needs-you fixture points at. */
export const CARE_PROPOSAL_PATH = 'Workspace/Proposals/guide-trim-duplicates.md'

function base(overrides: Partial<CareReport>): CareReport {
  const steps = overrides.steps ?? []
  return {
    status: 'ok',
    modified_at: CARE_WRITTEN_AT,
    updated: '2026-11-27',
    weekly_done: '2026-11-24',
    weekly_retry: [],
    interrupted_ids: [],
    nothing_due: false,
    malformed: false,
    needs_you: [],
    notes: [],
    workspace: CARE_WORKSPACE,
    report_path: `memory-vault/${CARE_WORKSPACE}/Workspace/Care-Report.md`,
    unsure_path: `memory-vault/${CARE_WORKSPACE}/Workspace/Unsure.md`,
    schedule: { id: `system-memory-curation@${CARE_WORKSPACE}`, enabled: true, last_status: 'ok', last_run_at: '2026-11-27T06:00:00+00:00' },
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
  // The items of a real run, rewritten to the needs-you standard
  // (curation-report-example.md), with its leftovers as notes.
  needsYou: base({
    steps: fiveDone,
    weekly_done: '2026-11-27',
    needs_you: [
      need('A proposal would trim your guide: it removes an outdated status section and a repeated folder path.', 'Choose Review proposal, then Approve or Reject.', CARE_PROPOSAL_PATH, 'draft'),
      need('Your always-loaded memory is nearly full (98% and 95%) and everything in it still matters.', 'Choose Fix with a chat to trim it together.', 'AGENTS.md'),
      need('13 suggestions are waiting in Unsure, such as which brief template to keep, a project\'s status and a new People page.', 'Open Memory > Unsure and approve or reject them.', 'Workspace/Unsure.md'),
      need('4 notes need a decision the run cannot make alone, such as two notes for one person.', 'Choose Fix with a chat to go through them.', 'Workspace/Care-Report.md'),
    ],
    notes: [
      'Re-checking older notes did not finish; it tries again on the next run.',
      'One suggestion in Unsure waits until memory has room.',
      '17 memory passes were not re-read this time; the next run continues.',
      'About 200 older patterns are still to be moved to Unsure; the next run continues.',
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
    weekly_retry: [{ id: 'hygiene', runs: 2 }, { id: 'guide-review', runs: 1 }],
    notes: ['Hygiene did not finish; it runs again on the next run.'],
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
    running: { since: '2026-11-27T06:00:00+00:00', step: 'learnings', index: 3, total: 6 },
  }),
  never: base({
    status: 'missing', modified_at: '', updated: '', weekly_done: '', steps: [],
    schedule: { id: `system-memory-curation@${CARE_WORKSPACE}`, enabled: true, last_status: '', last_run_at: '' },
  }),
} satisfies Record<string, CareReport>
