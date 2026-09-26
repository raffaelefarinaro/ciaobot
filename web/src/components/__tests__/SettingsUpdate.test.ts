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
 * a time. `attach` puts the view in the document, which focus assertions need.
 */
async function mountCard(
  update: EngineUpdateStatus | EngineUpdateStatus[],
  packageStatus: Record<string, unknown> = { mode: 'installer', current_version: '0.19.0', update_available: true, latest_version: '0.20.0' },
  { attach = false }: { attach?: boolean } = {},
) {
  setActivePinia(createPinia())
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: { template: '<div />' } }] })
  await router.push('/settings')
  await router.isReady()
  const answers = Array.isArray(update) ? [...update] : [update]
  let reads = 0
  vi.spyOn(api, 'get').mockImplementation(async (path) => {
    if (path === '/api/startup-status') return { node_role: 'host', state_valid: true } as never
    if (path === '/api/local/status') return { git_repo: true, branch: 'main', dirty: false, restart_only: true } as never
    if (path === '/api/update/status') {
      const answer = answers[Math.min(reads, answers.length - 1)]
      reads += 1
      return answer as never
    }
    if (path === '/api/package/status') return packageStatus as never
    throw new Error('Unrelated settings data unavailable in this test')
  })
  const post = vi.spyOn(api, 'post').mockResolvedValue({ started: true } as never)
  const beginRestart = vi.spyOn(useProjectStore(), 'beginServerRestart').mockImplementation(() => {})
  const wrapper = mount(SettingsView, {
    global: { plugins: [router], stubs: { Teleport: true } },
    attachTo: attach ? document.body : undefined,
  })
  await flushPromises()
  return { wrapper, post, beginRestart }
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
  const { wrapper } = await mountCard([
    { install_mode: 'installer', can_update: true, operation: null },
    { install_mode: 'installer', can_update: true, operation: operation('downloading') },
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
