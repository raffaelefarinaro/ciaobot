import { describe, expect, it } from 'vitest'
import {
  isPostprocessing,
  postprocessFailed,
  postprocessLabel,
  postprocessNeedsRetry,
  postprocessOutcomes,
  postprocessSummary,
  postprocessUnfinished,
  tidyingSummary,
} from './postprocessView'
import type { ChatPostprocess } from './types'

function running(step: string): ChatPostprocess {
  return { state: 'running', step, expected: ['trajectory'], steps: {} }
}

describe('postprocessLabel', () => {
  it('names the step that is running', () => {
    expect(postprocessLabel(running('trajectory'))).toBe('saving trajectory')
  })

  it('always says something for an unknown or missing step', () => {
    // The signal is already visible at this point, so an empty label would
    // render a dot with no explanation.
    expect(postprocessLabel(running('something_new'))).toBe('tidying up')
    expect(postprocessLabel(running(''))).toBe('tidying up')
  })

  it('says nothing once the pipeline has settled', () => {
    expect(postprocessLabel({ state: 'done', step: 'trajectory' })).toBe('')
    expect(postprocessLabel(null)).toBe('')
  })
})

describe('isPostprocessing', () => {
  it('is true only while the pipeline is alive', () => {
    expect(isPostprocessing(running('trajectory'))).toBe(true)
    expect(isPostprocessing({ state: 'done' })).toBe(false)
    expect(isPostprocessing(undefined)).toBe(false)
  })
})

describe('postprocessOutcomes', () => {
  it('reports what the pipeline produced', () => {
    const done: ChatPostprocess = {
      state: 'done',
      steps: { trajectory: { status: 'ok', extra: { path: '/x.json' } } },
    }
    expect(postprocessOutcomes(done)).toEqual(['trajectory saved'])
  })

  it('ignores a step the manifest never planned', () => {
    // A chat archived by an older build carries the removed stages. They are
    // history, not outcomes this pipeline produced.
    const legacy: ChatPostprocess = {
      state: 'done',
      steps: {
        insights: { status: 'ok' },
        project_doc_update: { status: 'ok', extra: { wrote: true } },
        memory_proposals: { status: 'ok', extra: { proposals: 3 } },
        trajectory: { status: 'ok' },
      },
    }
    expect(postprocessOutcomes(legacy)).toEqual(['trajectory saved'])
  })

  it('drops a skipped step but never drops a failure', () => {
    const skipped: ChatPostprocess = {
      state: 'done',
      steps: { trajectory: { status: 'skipped' } },
    }
    expect(postprocessOutcomes(skipped)).toEqual([])

    const failed: ChatPostprocess = {
      state: 'done',
      steps: { trajectory: { status: 'error' } },
    }
    // This line is the only place in the app a user would see the failure.
    expect(postprocessOutcomes(failed)).toEqual(['trajectory failed'])
  })

  it('ignores steps that have not finished yet', () => {
    expect(postprocessOutcomes(running('trajectory'))).toEqual([])
  })
})

describe('postprocessSummary', () => {
  it('joins the outcomes into one line', () => {
    const pp: ChatPostprocess = {
      state: 'done',
      steps: { trajectory: { status: 'ok' } },
    }
    expect(postprocessSummary(pp)).toBe('trajectory saved')
  })

  it('stays silent while the pipeline is still running', () => {
    // The live label covers that state; two lines at once would contradict.
    expect(postprocessSummary(running('trajectory'))).toBe('')
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

  it('reports partial completion as unfinished work, not success', () => {
    // The whole point of the manifest: a crash before the trajectory landed must
    // not read as "everything settled cleanly".
    const pp: ChatPostprocess = {
      state: 'incomplete',
      steps: { trajectory: { status: 'error' } },
      job: {
        job_id: 'j',
        state: 'incomplete',
        unfinished: ['trajectory'],
      },
    }
    expect(postprocessSummary(pp)).toBe('trajectory failed · trajectory not finished')
  })

  it('names the blocked reason when a job needs attention', () => {
    const pp: ChatPostprocess = {
      state: 'blocked',
      blocked_reason: 'archive content changed since the job was created',
    }
    expect(postprocessSummary(pp)).toBe('archive content changed since the job was created')
  })
})

describe('postprocessFailed', () => {
  it('is true when any step errored', () => {
    expect(postprocessFailed({ state: 'done', steps: { trajectory: { status: 'error' } } })).toBe(true)
    expect(postprocessFailed({ state: 'done', steps: { trajectory: { status: 'ok' } } })).toBe(false)
    expect(postprocessFailed(null)).toBe(false)
  })
})

describe('postprocessNeedsRetry', () => {
  it('is true for a settled pipeline that still has work', () => {
    expect(
      postprocessNeedsRetry({
        state: 'incomplete',
        steps: { trajectory: { status: 'error' } },
        job: { job_id: 'j', state: 'incomplete', unfinished: ['trajectory'] },
      }),
    ).toBe(true)
    expect(postprocessNeedsRetry({ state: 'blocked' })).toBe(true)
  })

  it('is false while running or when everything settled', () => {
    // A running pipeline is still trying, not a retry case.
    expect(postprocessNeedsRetry(running('trajectory'))).toBe(false)
    expect(postprocessNeedsRetry({ state: 'done', steps: { trajectory: { status: 'ok' } } })).toBe(false)
    expect(postprocessNeedsRetry({ state: 'done', steps: { trajectory: { status: 'skipped' } } })).toBe(false)
    expect(postprocessNeedsRetry(null)).toBe(false)
  })
})

describe('postprocessUnfinished', () => {
  it('reads the manifest unfinished list', () => {
    expect(
      postprocessUnfinished({
        state: 'incomplete',
        job: { job_id: 'j', state: 'incomplete', unfinished: ['trajectory'] },
      }),
    ).toEqual(['trajectory'])
    expect(postprocessUnfinished({ state: 'done' })).toEqual([])
    expect(postprocessUnfinished(null)).toEqual([])
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
