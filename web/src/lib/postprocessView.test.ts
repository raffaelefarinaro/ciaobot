import { describe, expect, it } from 'vitest'
import {
  isPostprocessing,
  postprocessFailed,
  postprocessLabel,
  postprocessNeedsInsights,
  postprocessOutcomes,
  postprocessSummary,
  tidyingSummary,
} from './postprocessView'
import type { ChatPostprocess } from './types'

function running(step: string): ChatPostprocess {
  return { state: 'running', step, expected: ['insights'], steps: {} }
}

describe('postprocessLabel', () => {
  it('names the step that is running', () => {
    expect(postprocessLabel(running('insights'))).toBe('extracting insights')
    expect(postprocessLabel(running('project_doc_update'))).toBe('folding into project doc')
    expect(postprocessLabel(running('trajectory'))).toBe('saving trajectory')
    expect(postprocessLabel(running('memory_proposals'))).toBe('proposing memories')
  })

  it('always says something for an unknown or missing step', () => {
    // The signal is already visible at this point, so an empty label would
    // render a dot with no explanation.
    expect(postprocessLabel(running('something_new'))).toBe('tidying up')
    expect(postprocessLabel(running(''))).toBe('tidying up')
  })

  it('says nothing once the pipeline has settled', () => {
    expect(postprocessLabel({ state: 'done', step: 'insights' })).toBe('')
    expect(postprocessLabel(null)).toBe('')
  })
})

describe('isPostprocessing', () => {
  it('is true only while the pipeline is alive', () => {
    expect(isPostprocessing(running('insights'))).toBe(true)
    expect(isPostprocessing({ state: 'done' })).toBe(false)
    expect(isPostprocessing(undefined)).toBe(false)
  })
})

describe('postprocessOutcomes', () => {
  const full: ChatPostprocess = {
    state: 'done',
    steps: {
      insights: { status: 'ok' },
      project_doc_update: { status: 'ok', extra: { wrote: true } },
      trajectory: { status: 'ok', extra: { path: '/x.json' } },
      memory_proposals: { status: 'ok', extra: { proposals: 3, promoted: 1 } },
    },
  }

  it('reports what the pipeline produced, in execution order', () => {
    expect(postprocessOutcomes(full)).toEqual([
      'insights added',
      'project doc updated',
      'trajectory saved',
      '3 memory proposals',
      '1 memory saved',
    ])
  })

  it('counts proposals rather than reporting a bare boolean', () => {
    const one: ChatPostprocess = {
      state: 'done',
      steps: { memory_proposals: { status: 'ok', extra: { proposals: 1 } } },
    }
    expect(postprocessOutcomes(one)).toEqual(['1 memory proposal'])
  })

  it('omits a project doc that had no material change to fold', () => {
    const noop: ChatPostprocess = {
      state: 'done',
      steps: {
        insights: { status: 'ok' },
        project_doc_update: { status: 'ok', extra: { wrote: false } },
      },
    }
    expect(postprocessOutcomes(noop)).toEqual(['insights added'])
  })

  it('drops skipped steps but never drops a failure', () => {
    const mixed: ChatPostprocess = {
      state: 'done',
      steps: {
        insights: { status: 'error' },
        trajectory: { status: 'ok' },
        memory_proposals: { status: 'skipped' },
      },
    }
    // This line is the only place in the app a user would see the failure.
    expect(postprocessOutcomes(mixed)).toEqual(['insights failed', 'trajectory saved'])
  })

  it('ignores steps that have not finished yet', () => {
    expect(postprocessOutcomes(running('insights'))).toEqual([])
  })
})

describe('postprocessSummary', () => {
  it('joins the outcomes into one line', () => {
    const pp: ChatPostprocess = {
      state: 'done',
      steps: {
        insights: { status: 'ok' },
        trajectory: { status: 'ok' },
      },
    }
    expect(postprocessSummary(pp)).toBe('insights added · trajectory saved')
  })

  it('stays silent while the pipeline is still running', () => {
    // The live label covers that state; two lines at once would contradict.
    expect(postprocessSummary(running('insights'))).toBe('')
  })

  it('says a pipeline produced nothing rather than rendering blank', () => {
    // Blank reads as "this never ran", which is a different fact.
    expect(postprocessSummary({ state: 'done', steps: {} })).toBe('nothing durable to save')
  })

  it('explains a pipeline a restart killed', () => {
    expect(postprocessSummary({ state: 'done', steps: {}, interrupted: true }))
      .toBe('tidy-up interrupted by a restart')
  })

  it('is empty for a chat archived before this existed', () => {
    expect(postprocessSummary(null)).toBe('')
  })
})

describe('postprocessFailed', () => {
  it('is true when any step errored', () => {
    expect(postprocessFailed({ state: 'done', steps: { insights: { status: 'error' } } })).toBe(true)
    expect(postprocessFailed({ state: 'done', steps: { insights: { status: 'ok' } } })).toBe(false)
    expect(postprocessFailed(null)).toBe(false)
  })
})

describe('postprocessNeedsInsights', () => {
  it('is true only for a settled pipeline whose insights step failed', () => {
    expect(postprocessNeedsInsights({ state: 'done', steps: { insights: { status: 'error' } } })).toBe(true)
    // A running pipeline is still trying, not a retry case.
    expect(postprocessNeedsInsights(running('insights'))).toBe(false)
    // Insights skipped (e.g. the archive predates the pipeline) is not
    // something a retry button would fix.
    expect(postprocessNeedsInsights({ state: 'done', steps: { insights: { status: 'skipped' } } })).toBe(false)
    // Insights succeeded — nothing to retry.
    expect(postprocessNeedsInsights({ state: 'done', steps: { insights: { status: 'ok' } } })).toBe(false)
    // A different step failing (say the project doc) is not an insights failure.
    expect(postprocessNeedsInsights({ state: 'done', steps: { project_doc_update: { status: 'error' } } })).toBe(false)
    expect(postprocessNeedsInsights(null)).toBe(false)
  })
})

describe('tidyingSummary', () => {
  it('reads as a count, with no plural bug at one', () => {
    expect(tidyingSummary(1)).toBe('1 tidying up')
    expect(tidyingSummary(2)).toBe('2 tidying up')
  })

  it('is empty when nothing is running, so no fragment renders', () => {
    expect(tidyingSummary(0)).toBe('')
    expect(tidyingSummary(-1)).toBe('')
  })
})
