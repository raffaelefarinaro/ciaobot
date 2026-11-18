// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'
import SettingsView from '../SettingsView.vue'

afterEach(() => vi.restoreAllMocks())

/**
 * Mount the Updates card against one package answer. Everything else the view
 * asks for is refused, exactly as the other Settings tests do, so a test only
 * ever sees the answer it cares about. `packageStatus` may be a list: the card
 * reads it in order and then keeps reading the last of it, for the reads a
 * settled card makes.
 *
 * Every path the card requests is recorded on the spy, so a test can assert
 * the card never asks a route it no longer owns.
 */
async function mountCard(
  packageStatus: Record<string, unknown> | Record<string, unknown>[] = { mode: 'installer', current_version: '2.0.0', update_available: false, latest_version: '2.0.0' },
  { attach = false }: { attach?: boolean } = {},
) {
  setActivePinia(createPinia())
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: { template: '<div />' } }] })
  await router.push('/settings')
  await router.isReady()
  const packageAnswers = Array.isArray(packageStatus) ? [...packageStatus] : [packageStatus]
  let packageReads = 0
  const getSpy = vi.spyOn(api, 'get').mockImplementation(async (path) => {
    if (path === '/api/local/status') return { git_repo: true, branch: 'main', dirty: false, restart_only: true } as never
    if (path === '/api/package/status') {
      const answer = packageAnswers[Math.min(packageReads, packageAnswers.length - 1)]
      packageReads += 1
      return answer as never
    }
    throw new Error('Unrelated settings data unavailable in this test')
  })
  const post = vi.spyOn(api, 'post').mockResolvedValue({ ok: true } as never)
  const beginRestart = vi.spyOn(useProjectStore(), 'beginServerRestart').mockImplementation(() => {})
  const wrapper = mount(SettingsView, {
    global: { plugins: [router], stubs: { Teleport: true } },
    attachTo: attach ? document.body : undefined,
  })
  await flushPromises()
  return { wrapper, getSpy, post, beginRestart }
}

function buttonLabels(wrapper: Awaited<ReturnType<typeof mountCard>>['wrapper']) {
  return wrapper.findAll('button').map(b => b.text())
}

function button(wrapper: Awaited<ReturnType<typeof mountCard>>['wrapper'], label: string) {
  return wrapper.findAll('button').find(b => b.text() === label)
}

/** The paths the card requested, for assertions against a route it dropped. */
function requestedPaths(getSpy: Awaited<ReturnType<typeof mountCard>>['getSpy']) {
  return getSpy.mock.calls.map(call => call[0])
}

it('says the app updates the engine together with Ciaobot for Mac, and offers no button', async () => {
  const { wrapper } = await mountCard(
    { mode: 'bundled_app', current_version: '2.0.0', update_available: true, latest_version: '2.0.1' },
  )
  try {
    const card = wrapper.get('.card')
    expect(card.text()).toContain('Ciaobot for Mac updates this engine together with the app.')
    // The one-line installer copy went with the engine updater. If it ever
    // comes back, these two asserts name it directly.
    expect(card.text()).not.toContain('install.sh')
    expect(card.find('code').exists()).toBe(false)
    const labels = buttonLabels(wrapper)
    expect(labels).not.toContain('Update & restart')
    expect(labels).not.toContain('Update to 2.0.1')
  } finally {
    wrapper.unmount()
  }
})

it('says an installer engine no longer updates itself, and never asks the coordinator', async () => {
  const { wrapper, getSpy, post } = await mountCard(
    { mode: 'installer', current_version: '1.10.0', update_available: true, latest_version: '1.11.0' },
  )
  try {
    const card = wrapper.get('.card')
    expect(card.text()).toContain('This engine was installed by the old Ciaobot installer')
    expect(card.text()).toContain('no longer updates itself')
    expect(card.text()).toContain('Install Ciaobot for Mac to keep it up to date.')
    expect(card.find('code').exists()).toBe(false)
    const labels = buttonLabels(wrapper)
    expect(labels).not.toContain('Update & restart')
    expect(labels).not.toContain('Update to 1.11.0')
    // The engine updater is gone from the card: it asks nothing about a run,
    // staged or otherwise, and nothing here can start one behind the copy.
    expect(requestedPaths(getSpy)).not.toContain('/api/update/status')
    // And nothing the card does in installer mode writes: no update POST can
    // ride along with a card that offers no update.
    expect(post).not.toHaveBeenCalled()
  } finally {
    wrapper.unmount()
  }
})

