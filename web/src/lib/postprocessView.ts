// Phrasing for the post-archive pipeline.
//
// Archiving a chat starts one background task that extracts insights, folds the
// project doc, saves a trajectory and files memory proposals. None of that used
// to be visible anywhere in the app. These helpers turn the raw per-step
// telemetry into the two sentences a user actually wants:
//
//   while it runs   → "extracting insights…"
//   once it settles → "insights added · project doc updated · 3 memory proposals"
//
// The settled line is the important half: it stays on the archived chat as the
// permanent record of what Ciaobot took from that conversation.

import type { ChatPostprocess, ChatPostprocessStep } from './types'

/** Execution order, matching REGISTRY order in ciao/job_runs.py. */
export const POSTPROCESS_STEPS = [
  'insights',
  'project_doc_update',
  'trajectory',
  'memory_proposals',
] as const

/** Present tense, for the step currently running. */
const RUNNING_LABEL: Record<string, string> = {
  insights: 'extracting insights',
  project_doc_update: 'folding into project doc',
  trajectory: 'saving trajectory',
  memory_proposals: 'proposing memories',
}

/** What to call a step that failed. Named by its subject, not its verb. */
const FAILED_LABEL: Record<string, string> = {
  insights: 'insights failed',
  project_doc_update: 'project doc failed',
  trajectory: 'trajectory failed',
  memory_proposals: 'memory proposals failed',
}

export function isPostprocessing(pp: ChatPostprocess | null | undefined): boolean {
  return !!pp && pp.state === 'running'
}

/**
 * The line shown while the pipeline runs. Falls back to a generic phrase rather
 * than an empty string: the signal is visible, so it must always say something.
 */
export function postprocessLabel(pp: ChatPostprocess | null | undefined): string {
  if (!isPostprocessing(pp)) return ''
  const step = pp?.step || ''
  return RUNNING_LABEL[step] || 'tidying up'
}

function stepExtra(step: ChatPostprocessStep | undefined): Record<string, unknown> {
  return (step?.extra || {}) as Record<string, unknown>
}

function countOf(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : 0
}

/**
 * What came out of the pipeline, as short past-tense fragments in execution
 * order. Steps that were skipped contribute nothing — "nothing to do" is not
 * worth a fragment — but failures always appear, because this line is the only
 * place a user would ever see one.
 */
export function postprocessOutcomes(pp: ChatPostprocess | null | undefined): string[] {
  if (!pp) return []
  const steps = pp.steps || {}
  const out: string[] = []
  for (const job of POSTPROCESS_STEPS) {
    const step = steps[job]
    if (!step) continue
    if (step.status === 'error') {
      out.push(FAILED_LABEL[job] || `${job} failed`)
      continue
    }
    if (step.status === 'skipped') continue
    const extra = stepExtra(step)
    switch (job) {
      case 'insights':
        out.push('insights added')
        break
      case 'project_doc_update':
        // `wrote: false` means the model found no material change worth folding.
        if (extra.wrote) out.push('project doc updated')
        break
      case 'trajectory':
        out.push('trajectory saved')
        break
      case 'memory_proposals': {
        const proposed = countOf(extra.proposals)
        const promoted = countOf(extra.promoted)
        if (proposed) {
          out.push(`${proposed} memory proposal${proposed === 1 ? '' : 's'}`)
        }
        if (promoted) {
          out.push(`${promoted} memory saved`)
        }
        break
      }
    }
  }
  return out
}

/** The settled one-liner, or '' when the pipeline produced nothing to report. */
export function postprocessSummary(pp: ChatPostprocess | null | undefined): string {
  if (!pp || pp.state !== 'done') return ''
  const outcomes = postprocessOutcomes(pp)
  if (outcomes.length) return outcomes.join(' · ')
  // A pipeline that ran but produced nothing durable is still worth one word:
  // silence here reads as "this never ran", which is a different fact.
  return pp.interrupted ? 'tidy-up interrupted by a restart' : 'nothing durable to save'
}

/** True when any step failed — the one case that deserves more than grey. */
export function postprocessFailed(pp: ChatPostprocess | null | undefined): boolean {
  const steps = pp?.steps || {}
  return Object.values(steps).some((step) => step?.status === 'error')
}

/**
 * "2 tidying up" for a lane header, or '' when nothing is running. Plural
 * handled here so the callers stay markup-only.
 */
export function tidyingSummary(count: number): string {
  if (count < 1) return ''
  return `${count} tidying up`
}
