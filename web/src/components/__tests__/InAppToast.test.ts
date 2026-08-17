// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { useProjectStore } from '../../stores/projects'
import InAppToast from '../InAppToast.vue'

const routerPush = vi.hoisted(() => vi.fn())
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush }),
}))

function mountToast() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  const wrapper = mount(InAppToast, { global: { plugins: [pinia] } })
  return { wrapper, store }
}

beforeEach(() => {
  routerPush.mockReset()
})

describe('error toast Fix action', () => {
  it('routes to the configured settings page instead of seeding a fix chat', async () => {
    const { wrapper, store } = mountToast()
    const fixError = vi.spyOn(store, 'fixError')
    store.pushErrorToast('Google Workspace login needs attention', 're-auth', {
      fixRoute: '/settings/workspaces',
      fixLabel: 'Fix in Settings',
    })
    await wrapper.vm.$nextTick()

    const fixBtn = wrapper.find('.toast-fix')
    expect(fixBtn.text()).toBe('Fix in Settings')
    await fixBtn.trigger('click')

    expect(routerPush).toHaveBeenCalledWith('/settings/workspaces')
    expect(fixError).not.toHaveBeenCalled()
    expect(store.toasts).toHaveLength(0)
  })

  it('still seeds a fix chat for errors without a settings route', async () => {
    const { wrapper, store } = mountToast()
    const fixError = vi.spyOn(store, 'fixError').mockResolvedValue(undefined)
    store.pushErrorToast('Could not archive chat', 'boom')
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.toast-fix').text()).toBe('Fix this error')
    await wrapper.find('.toast-fix').trigger('click')

    expect(fixError).toHaveBeenCalledWith({ errorText: 'boom', title: 'Fix error' })
    expect(routerPush).not.toHaveBeenCalled()
  })
})
