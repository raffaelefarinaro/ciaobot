// @vitest-environment jsdom
//
// Settings > Workspace > Overview > "Shared brains", mounted on its own. The card
// owns its data: `api`, the confirm dialog and the project store are the only
// inputs. What is pinned: the privacy sentence is always shown, a save sends the
// whole list and re-reads, a refusal keeps the typed text and shows the engine's
// words, removing asks first and names the brain, and a stale reply never lands
// on another workspace.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SettingsSharedBrains from '../settings/SettingsSharedBrains.vue'
import { useProjectStore } from '../../stores/projects'
import { SHARED_BRAIN_PRIVACY } from '../../lib/sharedBrain'
import type { SharedBrainState } from '../../lib/types'

function apiError(message: string, payload?: Record<string, unknown>): Error {
  return Object.assign(new Error(message), payload ? { payload } : {})
}

const apiGet = vi.hoisted(() => vi.fn())
const askConfirm = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: vi.fn(), patch: vi.fn() },
}))
vi.mock('../../lib/confirm', () => ({ askConfirm }))

function state(over: Partial<SharedBrainState> = {}): SharedBrainState {
  return {
    name: 'team-notes',
    identity: 'github.com/acme/team-notes',
    path: '/Users/me/team-notes',
    origin_changed: false,
    missing: false,
    problem: '',
    branch: 'main',
    head: 'abc1234',
    head_date: '2026-11-20',
    dirty: false,
    upstream: true,
    fetched_at: null,
    last_update: { outcome: 'up_to_date', reason: '', detail: '', at: '2026-11-22T10:00:00Z' },
    ...over,
  }
}

const TEAM = state()
const OTHER = state({
  name: 'other-notes',
  identity: 'github.com/acme/other-notes',
  path: '/Users/me/other-notes',
  last_update: { outcome: 'skipped', reason: 'local_changes', detail: '', at: '2026-11-22T10:00:00Z' },
})

const mounted: VueWrapper[] = []

async function mountCard() {
  const view = mount(SettingsSharedBrains)
  mounted.push(view)
  await flushPromises()
  return view
}

function button(view: VueWrapper, label: string) {
  return view.findAll('button').find((b) => b.text() === label)
}

function field(view: VueWrapper, label: string) {
  const row = view.findAll('label').find((l) => l.find('.shared-brain-label').text() === label)
  return row ? row.find('input') : view.find('input[data-absent]')
}

let projectStore: ReturnType<typeof useProjectStore>
let updateWorkspace: ReturnType<typeof vi.fn>

beforeEach(() => {
  setActivePinia(createPinia())
  projectStore = useProjectStore()
  projectStore.activeWorkspace = 'personal'
  apiGet.mockReset().mockResolvedValue({ shared_brains: [] })
  askConfirm.mockReset().mockResolvedValue(true)
  updateWorkspace = vi.fn().mockResolvedValue({ workspaces: [] })
  projectStore.updateWorkspace = updateWorkspace as typeof projectStore.updateWorkspace
})

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
})

describe('SettingsSharedBrains loading and failures', () => {
  it("reads the active workspace's brains on mount", async () => {
    await mountCard()
    expect(apiGet).toHaveBeenCalledWith('/api/workspaces/personal/shared-brains')
  })

  it('shows the privacy sentence with no brains and with brains', async () => {
    const empty = await mountCard()
    expect(empty.text()).toContain(SHARED_BRAIN_PRIVACY)
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM] })
    const listed = await mountCard()
    expect(listed.text()).toContain(SHARED_BRAIN_PRIVACY)
  })

  it('shows a pending read as its own status, never as an empty claim', async () => {
    apiGet.mockReturnValue(new Promise(() => {}))
    const view = mount(SettingsSharedBrains)
    mounted.push(view)
    await flushPromises()
    expect(view.find('[role="status"]').text()).toContain('Checking the shared brains')
    expect(field(view, 'Folder path').exists()).toBe(false)
  })

  it('a first-load failure shows the error and a retry that loads', async () => {
    apiGet.mockRejectedValueOnce(apiError('boom'))
    const view = await mountCard()
    expect(view.find('[role="alert"]').text()).toContain('Could not read the shared brains')
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM] })
    await button(view, 'Try again')!.trigger('click')
    await flushPromises()
    expect(view.text()).toContain('team-notes')
  })
})

describe('SettingsSharedBrains linking', () => {
  it('sends the whole list with the typed path, trimmed and without a name, then re-reads', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM] })
    const view = await mountCard()
    await field(view, 'Folder path').setValue('  /x/y  ')
    await view.find('form').trigger('submit')
    await flushPromises()
    expect(updateWorkspace).toHaveBeenCalledWith('personal', {
      shared_brains: [{ path: TEAM.path, name: TEAM.name }, { path: '/x/y' }],
    })
    expect(apiGet).toHaveBeenCalledTimes(2)
  })

  it('sends the name when one is typed', async () => {
    const view = await mountCard()
    await field(view, 'Folder path').setValue('/x/y')
    await field(view, 'Name (optional)').setValue(' notes ')
    await view.find('form').trigger('submit')
    await flushPromises()
    expect(updateWorkspace).toHaveBeenCalledWith('personal', { shared_brains: [{ path: '/x/y', name: 'notes' }] })
  })

  it("shows the engine's refusal inline in an alert and keeps the typed text", async () => {
    updateWorkspace.mockRejectedValueOnce(apiError('Not a git repository: /x/y', { error: 'Not a git repository: /x/y' }))
    const view = await mountCard()
    await field(view, 'Folder path').setValue('/x/y')
    await view.find('form').trigger('submit')
    await flushPromises()
    expect(view.find('[role="alert"]').text()).toContain('Not a git repository: /x/y')
    expect((field(view, 'Folder path').element as HTMLInputElement).value).toBe('/x/y')
  })
})

