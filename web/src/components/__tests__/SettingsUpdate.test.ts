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

/**
 * Mount the Updates card against one update-status answer and one package
 * answer. Everything else the view asks for is refused, exactly as the other
 * Settings tests do, so a test only ever sees the two it cares about.
 */
async function mountCard(
  update: EngineUpdateStatus,
  packageStatus: Record<string, unknown> = { mode: 'installer', current_version: '0.19.0', update_available: true, latest_version: '0.20.0' },
) {
  setActivePinia(createPinia())
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: { template: '<div />' } }] })
  await router.push('/settings')
  await router.isReady()
  vi.spyOn(api, 'get').mockImplementation(async (path) => {
    if (path === '/api/startup-status') return { node_role: 'host', state_valid: true } as never
    if (path === '/api/local/status') return { git_repo: true, branch: 'main', dirty: false, restart_only: true } as never
    if (path === '/api/update/status') return update as never
    if (path === '/api/package/status') return packageStatus as never
    throw new Error('Unrelated settings data unavailable in this test')
  })
  const post = vi.spyOn(api, 'post').mockResolvedValue({ started: true } as never)
  const beginRestart = vi.spyOn(useProjectStore(), 'beginServerRestart').mockImplementation(() => {})
  const wrapper = mount(SettingsView, { global: { plugins: [router], stubs: { Teleport: true } } })
  await flushPromises()
  return { wrapper, post, beginRestart }
}

function buttonLabels(wrapper: Awaited<ReturnType<typeof mountCard>>['wrapper']) {
  return wrapper.findAll('button').map(b => b.text())
}

it('offers Stage for an installer engine with an update available', async () => {
  const { wrapper, post } = await mountCard({ install_mode: 'installer', can_update: true, operation: null })
  try {
    expect(buttonLabels(wrapper)).toContain('Stage update')
    await wrapper.findAll('button').find(b => b.text() === 'Stage update')!.trigger('click')
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
    // The record's phase, not the boot animation: the first five rows are done
    // and the last is live, with nothing advancing behind it.
    expect(wrapper.find('.update-progress-overlay').exists()).toBe(true)
    expect(wrapper.findAll('.update-progress-log-row').map(r => r.classes().find(c => c.startsWith('is-'))))
      .toEqual(['is-done', 'is-done', 'is-done', 'is-done', 'is-done', 'is-in_progress'])
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

it('applies a staged job and starts the restart overlay', async () => {
  const { wrapper, post, beginRestart } = await mountCard({
    install_mode: 'installer',
    can_update: true,
    operation: operation('staged'),
  })
  try {
    expect(buttonLabels(wrapper)).toContain('Apply update')
    await wrapper.findAll('button').find(b => b.text() === 'Apply update')!.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/update/apply')
    // The apply drains and reboots the engine, so it needs the same overlay a
    // Settings restart uses; a second one would be a second reload loop.
    expect(beginRestart).toHaveBeenCalledWith('Updating Ciaobot… the engine will restart')
  } finally {
    wrapper.unmount()
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
