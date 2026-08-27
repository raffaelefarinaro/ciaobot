// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import AppTabs from '../AppTabs.vue'

describe('AppTabs', () => {
  it('links the primary destinations and marks the current view', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/schedules', component: { template: '<div />' } },
        { path: '/memory', component: { template: '<div />' } },
        { path: '/settings', component: { template: '<div />' } },
      ],
    })
    await router.push('/memory')
    await router.isReady()

    const wrapper = mount(AppTabs, { global: { plugins: [router] } })

    expect(wrapper.findAll('a')).toHaveLength(4)
    expect(wrapper.find('.app-tab--active').text()).toBe('memory')
    expect(wrapper.find('a[href="/schedules"]').text()).toBe('schedule')
    wrapper.unmount()
  })
})
