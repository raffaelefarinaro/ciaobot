// @vitest-environment jsdom
//
// Settings → General → "Memory backup", mounted on its own: the panel owns its
// data and its actions, so `api` and the clipboard are the only inputs and both
// are stubbed here.
//
// The two claims this card makes are the ones worth pinning. A memory is
// ALWAYS saved on the device it was written on; the online copy is a second
// copy that can be behind, offline, or refused — and a card that blurs the two
// tells someone their memory is safe when it is not. The other is the setup
// pair: "Set up in Ciaobot" sends the canonical prompt into a chat exactly
// once, and "Copy setup prompt" hands the same text to the clipboard with no
// provider and no repository required.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SettingsMemoryBackup from '../settings/SettingsMemoryBackup.vue'

// Held out of the factory so these tests can count calls: the refresh budget is
// the one behaviour a plain assertion cannot see.
const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const writeClipboard = vi.hoisted(() => vi.fn(async () => true))

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch },
}))

vi.mock('../../lib/codeCopy', () => ({
  writeClipboard,
}))

/** The #689 status contract, shaped the way the route serves it. */
function status(overrides: Record<string, unknown> = {}) {
  return {
    state: 'not_configured',
    scope: 'memory-vault, skills',
    branch: '',
    remote: '',
    last_remote: '',
    enabled: true,
    interval_s: 300,
    last_attempt_at: '',
    last_success_at: '',
    last_success_commit: '',
    pending_changes: 0,
    pending_commits: 0,
    reason: "the data root has no 'origin' remote yet",
    ...overrides,
  }
}

const CONFIGURED = status({
  state: 'ready',
  branch: 'main',
  remote: 'https://github.com/person/memory.git',
  last_success_at: new Date(Date.now() - 60_000).toISOString(),
  last_success_commit: 'abc1234',
  reason: 'up to date',
})

const PROMPT = { context: { folder: '/Users/me/Ciao' }, prompt: 'Set up the private repository.' }

// Several tests mount a second panel to compare two states side by side, so the
// stack is unmounted in the afterEach rather than the last one alone.
const mounted: VueWrapper[] = []

async function mountPanel() {
  const view = mount(SettingsMemoryBackup)
  mounted.push(view)
  await flushPromises()
  return view
}

function button(view: VueWrapper, label: string) {
  const match = view.findAll('button').find((b) => b.text() === label)
  if (!match) throw new Error(`no button labelled ${label}`)
  return match
}

function row(view: VueWrapper, label: string) {
  const match = view.findAll('.set-subrow').find((r) => r.text().startsWith(label))
  if (!match) throw new Error(`no row labelled ${label}`)
  return match
}

