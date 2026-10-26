// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import NotificationBell from '../NotificationBell.vue'
import { requestInboxOpen } from '../../composables/useInboxPopover'
import { useProjectStore } from '../../stores/projects'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import { useHousekeepingStore } from '../../stores/housekeeping'
import { useTaskStore } from '../../stores/tasks'
import { useProposalsStore } from '../../stores/proposals'
import { useVaultReviewStore } from '../../stores/vaultReview'
import { api } from '../../lib/api'
import { readyHealth } from '../../lib/sourceHealth'
import type { SourceHealth } from '../../lib/sourceHealth'

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

const Empty = defineComponent({ template: '<div />' })
const stale: SourceHealth = { status: 'stale', lastSuccessAt: '2026-10-26T10:00:00.000Z', error: 'x' }
const unavailable: SourceHealth = { status: 'unavailable', lastSuccessAt: '', error: 'x' }

function chat(id: string) {
  return {
    chat_id: id,
    project_id: 'p1',
    title: `Chat ${id}`,
    archived: false,
    local: true,
    last_activity_at: '2026-10-26T09:30:00Z',
    last_read_at: '2026-10-26T09:00:00Z',
  }
}

let wrapper: VueWrapper | null = null

async function mountBell(opts: { chats?: number; health?: SourceHealth; variant?: 'popover' | 'sheet' } = {}, host?: HTMLElement) {
  const projects = useProjectStore()
  const taskSignals = useTaskSignalsStore()
  projects.projects = [{ project_id: 'p1', name: 'General', workspace: 'personal' }] as never
  projects.activeWorkspace = 'personal'
  projects.activeChatId = null
  projects.chats = Array.from({ length: opts.chats ?? 0 }, (_, i) => chat(`c${i}`)) as never
  projects.chatsHealth = opts.health ?? readyHealth()
  taskSignals.healthWorkspace = 'personal'
  taskSignals.loadedWorkspace = 'personal'
  taskSignals.health = readyHealth()
  const housekeeping = useHousekeepingStore()
  housekeeping.actionsHealth = readyHealth()
  housekeeping.updateTasksWorkspace = 'personal'
  housekeeping.updateTasksHealth = readyHealth()
  useTaskStore().schedulesLoaded = true
  useProposalsStore().loaded = true
  useVaultReviewStore().loadedWorkspace = 'personal'

  vi.mocked(api.get).mockReset()
  vi.mocked(api.get).mockImplementation(async (url: string) => {
    if (url.startsWith('/api/chats')) return projects.chats
    if (url.startsWith('/api/tasks')) return { workspace: 'personal', tasks: [] }
    return []
  })

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/chat/:chatId', component: Empty }, { path: '/', component: Empty }],
  })
  await router.push('/')
  wrapper = mount(NotificationBell, {
    props: opts.variant ? { variant: opts.variant } : {},
    attachTo: host ?? document.body,
    global: { plugins: [router] },
  })
  await flushPromises()
  return { wrapper, router }
}

const bellButton = () => document.body.querySelector('button.notif-bell') as HTMLButtonElement
const dialog = () => document.body.querySelector('[role="dialog"]') as HTMLElement | null

