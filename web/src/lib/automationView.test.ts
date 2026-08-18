import { describe, expect, it } from 'vitest'
import type { AutomationProcess, JobRun } from './types'
import {
  attentionSource,
  automationHeadline,
  automationHealth,
  groupAutomations,
  isRunningNow,
  lastRunSentence,
  overallHealth,
  pipelineSteps,
  retryModelOptions,
  runOutcome,
} from './automationView'

function run(over: Partial<JobRun> = {}): JobRun {
  return {
    job: 'insights',
    label: 'Session insights',
    category: 'content',
    started_at: '2026-08-03T20:00:00+00:00',
    ended_at: '2026-08-03T20:06:14+00:00',
    duration_ms: 374000,
    status: 'ok',
    model: 'sonnet',
    provider: 'claude',
    error: null,
    extra: {},
    ...over,
  }
}

function job(over: Partial<AutomationProcess> = {}): AutomationProcess {
  return {
    job: 'insights',
    label: 'Session insights',
    category: 'content',
    description: 'Extracts durable insights from an archived session transcript.',
    trigger: 'When a chat is archived.',
    last_run: run(),
    recent: [run()],
    stats: { total_runs: 1, success_rate: 1, avg_duration_ms: 374000, last_error: null },
    ...over,
  }
}

describe('automationHealth', () => {
  it('reads health from the most recent run', () => {
    expect(automationHealth(job())).toBe('ok')
    expect(automationHealth(job({ last_run: run({ status: 'error' }) }))).toBe('error')
    expect(automationHealth(job({ last_run: run({ status: 'skipped' }) }))).toBe('idle')
    expect(automationHealth(job({ last_run: null }))).toBe('never')
  })
})

describe('overallHealth', () => {
  it('reports a failing bulk variant as the parent failing', () => {
    const insights = job({
      sub_jobs: [
        job({ job: 'backfill_insights', label: 'Insights backfill', last_run: run({ status: 'error' }) }),
      ],
    })

    expect(automationHealth(insights)).toBe('ok')
    expect(overallHealth(insights)).toBe('error')
    expect(attentionSource(insights).job).toBe('backfill_insights')
  })

  it('names the failing variant so the row is not misread', () => {
    const insights = job({
      sub_jobs: [
        job({ job: 'backfill_insights', label: 'Insights backfill', last_run: run({ status: 'error' }) }),
      ],
    })

    expect(lastRunSentence(insights, () => '2 days ago')).toBe(
      'Insights backfill: failed 2 days ago',
    )
  })

  it('prefers the job’s own failure over a variant’s', () => {
    const insights = job({
      last_run: run({ status: 'error' }),
      sub_jobs: [job({ job: 'backfill_insights', last_run: run({ status: 'error' }) })],
    })

    expect(attentionSource(insights).job).toBe('insights')
  })
})

describe('groupAutomations', () => {
  it('puts failures first and folds away settled one-time migrations', () => {
    const failing = job({ job: 'insights', last_run: run({ status: 'error' }) })
    const healthy = job({ job: 'title', label: 'Title generation' })
    const migration = job({ job: 'memory_migration', label: 'Legacy memory migration', one_time: true })

    const groups = groupAutomations([healthy, migration, failing])

    expect(groups.attention.map((i) => i.job)).toEqual(['insights'])
    expect(groups.healthy.map((i) => i.job)).toEqual(['title'])
    expect(groups.settled.map((i) => i.job)).toEqual(['memory_migration'])
  })

  it('keeps a failed one-time migration in the attention group', () => {
    const migration = job({ job: 'memory_migration', one_time: true, last_run: run({ status: 'error' }) })

    const groups = groupAutomations([migration])

    expect(groups.attention).toHaveLength(1)
    expect(groups.settled).toHaveLength(0)
  })
})

describe('automationHeadline', () => {
  it('names what is broken', () => {
    const failing = job({ label: 'Session insights', last_run: run({ status: 'error' }) })
    const healthy = job({ job: 'title', label: 'Title generation' })

    expect(automationHeadline([failing, healthy])).toBe(
      '1 of 2 automations needs attention: Session insights.',
    )
  })

  it('says everything is fine, counting one-time migrations separately', () => {
    const healthy = job()
    const migration = job({ job: 'memory_migration', one_time: true })

    expect(automationHeadline([healthy, migration])).toBe(
      '1 automation healthy, 1 one-time.',
    )
  })

  it('handles an empty log', () => {
    expect(automationHeadline([])).toBe('No automations recorded yet.')
  })
})

