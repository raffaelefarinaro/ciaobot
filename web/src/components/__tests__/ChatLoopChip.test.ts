// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, nextTick } from 'vue'
import type { Schedule } from '../../lib/types'
import { useTaskStore } from '../../stores/tasks'
import ChatLoopChip from '../ChatLoopChip.vue'

const Empty = defineComponent({ render: () => h('div') })

function loop(id: string, overrides: Partial<Schedule> = {}): Schedule {
  return {
    schedule_id: id, title: `Loop ${id}`, prompt: 'watch', daily_time_utc: '', timezone_name: 'UTC',
    frequency: 'interval', interval_minutes: 5, enabled: true, scope: 'user', run_at_date: null,
    web_chat_id: 'chat-a', owner_chat_id: 'chat-a', expires_at: '2099-01-01T14:30:00Z',
    max_runs: 10, run_count: 3, ...overrides,
  } as Schedule
}

let wrapper: VueWrapper | null = null
let router: ReturnType<typeof createRouter>

async function mountChip(props: { chatId?: string, archived?: boolean } = {}, schedules: Schedule[] = [loop('l1')]) {
  setActivePinia(createPinia())
  const store = useTaskStore()
  store.schedules = schedules
  store.schedulesLoaded = true
  store.deleteSchedule = vi.fn(async () => {})
  store.fetchSchedules = vi.fn(async () => {})
  router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: Empty }, { path: '/schedules/:id', component: Empty }],
  })
  await router.push('/')
  const bar = defineComponent({
    setup: () => () => h('div', { class: 'input-actions composer-bar' }, [
      h(ChatLoopChip, { chatId: props.chatId ?? 'chat-a', archived: props.archived ?? false }),
    ]),
  })
  wrapper = mount(bar, { attachTo: document.body, global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

const chip = () => wrapper!.find<HTMLButtonElement>('button.composer-chip')
const menuItems = () => Array.from(document.body.querySelectorAll<HTMLElement>('[role="menuitem"]'))
async function openMenu() {
  chip().element.click()
  await flushPromises()
  await nextTick()
}

describe('ChatLoopChip', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })
  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    document.body.innerHTML = ''
  })

  it('is absent when the chat has no loop', async () => {
    await mountChip({}, [])
    expect(chip().exists()).toBe(false)
  })

  it('is absent when the chat is archived', async () => {
    await mountChip({ archived: true })
    expect(chip().exists()).toBe(false)
  })

  it('names a single loop with its cadence and end, and carries the full summary for assistive tech', async () => {
    await mountChip()
    expect(chip().text()).toContain('Loop · every 5 min · ends ')
    const label = chip().attributes('aria-label') ?? ''
    expect(label).toContain('every 5 min')
    expect(label).toContain('7 runs left')
    expect(chip().attributes('title')).toBe(label)
    expect(chip().attributes('aria-haspopup')).toBe('menu')
    expect(chip().attributes('aria-expanded')).toBe('false')
  })

  it('counts several loops in the chat', async () => {
    await mountChip({}, [loop('l1'), loop('l2')])
    expect(chip().text()).toContain('Loops · 2')
    expect(chip().attributes('aria-label')).toBe('2 loops in this chat')
  })

  it('matches only loops this chat owns, not automations merely bound to it', async () => {
    await mountChip({ chatId: 'chat-a' }, [
      loop('other-owner', { owner_chat_id: 'chat-b', web_chat_id: 'chat-a' }),
      loop('routine', { owner_chat_id: undefined, web_chat_id: 'chat-a', frequency: 'daily' }),
    ])
    expect(chip().exists()).toBe(false)
  })

  it('lists each loop in the menu with Open and Stop, and Stop removes exactly that loop once', async () => {
    await mountChip({}, [loop('l1'), loop('l2')])
    await openMenu()
    const names = menuItems().map(el => el.textContent?.trim())
    expect(names).toEqual(['Open in Automations', 'Stop loop', 'Open in Automations', 'Stop loop'])
    const store = useTaskStore()
    menuItems()[1].click()
    await flushPromises()
    expect(store.deleteSchedule).toHaveBeenCalledTimes(1)
    expect(store.deleteSchedule).toHaveBeenCalledWith('l1')
  })

  it('opens the loop in Automations', async () => {
    await mountChip()
    await openMenu()
    menuItems()[0].click()
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/schedules/l1')
  })

  it('shows a plain-language error in the menu and keeps the chip when Stop fails', async () => {
    await mountChip()
    const store = useTaskStore()
    store.deleteSchedule = vi.fn(async () => { throw Object.assign(new Error('boom'), { status: 500 }) })
    await openMenu()
    menuItems()[1].click()
    await flushPromises()
    const alert = document.body.querySelector('[role="alert"]')
    expect(alert?.textContent).toContain('Could not stop the loop')
    expect(alert?.textContent).not.toContain('boom')
    // The menu stays open so the user can retry, and the loop is still listed.
    expect(menuItems().length).toBeGreaterThan(0)
    expect(store.fetchSchedules).not.toHaveBeenCalled()
  })

  it('treats a loop already gone (404) as stopped: no error, and the list is refreshed', async () => {
    await mountChip()
    const store = useTaskStore()
    store.deleteSchedule = vi.fn(async () => { throw Object.assign(new Error('gone'), { status: 404 }) })
    await openMenu()
    menuItems()[1].click()
    await flushPromises()
    expect(document.body.querySelector('[role="alert"]')).toBeNull()
    expect(store.fetchSchedules).toHaveBeenCalledTimes(1)
  })

  it('opens from the keyboard and walks the items with the arrow keys', async () => {
    await mountChip()
    chip().element.focus()
    await chip().trigger('keydown', { key: 'Enter' })
    await flushPromises()
    await nextTick()
    expect(chip().attributes('aria-expanded')).toBe('true')
    const menu = document.body.querySelector<HTMLElement>('[role="menu"]')!
    const first = document.activeElement
    expect(menuItems()).toContain(first)
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    expect(menuItems()).toContain(document.activeElement)
    expect(document.activeElement).not.toBe(first)
  })

  it('closes on Escape and returns focus to the chip', async () => {
    await mountChip()
    await openMenu()
    expect(chip().attributes('aria-expanded')).toBe('true')
    document.body.querySelector<HTMLElement>('[role="menu"]')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    await nextTick()
    expect(document.body.querySelector('[role="menu"]')).toBeNull()
    // Reka returns focus once the menu's close animation frame has run.
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(document.activeElement).toBe(chip().element)
  })
})
