// @vitest-environment jsdom
//
// Settings > Workspace > Overview > "Weekly care cycle", mounted on its own.
// The card owns its data: `api`, the confirm dialog and the project store are
// the only inputs. What is pinned: it never switches without the checkbox AND
// the confirm, it posts exactly {workspace, confirm_quiesced, digest}, a stale
// reply never lands on another workspace, and each state says what is true.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SettingsCareCycle from '../settings/SettingsCareCycle.vue'
import { useProjectStore } from '../../stores/projects'

// The api module's ApiError is not exported; a rejection only needs `message` and `payload`.
function apiError(message: string, payload?: Record<string, unknown>): Error {
  return Object.assign(new Error(message), payload ? { payload } : {})
}

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const askConfirm = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost },
}))
vi.mock('../../lib/confirm', () => ({ askConfirm }))

const DIGEST = 'digest-1'
const BACKUP = '/vault/Workspace/Curation-State.v1.bak'

function ready(overrides: Record<string, unknown> = {}) {
  return {
    workspace: 'personal',
    on_host: true,
    lease_live: false,
    status: 'v1',
    live_lease: null,
    marker: 'present',
    last_full_pass: '2026-11-09',
    daily: 2,
    weekly_historical: 3,
    ambiguous: ['rules:one'],
    dropped: [],
    backup_path: BACKUP,
    digest: DIGEST,
    blockers: [],
    ...overrides,
  }
}

const mounted: VueWrapper[] = []

async function mountCard() {
  const view = mount(SettingsCareCycle)
  mounted.push(view)
  await flushPromises()
  return view
}

function button(view: VueWrapper, label: string) {
  return view.findAll('button').find((b) => b.text() === label)
}

function switchButton(view: VueWrapper) {
  return button(view, 'Switch this workspace')
}

function checkbox(view: VueWrapper) {
  return view.find('input[type="checkbox"]')
}

async function tickAndSwitch(view: VueWrapper) {
  await checkbox(view).setValue(true)
  await switchButton(view)!.trigger('click')
  await flushPromises()
}

let projectStore: ReturnType<typeof useProjectStore>

beforeEach(() => {
  setActivePinia(createPinia())
  projectStore = useProjectStore()
  projectStore.activeWorkspace = 'personal'
  apiGet.mockReset().mockResolvedValue(ready())
  apiPost.mockReset().mockResolvedValue({
    workspace: 'personal',
    status: 'migrated',
    backup_path: BACKUP,
    digest: DIGEST,
  })
  askConfirm.mockReset().mockResolvedValue(true)
})

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
})

describe('SettingsCareCycle loading and failures', () => {
  it("reads the active workspace's dry run on mount", async () => {
    await mountCard()
    expect(apiGet).toHaveBeenCalledWith('/api/care/cycle?workspace=personal')
  })

  it('shows a pending read as its own status, never as a claim', async () => {
    apiGet.mockReturnValue(new Promise(() => {}))
    const view = mount(SettingsCareCycle)
    mounted.push(view)
    await flushPromises()
    const status = view.find('[role="status"]')
    expect(status.text()).toContain('Checking the care state')
    expect(switchButton(view)).toBeUndefined()
  })

  it('a first-load failure says so and offers a retry that loads', async () => {
    apiGet.mockRejectedValueOnce(apiError('boom'))
    const view = await mountCard()
    expect(view.find('[role="alert"]').text()).toContain('Could not read the care state')
    apiGet.mockResolvedValueOnce(ready())
    await button(view, 'Try again')!.trigger('click')
    await flushPromises()
    expect(switchButton(view)).toBeDefined()
    expect(view.find('[role="alert"]').exists()).toBe(false)
  })

  it('a failed re-read keeps the data on screen, says it is stale, and retries', async () => {
    const view = await mountCard()
    // The re-read after a switch fails: the last known preview stays, marked stale.
    apiGet.mockRejectedValueOnce(apiError('offline'))
    await tickAndSwitch(view)
    expect(view.find('.hint--warn').text()).toContain('Showing the last known state')
    expect(switchButton(view)).toBeDefined()
    apiGet.mockResolvedValueOnce(ready({ status: 'v2' }))
    await view.find('.hint--warn .set-link').trigger('click')
    await flushPromises()
    expect(view.find('.hint--warn').exists()).toBe(false)
    expect(view.text()).toContain('already uses the weekly cycle')
  })
})

