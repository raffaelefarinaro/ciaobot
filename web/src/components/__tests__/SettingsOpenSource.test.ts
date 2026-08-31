// @vitest-environment jsdom

// The open-source card mirrors the home strip's GitHub-star ask: face on the
// right plus Star/Later buttons while the ask is live (the backend gates it on
// a `starred` receipt or a snooze), quiet prose once it clears.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import { useHousekeepingStore } from '../../stores/housekeeping'
import type { OperatorAction } from '../../lib/types'

const Stub = defineComponent({ render: () => h('div') })

function starAction(): OperatorAction {
  return {
    id: 'github-star',
    kind: 'github-star',
    severity: 10,
    title: 'Enjoying Ciaobot?',
    detail: 'If Ciaobot is working well for you, a GitHub star helps other developers discover it.',
    glyph: '★',
    workspace: '',
    run_label: 'I starred it',
    chat_label: '',
    chat_prompt: '',
    view_label: '',
    view_route: '',
    link_label: 'Star on GitHub',
    link_url: 'https://github.com/raffaelefarinaro/ciaobot',
    dismiss_label: 'Later',
    blocking: false,
  }
}

async function mountSettings(): Promise<ReturnType<typeof mount>> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/settings', component: Stub }, { path: '/settings/:tab', component: Stub }],
  })
  await router.push('/settings')
  await router.isReady()
  const { default: SettingsView } = await import('../SettingsView.vue')
  const wrapper = mount(SettingsView, {
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
  })
  await flushPromises()
  return wrapper
}

describe('the open-source card star nudge', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('shows the face and Star/Later actions only while the ask is live', async () => {
    const housekeeping = useHousekeepingStore()
    vi.spyOn(housekeeping, 'init').mockImplementation(() => {})
    housekeeping.actions = [starAction()]

    const wrapper = await mountSettings()
    const card = wrapper.find('.open-source-card')
    expect(card.classes()).toContain('open-source-card--with-face')
    expect(wrapper.find('.open-source-face').attributes('src')).toBe('/face.png')
    expect(wrapper.findAll('button').map(b => b.text())).toContain('★ Star on GitHub')
    wrapper.unmount()
  })

  it('stays quiet prose once the star is given (or snoozed)', async () => {
    const housekeeping = useHousekeepingStore()
    vi.spyOn(housekeeping, 'init').mockImplementation(() => {})
    housekeeping.actions = []

    const wrapper = await mountSettings()
    expect(wrapper.find('.open-source-card').classes()).not.toContain('open-source-card--with-face')
    expect(wrapper.find('.open-source-face').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('★ Star on GitHub')
    wrapper.unmount()
  })

  it('stars through the housekeeping action and thanks via toast', async () => {
    const housekeeping = useHousekeepingStore()
    vi.spyOn(housekeeping, 'init').mockImplementation(() => {})
    housekeeping.actions = [starAction()]
    const runSpy = vi.spyOn(housekeeping, 'run').mockResolvedValue({ ok: true, summary: 'Thank you!' })

    const { useProjectStore } = await import('../../stores/projects')
    const projectStore = useProjectStore()
    const toastSpy = vi.spyOn(projectStore, 'pushToast').mockImplementation(() => ({ id: 1 }) as never)

    const wrapper = await mountSettings()
    await wrapper.findAll('button').find(b => b.text() === '★ Star on GitHub')!.trigger('click')
    expect(runSpy).toHaveBeenCalledWith('github-star')
    expect(toastSpy).toHaveBeenCalledWith(expect.objectContaining({ title: '★ Starred — thank you!' }))
    wrapper.unmount()
  })

  it("'Later' dismisses through the housekeeping store", async () => {
    const housekeeping = useHousekeepingStore()
    vi.spyOn(housekeeping, 'init').mockImplementation(() => {})
    housekeeping.actions = [starAction()]
    const dismissSpy = vi.spyOn(housekeeping, 'dismiss').mockResolvedValue({ ok: true, summary: 'Maybe later.' })

    const wrapper = await mountSettings()
    await wrapper.findAll('button').find(b => b.text() === 'Later')!.trigger('click')
    expect(dismissSpy).toHaveBeenCalledWith('github-star')
    wrapper.unmount()
  })
})