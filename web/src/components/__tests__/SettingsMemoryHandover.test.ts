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
    memory_approval: {},
    accept_resolved: inherited('approve'),
    acting_available: true,
    agreement: agreement(agree),
    ...overrides,
  }
}

// Both kinds resolved to one mode, inherited from the legacy handover.
function inherited(mode: 'auto' | 'approve') {
  return {
    memory: { mode, inherited: true },
    profile: { mode, inherited: true },
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
    const view = await mountCard()
    expect(apiGet).toHaveBeenCalledWith('/api/proposals/handover?workspace=personal')
    expect(view.text()).toContain('New workspaces start at Dismiss and accept')
    expect(view.text()).toContain('You can change this choice at any time')
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
    const buttons = group.findAll('button.toggle-btn[role="radio"]')
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

  it('keeps the buttons focusable while saving so a second arrow still works', async () => {
    let release: () => void = () => {}
    projectStore.updateWorkspace = vi.fn(() => new Promise<void>((r) => { release = r })) as never
    const view = mount(SettingsMemoryHandover, { attachTo: document.body })
    mounted.push(view)
    await flushPromises()
    const off = radio(view, 'off')
    ;(off.element as HTMLButtonElement).focus()
    await off.trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    // Saving: nothing is natively disabled, so focus is not dropped to BODY.
    const dismiss = radio(view, 'dismiss')
    expect(dismiss.attributes('disabled')).toBeUndefined()
    expect(dismiss.attributes('aria-disabled')).toBe('true')
    expect(document.activeElement).toBe(dismiss.element)
    // A second choice while the first saves is ignored.
    await dismiss.trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    expect(projectStore.updateWorkspace).toHaveBeenCalledTimes(1)
    expect(askConfirm).not.toHaveBeenCalled()
    release()
    await flushPromises()
  })

  it('the first enabled option is the tab stop when the saved choice is disabled', async () => {
    apiGet.mockResolvedValue(response({ handover: 'all', acting_available: false }))
    const view = await mountCard()
    expect(radio(view, 'all').attributes('tabindex')).toBe('-1')
    expect(radio(view, 'off').attributes('tabindex')).toBe('0')
    const handoverGroup = view.find('[role="radiogroup"][aria-label="Memory handover"]')
    expect(handoverGroup.findAll('button[role="radio"]').filter((b) => b.attributes('tabindex') === '0')).toHaveLength(1)
  })

  it('all but Off are disabled when acting is unavailable', async () => {
    apiGet.mockResolvedValue(response({ acting_available: false }))
    const view = await mountCard()
    expect(radio(view, 'off').attributes('disabled')).toBeUndefined()
    expect(radio(view, 'dismiss').attributes('disabled')).toBeDefined()
    expect(radio(view, 'all').attributes('disabled')).toBeDefined()
  })
})

// ── Per-kind acceptance (#3242) ─────────────────────────────────────────────

const KIND_GROUP: Record<string, string> = { memory: 'handover-kind-memory', profile: 'handover-kind-profile' }
const KIND_LABEL: Record<string, string> = { auto: 'Let Workspace care add it', approve: 'Ask me' }

function kindRadio(view: VueWrapper, kind: string, mode: 'auto' | 'approve') {
  const group = view.find(`[aria-labelledby="${KIND_GROUP[kind]}"]`)
  return group.findAll('button[role="radio"]').find((b) => b.text() === KIND_LABEL[mode])!
}

function kindChecked(view: VueWrapper, kind: string, mode: 'auto' | 'approve'): boolean {
  return kindRadio(view, kind, mode).attributes('aria-checked') === 'true'
}

function resetLink(view: VueWrapper, kind: string) {
  return view.findAll('.handover-kind').find((row) => row.find('.handover-kind__label').text().toLowerCase() === kind)!
    .findAll('button').find((b) => b.text() === 'Reset to inherited')
}

