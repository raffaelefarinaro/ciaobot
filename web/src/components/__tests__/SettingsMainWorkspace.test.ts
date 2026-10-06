// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'
import SettingsView from '../SettingsView.vue'

afterEach(() => vi.restoreAllMocks())

const ROOT = '/Users/me/ciaobot'

async function mountSettings(move: Record<string, unknown>) {
  setActivePinia(createPinia())
  const stub = defineComponent({ render: () => h('div') })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: stub }] })
  await router.push('/settings')
  await router.isReady()
  vi.spyOn(api, 'get').mockImplementation(async (path) => {
    if (path === '/api/settings/routines') {
      return { workspace_context: { workspace_root: ROOT, vault_root: `${ROOT}/memory-vault` } } as never
    }
    if (path === '/api/workspace-move') return { workspace_root: ROOT, operation: null, ...move } as never
    if (path.startsWith('/api/workspace-move/dirs')) {
      return { path: '/Users/me', display_path: '~', parent: '/Users', dirs: [{ name: 'ciaobot', path: ROOT }, { name: 'work', path: '/Users/me/work' }] } as never
    }
    throw new Error('Unrelated settings data unavailable in this test')
  })
  const wrapper = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { UpdateProgressView: stub } },
  })
  await flushPromises()
  const card = () => wrapper.find('.workspace-root-path').element.closest('.card') as HTMLElement
  return { wrapper, card }
}

it('names the CLI command, not a .env key, when this browser cannot move the workspace', async () => {
  const { wrapper, card } = await mountSettings({ local: false })
  try {
    // The engine finds `.env` inside this folder, so a CIAO_WORKSPACE line in
    // that file can never move it.
    expect(card().textContent).not.toContain('CIAO_WORKSPACE')
    expect(card().textContent).toContain('ciao workspace-move <new folder>')
    expect(wrapper.findAll('button').some(b => b.text() === 'Move…')).toBe(false)
  } finally {
    wrapper.unmount()
  }
})

it('moves the workspace from the dialog and waits for the restart', async () => {
  const { wrapper } = await mountSettings({ local: true })
  const post = vi.spyOn(api, 'post').mockImplementation(async (path, body) => {
    const target = (body as { target: string }).target
    if (path === '/api/workspace-move/plan') {
      const ok = target === '/Users/me/ciaobot-new'
      return { source: ROOT, target, ok, refusals: ok ? [] : ['That is the folder the workspace is already in.'], warnings: [] } as never
    }
    return { operation: { phase: 'queued' } } as never
  })
  const beginRestart = vi.spyOn(useProjectStore(), 'beginServerRestart').mockImplementation(() => {})
  try {
    await wrapper.findAll('button').find(b => b.text() === 'Move…')!.trigger('click')
    await flushPromises()
    const input = document.querySelector<HTMLInputElement>('#workspace-move-name')!
    expect(input.value).toBe('ciaobot')
    expect(document.body.textContent).toContain('already in')
    const move = () => [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Move')!
    expect(move().disabled).toBe(true)

    input.value = 'ciaobot-new'
    input.dispatchEvent(new Event('input'))
    await flushPromises()
    expect(move().disabled).toBe(false)
    move().click()
    await flushPromises()

    expect(post).toHaveBeenCalledWith('/api/workspace-move', { target: '/Users/me/ciaobot-new' })
    expect(beginRestart).toHaveBeenCalledWith(expect.stringContaining('Moving the workspace'))
  } finally {
    wrapper.unmount()
  }
})

it('reports a move that was rolled back', async () => {
  const { wrapper, card } = await mountSettings({
    local: true,
    operation: { phase: 'rolled_back', source: ROOT, target: '/Users/me/x', error: 'the engine did not start', updated_at: new Date().toISOString() },
  })
  try {
    expect(card().textContent).toContain('did not finish and was undone: the engine did not start')
  } finally {
    wrapper.unmount()
  }
})