it('keeps the pip guidance and the Update button on an editable install', async () => {
  const { wrapper, post } = await mountCard(
    { mode: 'editable', current_version: '0.19.0', update_available: true, latest_version: '0.20.0' },
  )
  try {
    expect(wrapper.text()).toContain('Check the installed package version')
    const labels = buttonLabels(wrapper)
    // The pip card's own Update button, untouched by the engine updater's
    // removal: the card still opens the changelog and offers the upgrade.
    expect(labels).toContain('Update to 0.20.0')
    expect(labels).not.toContain('Update & restart')
    expect(post).not.toHaveBeenCalled()
  } finally {
    wrapper.unmount()
  }
})

it('shows the update progress overlay while the run is going, then hands over to the restart notice', async () => {
  const { wrapper, post, beginRestart } = await mountCard(
    { mode: 'editable', current_version: '0.19.0', update_available: true, latest_version: '0.20.0' },
  )
  try {
    await button(wrapper, 'Update to 0.20.0')!.trigger('click')
    await flushPromises()
    // Hold the run open so the in-flight state is observable: a package
    // update reports itself through the full-window overlay, not only
    // through the disabled buttons on the card behind it.
    let resolveUpdate!: (value: { ok: boolean }) => void
    post.mockImplementation(() => new Promise((resolve) => { resolveUpdate = resolve }))
    await button(wrapper, 'Update & Restart')!.trigger('click')
    expect(post).toHaveBeenCalledWith('/api/package/update')
    expect(beginRestart).not.toHaveBeenCalled()
    const overlay = wrapper.get('.update-progress-overlay')
    expect(overlay.text()).toContain('update · v0.20.0')
    // Success ends the run: the overlay gives way and the app-level restart
    // notice takes over, which waits for the restarted engine and reloads.
    resolveUpdate({ ok: true })
    await flushPromises()
    expect(beginRestart).toHaveBeenCalledWith('Update complete. Restarting Ciaobot with the latest version…')
    expect(wrapper.find('.update-progress-overlay').exists()).toBe(false)
  } finally {
    wrapper.unmount()
  }
})

it('does not claim up to date when the version check failed', async () => {
  // `package_status` answers `update_available: false` with the reason in `error`
  // on a rate limit or any network failure, and the cached variant only papers
  // over that once a good answer has been seen. The card would then read "Up to
  // date · 1.10.0" with "Update check failed" directly beneath it.
  const { wrapper } = await mountCard(
    { mode: 'installer', current_version: '1.10.0', update_available: false, latest_version: '', error: 'HTTP 403: rate limit exceeded' },
  )
  try {
    expect(wrapper.text()).not.toContain('Up to date')
    // The reason is shown instead of a claim the card cannot support.
    expect(wrapper.text()).toContain('Update check failed: HTTP 403: rate limit exceeded')
  } finally {
    wrapper.unmount()
  }
})

it('says up to date for an installer install with nothing newer', async () => {
  const { wrapper } = await mountCard(
    { mode: 'installer', current_version: '2.0.0', update_available: false, latest_version: '2.0.0' },
  )
  try {
    // A status, not a button, and nothing to click.
    expect(wrapper.text()).toContain('Up to date · 2.0.0')
    expect(buttonLabels(wrapper)).not.toContain('Update & restart')
  } finally {
    wrapper.unmount()
  }
})

it('collapses Updates to its title and an up-to-date badge when nothing is in progress', async () => {
  const { wrapper } = await mountCard(
    { mode: 'installer', current_version: '2.0.0', update_available: false, latest_version: '2.0.0' },
  )
  try {
    // The first card on General is Updates. Its title stays; the status sits in
    // the corner as a badge, and the copy is not rendered at all.
    const card = wrapper.get('.card')
    expect(card.get('.section-title').text()).toBe('Updates')
    expect(card.get('.settings-card-header .badge--success').text()).toBe('Up to date · 2.0.0')
    expect(card.findAll('p.hint')).toHaveLength(0)
    expect(card.find('code').exists()).toBe(false)
  } finally {
    wrapper.unmount()
  }
})

it('collapses a bundled app that is up to date, without the reinstall copy', async () => {
  const { wrapper } = await mountCard(
    { mode: 'bundled_app', current_version: '2.0.0', update_available: false, latest_version: '2.0.0' },
  )
  try {
    const card = wrapper.get('.card')
    expect(card.get('.settings-card-header .badge--success').text()).toBe('Up to date · 2.0.0')
    expect(card.findAll('p.hint')).toHaveLength(0)
    expect(card.find('code').exists()).toBe(false)
  } finally {
    wrapper.unmount()
  }
})