beforeEach(() => {
  apiGet.mockReset().mockResolvedValue(status())
  apiPost.mockReset().mockResolvedValue({ ok: true, chat_id: 'c-1', reused: false })
  apiPatch.mockReset().mockResolvedValue(CONFIGURED)
  writeClipboard.mockReset().mockResolvedValue(true)
})

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('SettingsMemoryBackup when no repository is connected', () => {
  it('says the memory is safe here and offers both setup actions', async () => {
    const view = await mountPanel()

    expect(view.text()).toContain(
      'Your memory is saved on this device. Connect a private GitHub repository to back it up automatically.',
    )
    expect(button(view, 'Set up in Ciaobot').classes()).toContain('btn-primary')
    expect(button(view, 'Copy setup prompt').classes()).toContain('btn-secondary')
    // No configured-only control leaks into the unconfigured view.
    expect(view.findAll('button').map((b) => b.text())).not.toContain('Back up now')
  })

  it('sends the setup prompt into a chat exactly once and disables while in flight', async () => {
    let resolve: (value: unknown) => void = () => {}
    apiPost.mockReturnValue(
      new Promise((r) => { resolve = r }),
    )
    const view = mount(SettingsMemoryBackup)
    await flushPromises()

    const setup = button(view, 'Set up in Ciaobot')
    await setup.trigger('click')
    await flushPromises()

    // A second click while the first is in flight must not dispatch twice.
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/local/backup/setup-chat')
    expect(setup.attributes('disabled')).toBeDefined()
    expect(setup.text()).toBe('Starting setup...')

    resolve({ ok: true, chat_id: 'c-1', reused: false })
    await flushPromises()
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(view.text()).toContain('Ciao is setting this up in a chat')
  })

  it('offers the chat it opened, and re-entry is not a second setup', async () => {
    apiPost.mockResolvedValue({ ok: true, chat_id: 'c-9', reused: true })
    const view = await mountPanel()
    await button(view, 'Set up in Ciaobot').trigger('click')
    await flushPromises()

    expect(view.text()).toContain('Reopened the setup chat that was already running')
    const opened = view.findAll('button').find((b) => b.text() === 'Open the setup chat')
    expect(opened).toBeTruthy()
    await opened!.trigger('click')
    expect(view.emitted('open-chat')?.[0]).toEqual(['c-9'])
  })

  it('copies the canonical prompt with no provider and no repository in play', async () => {
    apiGet.mockImplementation(async (path: string) =>
      (path === '/api/local/backup/setup-prompt' ? PROMPT : status()),
    )
    const view = await mountPanel()
    await button(view, 'Copy setup prompt').trigger('click')
    await flushPromises()

    expect(apiGet).toHaveBeenCalledWith('/api/local/backup/setup-prompt')
    expect(writeClipboard).toHaveBeenCalledWith(PROMPT.prompt)
    expect(view.find('[role="status"]').text()).toContain('Setup prompt copied')
  })

  it('says so when the clipboard refuses, rather than claiming a copy', async () => {
    apiGet.mockImplementation(async (path: string) =>
      (path === '/api/local/backup/setup-prompt' ? PROMPT : status()),
    )
    writeClipboard.mockResolvedValue(false)
    const view = await mountPanel()
    await button(view, 'Copy setup prompt').trigger('click')
    await flushPromises()

    expect(writeClipboard).toHaveBeenCalledWith(PROMPT.prompt)
    expect(view.find('[role="alert"]').text()).toContain('the browser blocked the copy')
    expect(view.text()).not.toContain('Setup prompt copied')
  })

  it('reports a setup that could not start', async () => {
    apiPost.mockRejectedValue(
      Object.assign(new Error('unauthorized'), { payload: { error: 'no General project' } }),
    )
    const view = await mountPanel()
    await button(view, 'Set up in Ciaobot').trigger('click')
    await flushPromises()

    expect(view.find('[role="alert"]').text()).toContain('no General project')
  })
})

