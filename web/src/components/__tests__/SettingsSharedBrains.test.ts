// @vitest-environment jsdom
//
// Settings > Workspace > Overview > "Shared brains", mounted on its own. The card
// owns its data: `api`, the confirm dialog and the project store are the only
// inputs. What is pinned: the privacy sentence is always shown, Folder and Git
// are an explicit choice, a save sends the whole list with each row's kind and
// re-reads, a refusal keeps the typed text and shows the engine's words, removing
// asks first and names the brain, and a stale reply never lands on another
// workspace.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SettingsSharedBrains from '../settings/SettingsSharedBrains.vue'
import { useProjectStore } from '../../stores/projects'
import { SHARED_BRAIN_PRIVACY, sharedBrainStatus } from '../../lib/sharedBrain'
import type { SharedBrainState } from '../../lib/types'

function apiError(message: string, payload?: Record<string, unknown>): Error {
  return Object.assign(new Error(message), payload ? { payload } : {})
}

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const askConfirm = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn() },
}))
vi.mock('../../lib/confirm', () => ({ askConfirm }))

function state(over: Partial<SharedBrainState> = {}): SharedBrainState {
  return {
    name: 'team-notes',
    kind: 'git',
    identity: 'github.com/acme/team-notes',
    path: '/Users/me/team-notes',
    lifetime: 'lifetime-team-notes',
    sharing_policy: 'review',
    sharing_generation: 0,
    sharing_granted_at: '',
    sharing_grant_pending: false,
    sharing_invalidation_failed: false,
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

const LOCAL = state({
  name: 'local-notes',
  kind: 'folder',
  identity: 'folder:/Users/me/local-notes',
  path: '/Users/me/local-notes',
  branch: '',
  head: '',
  head_date: '',
  dirty: false,
  upstream: false,
  fetched_at: null,
  last_update: null,
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

function kindSelect(view: VueWrapper) {
  return view.find('select[aria-labelledby="shared-brain-kind-label"]')
}

let projectStore: ReturnType<typeof useProjectStore>
let updateWorkspace: ReturnType<typeof vi.fn>

beforeEach(() => {
  setActivePinia(createPinia())
  projectStore = useProjectStore()
  projectStore.activeWorkspace = 'personal'
  apiGet.mockReset().mockResolvedValue({ shared_brains: [] })
  apiPost.mockReset().mockResolvedValue({ shared_brain: state() })
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
    expect(empty.text()).toContain('Folder links are read in place for reference')
    expect(empty.text()).toContain('does not confirm that Google Drive, iCloud or another sync service has finished syncing')
    expect(empty.text()).not.toContain('To set up a Git repository')
    expect(SHARED_BRAIN_PRIVACY).toContain('not automatically synced to a shared folder')
    expect(SHARED_BRAIN_PRIVACY).toContain('reviews and approves that exact note')
    expect(SHARED_BRAIN_PRIVACY).toContain('no ongoing or automatic permission')
    expect(SHARED_BRAIN_PRIVACY).not.toContain('pulls')

    const type = kindSelect(empty)
    await type.setValue('git')
    expect(empty.text()).toContain('To set up a Git repository')
    expect(empty.text()).toContain('clone it on this computer outside the Ciaobot install and vaults')
    expect(empty.text()).toContain('Private HTTPS credential helpers are not used')
    expect(empty.text()).toContain('Public HTTPS can be used to read updates')
    expect(empty.text()).toContain('Publishing needs noninteractive write access')
    expect(empty.text()).toContain('use SSH when the remote requires authentication')
    await type.setValue('folder')
    expect(empty.text()).not.toContain('To set up a Git repository')
    expect(SHARED_BRAIN_PRIVACY).toContain('unless you explicitly choose automatic sharing')
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM] })
    const listed = await mountCard()
    expect(listed.text()).toContain(SHARED_BRAIN_PRIVACY)
    expect(listed.text()).not.toContain('To set up a Git repository')
    await kindSelect(listed).setValue('git')
    expect(listed.text()).toContain('To set up a Git repository')
    expect(listed.text()).toContain('Public HTTPS can be used to read updates')
    expect(listed.text()).toContain('Publishing needs noninteractive write access')
    expect(listed.text()).toContain('use SSH when the remote requires authentication')
  })

  it('shows the checked-out commit and dirty state separately from a skipped update', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [state({
      dirty: true,
      last_update: { outcome: 'skipped', reason: 'local_changes', detail: '', at: '2026-11-22T10:00:00Z' },
    })] })
    const view = await mountCard()
    const row = view.find('.shared-brain-row')
    expect(row.text()).toContain('Branch main')
    expect(row.text()).toContain('Checked-out commit abc1234')
    expect(row.text()).toContain('Local changes')
    expect(row.find('.shared-brain-status').text()).toBe('Not updated: local changes in the folder')
  })

  it('shows an unknown commit without a branch and no leading separator when dirty', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [state({ branch: '', head: '', dirty: true })] })
    const view = await mountCard()
    const metadata = view.find('.shared-brain-row p.hint--compact')
    expect(metadata.text()).toBe('Checked-out commit unknown · Local changes')
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
  it('offers Folder and Git choices and sends the selected kind with the new row', async () => {
    const view = await mountCard()
    const select = kindSelect(view)
    expect(select.exists()).toBe(true)
    expect(select.text()).toContain('Folder (reference only)')
    expect(select.text()).toContain('Git repository')
    expect((select.element as HTMLSelectElement).value).toBe('folder')
    expect(button(view, 'Link folder')!.text()).toBe('Link folder')

    await select.setValue('git')
    expect(button(view, 'Link Git repository')!.text()).toBe('Link Git repository')
    await field(view, 'Folder path').setValue('/x/y')
    await view.find('form').trigger('submit')
    await flushPromises()
    expect(updateWorkspace).toHaveBeenCalledWith('personal', {
      shared_brains: [{ path: '/x/y', kind: 'git' }],
    })
    expect(apiGet).toHaveBeenCalledTimes(2)
  })

  it('sends spaces, brackets, Unicode and a trailing space in a folder path verbatim', async () => {
    const path = '/Users/me/Team Notes [draft] café '
    const view = await mountCard()
    await field(view, 'Folder path').setValue(path)
    await view.find('form').trigger('submit')
    await flushPromises()
    expect(updateWorkspace).toHaveBeenCalledWith('personal', {
      shared_brains: [{ path, kind: 'folder' }],
    })
  })

  it('preserves every existing row kind when linking or removing another row', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM, LOCAL] })
    const view = await mountCard()
    await field(view, 'Folder path').setValue('/x/y')
    await view.find('form').trigger('submit')
    await flushPromises()
    expect(updateWorkspace).toHaveBeenCalledWith('personal', {
      shared_brains: [
        { path: TEAM.path, name: TEAM.name, kind: 'git' },
        { path: LOCAL.path, name: LOCAL.name, kind: 'folder' },
        { path: '/x/y', kind: 'folder' },
      ],
    })

    updateWorkspace.mockClear()
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM, LOCAL] })
    const removeView = await mountCard()
    askConfirm.mockResolvedValueOnce(true)
    await removeView.findAll('.shared-brain-row')[0].find('button').trigger('click')
    await flushPromises()
    expect(updateWorkspace).toHaveBeenCalledWith('personal', {
      shared_brains: [{ path: LOCAL.path, name: LOCAL.name, kind: 'folder' }],
    })
  })

  it('sends the name when one is typed', async () => {
    const view = await mountCard()
    await kindSelect(view).setValue('git')
    await field(view, 'Folder path').setValue('/x/y')
    await field(view, 'Name (optional)').setValue(' notes ')
    await view.find('form').trigger('submit')
    await flushPromises()
    expect(updateWorkspace).toHaveBeenCalledWith('personal', { shared_brains: [{ path: '/x/y', name: 'notes', kind: 'git' }] })
  })

  it("shows the engine's refusal inline in an alert and keeps the typed text", async () => {
    updateWorkspace.mockRejectedValueOnce(apiError('Not a git repository: /x/y', { error: 'Not a git repository: /x/y' }))
    const view = await mountCard()
    await kindSelect(view).setValue('git')
    await field(view, 'Folder path').setValue('/x/y')
    await view.find('form').trigger('submit')
    await flushPromises()
    expect(view.find('[role="alert"]').text()).toContain('Not a git repository: /x/y')
    expect((field(view, 'Folder path').element as HTMLInputElement).value).toBe('/x/y')
  })
})

