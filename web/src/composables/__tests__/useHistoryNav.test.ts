// @vitest-environment jsdom
import { describe, expect, it, beforeEach } from 'vitest'
import { defineComponent, h, nextTick } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import { useHistoryNav } from '../useHistoryNav'
import { useProjectStore } from '../../stores/projects'

const Empty = defineComponent({ render: () => h('div') })

function makeRouter() {
  return createRouter({
    history: createWebHistory(),
    routes: [
      { path: '/', component: Empty },
      { path: '/chat/:chatId', component: Empty },
      { path: '/project/:projectId', component: Empty },
    ],
  })
}

describe('useHistoryNav', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/')
  })

  it('enables back after an in-app navigation and names the page it leads to', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    store.projects = [{ project_id: 'p1', name: 'Launch' }] as unknown as typeof store.projects
    store.chats = [{ chat_id: 'c1', title: 'Morning briefing' }] as unknown as typeof store.chats

    const router = makeRouter()
    await router.push('/project/p1')
    let nav!: ReturnType<typeof useHistoryNav>
    mount(defineComponent({ setup() { nav = useHistoryNav(); return () => h('div') } }), {
      global: { plugins: [router, pinia] },
    })
    // The first in-app entry has nowhere in the app to go back to.
    expect(nav.canBack.value).toBe(false)

    await router.push('/chat/c1')
    expect(nav.canBack.value).toBe(true)
    expect(nav.backLabel.value).toBe('Back to Launch')
    expect(nav.canForward.value).toBe(false)

    nav.back()
    await new Promise(resolve => window.addEventListener('popstate', resolve, { once: true }))
    await flushPromises()
    await nextTick()
    expect(router.currentRoute.value.path).toBe('/project/p1')
    expect(nav.canForward.value).toBe(true)
    expect(nav.forwardLabel.value).toBe('Forward to Morning briefing')
  })

  it('stays inert without a router', () => {
    let nav!: ReturnType<typeof useHistoryNav>
    mount(defineComponent({ setup() { nav = useHistoryNav(); return () => h('div') } }))
    expect(nav.canBack.value).toBe(false)
    expect(() => nav.back()).not.toThrow()
  })
})
