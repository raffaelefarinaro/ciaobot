// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { RouterLinkStub, flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import HomeTodayStats from '../HomeTodayStats.vue'
import { useProjectStore } from '../../stores/projects'
import { api } from '../../lib/api'

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

const get = vi.mocked(api.get)

const TODAY = {
  chats_opened: 14,
  messages_sent: 212,
  chats_archived: 5,
  memories_saved: 7,
  projects_worked_on: 4,
  tasks_completed: 3,
  avg_chats_opened_30d: 10.4,
  avg_messages_sent_30d: 151,
}

function mountToday() {
  return mount(HomeTodayStats, { global: { stubs: { RouterLink: RouterLinkStub } } })
}

describe('HomeTodayStats', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    get.mockReset()
  })

  it('shows today\'s counts with the 30-day averages', async () => {
    get.mockResolvedValue({ today: TODAY })
    const wrapper = mountToday()
    await flushPromises()

    const rows = wrapper.findAll('.rail-kv')
    expect(rows).toHaveLength(6)
    expect(rows.map(row => row.get('dt').text())).toEqual([
      'Chats opened', 'Messages sent', 'Chats archived', 'Memories saved', 'Projects worked on', 'Tasks completed',
    ])
    expect(rows.map(row => row.get('strong').text())).toEqual(['14', '212', '5', '7', '4', '3'])
    expect(wrapper.findAll('.home-today-avg').map(node => node.text())).toEqual(['avg 10', 'avg 151'])
    expect(rows[0].find('.home-today-avg').exists()).toBe(true)
    expect(rows[1].find('.home-today-avg').exists()).toBe(true)
    expect(rows[2].find('.home-today-avg').exists()).toBe(false)
    wrapper.unmount()
  })

  it('asks for the 30-day stats once, for the active workspace', async () => {
    get.mockResolvedValue({ today: TODAY })
    const wrapper = mountToday()
    await flushPromises()

    expect(get).toHaveBeenCalledTimes(1)
    expect(get).toHaveBeenCalledWith('/api/stats?workspace=personal&range=30')
    wrapper.unmount()
  })

  it('shows a dash in every row while loading, with no spinner', () => {
    get.mockReturnValue(new Promise(() => {}))
    const wrapper = mountToday()

    expect(wrapper.findAll('strong').map(node => node.text())).toEqual(Array(6).fill('—'))
    expect(wrapper.find('.home-today-avg').exists()).toBe(false)
    expect(wrapper.find('.spinner, [role="progressbar"], [aria-busy="true"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows a dash when the stats fail, and a dash for a null value', async () => {
    get.mockRejectedValue(new Error('engine offline'))
    const failed = mountToday()
    await flushPromises()
    expect(failed.findAll('strong').map(node => node.text())).toEqual(Array(6).fill('—'))
    expect(failed.find('.home-today-avg').exists()).toBe(false)
    expect(failed.text()).not.toContain('engine offline')
    failed.unmount()

    get.mockResolvedValue({ today: { ...TODAY, memories_saved: null, avg_messages_sent_30d: null } })
    const partial = mountToday()
    await flushPromises()
    expect(partial.findAll('.rail-kv')[3].get('strong').text()).toBe('—')
    expect(partial.findAll('.home-today-avg').map(node => node.text())).toEqual(['avg 10'])
    partial.unmount()
  })

  it('links to the Usage page', () => {
    get.mockReturnValue(new Promise(() => {}))
    const wrapper = mountToday()
    const link = wrapper.findComponent(RouterLinkStub)
    expect(link.props('to')).toBe('/stats')
    expect(link.text()).toBe('Usage')
    expect(wrapper.get('#home-today-title').text()).toBe('Today')
    wrapper.unmount()
  })

  it('says what the averages cover', () => {
    get.mockReturnValue(new Promise(() => {}))
    const wrapper = mountToday()
    expect(wrapper.get('.rail-note').text()).toBe('Averages over the last 30 days.')
    wrapper.unmount()
  })

  it('reloads when the workspace changes and reads a dash until the new stats arrive', async () => {
    get
      .mockResolvedValueOnce({ today: TODAY })
      .mockReturnValueOnce(new Promise(() => {}))
    const wrapper = mountToday()
    await flushPromises()
    expect(wrapper.findAll('strong')[0].text()).toBe('14')

    useProjectStore().activeWorkspace = 'work'
    await nextTick()
    await flushPromises()

    expect(get).toHaveBeenLastCalledWith('/api/stats?workspace=work&range=30')
    expect(wrapper.findAll('strong').map(node => node.text())).toEqual(Array(6).fill('—'))
    wrapper.unmount()
  })
})