describe('SettingsMemoryBackup with a repository connected', () => {
  beforeEach(() => {
    apiGet.mockResolvedValue(CONFIGURED)
  })

  it('shows the scope, the last verified online backup, and both controls', async () => {
    const view = await mountPanel()

    expect(row(view, 'What is backed up').text()).toContain('memory-vault, skills')
    expect(row(view, 'Last online backup').text()).toMatch(/minute|second|just now/)
    // The two copies named apart, not merged into one claim.
    expect(row(view, 'On this computer').text()).toContain('Saved here every time')
    expect(button(view, 'Back up now').classes()).toContain('btn-primary')
    expect(button(view, 'Pause').classes()).toContain('btn-secondary')
    expect(row(view, 'Automatic').text()).toContain('Automatic backup every 5 minutes')
  })

  it('links a web repository and leaves anything else as text', async () => {
    const view = await mountPanel()
    const link = row(view, 'Repository').find('a')
    expect(link.attributes('href')).toBe('https://github.com/person/memory.git')
    expect(link.attributes('rel')).toContain('noopener')

    // An scp-style origin is not a URL a browser should be told to navigate to.
    apiGet.mockResolvedValue(status({ state: 'ready', remote: 'git@github.com:person/memory.git' }))
    const other = await mountPanel()
    expect(row(other, 'Repository').find('a').exists()).toBe(false)
    expect(row(other, 'Repository').find('code').text()).toBe('git@github.com:person/memory.git')
  })

  it('names where the last run pushed when the live remote is empty', async () => {
    apiGet.mockResolvedValue(
      status({ state: 'paused', remote: '', last_remote: 'https://github.com/person/memory.git', reason: 'backup is paused' }),
    )
    const view = await mountPanel()
    expect(row(view, 'Repository').text()).toContain('https://github.com/person/memory.git')
  })

  it('backs up now and adopts the state the run reports', async () => {
    apiPost.mockResolvedValue(status({ state: 'pending', pending_commits: 2, reason: 'waiting: 2 commit(s) not yet on origin' }))
    const view = await mountPanel()
    await button(view, 'Back up now').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/local/backup/run')
    expect(view.find('.backup-state').text()).toContain('Waiting to upload')
    expect(view.find('[role="status"]').text()).toContain('Backup finished')
  })

  it('reads a run that could not do its job off the response, not off a guess', async () => {
    // A 400 answers with the same status object, so the reason is in the body.
    apiPost.mockRejectedValue(
      Object.assign(new Error('Bad Request'), {
        payload: status({ state: 'offline', reason: 'origin did not answer: connection refused' }),
      }),
    )
    const view = await mountPanel()
    await button(view, 'Back up now').trigger('click')
    await flushPromises()

    expect(view.find('.backup-state').text()).toContain('Offline')
    expect(view.find('.backup-reason').text()).toContain('connection refused')
    expect(view.find('[role="alert"]').text()).toContain('Your memory is still safe on this computer')
  })

  it('pauses and resumes through the owner switch', async () => {
    const view = await mountPanel()
    await button(view, 'Pause').trigger('click')
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledWith('/api/local/backup', { paused: true })
    expect(view.find('[role="status"]').text()).toContain(
      'Automatic backups are off. The copy on this computer is unaffected.',
    )

    // A paused status offers Resume, not Pause again.
    apiGet.mockResolvedValue(status({ state: 'paused', remote: 'https://github.com/p/m.git', reason: 'backup is paused' }))
    const paused = await mountPanel()
    await button(paused, 'Resume').trigger('click')
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledWith('/api/local/backup', { paused: false })
    expect(paused.find('[role="status"]').text()).toContain('Automatic backups are running again.')
  })

  it('turns a disabled backup back on', async () => {
    apiGet.mockResolvedValue(status({ state: 'paused', enabled: false, remote: 'https://github.com/p/m.git', reason: 'backup is turned off' }))
    const view = await mountPanel()
    await button(view, 'Turn on').trigger('click')
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledWith('/api/local/backup', { enabled: true })
    // Turning them on must not announce that they are off: this is the
    // recovery path, and the sentence is read out by a screen reader.
    expect(view.find('[role="status"]').text()).toContain('Automatic backups are running again')
  })
})

