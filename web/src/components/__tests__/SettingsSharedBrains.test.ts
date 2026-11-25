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

const FOLDER = state({
  name: 'shared-folder',
  kind: 'folder',
  identity: 'folder:/Users/me/shared',
  path: '/Users/me/shared',
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

let projectStore: ReturnType<typeof useProjectStore>
let updateWorkspace: ReturnType<typeof vi.fn>

beforeEach(() => {
  setActivePinia(createPinia())
  projectStore = useProjectStore()
  projectStore.activeWorkspace = 'personal'
  apiGet.mockReset().mockResolvedValue({ shared_brains: [] })
  apiPost.mockReset()
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
    expect(empty.text()).toContain('clone it on this computer outside the Ciaobot install and vaults')
    expect(empty.text()).toContain('Private HTTPS credential helpers are not used')
    expect(empty.text()).toContain('Public HTTPS can be used to read updates')
    expect(empty.text()).toContain('Publishing needs noninteractive write access')
    expect(empty.text()).toContain('use SSH when the remote requires authentication')
    expect(SHARED_BRAIN_PRIVACY).toContain('not automatically synced to a shared folder')
    expect(SHARED_BRAIN_PRIVACY).toContain('under an explicit grant you review in Sources')
    expect(SHARED_BRAIN_PRIVACY).toContain('no ongoing or automatic permission')
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM] })
    const listed = await mountCard()
    expect(listed.text()).toContain(SHARED_BRAIN_PRIVACY)
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

  it('reports a healthy folder as read in place, with no Git metadata and kind-aware guidance', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [FOLDER] })
    const view = await mountCard()
    const row = view.find('.shared-brain-row')
    expect(row.find('.shared-brain-status').text()).toBe('Linked folder. Read in place, never updated.')
    expect(row.text()).not.toContain('First update pending')
    expect(row.text()).not.toContain('Checked-out commit')
    expect(view.text()).toContain('An ordinary folder linked through another setup is read in place')
    expect(view.text()).toContain('Ciaobot never pulls, fetches or updates it')
    expect(view.text()).toContain('Adding a path here links a Git checkout')
  })

  it('reports a busy checkout and an unreadable folder accurately, never as pending or with a guessed cause', async () => {
    apiGet.mockResolvedValueOnce({
      shared_brains: [
        state({ problem: 'registry_unavailable', last_update: null }),
        state({
          ...FOLDER,
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
    expect(rows[1].find('.shared-brain-status').text()).toBe('Folder is not readable')
    expect(view.text()).not.toContain('First update pending')
  })

  it('renders mixed Git and folder links with their own state and both guidance lines', async () => {
    apiGet.mockResolvedValueOnce({ shared_brains: [TEAM, FOLDER] })
    const view = await mountCard()
    const rows = view.findAll('.shared-brain-row')
    expect(rows[0].find('.shared-brain-status').text()).toBe('Up to date')
    expect(rows[0].text()).toContain('Checked-out commit abc1234')
    expect(rows[1].find('.shared-brain-status').text()).toBe('Linked folder. Read in place, never updated.')
    expect(rows[1].text()).not.toContain('Checked-out commit')
    expect(view.text()).toContain('Adding a path here links a Git checkout')
    expect(view.text()).toContain('An ordinary folder linked through another setup is read in place')
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

// ---------------------------------------------------------------------------
// Workspace-scoped Sources panel: picker, exact preview, consent and revoke.

import type {
  ShareEvidenceGrant,
  ShareEvidencePreview,
  ShareEvidenceState,
} from '../../lib/types'

function source(kind: 'stream' | 'document', projectId = 'proj-alpha') {
  return {
    source_id: kind === 'stream' ? `stream:${projectId}` : 'document:Decisions.md',
    kind,
    label: kind === 'stream' ? 'Structured decisions · Alpha' : 'Decision document · Decisions.md',
    project_id: projectId,
    project_name: kind === 'stream' ? 'Alpha' : 'Alpha',
    entry_count: kind === 'stream' ? 2 : 3,
  }
}

function grant(over: Partial<ShareEvidenceGrant> = {}): ShareEvidenceGrant {
  return {
    grant_id: 'grn_aaaaaaaaaaaaaaaaaaaaaaaa',
    type: 'standing',
    project_id: 'proj-alpha',
    source_id: 'stream:proj-alpha',
    source_kind: 'stream',
    source_label: 'Structured decisions · Alpha',
    brain: {
      name: 'team-notes',
      identity: 'github.com/acme/team-notes',
      link_fingerprint: 'f'.repeat(16),
    },
    audience: 'github.com/acme/team-notes',
    field_mask: ['decision', 'effective_date'],
    producers: ['memory_extraction'],
    issued_at: '2026-11-25T10:00:00Z',
    start_revision: 2,
    consent_generation: 1,
    active: true,
    linked: true,
    status: 'active',
    revoked_at: null,
    revoked_generation: null,
    withdrawn_at: null,
    withdrawal_reason: '',
    history: [{ event: 'issued', at: '2026-11-25T10:00:00Z', generation: 1 }],
    ...over,
  }
}

function evidence(over: Partial<ShareEvidenceState> = {}): ShareEvidenceState {
  return {
    workspace: 'personal',
    mode: 'review',
    fields: ['decision', 'reason', 'effective_date'],
    producers: ['memory_extraction', 'nightly_curation'],
    brains: [{ name: 'team-notes', identity: 'github.com/acme/team-notes' }],
    projects: [{ project_id: 'proj-alpha', name: 'Alpha', sources: [source('stream'), source('document')] }],
    grants: [],
    ...over,
  }
}

function preview(over: Partial<ShareEvidencePreview['preview']> = {}): ShareEvidencePreview {
  return {
    preview_token: 'sev_preview-token',
    preview: {
      grant_type: 'standing',
      scope: 'future_decisions',
      summary: 'Future structured decisions in this project can leave without review.',
      future_items_without_review: true,
      project_id: 'proj-alpha',
      source_id: 'stream:proj-alpha',
      source_label: 'Structured decisions · Alpha',
      entry_label: 'Decisions from revision 3 onward',
      fields: [[
        { field: 'decision', value: 'Use the new vendor' },
        { field: 'effective_date', value: '2026-11-20' },
      ]],
      field_mask: ['decision', 'effective_date'],
      excluded_fields: ['reason'],
      producers: ['memory_extraction'],
      recipient: {
        brain: 'team-notes',
        identity: 'github.com/acme/team-notes',
        audience: 'github.com/acme/team-notes',
      },
      expected_generation: '2',
      expires_at: '2026-11-25T10:15:00Z',
      notice: 'Only entries recorded after this grant can be extracted.',
      ...over,
    },
  }
}

function routeGet(states: Record<string, () => ShareEvidenceState>) {
  return (path: string) => {
    const match = /^\/api\/workspaces\/([^/]+)\/share-evidence$/.exec(path)
    if (match) return Promise.resolve(states[decodeURIComponent(match[1])]())
    if (path.endsWith('/shared-brains')) return Promise.resolve({ shared_brains: [] })
    return Promise.reject(new Error(`unexpected GET ${path}`))
  }
}

async function openSources() {
  const view = await mountCard()
  await button(view, 'Sources and consent')!.trigger('click')
  await flushPromises()
  return view
}

describe('SettingsSharedBrains sources and consent', () => {
  it('server-filters the brain, project and actual source and states the review default', async () => {
    apiGet.mockImplementation(routeGet({ personal: () => evidence() }))
    const view = await openSources()
    const brain = view.find('.sources-grid select')
    expect(brain.findAll('option').map((o) => o.text())).toEqual([
      'team-notes — github.com/acme/team-notes',
    ])
    expect(view.findAll('.sources-grid select')[2].findAll('option').map((o) => o.text())).toEqual([
      'Structured decisions · Alpha (2)',
      'Decision document · Decisions.md (3)',
    ])
    expect(view.find('.sources-mode').text()).toBe('Review')
    expect(view.text()).toContain('Nothing from this project stream leaves automatically')
  })

  it('previews exact fields, states future-without-review, and cancels without a grant', async () => {
    apiGet.mockImplementation(routeGet({ personal: () => evidence() }))
    apiPost.mockResolvedValueOnce(preview())
    const view = await openSources()
    await button(view, 'Preview exact scope')!.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      '/api/workspaces/personal/share-evidence/preview',
      expect.objectContaining({
        brain: 'team-notes',
        project_id: 'proj-alpha',
        source_id: 'stream:proj-alpha',
        grant_type: 'standing',
        field_mask: ['decision', 'reason', 'effective_date'],
        producers: ['memory_extraction', 'nightly_curation'],
      }),
    )
    const panel = view.find('.sources-preview')
    expect(panel.text()).toContain('Use the new vendor')
    expect(panel.text()).toContain('github.com/acme/team-notes')
    expect(panel.text()).toContain('Future items can leave without another review')
    // Rendered fields are values, not an editable replacement for the snapshot.
    expect(panel.findAll('input').length).toBe(0)
    await button(view, 'Cancel')!.trigger('click')
    expect(view.find('.sources-preview').exists()).toBe(false)
    expect(apiPost).toHaveBeenCalledTimes(1)
  })

  it('confirms the previewed token, lists the grant and re-reads', async () => {
    let grants: ShareEvidenceGrant[] = []
    let reads = 0
    apiGet.mockImplementation((path: string) => {
      if (path.endsWith('/share-evidence')) {
        reads += 1
        return Promise.resolve(evidence({ grants, mode: grants.length ? 'automatic' : 'review' }))
      }
      return Promise.resolve({ shared_brains: [] })
    })
    apiPost.mockImplementation((path: string) => {
      if (path.endsWith('/preview')) return Promise.resolve(preview())
      if (path.endsWith('/confirm')) {
        grants = [grant()]
        return Promise.resolve({ grant: grants[0], mode: 'automatic' })
      }
      return Promise.reject(new Error(`unexpected POST ${path}`))
    })
    const view = await openSources()
    await button(view, 'Preview exact scope')!.trigger('click')
    await flushPromises()
    await button(view, 'Confirm and grant')!.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      '/api/workspaces/personal/share-evidence/confirm',
      { preview_token: 'sev_preview-token', confirm: true },
    )
    expect(reads).toBe(2)
    expect(view.find('.sources-mode').text()).toBe('Automatic for granted streams')
    expect(view.text()).toContain('grn_aaaaaaaaaaaaaaaaaaaaaaaa')
    await button(view, 'Inspect exact scope')!.trigger('click')
    expect(view.find('.sources-grant-detail').text()).toContain('gen 1')
  })

  it('inspects the exact scope and revokes a generation with a fresh grant required', async () => {
    let grants = [grant()]
    apiGet.mockImplementation((path: string) => {
      if (path.endsWith('/share-evidence')) return Promise.resolve(evidence({ grants, mode: 'automatic' }))
      return Promise.resolve({ shared_brains: [] })
    })
    apiPost.mockImplementation((path: string) => {
      if (path.endsWith('/revoke')) {
        grants = [grant({ active: false, status: 'revoked', revoked_at: '2026-11-25T11:00:00Z', revoked_generation: 1 })]
        return Promise.resolve({ grant: grants[0], fresh_grant_required: true })
      }
      return Promise.reject(new Error(`unexpected POST ${path}`))
    })
    const view = await openSources()
    await button(view, 'Inspect exact scope')!.trigger('click')
    await flushPromises()
    expect(view.find('.sources-grant-detail').text()).toContain('Revision 2')
    const revoke = button(view, 'Revoke')!
    expect(revoke.element.tagName).toBe('BUTTON')
    expect(revoke.attributes('disabled')).toBeUndefined()
    await revoke.trigger('click')
    await flushPromises()
    expect(askConfirm).toHaveBeenCalledWith(
      expect.stringContaining('team-notes'),
      expect.objectContaining({ confirmLabel: 'Revoke', destructive: true }),
    )
    expect(view.text()).toContain('Generation 1 was revoked')
    expect(view.text()).toContain('A fresh preview and grant are required')
    expect(view.find('.sources-grant-state').text()).toBe('Revoked')
    expect(button(view, 'Revoke')!.attributes('disabled')).toBeDefined()
  })

  it('drops another workspace source, preview and grant before they can be submitted', async () => {
    const grants = [grant()]
    const states: Record<string, () => ShareEvidenceState> = {
      personal: () => evidence({ grants, mode: 'automatic' }),
      work: () => evidence({
        workspace: 'work',
        brains: [{ name: 'work-notes', identity: 'github.com/acme/work' }],
        projects: [{
          project_id: 'proj-foreign',
          name: 'Foreign',
          sources: [{ ...source('stream', 'proj-foreign'), label: 'Foreign decisions' }],
        }],
        grants: [grant({
          grant_id: 'grn_bbbbbbbbbbbbbbbbbbbbbbbb',
          brain: {
            name: 'work-notes',
            identity: 'github.com/acme/work',
            link_fingerprint: 'd'.repeat(16),
          },
          audience: 'github.com/acme/work',
          project_id: 'proj-foreign',
        })],
        mode: 'automatic',
      }),
    }
    apiGet.mockImplementation(routeGet(states))
    apiPost.mockResolvedValueOnce(preview())
    const view = await openSources()
    await button(view, 'Preview exact scope')!.trigger('click')
    await flushPromises()
    expect(view.text()).toContain('grn_aaaaaaaaaaaaaaaaaaaaaaaa')

    projectStore.activeWorkspace = 'work'
    await flushPromises()
    expect(view.find('.sources-preview').exists()).toBe(false)
    expect(view.text()).not.toContain('grn_aaaaaaaaaaaaaaaaaaaaaaaa')
    expect(view.text()).not.toContain('proj-alpha')
    expect(view.text()).toContain('grn_bbbbbbbbbbbbbbbbbbbbbbbb')
    expect(view.text()).toContain('Foreign decisions')
    expect(apiPost).toHaveBeenCalledTimes(1)
  })

  it('records a structured decision and shows the standing disclosure where it is entered', async () => {
    let grants: ShareEvidenceGrant[] = []
    apiGet.mockImplementation((path: string) => {
      if (path.endsWith('/share-evidence')) return Promise.resolve(evidence({ grants, mode: grants.length ? 'automatic' : 'review' }))
      return Promise.resolve({ shared_brains: [] })
    })
    apiPost.mockImplementation((path: string) => {
      if (path.endsWith('/decisions')) {
        grants = [grant()]
        return Promise.resolve({ entry: { decision: 'Use the new vendor' }, mode: 'automatic' })
      }
      return Promise.reject(new Error(`unexpected POST ${path}`))
    })
    const view = await openSources()
    const labels = view.findAll('.sources-form label')
    await labels[0].find('input').setValue('Use the new vendor')
    await labels[2].find('input').setValue('2026-11-20')
    await view.find('.sources-form').trigger('submit')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      '/api/workspaces/personal/share-evidence/decisions',
      { project_id: 'proj-alpha', decision: 'Use the new vendor', reason: '', effective_date: '2026-11-20' },
    )
    expect(view.text()).toContain('Standing disclosures active for this project stream')
    expect(view.text()).toContain('decision, effective_date can leave automatically for memory_extraction')
  })

  it('shows every active standing scope for the selected project, not just the first', async () => {
    apiGet.mockImplementation(routeGet({
      personal: () => evidence({
        mode: 'automatic',
        grants: [
          grant({
            grant_id: 'grn_111111111111111111111111',
            field_mask: ['decision'],
            producers: ['memory_extraction'],
          }),
          grant({
            grant_id: 'grn_222222222222222222222222',
            field_mask: ['effective_date', 'reason'],
            producers: ['nightly_curation'],
            brain: {
              name: 'other-notes',
              identity: 'github.com/acme/other-notes',
              link_fingerprint: 'e'.repeat(16),
            },
            audience: 'github.com/acme/other-notes',
          }),
        ],
        brains: [
          { name: 'team-notes', identity: 'github.com/acme/team-notes' },
          { name: 'other-notes', identity: 'github.com/acme/other-notes' },
        ],
      }),
    }))
    const view = await openSources()
    const disclosures = view.findAll('.sources-disclosures li')
    expect(disclosures.map((row) => row.text())).toEqual([
      'To team-notes: decision can leave automatically for memory_extraction.',
      'To other-notes: effective_date, reason can leave automatically for nightly_curation.',
    ])
  })

  it('shows a same-name relink as unlinked and does not claim its scope as active', async () => {
    apiGet.mockImplementation(routeGet({
      personal: () => evidence({
        mode: 'review',
        grants: [grant({ linked: false, status: 'unlinked' })],
      }),
    }))
    const view = await openSources()
    expect(view.find('.sources-grant-state').text()).toBe('Brain unlinked')
    expect(view.find('.sources-disclosures').exists()).toBe(false)
    expect(view.text()).toContain('Review is active. Nothing from this project stream leaves automatically')
    await button(view, 'Inspect exact scope')!.trigger('click')
    expect(view.find('.sources-grant-detail').text()).toContain('f'.repeat(16))
  })

  it('renders the sources toggle as an expandable keyboard control', async () => {
    apiGet.mockImplementation(routeGet({ personal: () => evidence() }))
    const view = await mountCard()
    const toggle = button(view, 'Sources and consent')!
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.attributes('aria-controls')).toBe('share-sources-panel')
    await toggle.trigger('click')
    await flushPromises()
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(view.find('#share-sources-panel').exists()).toBe(true)
  })
})
