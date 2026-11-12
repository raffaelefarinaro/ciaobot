// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import ListRow from '../ListRow.vue'

async function router() {
  const r = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div />' } }] })
  await r.push('/')
  await r.isReady()
  return r
}

describe('ListRow', () => {
  it('is a link with a title, sub-line and end column when given a route', async () => {
    const wrapper = mount(ListRow, {
      props: { to: '/schedules/a', text: 'Inbox sweep' },
      slots: { sub: 'Every 30 min', end: 'Thu 12:30' },
      global: { plugins: [await router()] },
    })
    const root = wrapper.get('a.list-row')
    expect(root.attributes('href')).toBe('/schedules/a')
    expect(root.get('.list-row-title').text()).toBe('Inbox sweep')
    expect(root.get('.list-row-sub').text()).toBe('Every 30 min')
    expect(root.get('.list-row-end').text()).toBe('Thu 12:30')
  })

  it('is a button without a route, and keeps the host\'s title as its tooltip', async () => {
    const wrapper = mount(ListRow, {
      attrs: { title: 'alpha first conversation', class: 'home-chat-item' },
      slots: { heading: '<span class="list-row-title home-chat-title">alpha</span>' },
      global: { plugins: [await router()] },
    })
    const root = wrapper.get('button.list-row')
    expect(root.attributes('type')).toBe('button')
    expect(root.attributes('title')).toBe('alpha first conversation')
    expect(root.classes()).toContain('home-chat-item')
    expect(root.get('.list-row-heading .home-chat-title').text()).toBe('alpha')
    // No sub or end slot: no empty columns.
    expect(root.find('.list-row-sub').exists()).toBe(false)
    expect(root.find('.list-row-end').exists()).toBe(false)
  })
})