describe('SettingsSharedBrains listed brains', () => {
  it('renders one row per brain, each with its own kind and status words', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM, LOCAL] })
    const view = await mountCard()
    const rows = view.findAll('.shared-brain-row')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('Up to date')
    expect(rows[0].text()).toContain('Git repository')
    expect(rows[0].text()).toContain('Checked-out commit abc1234')
    expect(rows[1].text()).toContain('Folder · reference only')
    expect(rows[1].text()).toContain('Folder is locally available. Read in place, never updated.')
    expect(rows[1].text()).toContain('does not confirm that Google Drive, iCloud or another sync service has finished syncing')
    expect(rows[1].text()).not.toContain('Checked-out commit')
    expect(rows[1].find('.shared-brain-policy-toggle').exists()).toBe(false)
  })

  it('reports an unreadable folder as local unavailability, never as pending', async () => {
    apiGet.mockResolvedValueOnce({
      shared_brains: [
        state({ problem: 'registry_unavailable', last_update: null }),
        state({
          ...LOCAL,
          problem: 'inaccessible',
          identity: 'folder:/Users/me/unreadable',
          path: '/Users/me/unreadable',
        }),
      ],
    })
    const view = await mountCard()
    const rows = view.findAll('.shared-brain-row')
    expect(rows[0].find('.shared-brain-status').text()).toBe('Inspection temporarily unavailable')
    expect(rows[0].text()).toContain('Try again in a moment')
    expect(rows[0].text()).not.toContain('Workspace settings')
    expect(rows[1].find('.shared-brain-status').text()).toBe('Folder is not available to read on this computer')
    expect(view.text()).not.toContain('First update pending')
  })

  it('renders mixed Git and folder links with their own state and kind-aware guidance', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM, LOCAL] })
    const view = await mountCard()
    const rows = view.findAll('.shared-brain-row')
    expect(rows[0].find('.shared-brain-status').text()).toBe('Up to date')
    expect(rows[0].text()).toContain('Checked-out commit abc1234')
    expect(rows[1].find('.shared-brain-status').text()).toBe('Folder is locally available. Read in place, never updated.')
    expect(rows[1].text()).not.toContain('Checked-out commit')
    expect(view.text()).toContain('Folder links are read in place for reference')
    expect(view.text()).not.toContain('To set up a Git repository')
    await kindSelect(view).setValue('git')
    expect(view.text()).toContain('To set up a Git repository')
    expect(view.text()).not.toContain('Folder links are read in place for reference')
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
    expect(updateWorkspace).toHaveBeenCalledWith('personal', { shared_brains: [{ path: OTHER.path, name: OTHER.name, kind: 'git' }] })
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
        { path: TEAM.path, name: 'team-notes', kind: 'git' },
        { path: third.path, name: 'third-notes', kind: 'git' },
      ],
    })
  })
})