describe('SettingsSharedBrains listed brains', () => {
  it('renders one row per brain, each with its own status words', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM, OTHER] })
    const view = await mountCard()
    const rows = view.findAll('.shared-brain-row')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('Up to date')
    expect(rows[1].text()).toContain('Not updated: local changes in the folder')
  })

  it('hides the add form at the limit of five brains', async () => {
    const five = Array.from({ length: 5 }, (_, i) => state({ name: `brain-${i}`, path: `/p/${i}` }))
    apiGet.mockResolvedValueOnce({ shared_brains: five })
    const view = await mountCard()
    expect(field(view, 'Folder path').exists()).toBe(false)
    expect(view.text()).toContain('You can connect up to 5 shared brains. Remove one to add another.')
  })

  it('removing asks first, names the brain, and sends the list without it', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM, OTHER] })
    const view = await mountCard()
    askConfirm.mockResolvedValueOnce(false)
    await view.findAll('.shared-brain-row')[0].findAll('button').find((b) => b.text() === 'Remove')!.trigger('click')
    await flushPromises()
    expect(askConfirm.mock.calls[0][0]).toContain('team-notes')
    expect(updateWorkspace).not.toHaveBeenCalled()

    askConfirm.mockResolvedValueOnce(true)
    apiGet.mockResolvedValueOnce({ shared_brains: [OTHER] })
    await view.findAll('.shared-brain-row')[0].findAll('button').find((b) => b.text() === 'Remove')!.trigger('click')
    await flushPromises()
    expect(updateWorkspace).toHaveBeenCalledWith('personal', { shared_brains: [{ path: OTHER.path, name: OTHER.name }] })
  })

  it('a stale read for a previous workspace never lands on the active one', async () => {
    let answerAlpha: (v: unknown) => void = () => {}
    apiGet.mockReturnValueOnce(new Promise((resolve) => { answerAlpha = resolve }))
    const view = mount(SettingsSharedBrains)
    mounted.push(view)
    await flushPromises()
    apiGet.mockResolvedValueOnce({ shared_brains: [OTHER] })
    projectStore.activeWorkspace = 'other'
    await flushPromises()
    answerAlpha({ shared_brains: [TEAM] })
    await flushPromises()
    expect(view.text()).toContain('other-notes')
    expect(view.text()).not.toContain('team-notes')
  })

  it('removing the middle of three sends the other two rows in order, by name', async () => {
    const third = state({ name: 'third-notes', identity: 'github.com/acme/third-notes', path: '/Users/me/third-notes' })
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM, OTHER, third] })
    const view = await mountCard()
    askConfirm.mockResolvedValueOnce(true)
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM, third] })
    await view.findAll('.shared-brain-row')[1].find('button').trigger('click')
    await flushPromises()
    expect(updateWorkspace).toHaveBeenCalledTimes(1)
    expect(updateWorkspace).toHaveBeenCalledWith('personal', {
      shared_brains: [
        { path: TEAM.path, name: 'team-notes' },
        { path: third.path, name: 'third-notes' },
      ],
    })
  })
})

describe('SettingsSharedBrains workspace switch and stale lists', () => {
  it('a save still pending when the workspace changes does not leave the new one disabled', async () => {
    let finishSave: (v: unknown) => void = () => {}
    updateWorkspace.mockReturnValueOnce(new Promise((resolve) => { finishSave = resolve }))
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM] })
    const view = await mountCard()
    await field(view, 'Folder path').setValue('/x/y')
    void view.find('form').trigger('submit')
    await flushPromises()

    apiGet.mockResolvedValueOnce({ shared_brains: [OTHER] })
    projectStore.activeWorkspace = 'other'
    await flushPromises()
    finishSave({ workspaces: [] })
    await flushPromises()

    await field(view, 'Folder path').setValue('/z/w')
    expect(button(view, 'Link folder')!.attributes('disabled')).toBeUndefined()
    expect(view.find('.shared-brain-row button').attributes('disabled')).toBeUndefined()
  })

  it('after a save whose re-read fails, Remove and add stay off until a re-read succeeds', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM] })
    const view = await mountCard()
    await field(view, 'Folder path').setValue('/x/y')
    apiGet.mockRejectedValueOnce(apiError('engine blip'))
    await view.find('form').trigger('submit')
    await flushPromises()
    expect(updateWorkspace).toHaveBeenCalledWith('personal', { shared_brains: [{ path: TEAM.path, name: TEAM.name }, { path: '/x/y' }] })

    expect(view.find('.shared-brain-row button').attributes('disabled')).toBeDefined()
    expect(button(view, 'Link folder')!.attributes('disabled')).toBeDefined()

    updateWorkspace.mockClear()
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM] })
    await button(view, 'Try again')!.trigger('click')
    await flushPromises()
    expect(view.find('.shared-brain-row button').attributes('disabled')).toBeUndefined()
    expect(updateWorkspace).not.toHaveBeenCalled()
  })
})
