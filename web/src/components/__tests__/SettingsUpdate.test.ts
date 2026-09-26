// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'
import SettingsView from '../SettingsView.vue'
import type { EngineUpdateOperation, EngineUpdateStatus } from '../../lib/types'

afterEach(() => vi.restoreAllMocks())

function operation(phase: string, extra: Partial<EngineUpdateOperation> = {}): EngineUpdateOperation {
  return {
    id: '20260926T101500Z-ab12',
    phase,
    from_version: '0.19.0',
    to_version: '0.20.0',
    started_at: '2026-09-26T10:15:00+00:00',
    updated_at: '2026-09-26T10:15:20+00:00',
    ...extra,
  }
}

/** How long the card waits between `/api/update/status` reads. */
const POLL_MS = 2000

/**
 * Mount the Updates card against one update-status answer and one package
 * answer. Everything else the view asks for is refused, exactly as the other
 * Settings tests do, so a test only ever sees the two it cares about.
 *
 * `update` may be a list, which the card reads in order and then keeps reading
 * the last of: that is how a run is driven forward across polls, one record at
 * a time. `packageStatus` takes a list the same way, for the reads a settled
 * record makes. `attach` puts the view in the document, which focus assertions
 * need.
 */
async function mountCard(
  update: EngineUpdateStatus | EngineUpdateStatus[],
  packageStatus: Record<string, unknown> | Record<string, unknown>[] = { mode: 'installer', current_version: '0.19.0', update_available: true, latest_version: '0.20.0' },
  { attach = false }: { attach?: boolean } = {},
) {
  setActivePinia(createPinia())
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: { template: '<div />' } }] })
  await router.push('/settings')
  await router.isReady()
  const answers = Array.isArray(update) ? [...update] : [update]
  const packageAnswers = Array.isArray(packageStatus) ? [...packageStatus] : [packageStatus]
  let reads = 0
  let packageReads = 0
  vi.spyOn(api, 'get').mockImplementation(async (path) => {
    if (path === '/api/startup-status') return { node_role: 'host', state_valid: true } as never
    if (path === '/api/local/status') return { git_repo: true, branch: 'main', dirty: false, restart_only: true } as never
    if (path === '/api/update/status') {
      const answer = answers[Math.min(reads, answers.length - 1)]
      reads += 1
      return answer as never
    }
    if (path === '/api/package/status') {
      const answer = packageAnswers[Math.min(packageReads, packageAnswers.length - 1)]
      packageReads += 1
      return answer as never
    }
    throw new Error('Unrelated settings data unavailable in this test')
  })
  const post = vi.spyOn(api, 'post').mockResolvedValue({ started: true } as never)
  const beginRestart = vi.spyOn(useProjectStore(), 'beginServerRestart').mockImplementation(() => {})
  const wrapper = mount(SettingsView, {
    global: { plugins: [router], stubs: { Teleport: true } },
    attachTo: attach ? document.body : undefined,
  })
  await flushPromises()
  // How many times the card has read the status route, which is the poll's own
  // scoreboard: a run whose record has not landed yet is only visible as a read
  // that still happened.
  const statusReads = () => reads
  return { wrapper, post, beginRestart, statusReads }
}

function buttonLabels(wrapper: Awaited<ReturnType<typeof mountCard>>['wrapper']) {
  return wrapper.findAll('button').map(b => b.text())
}

function button(wrapper: Awaited<ReturnType<typeof mountCard>>['wrapper'], label: string) {
  return wrapper.findAll('button').find(b => b.text() === label)
}

/** Run the card's poll once, the way the clock would. */
async function poll() {
  await vi.advanceTimersByTimeAsync(POLL_MS)
  await flushPromises()
}

it('offers Stage for an installer engine with an update available', async () => {
  const { wrapper, post } = await mountCard({ install_mode: 'installer', can_update: true, operation: null })
  try {
    expect(buttonLabels(wrapper)).toContain('Stage update')
    await button(wrapper, 'Stage update')!.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/update/stage')
  } finally {
    wrapper.unmount()
  }
})