describe('NotificationBell', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    document.body.innerHTML = ''
  })

  it('badge_shows_count_and_99_plus', async () => {
    await mountBell()
    expect(document.body.querySelector('.notif-bell-count')).toBeNull()
    wrapper!.unmount()
    setActivePinia(createPinia())
    await mountBell({ chats: 3 })
    expect(document.body.querySelector('.notif-bell-count')?.textContent).toBe('3')
    wrapper!.unmount()
    setActivePinia(createPinia())
    await mountBell({ chats: 120 })
    expect(document.body.querySelector('.notif-bell-count')?.textContent).toBe('99+')
  })

  it('accessible_name_carries_full_count_and_partial_note', async () => {
    await mountBell({ chats: 2, health: stale })
    expect(bellButton().getAttribute('aria-label')).toBe(
      'Notifications, 2 items. Some sources could not be fully checked',
    )
  })

  it('partial_ring_only_when_stale_or_unavailable', async () => {
    await mountBell({ chats: 1 })
    expect(document.body.querySelector('.notif-bell-ring')).toBeNull()
    wrapper!.unmount()
    setActivePinia(createPinia())
    await mountBell({ chats: 1, health: stale })
    expect(document.body.querySelector('.notif-bell-ring')).not.toBeNull()
    wrapper!.unmount()
    setActivePinia(createPinia())
    await mountBell({ chats: 1, health: unavailable })
    expect(document.body.querySelector('.notif-bell-ring')).not.toBeNull()
  })

  it('click_toggles_dialog_and_aria_expanded', async () => {
    await mountBell({ chats: 1 })
    const btn = bellButton()
    expect(btn.getAttribute('aria-haspopup')).toBe('dialog')
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    expect(dialog()).toBeNull()
    btn.click()
    await flushPromises()
    expect(btn.getAttribute('aria-expanded')).toBe('true')
    expect(dialog()?.getAttribute('aria-label')).toBe('Notifications')
    expect(dialog()?.querySelector('h2')?.textContent).toContain('Notifications')
    expect(dialog()?.querySelector('.notif-row')).not.toBeNull()
    btn.click()
    await flushPromises()
    expect(dialog()).toBeNull()
    expect(btn.getAttribute('aria-expanded')).toBe('false')
  })

  it('escape_closes_and_returns_focus_to_bell', async () => {
    await mountBell({ chats: 1 })
    const outer = vi.fn()
    document.addEventListener('keydown', outer)
    bellButton().click()
    await flushPromises()
    const ev = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    dialog()!.dispatchEvent(ev)
    await flushPromises()
    document.removeEventListener('keydown', outer)
    expect(dialog()).toBeNull()
    expect(document.activeElement).toBe(bellButton())
    expect(outer).not.toHaveBeenCalled()
  })

  it('outside_click_closes', async () => {
    await mountBell({ chats: 1 })
    bellButton().click()
    await flushPromises()
    dialog()!.querySelector('h2')!.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }))
    await flushPromises()
    expect(dialog()).not.toBeNull()
    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }))
    await flushPromises()
    expect(dialog()).toBeNull()
  })

  it('row_navigation_closes_and_emits_navigate', async () => {
    const { router } = await mountBell({ chats: 1 })
    bellButton().click()
    await flushPromises()
    ;(dialog()!.querySelector('.notif-row') as HTMLElement).click()
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/chat/c0')
    expect(dialog()).toBeNull()
    expect(wrapper!.emitted('navigate')).toHaveLength(1)
  })

  it('sheet_variant_renders_backdrop', async () => {
    await mountBell({ chats: 1, variant: 'sheet' })
    bellButton().click()
    await flushPromises()
    const backdrop = document.body.querySelector('.notif-backdrop') as HTMLElement
    expect(backdrop).not.toBeNull()
    expect(dialog()?.classList.contains('notif-popover--sheet')).toBe(true)
    backdrop.click()
    await flushPromises()
    expect(dialog()).toBeNull()
  })

  it('popover_anchors_to_the_sidebar_edge', async () => {
    const aside = document.createElement('aside')
    aside.className = 'sidebar'
    document.body.appendChild(aside)
    vi.spyOn(aside, 'getBoundingClientRect').mockReturnValue({ right: 300, left: 0, top: 0, bottom: 0, width: 300, height: 0, x: 0, y: 0, toJSON: () => ({}) })
    const { wrapper: w } = await mountBell({ chats: 1 }, aside)
    void w
    ;(wrapper!.vm as unknown as { open: () => void }).open()
    await flushPromises()
    expect(dialog()!.style.getPropertyValue('--nb-left')).toBe('308px')
  })

  it('escape_on_a_non_focusable_child_does_not_reach_the_document', async () => {
    await mountBell({ chats: 1 })
    const outer = vi.fn()
    document.addEventListener('keydown', outer)
    bellButton().click()
    await flushPromises()
    const child = dialog()!.querySelector('.notif-summary')!
    child.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await flushPromises()
    document.removeEventListener('keydown', outer)
    expect(outer).not.toHaveBeenCalled()
    expect(dialog()).toBeNull()
  })

  it('focusing_outside_closes_the_dialog', async () => {
    await mountBell({ chats: 1 })
    const other = document.createElement('button')
    document.body.appendChild(other)
    bellButton().click()
    await flushPromises()
    expect(dialog()).not.toBeNull()
    other.focus()
    await flushPromises()
    expect(dialog()).toBeNull()
  })

  it('escape_on_the_active_element_after_open_returns_focus_to_bell', async () => {
    await mountBell({ chats: 1 })
    ;(wrapper!.vm as unknown as { open: () => void }).open()
    await flushPromises()
    expect(dialog()!.contains(document.activeElement)).toBe(true)
    document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(dialog()).toBeNull()
    expect(document.activeElement).toBe(bellButton())
  })

  it('zero_size_bell_closes_on_resize_but_still_opens', async () => {
    await mountBell({ chats: 1, variant: 'sheet' })
    // jsdom lays nothing out, so every rect is zero: opening must still work.
    bellButton().click()
    await flushPromises()
    expect(dialog()).not.toBeNull()
    window.dispatchEvent(new Event('resize'))
    await flushPromises()
    expect(dialog()).toBeNull()
  })

  it('opens_on_request_only_when_visible', async () => {
    await mountBell({ chats: 1 })
    const rect = (r: Partial<DOMRect>) => ({ x: 0, y: 0, toJSON: () => ({}), ...r }) as DOMRect
    const spy = vi.spyOn(bellButton(), 'getBoundingClientRect')
    // Laid out but translated off screen (the drawer's rail bell on a phone).
    spy.mockReturnValue(rect({ left: -50, right: -10, top: 10, bottom: 40, width: 40, height: 30 }))
    requestInboxOpen()
    await flushPromises()
    expect(dialog()).toBeNull()
    spy.mockReturnValue(rect({ left: 10, right: 50, top: 10, bottom: 40, width: 40, height: 30 }))
    requestInboxOpen()
    await flushPromises()
    expect(dialog()).not.toBeNull()
  })
})