describe('lastRunSentence', () => {
  const relative = () => '2 hours ago'

  it('describes the last run in words', () => {
    expect(lastRunSentence(job(), relative)).toBe('Ran 2 hours ago')
    expect(lastRunSentence(job({ last_run: run({ status: 'error' }) }), relative)).toBe(
      'Failed 2 hours ago',
    )
    expect(lastRunSentence(job({ last_run: run({ status: 'skipped' }) }), relative)).toBe(
      'Nothing to do 2 hours ago',
    )
    expect(lastRunSentence(job({ last_run: null }), relative)).toBe('Never run')
  })
})

describe('runOutcome', () => {
  it('prefers the skip reason, then the summary, then the error', () => {
    expect(runOutcome(run({ status: 'skipped', extra: { skip_reason: 'client mode' } })))
      .toBe('Skipped: client mode')
    expect(runOutcome(run({ extra: { summary: 'Backfilled 3 archives' } })))
      .toBe('Backfilled 3 archives')
    expect(runOutcome(run({ status: 'error', error: 'TimeoutError' }))).toBe('TimeoutError')
    expect(runOutcome(run())).toBe('')
  })
})

describe('retryModelOptions', () => {
  it('flattens provider model lists into concrete model ids', () => {
    const options = retryModelOptions(
      {
        ollama: ['qwen3-coder:30b', 'qwen3:4b'],
        openrouter: ['anthropic/claude-sonnet-5'],
      },
      { ollama: 'Ollama (local)', openrouter: 'OpenRouter' },
    )

    expect(options.map((o) => o.value)).toEqual([
      'qwen3-coder:30b',
      'qwen3:4b',
      'anthropic/claude-sonnet-5',
    ])
    expect(options[0].label).toBe('Ollama (local) — qwen3-coder:30b')
  })

  it('drops duplicate model ids and survives a missing table', () => {
    const options = retryModelOptions({
      ollama: ['qwen3:4b', 'qwen3:4b'],
    })

    expect(options).toHaveLength(1)
    expect(retryModelOptions(undefined)).toEqual([])
  })
})

describe('pipelineSteps', () => {
  const pipeline = () => job({
    pipeline_label: 'When you archive a chat',
    steps: [
      job({
        job: 'project_doc_update',
        label: 'Project doc update',
        step_condition: 'if the chat belongs to a real project',
      }),
      job({
        job: 'trajectory',
        label: 'Trajectory capture',
        step_condition: 'always — also runs standalone',
      }),
      job({
        job: 'memory_proposals',
        label: 'Memory proposals',
        step_condition: 'if insights produced output',
      }),
    ],
  })

  it('lists the owning job first: it is the first step, not just the heading', () => {
    expect(pipelineSteps(pipeline()).map(step => step.job)).toEqual([
      'insights',
      'project_doc_update',
      'trajectory',
      'memory_proposals',
    ])
  })

  it('carries each step condition, and none for the owning job', () => {
    const steps = pipelineSteps(pipeline())
    expect(steps[0].condition).toBe('')
    expect(steps[1].condition).toBe('if the chat belongs to a real project')
  })

  it('is empty for a job that is not a pipeline, so callers render no list', () => {
    expect(pipelineSteps(job({ job: 'title', label: 'Title generation' }))).toEqual([])
  })
})

describe('health with pipeline steps', () => {
  it('reports a failed step as a failure of the pipeline', () => {
    // A failed memory-proposals step must not hide inside a row badged healthy.
    const item = job({
      steps: [job({
        job: 'memory_proposals',
        label: 'Memory proposals',
        last_run: run({ status: 'error', error: 'boom' }),
      })],
    })
    expect(overallHealth(item)).toBe('error')
    expect(attentionSource(item).job).toBe('memory_proposals')
  })

  it('stays healthy when every step is healthy', () => {
    const item = job({ steps: [job({ job: 'trajectory', label: 'Trajectory capture' })] })
    expect(overallHealth(item)).toBe('ok')
    expect(attentionSource(item).job).toBe('insights')
  })

  it('names the failing step instead of blaming the pipeline', () => {
    const item = job({
      steps: [job({
        job: 'trajectory',
        label: 'Trajectory capture',
        last_run: run({ status: 'error' }),
      })],
    })
    expect(lastRunSentence(item, () => '2 hours ago')).toBe(
      'Trajectory capture: failed 2 hours ago',
    )
  })
})

describe('isRunningNow', () => {
  it('is true when the job or any nested step is running', () => {
    expect(isRunningNow(job({ running: true }))).toBe(true)
    expect(isRunningNow(job({ steps: [job({ job: 'trajectory', running: true })] }))).toBe(true)
    expect(isRunningNow(job())).toBe(false)
  })
})
