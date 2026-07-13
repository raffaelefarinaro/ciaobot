// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest'
import {
  GETTING_STARTED_ITEMS,
  GETTING_STARTED_STORAGE_KEY,
  clearChecklistDismissed,
  countReadyProviders,
  gettingStartedProgress,
  isChecklistDismissed,
  markChecklistDismissed,
  type GettingStartedState,
} from './gettingStarted'

const FRESH: GettingStartedState = {
  providerReadyCount: 1,
  workspaceCount: 1,
  userProjectCount: 0,
  scheduleCount: 0,
  activeChatCount: 0,
}

const VETERAN: GettingStartedState = {
  providerReadyCount: 2,
  workspaceCount: 2,
  userProjectCount: 3,
  scheduleCount: 1,
  activeChatCount: 5,
}

describe('gettingStarted', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('every item deep-links to a route', () => {
    for (const item of GETTING_STARTED_ITEMS) {
      expect(item.route.startsWith('/')).toBe(true)
      expect(item.cta.length).toBeGreaterThan(0)
    }
  })

  it('a fresh install has everything open except nothing', () => {
    const progress = gettingStartedProgress(FRESH)
    expect(progress.doneCount).toBe(0)
    expect(progress.allDone).toBe(false)
    expect(progress.total).toBe(GETTING_STARTED_ITEMS.length)
  })

  it('items complete from real state', () => {
    const progress = gettingStartedProgress(VETERAN)
    expect(progress.allDone).toBe(true)

    const byId = (state: GettingStartedState) =>
      Object.fromEntries(gettingStartedProgress(state).items.map(i => [i.id, i.done]))

    expect(byId({ ...FRESH, activeChatCount: 1 })['first-chat']).toBe(true)
    expect(byId({ ...FRESH, userProjectCount: 1 })['project']).toBe(true)
    expect(byId({ ...FRESH, workspaceCount: 2 })['workspace']).toBe(true)
    expect(byId({ ...FRESH, scheduleCount: 1 })['schedule']).toBe(true)
    // The setup-wizard provider alone does not complete the provider item.
    expect(byId({ ...FRESH, providerReadyCount: 1 })['provider']).toBe(false)
    expect(byId({ ...FRESH, providerReadyCount: 2 })['provider']).toBe(true)
  })

  it('counts ready providers defensively', () => {
    expect(countReadyProviders(undefined)).toBe(0)
    expect(countReadyProviders(null)).toBe(0)
    expect(countReadyProviders([])).toBe(0)
    expect(countReadyProviders({ providers: 'oops' })).toBe(0)
    expect(
      countReadyProviders({
        providers: {
          claude: { ok: true },
          codex: { ok: false },
          ollama: { ok: true },
          openrouter: null,
        },
      }),
    ).toBe(2)
  })

  it('tracks dismissal in localStorage', () => {
    expect(isChecklistDismissed()).toBe(false)
    markChecklistDismissed()
    expect(isChecklistDismissed()).toBe(true)
    expect(localStorage.getItem(GETTING_STARTED_STORAGE_KEY)).toBe('1')
    clearChecklistDismissed()
    expect(isChecklistDismissed()).toBe(false)
  })
})