it('shows real progress from the persisted phase, with no invented timer', async () => {
  vi.useFakeTimers()
  try {
    const { wrapper } = await mountCard({
      install_mode: 'installer',
      can_update: true,
      operation: operation('swapping'),
    })
    // The record's phases, in the coordinator's order: eight behind the swap are
    // done, the swap is live, and nothing advances behind it.
    expect(wrapper.find('.update-progress-overlay').exists()).toBe(true)
    expect(wrapper.findAll('.update-progress-log-row').map(r => r.classes().find(c => c.startsWith('is-'))))
      .toEqual([
        'is-done', 'is-done', 'is-done', 'is-done', 'is-done', 'is-done', 'is-done', 'is-done',
        'is-in_progress', 'is-pending', 'is-pending', 'is-pending',
      ])
    expect(wrapper.find('.update-progress-phase').text()).toBe('Swapping in the new version')
    expect(buttonLabels(wrapper)).not.toContain('Stage update')
    expect(buttonLabels(wrapper)).not.toContain('Apply update')
    wrapper.unmount()
  } finally {
    vi.useRealTimers()
  }
})

it('shows the failure error and no success line for a rolled-back job', async () => {
  const { wrapper } = await mountCard({
    install_mode: 'installer',
    can_update: true,
    operation: operation('rolled_back', { error: 'the new engine never answered' }),
  })
  try {
    expect(wrapper.text()).toContain('the new engine never answered')
    expect(wrapper.text()).not.toContain('up to date')
    expect(wrapper.find('.update-progress-overlay').exists()).toBe(false)
    // A retry path, and the version the engine went back to.
    expect(buttonLabels(wrapper)).toContain('Stage again')
    expect(wrapper.text()).toContain('Back on v0.19.0')
  } finally {
    wrapper.unmount()
  }
})

it('offers a retry for a record that failed without a reason to show', async () => {
  const { wrapper } = await mountCard({
    install_mode: 'installer',
    can_update: true,
    operation: operation('failed'),
  })
  try {
    expect(buttonLabels(wrapper)).toContain('Retry')
    expect(wrapper.text()).not.toContain('up to date')
  } finally {
    wrapper.unmount()
  }
})

it('applies a staged job and starts the restart overlay', async () => {
  const { wrapper, post, beginRestart } = await mountCard({
    install_mode: 'installer',
    can_update: true,
    operation: operation('staged'),
  })
  try {
    expect(buttonLabels(wrapper)).toContain('Apply update')
    await button(wrapper, 'Apply update')!.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/update/apply')
    // The apply drains and reboots the engine, so it needs the same overlay a
    // Settings restart uses; a second one would be a second reload loop.
    expect(beginRestart).toHaveBeenCalledWith('Updating Ciaobot… the engine will restart')
  } finally {
    wrapper.unmount()
  }
})

it('offers Apply alone for a staged job, because staging one is refused', async () => {
  const { wrapper, post } = await mountCard({
    install_mode: 'installer',
    can_update: true,
    operation: operation('staged'),
  })
  try {
    // `/api/update/stage` answers 409 for a non-terminal record by design, so a
    // Re-stage button here could never succeed. Stage again is offered from the
    // terminal failure phases, where a new record may be written.
    const labels = buttonLabels(wrapper)
    expect(labels).toContain('Apply update')
    expect(labels.filter(label => /stage/i.test(label))).toEqual([])
    expect(post).not.toHaveBeenCalled()
  } finally {
    wrapper.unmount()
  }
})

