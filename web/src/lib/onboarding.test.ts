// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { gettingStartedProgress, type GettingStartedState } from './gettingStarted'
import { isOnboardingFinished } from './onboarding'

const VETERAN: GettingStartedState = {
  providerReadyCount: 2,
  workspaceCount: 2,
  userProjectCount: 3,
  scheduleCount: 1,
  activeChatCount: 5,
}

describe('onboarding', () => {
  it('is finished only when checklist and tour are both complete', () => {
    const progress = gettingStartedProgress(VETERAN)
    expect(isOnboardingFinished(progress, false)).toBe(false)
    expect(isOnboardingFinished(progress, true)).toBe(true)
  })
})
