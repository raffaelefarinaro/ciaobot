// Grouping and phrasing for Settings → Automations.
//
// The page used to render every job as one uniform row carrying a status
// badge, two capability chips, a success rate and a run count — a lot of
// detail that never answered the two questions a user actually has: is
// anything broken, and when does this thing run? These helpers sort the rows
// by what needs a decision and turn the telemetry into sentences.

import type { AutomationProcess, JobRun } from './types'

export type AutomationHealth = 'error' | 'ok' | 'idle' | 'never'

/** Health of a job from its most recent run. */
export function automationHealth(item: AutomationProcess): AutomationHealth {
  const last = item.last_run
  if (!last) return 'never'
  if (last.status === 'error') return 'error'
  if (last.status === 'skipped') return 'idle'
  return 'ok'
}

/** Everything nested under a row: bulk variants and pipeline steps alike. */
function nested(item: AutomationProcess): AutomationProcess[] {
  return [...(item.sub_jobs || []), ...(item.steps || [])]
}

/**
 * Health of a job including its bulk variants and pipeline steps: a failed
 * insights backfill is a failure of Session insights as far as the user is
 * concerned, and a failed memory-proposals step is a failure of the archive
 * pipeline. Either would otherwise hide inside a row badged as healthy.
 */
export function overallHealth(item: AutomationProcess): AutomationHealth {
  if (automationHealth(item) === 'error') return 'error'
  if (nested(item).some((sub) => automationHealth(sub) === 'error')) return 'error'
  return automationHealth(item)
}

/**
 * The entry a row should report on: the failing one when something failed,
 * so the status line and error text describe the actual problem.
 */
export function attentionSource(item: AutomationProcess): AutomationProcess {
  if (automationHealth(item) === 'error') return item
  return nested(item).find((sub) => automationHealth(sub) === 'error') || item
}

/** One line of a pipeline's step list. */
export interface PipelineStep {
  job: string
  label: string
  health: AutomationHealth
  /** When this step is skipped, in the user's terms. '' for the owning job. */
  condition: string
  running: boolean
}

/**
 * The steps of a pipeline, owning job first.
 *
 * The job that owns the pipeline is a step of it too — Session insights is the
 * first thing that runs when you archive a chat — so it belongs in this list
 * rather than being represented only by the group heading. Returns [] for a job
 * that is not a pipeline, which is how callers decide whether to render a list.
 */
export function pipelineSteps(item: AutomationProcess): PipelineStep[] {
  const steps = item.steps || []
  if (!steps.length) return []
  const asStep = (entry: AutomationProcess, condition: string): PipelineStep => ({
    job: entry.job,
    label: entry.label,
    health: automationHealth(entry),
    condition,
    running: !!entry.running,
  })
  return [
    asStep(item, ''),
    ...steps.map((step) => asStep(step, step.step_condition || '')),
  ]
}

/** True when any step of this row is running right now. */
export function isRunningNow(item: AutomationProcess): boolean {
  return !!item.running || nested(item).some((sub) => !!sub.running)
}

/**
 * A one-time migration that already ran is history, not a live automation:
 * it keeps a green badge forever and nothing will ever change it.
 */
export function isSettledOneTime(item: AutomationProcess): boolean {
  return !!item.one_time && overallHealth(item) !== 'error'
}

export interface AutomationGroups {
  /** Failed last time — the only rows that need the user to do something. */
  attention: AutomationProcess[]
  /** Ran fine, or is waiting for its trigger. */
  healthy: AutomationProcess[]
  /** Settled one-time migrations, folded away behind a disclosure. */
  settled: AutomationProcess[]
}

export function groupAutomations(items: AutomationProcess[]): AutomationGroups {
  const groups: AutomationGroups = { attention: [], healthy: [], settled: [] }
  for (const item of items) {
    if (overallHealth(item) === 'error') groups.attention.push(item)
    else if (isSettledOneTime(item)) groups.settled.push(item)
    else groups.healthy.push(item)
  }
  return groups
}

/** Header sentence: the answer to "is anything broken?" in one line. */
export function automationHeadline(items: AutomationProcess[]): string {
  if (items.length === 0) return 'No automations recorded yet.'
  const { attention, healthy, settled } = groupAutomations(items)
  const live = attention.length + healthy.length
  if (attention.length === 0) {
    const suffix = settled.length ? `, ${settled.length} one-time` : ''
    const subject = live === 1 ? '1 automation healthy' : `All ${live} automations healthy`
    return `${subject}${suffix}.`
  }
  const names = attention.map((item) => item.label).join(', ')
  const verb = attention.length === 1 ? 'needs' : 'need'
  return `${attention.length} of ${live} automations ${verb} attention: ${names}.`
}

/**
 * When a job last ran, in words. Never-run jobs say so instead of showing an
 * empty cell — "never" plus the trigger explains an idle row on its own.
 */
export function lastRunSentence(
  item: AutomationProcess,
  formatRelative: (iso: string) => string,
): string {
  const source = attentionSource(item)
  const last = source.last_run
  // A bulk variant's failure is named, so "Failed" cannot be read as the
  // parent's own last run having failed.
  const what = source === item ? '' : `${source.label}: `
  if (!last) return 'Never run'
  const when = formatRelative(last.ended_at || last.started_at)
  if (last.status === 'error') return what ? `${what}failed ${when}` : `Failed ${when}`
  if (last.status === 'skipped') return `Nothing to do ${when}`
  return `Ran ${when}`
}

/** Short "why was this skipped" / "what did it do" line for a run. */
export function runOutcome(run: JobRun): string {
  const extra: Record<string, unknown> = run.extra || {}
  const text = (value: unknown): string => (typeof value === 'string' ? value : '')
  const skipReason = text(extra.skip_reason)
  if (run.status === 'skipped' && skipReason) return `Skipped: ${skipReason}`
  const summary = text(extra.summary) || text(extra.message)
  if (summary) return summary
  if (run.status === 'error') return run.error || 'Failed'
  return ''
}

/**
 * Candidate models for a one-off retry, flattened from the routing table the
 * Models tab already uses: `{ provider: { tier: modelId } }`. Values are real
 * model ids, so the run does exactly what the label says.
 */
export interface RetryModelOption {
  value: string
  label: string
}

const TIER_ORDER = ['haiku', 'sonnet', 'opus', 'fable']

export function retryModelOptions(
  aliasTiers: Record<string, Record<string, string>> | undefined,
  providerLabels: Record<string, string> = {},
): RetryModelOption[] {
  const out: RetryModelOption[] = []
  const seen = new Set<string>()
  for (const [provider, tiers] of Object.entries(aliasTiers || {})) {
    const label = providerLabels[provider] || provider
    const keys = Object.keys(tiers || {}).sort(
      (a, b) => TIER_ORDER.indexOf(a) - TIER_ORDER.indexOf(b),
    )
    for (const tier of keys) {
      const model = tiers[tier]
      if (!model || seen.has(model)) continue
      seen.add(model)
      out.push({ value: model, label: `${label} · ${tier} — ${model}` })
    }
  }
  return out
}