it('recovers from a refusal that never wrote a record', async () => {
  vi.useFakeTimers()
  // A stage POST is answered 202 before the record exists, so release
  // resolution or locking can fail with the card holding a null operation and a
  // reason. Nothing will ever arrive on a poll for that, so the card has to stop
  // polling and offer the action again.
  const { wrapper, post } = await mountCard([
    { install_mode: 'installer', can_update: true, operation: null },
    { install_mode: 'installer', can_update: true, operation: null, error: 'the latest release could not be resolved' },
  ])
  try {
    await button(wrapper, 'Stage update')!.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/update/stage')

    await poll()
    expect(wrapper.text()).toContain('the latest release could not be resolved')

    // The retry is enabled, which is the whole point: the poll has stopped, so
    // nothing is holding the card busy any more.
    const retry = button(wrapper, 'Stage update')
    expect(retry).toBeDefined()
    expect(retry!.attributes('disabled')).toBeUndefined()
    await retry!.trigger('click')
    await flushPromises()
    expect(post.mock.calls.filter(call => call[0] === '/api/update/stage')).toHaveLength(2)
    wrapper.unmount()
  } finally {
    vi.useRealTimers()
  }
})

it('moves focus into the overlay while it is up and back to the card when it settles', async () => {
  vi.useFakeTimers()
  // The applying half is the takeover: the engine is replacing itself, so the
  // overlay is modal for as long as that lasts.
  const { wrapper } = await mountCard([
    { install_mode: 'installer', can_update: true, operation: null },
    { install_mode: 'installer', can_update: true, operation: operation('draining') },
    { install_mode: 'installer', can_update: true, operation: operation('staged') },
  ], undefined, { attach: true })
  try {
    const stage = button(wrapper, 'Stage update')!
    // A keyboard activation leaves focus on the button it activated.
    stage.element.focus()
    await stage.trigger('click')
    await flushPromises()
    expect(wrapper.find('.engine-update-overlay').exists()).toBe(false)

    await poll()
    const overlay = wrapper.get('.engine-update-overlay').element as HTMLElement
    const statusRegion = wrapper.get('.update-progress-content').element as HTMLElement
    const body = wrapper.get('.pane-body').element as HTMLElement
    // Focus lands on the overlay's status region, not on the inert layer.
    expect(document.activeElement).toBe(statusRegion)
    expect(statusRegion.getAttribute('role')).toBe('status')
    // The Settings behind a full-window overlay are inert, so Tab cannot reach
    // Restart or Deploy underneath it.
    expect(body.inert).toBe(true)
    expect(body.getAttribute('aria-hidden')).toBe('true')
    expect(overlay).not.toBe(document.activeElement)

    // Nothing in the overlay is actionable, so Tab stays on the status region
    // rather than walking out into the background.
    const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    window.dispatchEvent(tab)
    expect(tab.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(statusRegion)

    // The record settles: the overlay goes, the background is live again, and
    // focus lands on the action the run produced.
    await poll()
    expect(wrapper.find('.engine-update-overlay').exists()).toBe(false)
    expect(body.inert).not.toBe(true)
    const apply = button(wrapper, 'Apply update')
    expect(apply).toBeDefined()
    expect(document.activeElement).toBe(apply!.element)
    wrapper.unmount()
  } finally {
    vi.useRealTimers()
  }
})

it('leaves the app usable while a run is staging', async () => {
  vi.useFakeTimers()
  // Staging is a release lookup, a download and a wheel build with the engine
  // still serving, so the card's copy says Ciaobot keeps running until it is
  // ready to apply. A full-window modal would contradict that for minutes, and
  // `useModalFocus` makes the background inert all the way to `document.body` —
  // so the staging half renders its progress in the card and takes nothing over.
  const { wrapper, statusReads } = await mountCard([
    { install_mode: 'installer', can_update: true, operation: null },
    { install_mode: 'installer', can_update: true, operation: operation('downloading') },
    { install_mode: 'installer', can_update: true, operation: operation('staged') },
  ], undefined, { attach: true })
  try {
    await button(wrapper, 'Stage update')!.trigger('click')
    await flushPromises()
    expect(wrapper.find('.engine-update-overlay').exists()).toBe(false)

    await poll()
    const body = wrapper.get('.pane-body').element as HTMLElement
    // No takeover: no overlay, and nothing in the app behind it is inert, so
    // Restart, Deploy and the other panes stay reachable.
    expect(wrapper.find('.engine-update-overlay').exists()).toBe(false)
    expect(body.inert).not.toBe(true)
    expect(body.getAttribute('aria-hidden')).toBeNull()
    expect(document.body.inert).not.toBe(true)
    // The rows are the overlay's rows, in the card, driven by the record.
    const progress = wrapper.get('.engine-update-panel .update-progress-content')
    expect(progress.text()).toContain('Downloading the release')
    expect(wrapper.findAll('.update-progress-log-row.is-in_progress').map(r => r.find('.update-progress-log-name').text()))
      .toEqual(['downloading the release'])
    // Focus went to the region that replaced the button, not to a takeover.
    expect(document.activeElement).toBe(progress.element)
    // Tab is not claimed: the card is one place in a live app again.
    const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    window.dispatchEvent(tab)
    expect(tab.defaultPrevented).toBe(false)

    // The poll keeps running, and the run still ends on the staged panel.
    const reads = statusReads()
    await poll()
    expect(statusReads()).toBeGreaterThan(reads)
    expect(wrapper.find('.engine-update-overlay').exists()).toBe(false)
    expect(buttonLabels(wrapper)).toContain('Apply update')
    wrapper.unmount()
  } finally {
    vi.useRealTimers()
  }
})

it('does not pull focus for a run this tab did not start', async () => {
  // A run can start on the CLI or on another device, in which case this tab's
  // first read already finds it in flight. Staging is not a takeover, so it must
  // not take the focus the user has either — the card shows the rows and leaves
  // the keyboard where it was.
  const { wrapper } = await mountCard(
    { install_mode: 'installer', can_update: true, operation: operation('downloading') },
    undefined, { attach: true },
  )
  try {
    const restart = button(wrapper, 'Restart')!
    expect(restart).toBeDefined()
    restart.element.focus()
    expect(document.activeElement).toBe(restart.element)

    await flushPromises()
    expect(wrapper.find('.update-progress-phase').text()).toBe('Downloading the release')
    expect(wrapper.find('.engine-update-overlay').exists()).toBe(false)
    expect(document.activeElement).toBe(restart.element)
  } finally {
    wrapper.unmount()
  }
})

it('keeps polling a run whose record has not landed yet', async () => {
  vi.useFakeTimers()
  // A stage POST is answered 202 before the coordinator has written anything, and
  // it writes its first record only after the lock, the release lookup over the
  // network and the removal of any previous staged environment. The first reads
  // can therefore be serving the previous run's `failed` record — a record the
  // card was already holding when it asked for this one. Reading that as the end
  // of the run stops the poll, and the run then stages invisibly behind a panel
  // with a re-enabled button and nothing watching it.
  const { wrapper, post, statusReads } = await mountCard([
    { install_mode: 'installer', can_update: true, operation: operation('failed', { error: 'the wheel could not be built' }) },
    { install_mode: 'installer', can_update: true, operation: operation('failed', { error: 'the wheel could not be built' }) },
    { install_mode: 'installer', can_update: true, operation: operation('downloading') },
    { install_mode: 'installer', can_update: true, operation: operation('staged') },
  ])
  try {
    expect(statusReads()).toBe(1)
    await button(wrapper, 'Retry')!.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/update/stage')

    // Read two: the previous run's outcome, before the coordinator's first
    // write. The poll has to still be alive afterwards.
    await poll()
    expect(statusReads()).toBe(2)

    // Read three: this run's own record, in flight, in the card.
    await poll()
    expect(statusReads()).toBe(3)
    expect(wrapper.find('.update-progress-phase').text()).toBe('Downloading the release')
    // Staging is not a takeover, so the full-window overlay is not up.
    expect(wrapper.find('.engine-update-overlay').exists()).toBe(false)

    // Read four: the run's outcome, which ends the poll and the panel.
    await poll()
    expect(statusReads()).toBe(4)
    expect(buttonLabels(wrapper)).toContain('Apply update')

    const settled = statusReads()
    await poll()
    expect(statusReads()).toBe(settled)
    wrapper.unmount()
  } finally {
    vi.useRealTimers()
  }
})

it('stops a pre-record poll at the grace bound, so it cannot run forever', async () => {
  vi.useFakeTimers()
  // The other end of the same window: a run that produces neither a record nor
  // a refusal — a coordinator that died between the 202 and its first write.
  // Waiting has to be bounded, or every button in the card stays disabled until
  // the page is reloaded.
  const { wrapper, statusReads } = await mountCard(
    { install_mode: 'installer', can_update: true, operation: operation('failed') },
  )
  try {
    await button(wrapper, 'Retry')!.trigger('click')
    await flushPromises()
    const afterClick = statusReads()

    await vi.advanceTimersByTimeAsync(61_000)
    await flushPromises()
    const afterGrace = statusReads()
    expect(afterGrace).toBeGreaterThan(afterClick)

    await vi.advanceTimersByTimeAsync(10_000)
    expect(statusReads()).toBe(afterGrace)
    // The card is a way out again, which is the point of giving up.
    const retry = button(wrapper, 'Retry')
    expect(retry).toBeDefined()
    expect(retry!.attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  } finally {
    vi.useRealTimers()
  }
})

it('does not claim up to date when the version check failed', async () => {
  // `package_status` answers `update_available: false` with the reason in `error`
  // on a rate limit or any network failure, and the cached variant only papers
  // over that once a good answer has been seen. The card would then read "Up to
  // date · 0.19.0" with "Update check failed" directly beneath it.
  const { wrapper } = await mountCard(
    { install_mode: 'installer', can_update: true, operation: null },
    { mode: 'installer', current_version: '0.19.0', update_available: false, latest_version: '', error: 'HTTP 403: rate limit exceeded' },
  )
  try {
    expect(wrapper.text()).not.toContain('Up to date')
    // The reason is shown instead of a claim the card cannot support.
    expect(wrapper.text()).toContain('Update check failed: HTTP 403: rate limit exceeded')
  } finally {
    wrapper.unmount()
  }
})

it('returns focus to the card itself when a run leaves it nothing to click', async () => {
  vi.useFakeTimers()
  // An `applied` record is the run's outcome and offers no action, so there is no
  // button to focus: the panel is the target, and it carries `tabindex="-1"` so
  // focus never lands on nothing at all.
  const { wrapper } = await mountCard([
    { install_mode: 'installer', can_update: true, operation: null },
    { install_mode: 'installer', can_update: true, operation: operation('draining') },
    { install_mode: 'installer', can_update: true, operation: operation('applied') },
  ], [
    { mode: 'installer', current_version: '0.19.0', update_available: true, latest_version: '0.20.0' },
    // The apply succeeded, so the next check answers with the version that is
    // now installed: the record is history, and there is nothing left to do.
    { mode: 'installer', current_version: '0.20.0', update_available: false, latest_version: '0.20.0' },
  ], { attach: true })
  try {
    await button(wrapper, 'Stage update')!.trigger('click')
    await flushPromises()
    await poll()
    expect(document.activeElement).toBe(wrapper.get('.update-progress-content').element)

    await poll()
    const panel = wrapper.get('.engine-update-panel')
    expect(panel.attributes('tabindex')).toBe('-1')
    expect(panel.findAll('button')).toHaveLength(0)
    expect(document.activeElement).toBe(panel.element)
    expect(wrapper.text()).toContain('ciaobot is up to date')
    wrapper.unmount()
  } finally {
    vi.useRealTimers()
  }
})

it('says up to date for an installer engine with no job and nothing newer', async () => {
  const { wrapper } = await mountCard(
    { install_mode: 'installer', can_update: true, operation: null },
    { mode: 'installer', current_version: '0.20.0', update_available: false, latest_version: '0.20.0' },
  )
  try {
    // A status, not a disabled button, and nothing to click.
    expect(wrapper.text()).toContain('Up to date · 0.20.0')
    const labels = buttonLabels(wrapper)
    expect(labels).not.toContain('Stage update')
    expect(labels).not.toContain('Apply update')
  } finally {
    wrapper.unmount()
  }
})

it('keeps the non-installer guidance', async () => {
  const { wrapper, post } = await mountCard(
    { install_mode: 'editable', can_update: false, operation: null },
    { mode: 'editable', current_version: '0.19.0', update_available: true, latest_version: '0.20.0' },
  )
  try {
    expect(wrapper.text()).toContain('Check the installed package version')
    const labels = buttonLabels(wrapper)
    expect(labels).not.toContain('Stage update')
    expect(labels).not.toContain('Apply update')
    expect(labels).toContain('Update to 0.20.0')
    expect(post).not.toHaveBeenCalled()
  } finally {
    wrapper.unmount()
  }
})

it('stages again when a later release follows an applied record', async () => {
  // The record is never unlinked, so an `applied` record is the last run's
  // outcome forever. The release page is read independently of it, so a newer
  // release is the signal that the record is history — and the card has to offer
  // Stage again rather than claim the engine is up to date.
  const { wrapper, post } = await mountCard(
    { install_mode: 'installer', can_update: true, operation: operation('applied') },
    { mode: 'installer', current_version: '0.20.0', update_available: true, latest_version: '0.21.0' },
  )
  try {
    expect(wrapper.text()).not.toContain('ciaobot is up to date')
    expect(wrapper.text()).not.toContain('Up to date')
    expect(wrapper.text()).toContain('v0.21.0 is available')
    const stage = button(wrapper, 'Stage update')
    expect(stage).toBeDefined()
    expect(stage!.attributes('disabled')).toBeUndefined()
    await stage!.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/update/stage')
  } finally {
    wrapper.unmount()
  }
})

it('recovers from a refusal parked against an existing record', async () => {
  vi.useFakeTimers()
  // A terminal record is already not-busy, so nothing but the poll's own end
  // condition can tell the card that the run it just asked for will never write
  // a record of its own. The served reason is the newer answer, and it has to
  // win over the previous run's own error.
  const { wrapper, post } = await mountCard([
    { install_mode: 'installer', can_update: true, operation: operation('failed', { error: 'the wheel could not be built' }) },
    { install_mode: 'installer', can_update: true, operation: operation('failed', { error: 'the wheel could not be built' }), error: 'the latest release could not be resolved' },
  ])
  try {
    await button(wrapper, 'Retry')!.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/update/stage')

    await poll()
    expect(wrapper.text()).toContain('the latest release could not be resolved')
    expect(wrapper.text()).not.toContain('the wheel could not be built')

    const retry = button(wrapper, 'Stage update')
    expect(retry).toBeDefined()
    expect(retry!.attributes('disabled')).toBeUndefined()
    await retry!.trigger('click')
    await flushPromises()
    expect(post.mock.calls.filter(call => call[0] === '/api/update/stage')).toHaveLength(2)
    wrapper.unmount()
  } finally {
    vi.useRealTimers()
  }
})

it('drops the in-flight line once the record settles', async () => {
  vi.useFakeTimers()
  const { wrapper } = await mountCard([
    { install_mode: 'installer', can_update: true, operation: null },
    { install_mode: 'installer', can_update: true, operation: operation('downloading') },
    { install_mode: 'installer', can_update: true, operation: operation('failed', { error: 'the new engine never answered' }) },
  ])
  try {
    await button(wrapper, 'Stage update')!.trigger('click')
    await flushPromises()
    // "Staging the update…" is true while the run is in flight.
    expect(wrapper.text()).toContain('Staging the update')

    await poll()
    expect(wrapper.text()).toContain('Staging the update')

    // The run stopped: the panel says why, and the in-flight line is gone rather
    // than reading as the state under a settled panel.
    await poll()
    expect(wrapper.text()).toContain('the new engine never answered')
    expect(wrapper.text()).not.toContain('Staging the update')
    wrapper.unmount()
  } finally {
    vi.useRealTimers()
  }
})
