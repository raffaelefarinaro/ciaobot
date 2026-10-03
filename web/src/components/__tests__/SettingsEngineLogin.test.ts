// @vitest-environment jsdom
//
// Settings → Home → "Start at sign-in", mounted on its own: the panel owns its
// read and its one write, so `api` is the only input and it is stubbed here.
//
// Three things are worth pinning, because each is a way to lie about the host.
// The state is tri-state: `null` means the machine did not say, so it must read
// as Unknown with the reason and never as a position in either direction.
// `can_change` is the only licence to write, so every state without it carries
// no control at all rather than a dead one. And a refused or unconfirmed write
// answers with a fresh status instead of a 200, so the row keeps the position
// that is true and never the one that was asked for.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SettingsEngineLogin from '../settings/SettingsEngineLogin.vue'

// Held out of the factory so these tests can count calls and hand back a
// deferred promise: the write body and the in-flight lock are behaviours a
// plain assertion cannot see.
const apiGet = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const writeClipboard = vi.hoisted(() => vi.fn(async () => true))

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: vi.fn(), patch: apiPatch, del: vi.fn() },
}))

vi.mock('../../lib/codeCopy', () => ({
  COPY_FEEDBACK_MS: 1500,
  writeClipboard,
}))

/** The #913 status contract, shaped the way the route serves it. */
function status(overrides: Record<string, unknown> = {}) {
  return {
    platform: 'macos',
    supported: true,
    installed: true,
    enabled: false,
    can_change: true,
    reason: 'The engine service will not start when this user signs in.',
    setup_command: null,
    ...overrides,
  }
}

const ENABLED = status({
  enabled: true,
  reason: 'The engine service will start when this user signs in.',
})

const mounted: VueWrapper[] = []

async function mountPanel() {
  const view = mount(SettingsEngineLogin)
  mounted.push(view)
  await flushPromises()
  return view
}

function button(view: VueWrapper, label: string) {
  const match = view.findAll('button').find((b) => b.text() === label)
  if (!match) throw new Error(`no button labelled ${label}`)
  return match
}

/** The words the row claims, without the dot that only repeats them. */
function stateText(view: VueWrapper): string {
  return view.get('.login-state').text()
}

/** The one control on the card, when there is one. */
function actionButton(view: VueWrapper) {
  return view.find('.login-end button')
}

/** The 409/503 envelope: `error` beside the freshest status the route read. */
function failure(status_: number, message: string, fresh: Record<string, unknown>) {
  return Object.assign(new Error(message), {
    status,
    payload: { error: message, ...status(fresh) },
  })
}

