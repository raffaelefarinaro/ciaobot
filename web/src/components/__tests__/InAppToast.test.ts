// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
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

describe('info toast action', () => {
  it('runs and dismisses an info toast action', async () => {
    const { wrapper, store } = mountToast()
    const spy = vi.fn()
    store.pushToast({ chat_id: '', title: 't', body: 'b', actionLabel: 'Show question', onAction: spy })
    await wrapper.vm.$nextTick()

    const action = wrapper.find('.toast-action')
    expect(action.text()).toBe('Show question')
    expect(wrapper.find('.toast-fix').exists()).toBe(false)
    await action.trigger('click')

    expect(spy).toHaveBeenCalledOnce()
    expect(store.toasts).toHaveLength(0)
  })
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

  it('does not capture the pointer when the press starts on the toast link', async () => {
    // Capturing on the toast div retargets the compatibility click, so the
    // update toast's "What's new" anchor would never open its release notes.
    const { wrapper, store } = mountToast()
    store.pushToast({
      chat_id: '',
      title: 'Update available',
      body: 'v1.2.3 is ready to install.',
      linkUrl: 'https://github.com/raffaelefarinaro/ciaobot/releases/latest',
      linkLabel: 'What’s new',
    })
    await wrapper.vm.$nextTick()

    const toast = wrapper.find('.toast')
    const capture = vi.fn()
    ;(toast.element as HTMLElement).setPointerCapture = capture

    await wrapper.find('a.toast-link').trigger('pointerdown')
    expect(capture).not.toHaveBeenCalled()
    expect(wrapper.find('.toast-swiping').exists()).toBe(false)

    // The toast body itself still starts a swipe.
    await wrapper.find('.toast-body').trigger('pointerdown')
    expect(capture).toHaveBeenCalled()
    expect(wrapper.find('.toast-swiping').exists()).toBe(true)
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

describe('modal layering', () => {
  it('toasts drop under an open modal dialog', async () => {
    const { wrapper, store } = mountToast()
    store.pushToast({ chat_id: '', title: 't', body: 'b' })
    await flushPromises()
    expect(wrapper.find('.toast-stack').classes()).not.toContain('toast-stack--under-modal')

    const dialog = document.createElement('div')
    dialog.setAttribute('aria-modal', 'true')
    document.body.appendChild(dialog)
    try {
      await flushPromises()
      expect(wrapper.find('.toast-stack').classes()).toContain('toast-stack--under-modal')
    } finally {
      dialog.remove()
    }
    await flushPromises()
    expect(wrapper.find('.toast-stack').classes()).not.toContain('toast-stack--under-modal')
    wrapper.unmount()
  })
})