describe('SettingsCareCycle states', () => {
  it('not on the host: explains, offers the command to copy, no switch control', async () => {
    apiGet.mockResolvedValue({ workspace: 'personal', on_host: false, reason: 'not_on_host', lease_live: null })
    const view = await mountCard()
    expect(view.text()).toContain('computer running Ciaobot')
    expect(view.text()).toContain('ciao curation-cycle-migrate')
    expect(switchButton(view)).toBeUndefined()
    expect(checkbox(view).exists()).toBe(false)
    expect(view.find('button[aria-label="Copy the command"]').exists()).toBe(true)
  })

  it('already switched: a read-only line and the backup path, no switch control', async () => {
    apiGet.mockResolvedValue(ready({ status: 'v2' }))
    const view = await mountCard()
    expect(view.text()).toContain('already uses the weekly cycle')
    expect(view.text()).toContain(BACKUP)
    expect(switchButton(view)).toBeUndefined()
  })

  it('a live lease blocks the switch with the run-in-progress sentence, aria-disabled', async () => {
    apiGet.mockResolvedValue(
      ready({
        lease_live: true,
        live_lease: { holder: 'run', started_at: 'x', expires_at: 'y' },
        blockers: ['a live curation lease holds this vault; wait for the run to finish or expire'],
      }),
    )
    const view = await mountCard()
    expect(view.text()).toContain('A care run is in progress. Wait for it to finish.')
    const btn = switchButton(view)!
    expect(btn.attributes('aria-disabled')).toBe('true')
    expect(btn.attributes('aria-describedby')).toBe('care-cycle-reason')
    expect(view.find('#care-cycle-reason').text()).toContain('unavailable')
    await btn.trigger('click')
    await flushPromises()
    expect(askConfirm).not.toHaveBeenCalled()
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('an unreadable state says it cannot be read safely', async () => {
    apiGet.mockResolvedValue(ready({ status: 'unreadable', blockers: ['the v1 state cannot be read safely: bad'] }))
    const view = await mountCard()
    expect(view.text()).toContain('cannot be read safely')
    expect(view.text()).toContain('the v1 state cannot be read safely: bad')
  })

  it('ready: says what is kept, names the backup and the warning, and the switch stays off until the box is ticked', async () => {
    const view = await mountCard()
    const text = view.text()
    expect(text).toContain('2 daily progress records kept.')
    expect(text).toContain('3 weekly records kept as history')
    expect(text).toContain('1 weekly record is ambiguous and needs fresh proof.')
    expect(text).toContain(BACKUP)
    expect(text).toContain('Ciaobot does not undo this automatically')
    expect(text).toContain('Stop other copies first.')
    expect(text).toContain('This does not touch your notes.')
    expect(text).toContain('straddle midnight UTC, the switch is refused. Preview again.')
    expect(switchButton(view)!.attributes('aria-disabled')).toBe('true')
    await checkbox(view).setValue(true)
    expect(switchButton(view)!.attributes('aria-disabled')).toBeUndefined()
  })
})

describe('SettingsCareCycle confirmation and apply', () => {
  it('never switches without the checkbox, even when the dialog would confirm', async () => {
    const view = await mountCard()
    await switchButton(view)!.trigger('click')
    await flushPromises()
    expect(askConfirm).not.toHaveBeenCalled()
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('never switches when the checkbox is ticked but the confirm is declined', async () => {
    askConfirm.mockResolvedValue(false)
    const view = await mountCard()
    await tickAndSwitch(view)
    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('asks with a destructive, named confirm before switching', async () => {
    const view = await mountCard()
    await tickAndSwitch(view)
    const [message, options] = askConfirm.mock.calls[0]
    expect(message).toContain('Stop other copies first.')
    expect(message).toContain('Ciaobot does not undo this automatically')
    expect(options).toEqual({
      title: 'Switch Workspace care to the new weekly cycle?',
      confirmLabel: 'Switch',
      destructive: true,
    })
  })

  it('posts exactly the workspace, the literal confirmation and the previewed digest', async () => {
    const view = await mountCard()
    await tickAndSwitch(view)
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/care/cycle', {
      workspace: 'personal',
      confirm_quiesced: true,
      digest: DIGEST,
    })
    expect(apiPost.mock.calls[0][1].confirm_quiesced).toBe(true)
  })

  it('success shows the backup path as a status and re-reads the preview', async () => {
    const view = await mountCard()
    apiGet.mockResolvedValue(ready({ status: 'v2' }))
    await tickAndSwitch(view)
    const status = view.find('[role="status"]').text()
    expect(status).toContain('Switched.')
    expect(status).toContain(BACKUP)
    expect(view.text()).toContain('already uses the weekly cycle')
  })

  it('a refusal shows the plain reason and the blockers, and re-reads the preview', async () => {
    const view = await mountCard()
    const refusal = apiError('The switch was refused and nothing was written.', {
      error: 'The switch was refused and nothing was written.',
      blockers: ['the vault changed since the dry run (digest mismatch); run the dry run again'],
    })
    apiPost.mockRejectedValueOnce(refusal)
    apiGet.mockResolvedValueOnce(ready({ digest: 'digest-2' }))
    await tickAndSwitch(view)
    const alert = view.find('[role="alert"]')
    expect(alert.text()).toContain('The switch was refused and nothing was written.')
    expect(alert.text()).toContain('digest mismatch')
    expect(apiGet).toHaveBeenCalledTimes(2)
    // The checkbox is cleared: a new preview needs a new act.
    expect((checkbox(view).element as HTMLInputElement).checked).toBe(false)
  })

  it('a second click while the confirm is open sends nothing extra', async () => {
    let answer: (ok: boolean) => void = () => {}
    askConfirm.mockImplementation(() => new Promise<boolean>((resolve) => (answer = resolve)))
    const view = await mountCard()
    await checkbox(view).setValue(true)
    const btn = switchButton(view)!
    await btn.trigger('click')
    await btn.trigger('click')
    answer(true)
    await flushPromises()
    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledTimes(1)
  })

  it('while the switch is in flight the button is busy and a second click sends one request', async () => {
    let finish: (value: unknown) => void = () => {}
    apiPost.mockImplementation(() => new Promise((resolve) => (finish = resolve)))
    const view = await mountCard()
    await tickAndSwitch(view)
    const busy = view.findAll('button').find((b) => b.text() === 'Switching...')!
    expect(busy.attributes('aria-busy')).toBe('true')
    await busy.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledTimes(1)
    finish({ workspace: 'personal', status: 'migrated', backup_path: BACKUP, digest: DIGEST })
    await flushPromises()
  })
})

describe('SettingsCareCycle workspace changes', () => {
  it('a reply for the previous workspace never lands on the current one', async () => {
    let resolveFirst: (value: unknown) => void = () => {}
    apiGet.mockImplementationOnce(() => new Promise((resolve) => (resolveFirst = resolve)))
    const view = mount(SettingsCareCycle)
    mounted.push(view)
    await flushPromises()
    apiGet.mockResolvedValueOnce(ready({ workspace: 'work', backup_path: '/work/backup', digest: 'work-digest' }))
    projectStore.activeWorkspace = 'work'
    await flushPromises()
    resolveFirst(ready({ workspace: 'personal', digest: 'stale' }))
    await flushPromises()
    expect(apiGet).toHaveBeenLastCalledWith('/api/care/cycle?workspace=work')
    expect(view.text()).toContain('/work/backup')
    expect(view.text()).not.toContain('stale')
  })

  it('switching workspace clears a pending confirmation so the new workspace needs its own tick', async () => {
    const view = await mountCard()
    await checkbox(view).setValue(true)
    projectStore.activeWorkspace = 'work'
    await flushPromises()
    expect((checkbox(view).element as HTMLInputElement).checked).toBe(false)
  })
})

describe('SettingsCareCycle copy command', () => {
  it('the copy control is a real button and reports the result as a status', async () => {
    apiGet.mockResolvedValue({ workspace: 'personal', on_host: false, reason: 'not_on_host', lease_live: null })
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const view = await mountCard()
    await view.find('button[aria-label="Copy the command"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('ciao curation-cycle-migrate --vault-root <your vault folder> --dry-run')
    const statuses = view.findAll('[role="status"]').map((node) => node.text())
    expect(statuses.some((text) => text.includes('Copied.'))).toBe(true)
  })
})