beforeEach(() => {
  apiGet.mockReset().mockResolvedValue(status())
  apiPatch.mockReset().mockResolvedValue(ENABLED)
  writeClipboard.mockReset().mockResolvedValue(true)
})

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('SettingsEngineLogin when the host does not say', () => {
  it('reads the route on mount and stops there', async () => {
    const view = await mountPanel()

    expect(apiGet).toHaveBeenCalledTimes(1)
    expect(apiGet).toHaveBeenCalledWith('/api/service/login')
    expect(view.text()).toContain('Does not start when I sign in')
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('renders a pending read as a state of its own, never as an empty answer', async () => {
    let resolve: (value: unknown) => void = () => {}
    apiGet.mockReturnValue(new Promise((r) => { resolve = r }))
    const view = mount(SettingsEngineLogin)
    mounted.push(view)
    await flushPromises()

    expect(view.find('.login-state').exists()).toBe(false)
    expect(view.get('[role="status"]').text()).toContain('Checking this host')
    // "Could not ask" is not "nothing to start": no position is claimed.
    expect(view.text()).not.toContain('Unknown')

    resolve(status())
    await flushPromises()
    expect(view.text()).toContain('Does not start when I sign in')
  })

  it('shows a failed read with a Retry that asks again', async () => {
    apiGet.mockRejectedValueOnce(new Error('the engine did not answer'))
    const view = await mountPanel()

    expect(view.get('[role="alert"]').text()).toContain('the engine did not answer')
    // The failure is not read as a position in either direction.
    expect(view.find('.login-state').exists()).toBe(false)
    expect(view.findAll('button').map((b) => b.text())).not.toContain('Enable at sign-in')

    await button(view, 'Check again').trigger('click')
    await flushPromises()

    expect(apiGet).toHaveBeenCalledTimes(2)
    expect(view.text()).toContain('Does not start when I sign in')
    expect(view.find('[role="alert"]').exists()).toBe(false)
  })

  it('keeps a loading line through a first-load retry instead of going blank', async () => {
    apiGet.mockRejectedValueOnce(new Error('the engine did not answer'))
    const view = await mountPanel()
    expect(view.get('[role="alert"]').text()).toContain('the engine did not answer')

    // Held open on purpose: the gap between the click and the answer is where
    // the card used to render nothing at all.
    let resolve: (value: unknown) => void = () => {}
    apiGet.mockReturnValueOnce(new Promise((r) => { resolve = r }))
    await button(view, 'Check again').trigger('click')
    await flushPromises()

    expect(view.get('[role="status"]').text()).toContain('Checking this host')
    // The failed read is gone with the retry, and no position is claimed while
    // the host has not answered.
    expect(view.find('[role="alert"]').exists()).toBe(false)
    expect(view.find('.login-state').exists()).toBe(false)

    resolve(status())
    await flushPromises()
    expect(view.text()).toContain('Does not start when I sign in')
  })

  it('treats a body this page cannot read as a failed read, not as a position', async () => {
    // A partial payload is how "the machine did not say" would arrive by
    // accident: `enabled` missing is not `enabled: false`.
    apiGet.mockResolvedValueOnce({ platform: 'macos', supported: true })
    const view = await mountPanel()

    expect(view.get('[role="alert"]').text()).toContain('cannot read')
    expect(view.find('.login-state').exists()).toBe(false)
  })

  it('keeps the last known state when a later read fails', async () => {
    // The re-read a refused write offers is a read like any other, and it can
    // be the one that fails. The row is still the best answer there is.
    const view = await mountPanel()
    apiPatch.mockRejectedValue(failure(503, 'the operating system refused the change', { enabled: false }))
    await button(view, 'Enable at sign-in').trigger('click')
    await flushPromises()

    apiGet.mockRejectedValueOnce(new Error('the engine did not answer'))
    await button(view, 'Check again').trigger('click')
    await flushPromises()

    expect(stateText(view)).toContain('Does not start when I sign in')
    expect(view.get('.hint--warn[role="alert"]').text()).toContain('Showing the last known state')
  })
})

describe('SettingsEngineLogin when the host has an answer', () => {
  it('names the two positions in words', async () => {
    apiGet.mockResolvedValue(status({ enabled: false }))
    const off = await mountPanel()
    expect(stateText(off)).toContain('Does not start when I sign in')
    expect(button(off, 'Enable at sign-in').exists()).toBe(true)

    off.unmount()
    mounted.pop()

    apiGet.mockResolvedValue(status({ enabled: true }))
    const on = await mountPanel()
    expect(stateText(on)).toContain('Starts when I sign in')
    expect(button(on, 'Disable at sign-in').exists()).toBe(true)
  })

  it('reads null as Unknown with the reason, never as On or Off', async () => {
    apiGet.mockResolvedValue(status({
      enabled: null,
      can_change: false,
      reason: 'launchctl print-disabled gui/501 could not be run: not found',
    }))
    const view = await mountPanel()

    expect(stateText(view)).toContain('Unknown')
    expect(stateText(view)).not.toContain('Off')
    expect(stateText(view)).not.toContain('On')
    expect(view.get('.login-detail').text()).toContain('launchctl print-disabled')
  })

  it('keeps the three things apart in the copy', async () => {
    const view = await mountPanel()
    const text = view.text()

    // The next sign-in, and explicitly not the running engine.
    expect(text).toContain('That is the next sign-in')
    expect(text).toContain('never starts or stops the engine you are talking to now')
    // The installed app window is a separate, optional thing.
    expect(text).toContain('Use Ciaobot as an app')
    // Neither a promise of an always-running engine nor of offline use.
    expect(text.toLowerCase()).not.toContain('offline')
    expect(text.toLowerCase()).not.toContain('always')
  })
})

describe('SettingsEngineLogin without a licence to write', () => {
  it('says the service is not installed, shows the setup command, and offers no toggle', async () => {
    apiGet.mockResolvedValue(status({
      installed: false,
      enabled: null,
      can_change: false,
      reason: 'The Ciaobot engine service is not installed for this user.',
      setup_command: 'ciao service start --workspace /Users/me/Ciao',
    }))
    const view = await mountPanel()

    expect(stateText(view)).toContain('Not installed for this user')
    expect(view.get('code.login-command-text').text()).toBe('ciao service start --workspace /Users/me/Ciao')
    expect(actionButton(view).exists()).toBe(false)
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('copies the setup command with the helper the other settings panels use', async () => {
    apiGet.mockResolvedValue(status({
      installed: false,
      enabled: null,
      can_change: false,
      setup_command: 'ciao service start --workspace /Users/me/Ciao',
    }))
    const view = await mountPanel()

    await button(view, 'Copy').trigger('click')
    await flushPromises()

    expect(writeClipboard).toHaveBeenCalledWith('ciao service start --workspace /Users/me/Ciao')
    // Copying is not a change, and never reaches the route.
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('shows the reason and no toggle when can_change is false', async () => {
    apiGet.mockResolvedValue(status({
      enabled: null,
      can_change: false,
      reason: 'The engine task serves /Users/someone-else/Ciao, not this workspace.',
    }))
    const view = await mountPanel()

    expect(view.get('.login-detail').text()).toContain('The engine task serves /Users/someone-else/Ciao')
    // A switch that cannot be moved is a dead control, so there is none.
    expect(actionButton(view).exists()).toBe(false)
    expect(view.findAll('button').map((b) => b.text())).not.toContain('Enable at sign-in')
  })

  it('gives guidance and no toggle on a platform with no service backend', async () => {
    apiGet.mockResolvedValue(status({
      platform: 'linux',
      supported: false,
      installed: null,
      enabled: null,
      can_change: false,
      reason: 'Ciaobot does not read or change a service on Linux: the unit is enabled by hand (see docs/LINUX.md).',
    }))
    const view = await mountPanel()

    expect(stateText(view)).toContain('Not read on this platform')
    expect(view.get('.login-detail').text()).toContain('docs/LINUX.md')
    expect(actionButton(view).exists()).toBe(false)
    expect(apiPatch).not.toHaveBeenCalled()
  })
})

describe('SettingsEngineLogin when the change is accepted', () => {
  it('PATCHes exactly {"enabled": true} from false and shows the state it was given back', async () => {
    const view = await mountPanel()

    await button(view, 'Enable at sign-in').trigger('click')
    await flushPromises()

    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(apiPatch).toHaveBeenCalledWith('/api/service/login', { enabled: true })
    // One key, and the boolean itself: not the request echoed back as text.
    expect(apiPatch.mock.calls[0][1]).toEqual({ enabled: true })
    expect(stateText(view)).toContain('Starts when I sign in')
    expect(view.get('[role="status"]').text()).toContain('set to start when you sign in')
    // The control inverts to the state the machine re-read, not to the click.
    expect(button(view, 'Disable at sign-in').exists()).toBe(true)
  })

  it('PATCHes exactly {"enabled": false} from true', async () => {
    apiGet.mockResolvedValue(status({ enabled: true }))
    apiPatch.mockResolvedValue(status({ enabled: false }))
    const view = await mountPanel()

    await button(view, 'Disable at sign-in').trigger('click')
    await flushPromises()

    expect(apiPatch).toHaveBeenCalledWith('/api/service/login', { enabled: false })
    expect(stateText(view)).toContain('Does not start when I sign in')
  })

  it('locks the control while the write is in flight', async () => {
    let resolve: (value: unknown) => void = () => {}
    apiPatch.mockReturnValue(new Promise((r) => { resolve = r }))
    const view = await mountPanel()

    await button(view, 'Enable at sign-in').trigger('click')
    await flushPromises()

    const inFlight = actionButton(view)
    expect(inFlight.attributes('disabled')).toBeDefined()
    expect(inFlight.attributes('aria-busy')).toBe('true')
    // A second click cannot queue a second write.
    await inFlight.trigger('click')
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledTimes(1)

    resolve(ENABLED)
    await flushPromises()
    expect(actionButton(view).attributes('disabled')).toBeUndefined()
    expect(stateText(view)).toContain('Starts when I sign in')
  })
})

describe('SettingsEngineLogin when the change does not happen', () => {
  it('keeps the position it had, and shows the refusal beside the fresh state', async () => {
    apiPatch.mockRejectedValue(
      failure(409, 'the change is not this engine’s to make', {
        enabled: false,
        can_change: false,
        reason: 'This definition was configured by hand.',
      }),
    )
    const view = await mountPanel()

    await button(view, 'Enable at sign-in').trigger('click')
    await flushPromises()

    // Never flipped: the row shows what the machine still says.
    expect(stateText(view)).toContain('Does not start when I sign in')
    expect(stateText(view)).not.toContain('Starts when I sign in')
    expect(view.get('[role="alert"]').text()).toContain('the change is not this engine’s to make')
    // The refusal replaced the licence, so the control is gone rather than dead.
    expect(actionButton(view).exists()).toBe(false)
    // And the retry re-reads the truth instead of repeating the write.
    await button(view, 'Check again').trigger('click')
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(apiGet).toHaveBeenCalledTimes(2)
  })

  it('reports an unconfirmed change as unconfirmed, not as done', async () => {
    // 503: the OS refused, or the re-read did not agree. The route answers with
    // the state it did read, which is the one that goes on screen.
    apiPatch.mockRejectedValue(
      failure(503, 'the operating system refused the change', { enabled: false }),
    )
    const view = await mountPanel()

    await button(view, 'Enable at sign-in').trigger('click')
    await flushPromises()

    expect(stateText(view)).toContain('Does not start when I sign in')
    expect(view.get('[role="alert"]').text()).toContain('the operating system refused the change')
    // Nothing claims the change landed.
    expect(view.find('[role="status"]').exists()).toBe(false)
    // Still changeable, so the control is there and still says the old thing.
    expect(button(view, 'Enable at sign-in').exists()).toBe(true)
  })

  it('drops the last write’s line when a read starts, so neither can outlive the row it described', async () => {
    // A refusal is the reachable half of this; a success line is cleared by the
    // same load, and both are gone here because the read is still in flight.
    apiPatch.mockRejectedValue(
      failure(409, 'the change is not this engine’s to make', {
        enabled: false,
        can_change: false,
        reason: 'This definition was configured by hand.',
      }),
    )
    const view = await mountPanel()
    await button(view, 'Enable at sign-in').trigger('click')
    await flushPromises()
    expect(view.get('[role="alert"]').text()).toContain('the change is not this engine’s to make')

    let resolve: (value: unknown) => void = () => {}
    apiGet.mockReturnValueOnce(new Promise((r) => { resolve = r }))
    await button(view, 'Check again').trigger('click')
    await flushPromises()

    // Nothing of the write is left beside the row it used to describe.
    expect(view.find('[role="alert"]').exists()).toBe(false)
    expect(view.find('[role="status"]').exists()).toBe(false)
    // The row itself stays: a refresh keeps the position the machine last read.
    expect(stateText(view)).toContain('Does not start when I sign in')

    resolve(status({ can_change: true }))
    await flushPromises()
    expect(stateText(view)).toContain('Does not start when I sign in')
    expect(button(view, 'Enable at sign-in').exists()).toBe(true)
    expect(view.find('[role="alert"]').exists()).toBe(false)
  })

  it('keeps the position it had when the failure carries no status to show', async () => {
    apiPatch.mockRejectedValue(Object.assign(new Error('network gone'), { status: 503, payload: {} }))
    const view = await mountPanel()

    await button(view, 'Enable at sign-in').trigger('click')
    await flushPromises()

    expect(stateText(view)).toContain('Does not start when I sign in')
    expect(view.get('[role="alert"]').text()).toContain('network gone')
  })
})
