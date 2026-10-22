// @vitest-environment jsdom
//
// Settings -> "Memory handover", mounted on its own. The card owns its data:
// `api`, the confirm dialog and the project store are the only inputs.
//
// What is pinned: it never saves by itself (a suggestion is text, not a click),
// choosing "all" asks first, and its four failure/empty states stay distinct.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SettingsMemoryHandover from '../settings/SettingsMemoryHandover.vue'
import { useProjectStore } from '../../stores/projects'

const apiGet = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const askConfirm = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, patch: apiPatch },
}))
vi.mock('../../lib/confirm', () => ({ askConfirm }))

function agreement(overrides: Record<string, unknown> = {}) {
  return {
    decided: 0, matched: 0, rate: null, window: 50, min_decisions: 20, threshold: 0.9,
    predicted_accept: 0, accept_matched: 0, predicted_dismiss: 0, dismiss_matched: 0,
    suggest_handover: false,
    ...overrides,
  }
}

function response(overrides: Record<string, unknown> = {}, agree: Record<string, unknown> = {}) {
  return {
    workspace: 'personal',
    handover: 'off',
    acting_available: true,
    agreement: agreement(agree),
    ...overrides,
  }
}

const mounted: VueWrapper[] = []

async function mountCard() {
  const view = mount(SettingsMemoryHandover)
  mounted.push(view)
  await flushPromises()
  return view
}

const RADIO_LABEL: Record<string, string> = { off: 'Off', dismiss: 'Dismiss', all: 'Dismiss and accept' }

function radio(view: VueWrapper, value: string) {
  return view.findAll('button[role="radio"]').find((b) => b.text() === RADIO_LABEL[value])!
}

function isChecked(view: VueWrapper, value: string): boolean {
  return radio(view, value).attributes('aria-checked') === 'true'
}

let projectStore: ReturnType<typeof useProjectStore>

beforeEach(() => {
  setActivePinia(createPinia())
  projectStore = useProjectStore()
  projectStore.activeWorkspace = 'personal'
  projectStore.updateWorkspace = vi.fn().mockResolvedValue({}) as never
  apiGet.mockReset().mockResolvedValue(response())
  apiPatch.mockReset()
  askConfirm.mockReset().mockResolvedValue(true)
})

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
})

