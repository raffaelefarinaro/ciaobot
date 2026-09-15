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
const POSTPROCESS_STEPS = [
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

/** Short noun for a still-unfinished step in the settled summary line. */
const STEP_NOUN: Record<string, string> = {
  insights: 'insights',
  project_doc_update: 'project doc',
  trajectory: 'trajectory',
  memory_proposals: 'memory proposals',
}

/** Human-readable noun for a pipeline stage id, for "X not finished" lines. */
export function stepNoun(step: string): string {
  return STEP_NOUN[step] || step
}

export function isPostprocessing(pp: ChatPostprocess | null | undefined): boolean {
  return !!pp && pp.state === 'running'
}

/**
 * True when the pipeline settled with work still to do. A crash or a provider
 * failure leaves a manifest whose unfinished stages can be retried; the state
 * is 'incomplete' (retryable) or 'blocked' (needs a config/human change).
 */
export function isIncomplete(pp: ChatPostprocess | null | undefined): boolean {
  return !!pp && (pp.state === 'incomplete' || pp.state === 'blocked')
}

export function postprocessBlockedReason(
  pp: ChatPostprocess | null | undefined,
): string {
  return pp?.job?.blocked_reason || pp?.blocked_reason || ''
}

/** Stages the manifest still has to finish, in execution order. */
export function postprocessUnfinished(
  pp: ChatPostprocess | null | undefined,
): string[] {
  const job = pp?.job
  if (job?.unfinished?.length) return job.unfinished
  return []
}

/** Steps whose recorded status is an error, in execution order. */
export function postprocessErroredSteps(
  pp: ChatPostprocess | null | undefined,
): string[] {
  const steps = pp?.steps || {}
  return POSTPROCESS_STEPS.filter((job) => steps[job]?.status === 'error')
}

/**
 * Whether this chat's pipeline should offer a retry. A settled pipeline with
 * unfinished or failed stages is the retriggerable case; a pipeline still
 * running is not (a retry would be rejected), and one that never ran insights
 * is nothing a retry would fix.
 */
export function postprocessNeedsRetry(pp: ChatPostprocess | null | undefined): boolean {
  if (!pp || isPostprocessing(pp)) return false
  if (pp.state === 'incomplete') return true
  if (pp.state === 'blocked') return true
  // Back-compat: a legacy record with an errored insights step.
  const step = pp.steps?.['insights']
  return pp.state === 'done' && step?.status === 'error'
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
  if (!pp || (pp.state !== 'done' && pp.state !== 'incomplete' && pp.state !== 'blocked')) {
    return ''
  }
  const outcomes = postprocessOutcomes(pp)
  const unfinished = postprocessUnfinished(pp)
  if (unfinished.length) {
    // Partial completion is the important half of this feature: a crash left
    // work undone, and the line must say what is still missing rather than
    // claim the pipeline settled cleanly.
    const pending = unfinished
      .map((step) => STEP_NOUN[step] || step)
      .join(', ')
    const prefix = outcomes.length ? `${outcomes.join(' · ')} · ` : ''
    return `${prefix}${pending} not finished`
  }
  if (outcomes.length) return outcomes.join(' · ')
  if (pp.state === 'blocked') {
    return postprocessBlockedReason(pp) || 'blocked'
  }
  // A pipeline that ran but produced nothing durable is still worth one word:
  // silence here reads as "this never ran", which is a different fact.
  return pp.interrupted ? 'tidy-up interrupted by a restart' : 'nothing durable to save'
}

/** True when any step failed — the one case that deserves more than grey. */
export function postprocessFailed(pp: ChatPostprocess | null | undefined): boolean {
  if (pp?.state === 'blocked') return true
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