describe('SettingsMemoryBackup status states', () => {
  it.each([
    ['ready', 'Backed up'],
    ['running', 'Backing up now'],
    ['offline', 'Offline'],
    ['needs_attention', 'Needs attention'],
    ['paused', 'Paused'],
  ])('renders %s in its own words', async (state, label) => {
    apiGet.mockResolvedValue(
      status({ state, remote: 'https://github.com/p/m.git', reason: `because ${state}` }),
    )
    const view = await mountPanel()
    expect(view.find('.backup-state').text()).toContain(label)
    // The service's own reason is kept: it names the specific thing that is wrong.
    expect(view.find('.backup-reason').text()).toBe(`because ${state}`)
  })

  it('separates a local save still to commit from one waiting to upload', async () => {
    apiGet.mockResolvedValue(
      status({ state: 'pending', remote: 'https://github.com/p/m.git', pending_changes: 3, pending_commits: 0 }),
    )
    expect((await mountPanel()).find('.backup-state').text()).toContain('Saving locally')

    apiGet.mockResolvedValue(
      status({ state: 'pending', remote: 'https://github.com/p/m.git', pending_changes: 0, pending_commits: 1 }),
    )
    expect((await mountPanel()).find('.backup-state').text()).toContain('Waiting to upload')
  })

  it('drops the "up to date" echo under a label that already says it', async () => {
    const view = await mountPanel()
    expect(view.find('.backup-reason').exists()).toBe(false)
  })

  it('does not promise a run that is gated behind a setup', async () => {
    // With a repository, "never" means a run is still due. Without one it means
    // the setup has not happened, and the unconfigured half owns that sentence.
    apiGet.mockResolvedValue(
      status({ state: 'ready', remote: 'https://github.com/p/m.git', reason: 'up to date' }),
    )
    const view = await mountPanel()
    expect(row(view, 'Last online backup').text()).toContain('has not reached the repository yet')

    apiGet.mockResolvedValue(status({ state: 'not_configured', reason: 'no origin' }))
    const unconfigured = await mountPanel()
    // No status rows at all: the sentence about a next run belongs to the
    // configured half, where there is one to wait for.
    expect(unconfigured.findAll('.set-subrow')).toHaveLength(0)
  })
})

describe('SettingsMemoryBackup load states', () => {
  beforeEach(() => {
    apiGet.mockResolvedValue(CONFIGURED)
  })

  it('offers a retry instead of claiming there is no backup', async () => {
    apiGet.mockRejectedValue(new Error('Failed to fetch'))
    const view = await mountPanel()

    expect(view.find('[role="alert"]').text()).toContain('Failed to fetch')
    expect(view.text()).not.toContain('Your memory is saved on this device')
    // Named for what it re-reads rather than a bare "Retry": this page already
    // has another card whose only action is a Retry, and two identically named
    // controls on one Settings page tell a screen-reader user nothing about
    // which one they are on.
    expect(button(view, 'Check again')).toBeTruthy()
    expect(view.findAll('button').map((b) => b.text())).not.toContain('Retry')
  })

  it('recovers on a good re-read', async () => {
    apiGet.mockRejectedValueOnce(new Error('Failed to fetch'))
    const view = await mountPanel()
    expect(view.text()).not.toContain('Your memory is saved on this device')

    apiGet.mockResolvedValueOnce(status())
    await button(view, 'Check again').trigger('click')
    await flushPromises()

    expect(view.find('[role="alert"]').exists()).toBe(false)
    expect(view.text()).toContain('Your memory is saved on this device')
  })

  it('keeps the rows it has when a refresh fails, and says they are stale', async () => {
    const view = await mountPanel()
    expect(row(view, 'What is backed up').text()).toContain('memory-vault')

    apiGet.mockRejectedValue(new Error('offline'))
    await button(view, 'Check again').trigger('click')
    await flushPromises()

    expect(row(view, 'What is backed up').text()).toContain('memory-vault')
    expect(view.text()).toContain('Showing the last known status')
    expect(view.find('[role="alert"]').text()).toContain('offline')
  })

  it('re-reads on demand, and a good read clears the stale notice', async () => {
    const view = await mountPanel()
    apiGet.mockRejectedValue(new Error('offline'))
    await button(view, 'Check again').trigger('click')
    await flushPromises()
    expect(view.text()).toContain('Showing the last known status')

    apiGet.mockResolvedValue(
      status({ state: 'offline', remote: 'https://github.com/p/m.git', reason: 'origin did not answer' }),
    )
    await button(view, 'Check again').trigger('click')
    await flushPromises()

    expect(view.text()).not.toContain('Showing the last known status')
    expect(view.find('.backup-state').text()).toContain('Offline')
  })

  it('keeps a failed action from being cleared by a later refresh', async () => {
    apiPatch.mockRejectedValue(new Error('the switch did not move'))
    const view = await mountPanel()
    await button(view, 'Pause').trigger('click')
    await flushPromises()

    expect(view.find('[role="alert"]').text()).toContain('the switch did not move')
    expect(row(view, 'What is backed up').text()).toContain('memory-vault')
  })
})