describe('SettingsMemoryHandover', () => {
  it("fetches the active workspace's agreement on mount", async () => {
    await mountCard()
    expect(apiGet).toHaveBeenCalledWith('/api/proposals/handover?workspace=personal')
  })

  it('shows the rate and per-verdict breakdown', async () => {
    apiGet.mockResolvedValue(response({}, {
      decided: 25, matched: 23, rate: 0.92,
      predicted_dismiss: 15, dismiss_matched: 14, predicted_accept: 10, accept_matched: 9,
    }))
    const view = await mountCard()
    expect(view.text()).toContain('agreed 92%')
    expect(view.text()).toContain('last 25 decisions')
    expect(view.text()).toContain('predicted dismiss 15 times and was right 14')
    expect(view.text()).toContain('predicted accept 10 times and was right 9')
  })

  it('shows the empty state when no decision has a prediction', async () => {
    const view = await mountCard()
    expect(view.text()).toContain('No decisions with a prediction yet')
  })

  it('shows the suggestion only when the server says so, and never saves on its own', async () => {
    apiGet.mockResolvedValue(response({}, { decided: 20, matched: 20, rate: 1, suggest_handover: true }))
    const view = await mountCard()
    expect(view.text()).toContain('You could let it dismiss suggestions it predicts')
    expect(view.text()).not.toContain('matched your decision')
    expect(projectStore.updateWorkspace).not.toHaveBeenCalled()
    expect(apiPatch).not.toHaveBeenCalled()
    expect(isChecked(view, 'off')).toBe(true)

    apiGet.mockResolvedValue(response())
    const quiet = await mountCard()
    expect(quiet.text()).not.toContain('You could let it')
  })

  it('selecting Dismiss saves memory_handover alone and refetches', async () => {
    const view = await mountCard()
    apiGet.mockClear()
    apiGet.mockResolvedValue(response({ handover: 'dismiss' }))
    await radio(view, 'dismiss').trigger('click')
    await flushPromises()
    expect(projectStore.updateWorkspace).toHaveBeenCalledWith('personal', { memory_handover: 'dismiss' })
    expect(askConfirm).not.toHaveBeenCalled()
    expect(apiGet).toHaveBeenCalledTimes(1)
    expect(isChecked(view, 'dismiss')).toBe(true)
  })

  it('selecting Dismiss and accept asks first and does nothing when declined', async () => {
    const view = await mountCard()
    askConfirm.mockResolvedValue(false)
    await radio(view, 'all').trigger('click')
    await flushPromises()
    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(projectStore.updateWorkspace).not.toHaveBeenCalled()
    expect(isChecked(view, 'off')).toBe(true)

    askConfirm.mockResolvedValue(true)
    await radio(view, 'all').trigger('click')
    await flushPromises()
    expect(projectStore.updateWorkspace).toHaveBeenCalledWith('personal', { memory_handover: 'all' })
  })

  it('states the consequence of all: always-loaded memory, Undo, five per run', async () => {
    const view = await mountCard()
    apiGet.mockResolvedValue(response({ handover: 'all' }))
    await radio(view, 'all').trigger('click')
    await flushPromises()
    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(askConfirm.mock.calls[0][0]).toContain('History')
    expect(askConfirm.mock.calls[0][0]).toContain('undone')
    const hint = view.find('.handover-hint').text()
    expect(hint).toContain('always-loaded memory')
    expect(hint).toContain('Undo')
    expect(hint).toContain('5 changes per run')
  })

  it('says plainly when handover cannot act on this install', async () => {
    apiGet.mockResolvedValue(response({ acting_available: false, handover: 'dismiss' }))
    const view = await mountCard()
    expect(view.text()).toContain('cannot act on this install')
    expect(view.text()).toContain('no installed service that registers this workspace at this vault')
    // Off stays reachable so a saved dismiss/all can always be turned back off.
    expect(radio(view, 'off').attributes('disabled')).toBeUndefined()
    expect(radio(view, 'dismiss').attributes('disabled')).toBeDefined()
    expect(radio(view, 'all').attributes('disabled')).toBeDefined()
    await radio(view, 'off').trigger('click')
    await flushPromises()
    expect(projectStore.updateWorkspace).toHaveBeenCalledWith('personal', { memory_handover: 'off' })
    expect(view.text()).toContain('No decisions with a prediction yet')
  })

  it('does not warn when acting is available', async () => {
    const view = await mountCard()
    expect(view.text()).not.toContain('cannot act on this install')
  })

  it('a failed save shows its own error and restores the saved choice', async () => {
    projectStore.updateWorkspace = vi.fn().mockRejectedValue(new Error('disk full')) as never
    const view = await mountCard()
    await radio(view, 'dismiss').trigger('click')
    await flushPromises()
    const alert = view.find('[role="alert"]')
    expect(alert.text()).toContain('disk full')
    expect(isChecked(view, 'off')).toBe(true)
    expect(view.text()).not.toContain('Could not read')
  })

  it('a first-load failure shows the error and a retry, and no empty-state claim', async () => {
    apiGet.mockRejectedValueOnce(new Error('boom'))
    const view = await mountCard()
    expect(view.text()).toContain('boom')
    expect(view.text()).not.toContain('No decisions with a prediction yet')
    expect(view.find('button[role="radio"]').exists()).toBe(false)
    apiGet.mockResolvedValue(response())
    await view.findAll('button').find((b) => b.text() === 'Try again')!.trigger('click')
    await flushPromises()
    expect(view.text()).toContain('No decisions with a prediction yet')
  })

  it('a failed refresh keeps the last rate and marks it stale', async () => {
    apiGet.mockResolvedValue(response({}, { decided: 25, matched: 23, rate: 0.92 }))
    const view = await mountCard()
    apiGet.mockRejectedValue(new Error('offline'))
    projectStore.activeWorkspace = 'personal'
    await view.findAll('button').find((b) => b.text() === 'Refresh')!.trigger('click')
    await flushPromises()
    expect(view.text()).toContain('agreed 92%')
    expect(view.text()).toContain('Could not refresh: offline')
  })

  it('switching workspace refetches and ignores a late response for the old one', async () => {
    let resolveOld: (v: unknown) => void = () => {}
    apiGet.mockReturnValueOnce(new Promise((r) => { resolveOld = r }))
    const view = mount(SettingsMemoryHandover)
    mounted.push(view)
    await flushPromises()
    apiGet.mockResolvedValue(response({ workspace: 'work', handover: 'dismiss' }, { decided: 3, matched: 3, rate: 1 }))
    projectStore.activeWorkspace = 'work'
    await flushPromises()
    expect(apiGet).toHaveBeenLastCalledWith('/api/proposals/handover?workspace=work')
    resolveOld(response({ workspace: 'personal', handover: 'all' }, { decided: 9, matched: 1, rate: 0.1 }))
    await flushPromises()
    expect(isChecked(view, 'dismiss')).toBe(true)
    expect(view.text()).toContain('agreed 100%')
  })

  it('renders a three-way segmented radiogroup like Theme', async () => {
    const view = await mountCard()
    const group = view.find('[role="radiogroup"]')
    expect(group.attributes('aria-label')).toBe('Memory handover')
    expect(group.classes()).toContain('instance-toggle')
    const buttons = view.findAll('button.toggle-btn[role="radio"]')
    expect(buttons).toHaveLength(3)
    expect(view.text()).toContain('Dismiss and accept')
    expect(buttons.filter((b) => b.attributes('tabindex') === '0')).toHaveLength(1)
    expect(radio(view, 'off').attributes('tabindex')).toBe('0')
  })

  it('arrow keys move the selection and save', async () => {
    const view = await mountCard()
    apiGet.mockResolvedValue(response({ handover: 'dismiss' }))
    await radio(view, 'off').trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    expect(projectStore.updateWorkspace).toHaveBeenCalledWith('personal', { memory_handover: 'dismiss' })
    expect(askConfirm).not.toHaveBeenCalled()
    expect(isChecked(view, 'dismiss')).toBe(true)

    askConfirm.mockResolvedValue(false)
    await radio(view, 'dismiss').trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(isChecked(view, 'dismiss')).toBe(true)
    expect(isChecked(view, 'all')).toBe(false)
  })

  it('Home and End go to the first and last enabled option', async () => {
    apiGet.mockResolvedValue(response({ handover: 'dismiss' }))
    const view = await mountCard()
    await radio(view, 'dismiss').trigger('keydown', { key: 'Home' })
    await flushPromises()
    expect(projectStore.updateWorkspace).toHaveBeenLastCalledWith('personal', { memory_handover: 'off' })

    apiGet.mockResolvedValue(response({ handover: 'off', acting_available: false }))
    const locked = await mountCard()
    await radio(locked, 'off').trigger('keydown', { key: 'End' })
    await flushPromises()
    // Dismiss and accept are disabled, so End lands on the last enabled one: Off itself.
    expect(projectStore.updateWorkspace).toHaveBeenCalledTimes(1)
  })

  it('all but Off are disabled when acting is unavailable', async () => {
    apiGet.mockResolvedValue(response({ acting_available: false }))
    const view = await mountCard()
    expect(radio(view, 'off').attributes('disabled')).toBeUndefined()
    expect(radio(view, 'dismiss').attributes('disabled')).toBeDefined()
    expect(radio(view, 'all').attributes('disabled')).toBeDefined()
  })
})