describe('SettingsMemoryHandover per-kind acceptance', () => {
  it('shows each kind resolved now and says it is inherited, with no reset while inherited', async () => {
    apiGet.mockResolvedValue(response({ handover: 'all', accept_resolved: inherited('auto') }))
    const view = await mountCard()

    expect(kindChecked(view, 'memory', 'auto')).toBe(true)
    expect(kindChecked(view, 'profile', 'auto')).toBe(true)
    expect(view.text()).toContain('Inherited from the handover above (Dismiss and accept)')
    expect(resetLink(view, 'memory')).toBeUndefined()
  })

  it('shows an explicit kind as set, with a reset to inherited', async () => {
    apiGet.mockResolvedValue(response({
      handover: 'off',
      memory_approval: { memory: 'auto' },
      accept_resolved: { memory: { mode: 'auto', inherited: false }, profile: { mode: 'approve', inherited: true } },
    }))
    const view = await mountCard()

    expect(kindChecked(view, 'memory', 'auto')).toBe(true)
    expect(kindChecked(view, 'profile', 'approve')).toBe(true)
    expect(view.text()).toContain('Set for this kind.')
    expect(resetLink(view, 'memory')).toBeDefined()
    expect(resetLink(view, 'profile')).toBeUndefined()
  })

  it('choosing Let Workspace care add it asks first and saves that one kind only', async () => {
    const view = await mountCard()
    askConfirm.mockResolvedValue(false)
    await kindRadio(view, 'memory', 'auto').trigger('click')
    await flushPromises()
    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(projectStore.updateWorkspace).not.toHaveBeenCalled()
    expect(kindChecked(view, 'memory', 'approve')).toBe(true)

    askConfirm.mockResolvedValue(true)
    apiGet.mockResolvedValue(response({ memory_approval: { memory: 'auto' }, accept_resolved: {
      memory: { mode: 'auto', inherited: false }, profile: { mode: 'approve', inherited: true },
    } }))
    await kindRadio(view, 'memory', 'auto').trigger('click')
    await flushPromises()
    expect(projectStore.updateWorkspace).toHaveBeenCalledTimes(1)
    expect(projectStore.updateWorkspace).toHaveBeenCalledWith('personal', { memory_approval: { memory: 'auto' } })
    expect(kindChecked(view, 'memory', 'auto')).toBe(true)
  })

  it('choosing Ask me saves approve without asking, and the profile row saves its own key', async () => {
    apiGet.mockResolvedValue(response({ handover: 'all', accept_resolved: inherited('auto') }))
    const view = await mountCard()

    await kindRadio(view, 'memory', 'approve').trigger('click')
    await flushPromises()
    expect(askConfirm).not.toHaveBeenCalled()
    expect(projectStore.updateWorkspace).toHaveBeenLastCalledWith('personal', { memory_approval: { memory: 'approve' } })

    await kindRadio(view, 'profile', 'approve').trigger('click')
    await flushPromises()
    expect(projectStore.updateWorkspace).toHaveBeenLastCalledWith('personal', { memory_approval: { profile: 'approve' } })
  })

  it('reset sends null for that kind alone', async () => {
    apiGet.mockResolvedValue(response({
      memory_approval: { memory: 'approve', profile: 'auto' },
      accept_resolved: { memory: { mode: 'approve', inherited: false }, profile: { mode: 'auto', inherited: false } },
    }))
    const view = await mountCard()

    await resetLink(view, 'memory')!.trigger('click')
    await flushPromises()

    expect(projectStore.updateWorkspace).toHaveBeenCalledWith('personal', { memory_approval: { memory: null } })
    expect(askConfirm).not.toHaveBeenCalled()
  })

  it('a refused kind save rolls the row back and shows the server message', async () => {
    projectStore.updateWorkspace = vi.fn().mockRejectedValue(new Error('kind unknown')) as never
    const view = await mountCard()

    askConfirm.mockResolvedValue(true)
    await kindRadio(view, 'profile', 'auto').trigger('click')
    await flushPromises()

    expect(view.find('[role="alert"]').text()).toContain('kind unknown')
    expect(kindChecked(view, 'profile', 'approve')).toBe(true)
    expect(kindChecked(view, 'profile', 'auto')).toBe(false)
  })

  it('without acting available, Let Workspace care add it is disabled and Ask me stays reachable', async () => {
    apiGet.mockResolvedValue(response({ acting_available: false, memory_approval: { memory: 'auto' },
      accept_resolved: { memory: { mode: 'auto', inherited: false }, profile: { mode: 'approve', inherited: true } } }))
    const view = await mountCard()

    expect(kindRadio(view, 'memory', 'auto').attributes('disabled')).toBeDefined()
    expect(kindRadio(view, 'memory', 'approve').attributes('disabled')).toBeUndefined()
    expect(resetLink(view, 'memory')!.attributes('disabled')).toBeUndefined()
    await kindRadio(view, 'memory', 'approve').trigger('click')
    await flushPromises()
    expect(projectStore.updateWorkspace).toHaveBeenCalledWith('personal', { memory_approval: { memory: 'approve' } })
  })

  it('arrow keys on one kind save that kind and never the other', async () => {
    const view = await mountCard()
    askConfirm.mockResolvedValue(true)
    await kindRadio(view, 'profile', 'approve').trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()

    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(projectStore.updateWorkspace).toHaveBeenCalledWith('personal', { memory_approval: { profile: 'auto' } })
    expect(projectStore.updateWorkspace).toHaveBeenCalledTimes(1)
  })

  it('a late failed save for the old workspace never writes its error or rollback into the new one', async () => {
    let fail: (e: Error) => void = () => {}
    projectStore.updateWorkspace = vi.fn(() => new Promise<void>((_, reject) => { fail = reject })) as never
    const view = mount(SettingsMemoryHandover)
    mounted.push(view)
    await flushPromises()
    askConfirm.mockResolvedValue(true)
    await kindRadio(view, 'memory', 'auto').trigger('click')
    await flushPromises()

    // The new workspace resolves its memory row to Let Workspace care add it.
    apiGet.mockResolvedValue(response({
      workspace: 'work', handover: 'dismiss', memory_approval: { memory: 'auto' },
      accept_resolved: { memory: { mode: 'auto', inherited: false }, profile: { mode: 'approve', inherited: true } },
    }))
    projectStore.activeWorkspace = 'work'
    await flushPromises()
    expect(kindChecked(view, 'memory', 'auto')).toBe(true)

    // The old workspace's save now fails. Its rollback must not touch this card.
    fail(new Error('disk full'))
    await flushPromises()

    expect(view.find('[role="alert"]').exists()).toBe(false)
    expect(view.text()).not.toContain('disk full')
    expect(kindChecked(view, 'memory', 'auto')).toBe(true)
    expect(isChecked(view, 'dismiss')).toBe(true)
    expect(apiGet).toHaveBeenLastCalledWith('/api/proposals/handover?workspace=work')
  })
})