describe('SettingsMemoryBackup refresh', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  it('re-reads while unconfigured so a finished setup turns the section over', async () => {
    const view = mount(SettingsMemoryBackup)
    mounted.push(view)
    await vi.advanceTimersByTimeAsync(0)
    expect(apiGet).toHaveBeenCalledTimes(1)
    expect(view.text()).toContain('Your memory is saved on this device')

    // The setup chat lands: the bounded re-read is what notices.
    apiGet.mockResolvedValue(CONFIGURED)
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()

    expect(view.find('.backup-state').text()).toContain('Backed up')
    // Configured means no further re-reading: this is a bounded refresh of an
    // unconfigured install, not a second polling loop over a live one.
    const reads = apiGet.mock.calls.length
    await vi.advanceTimersByTimeAsync(60_000)
    expect(apiGet.mock.calls.length).toBe(reads)
  })

  it('stops re-reading once the bounded budget is spent', async () => {
    const view = mount(SettingsMemoryBackup)
    mounted.push(view)
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(36 * 10_000)
    await flushPromises()

    // One first read plus the bounded re-reads, and then it stops: a user who
    // never sets this up must not keep the panel asking forever.
    expect(apiGet.mock.calls.length).toBe(37)
    const reads = apiGet.mock.calls.length
    await vi.advanceTimersByTimeAsync(120_000)
    expect(apiGet.mock.calls.length).toBe(reads)
    // And the panel is still honest about what it last read.
    expect(view.text()).toContain('Your memory is saved on this device')
  })

  it('stops re-reading when the pane goes away', async () => {
    const view = mount(SettingsMemoryBackup)
    await vi.advanceTimersByTimeAsync(0)
    view.unmount()

    await vi.advanceTimersByTimeAsync(120_000)
    expect(apiGet).toHaveBeenCalledTimes(1)
  })

  it('drops a setup message when the install becomes configured', async () => {
    apiGet.mockImplementation(async (path: string) =>
      (path === '/api/local/backup/setup-prompt' ? PROMPT : status()),
    )
    const view = mount(SettingsMemoryBackup)
    mounted.push(view)
    await vi.advanceTimersByTimeAsync(0)
    await button(view, 'Copy setup prompt').trigger('click')
    await flushPromises()
    expect(view.text()).toContain('Setup prompt copied')

    // The copy result is about a control that no longer exists once the
    // repository is connected. Leaving it under a live backup status would be
    // a claim about a different moment, so it lives only in the half of the
    // card that offers the control.
    apiGet.mockImplementation(async (path: string) =>
      (path === '/api/local/backup/setup-prompt' ? PROMPT : CONFIGURED),
    )
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()

    expect(view.find('.backup-state').text()).toContain('Backed up')
    expect(view.text()).not.toContain('Setup prompt copied')
  })
})

describe('SettingsMemoryBackup guide', () => {
  it('opens the short guide and moves focus into it', async () => {
    const view = mount(SettingsMemoryBackup, { attachTo: document.body })
    mounted.push(view)
    await flushPromises()
    const guide = view.find('details.backup-guide')
    expect(guide.attributes('open')).toBeUndefined()

    const link = view.findAll('button').find((b) => b.text() === 'How memory backup works')!
    await link.trigger('click')
    await flushPromises()

    expect(guide.attributes('open')).toBeDefined()
    const body = view.find('.backup-guide-body')
    expect(document.activeElement).toBe(body.element)
  })

  it('covers both setup actions, the two copies, and where the repository is', async () => {
    const view = await mountPanel()
    const text = view.find('.backup-guide-body').text()
    for (const phrase of [
      'Set up in Ciaobot',
      'Copy setup prompt',
      'Ciaobot always saves to this computer first',
      'the machine Ciaobot is running on',
      'Back up now',
      'Repository',
      'Last online backup',
    ]) {
      expect(text).toContain(phrase)
    }
  })
})
