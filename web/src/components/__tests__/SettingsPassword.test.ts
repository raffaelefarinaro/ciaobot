// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import { api } from '../../lib/api'
import SettingsView from '../SettingsView.vue'

afterEach(() => vi.restoreAllMocks())

async function mountSettings() {
  setActivePinia(createPinia())
  const stub = defineComponent({ render: () => h('div') })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: stub }] })
  await router.push('/settings')
  await router.isReady()
  vi.spyOn(api, 'get').mockImplementation(async (path) => {
    if (path === '/api/auth/settings') return { password_configured: true } as never
    throw new Error('Unrelated settings data unavailable in this test')
  })
  const wrapper = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { UpdateProgressView: stub } },
  })
  await flushPromises()
  return wrapper
}

// Other cards on the page can show their own errors; scope to this one.
function cardResult(wrapper: Awaited<ReturnType<typeof mountSettings>>) {
  const card = (wrapper.find('#auth-current-password').element.closest('.settings-form-panel') as HTMLElement).parentElement!
  return card.querySelector('.action-result--error')
}

async function fillAndSave(wrapper: Awaited<ReturnType<typeof mountSettings>>) {
  await wrapper.find('#auth-current-password').setValue('wrong')
  const newPassword = wrapper.find('input[autocomplete="new-password"]')
  await newPassword.setValue('hunter22')
  const save = wrapper.findAll('button').find(b => b.text() === 'Save password')!
  await save.trigger('click')
  await flushPromises()
  return newPassword
}

it('shows "Current password is incorrect." inline and keeps the new password', async () => {
  const wrapper = await mountSettings()
  try {
    vi.spyOn(api, 'post').mockRejectedValue(
      Object.assign(new Error('Current password is incorrect.'), {
        status: 403,
        payload: { error: 'Current password is incorrect.', code: 'wrong_current_password' },
      }),
    )
    const newPassword = await fillAndSave(wrapper)
    expect(wrapper.find('#auth-current-password-error').text()).toBe('Current password is incorrect.')
    expect(document.activeElement).toBe(wrapper.find('#auth-current-password').element)
    expect(wrapper.find('#auth-current-password').attributes('aria-invalid')).toBe('true')
    expect((newPassword.element as HTMLInputElement).value).toBe('hunter22')
    expect(cardResult(wrapper)).toBeNull()

    await wrapper.find('#auth-current-password').setValue('wrong2')
    expect(wrapper.find('#auth-current-password-error').exists()).toBe(false)
  } finally {
    wrapper.unmount()
  }
})

it('reports other failures in the result line', async () => {
  const wrapper = await mountSettings()
  try {
    vi.spyOn(api, 'post').mockRejectedValue(
      Object.assign(new Error('disk full'), { status: 500, payload: { error: 'disk full' } }),
    )
    await fillAndSave(wrapper)
    expect(cardResult(wrapper)?.textContent).toContain('disk full')
    expect(wrapper.find('#auth-current-password-error').exists()).toBe(false)
  } finally {
    wrapper.unmount()
  }
})