describe('folder availability wording', () => {
  it.each([
    ['missing', 'Folder not available on this computer'],
    ['moved', 'Folder location changed'],
    ['outside_install', 'Folder is inside the Ciaobot install or a vault'],
    ['inaccessible', 'Folder is not available to read this computer'.replace('this computer', 'on this computer')],
  ])('describes %s as local availability', (problem, expected) => {
    expect(sharedBrainStatus(state({ kind: 'folder', problem })).text).toBe(expected)
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
    expect(updateWorkspace).toHaveBeenCalledWith('personal', {
      shared_brains: [{ path: TEAM.path, name: TEAM.name, kind: 'git' }, { path: '/x/y', kind: 'folder' }],
    })

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

describe('SettingsSharedBrains automatic sharing choice', () => {
  it('starts at review and confirms the audience and permanence before granting automatic', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM] })
    const view = await mountCard()
    expect(view.find('.shared-brain-row').text()).toContain('You review each note before it is shared.')
    expect(view.find('.shared-brain-policy-toggle').text()).toBe('Share automatically…')

    askConfirm.mockResolvedValueOnce(false)
    await view.find('.shared-brain-policy-toggle').trigger('click')
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalled()
    const words = askConfirm.mock.calls[0][0] as string
    expect(words).toContain(TEAM.name)
    expect(words).toContain(TEAM.identity)
    expect(words).toContain('everyone with access')
    expect(words).toContain("repository's history")
    expect(words).toContain('does not copy your vault itself')
    expect(words).toContain('private details from your memory or vault')
    expect(words).toContain('memory pass and nightly care can choose private material')
    expect(words).toContain('does not prove who can see it today')
    expect(words).toContain('standing permission for when the producer ships')

    askConfirm.mockResolvedValueOnce(true)
    apiGet.mockResolvedValueOnce({
      shared_brains: [state({
        sharing_policy: 'automatic',
        sharing_generation: 1,
        sharing_granted_at: '2026-11-25T09:00:00Z',
      })],
    })
    await view.find('.shared-brain-policy-toggle').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith(
      '/api/workspaces/personal/shared-brains/team-notes/sharing',
      { policy: 'automatic', identity: TEAM.identity, lifetime: TEAM.lifetime, generation: 0 },
    )
    expect(apiGet).toHaveBeenCalledTimes(2)
    expect(view.find('.shared-brain-row').text()).toContain(
      'Automatic sharing is chosen for everyone who can access github.com/acme/team-notes; it is not active yet.',
    )
    expect(view.find('.shared-brain-policy-toggle').text()).toBe('Turn off automatic sharing')
  })

  it('turns automatic sharing back into review and says published notes stay in history', async () => {
    apiGet.mockResolvedValueOnce({
      shared_brains: [state({
        sharing_policy: 'automatic',
        sharing_generation: 4,
        sharing_granted_at: '2026-11-24T10:00:00Z',
      })],
    })
    const view = await mountCard()
    askConfirm.mockResolvedValueOnce(false)
    await view.find('.shared-brain-policy-toggle').trigger('click')
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalled()
    const words = askConfirm.mock.calls[0][0] as string
    expect(words).toContain('already published stay')
    expect(words).toContain('Future notes will wait for your review')

    askConfirm.mockResolvedValueOnce(true)
    apiGet.mockResolvedValueOnce({ shared_brains: [state({ sharing_generation: 5 })] })
    await view.find('.shared-brain-policy-toggle').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      '/api/workspaces/personal/shared-brains/team-notes/sharing',
      { policy: 'review', identity: TEAM.identity, lifetime: TEAM.lifetime, generation: 4 },
    )
    expect(view.find('.shared-brain-row').text()).toContain('You review each note before it is shared.')
  })

  it('will not offer automatic sharing for a folder the engine cannot use', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [state({ problem: 'origin_changed', origin_changed: true })] })
    const view = await mountCard()
    const toggle = view.find('.shared-brain-policy-toggle')
    expect(toggle.attributes('disabled')).toBeDefined()
    expect(view.find('.shared-brain-row').text()).toContain('Fix this folder before choosing automatic sharing.')
  })

  it('keeps the revoke action when an old grant is pending after drift', async () => {
    apiGet.mockResolvedValueOnce({
      shared_brains: [state({
        sharing_policy: 'review',
        sharing_generation: 2,
        sharing_grant_pending: true,
        sharing_invalidation_failed: true,
        problem: 'origin_changed',
        origin_changed: true,
      })],
    })
    const view = await mountCard()
    expect(view.find('.shared-brain-row').text()).toContain(
      'Automatic sharing is paused, but its old grant still needs to be turned off.',
    )
    expect(view.find('.shared-brain-row').text()).toContain(
      'The old grant could not be removed; turn it off to finish revoking it.',
    )
    expect(view.find('.shared-brain-policy-toggle').text()).toBe('Turn off automatic sharing')
    expect(view.find('.shared-brain-policy-toggle').attributes('disabled')).toBeUndefined()

    askConfirm.mockResolvedValueOnce(true)
    apiGet.mockResolvedValueOnce({ shared_brains: [state({ sharing_generation: 3 })] })
    await view.find('.shared-brain-policy-toggle').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      '/api/workspaces/personal/shared-brains/team-notes/sharing',
      { policy: 'review', identity: TEAM.identity, lifetime: TEAM.lifetime, generation: 2 },
    )
  })

  it("shows the engine's refusal when the sharing choice is not saved", async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM] })
    const view = await mountCard()
    apiPost.mockRejectedValueOnce(apiError('This shared brain changed since the page was loaded.', {
      error: 'This shared brain changed since the page was loaded.',
      code: 'brain_changed',
    }))
    await view.find('.shared-brain-policy-toggle').trigger('click')
    await flushPromises()
    expect(view.find('[role="alert"]').text()).toContain('This shared brain changed')
  })
})
