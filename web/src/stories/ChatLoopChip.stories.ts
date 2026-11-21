import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, routerKey } from 'vue-router'
import { defineComponent, h, provide } from 'vue'
import ChatLoopChip from '../components/ChatLoopChip.vue'
import type { Schedule } from '../lib/types'
import { useTaskStore } from '../stores/tasks'

// Synthetic chat and loops only: no real names, prompts or paths. The chip
// reads its own chat's loops from the task store; the router is provided so
// its menu can navigate, and nothing is fetched (the fixture fence is quiet).
const CHAT = 'storybook-chat-alpha'
const FUTURE = '2099-01-01T14:30:00Z'

function loop(id: string, overrides: Partial<Schedule> = {}): Schedule {
  return {
    schedule_id: id,
    title: `Synthetic loop ${id}`,
    prompt: 'Check the synthetic build',
    daily_time_utc: '',
    timezone_name: 'UTC',
    frequency: 'interval',
    interval_minutes: 5,
    enabled: true,
    scope: 'user',
    run_at_date: null,
    web_chat_id: CHAT,
    owner_chat_id: CHAT,
    expires_at: FUTURE,
    max_runs: 10,
    run_count: 3,
    ...overrides,
  } as Schedule
}

/** A composer bar of the given width holding the chip for one chat. */
function chipIn(schedules: Schedule[], options: { archived?: boolean, width?: number } = {}) {
  return defineComponent({
    name: 'ChatLoopChipStory',
    setup() {
      const pinia = createPinia()
      setActivePinia(pinia)
      const tasks = useTaskStore(pinia)
      tasks.schedules = schedules
      tasks.schedulesLoaded = true
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [
          { path: '/', component: { render: () => null } },
          { path: '/schedules/:id', component: { render: () => null } },
        ],
      })
      provide(routerKey, router)
      return () => h(
        'div',
        {
          class: 'input-actions composer-bar',
          style: { width: options.width ? `${options.width}px` : '100%', maxWidth: '100%', boxSizing: 'border-box', display: 'flex', alignItems: 'center', gap: '4px' },
        },
        [h(ChatLoopChip, { chatId: CHAT, archived: options.archived ?? false })],
      )
    },
  })
}

const meta = {
  id: 'chat-loop-chip',
  title: 'Chat composer/ChatLoopChip',
  component: ChatLoopChip,
  args: { chatId: CHAT },
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof ChatLoopChip>
export default meta
type Story = StoryObj<typeof meta>

const chip = (canvasElement: HTMLElement) => canvasElement.querySelector<HTMLButtonElement>('button.chat-loop-chip')!

// One loop: the chip names its cadence and end, and the full summary is the
// accessible name and tooltip.
export const Default: Story = {
  render: () => chipIn([loop('alpha')]),
  play: async ({ canvasElement }) => {
    const button = chip(canvasElement)
    expect(button.textContent).toContain('Loop · every 5 min · ends')
    expect(button.getAttribute('aria-label')).toContain('7 runs left')
    expect(button.getAttribute('aria-haspopup')).toBe('menu')
    expect(button.getAttribute('aria-expanded')).toBe('false')
  },
}

// Two loops in one chat: the count, and the menu lists each with its own Stop.
export const Multiple: Story = {
  render: () => chipIn([loop('alpha'), loop('beta', { interval_minutes: 15, run_count: 9 })]),
  play: async ({ canvasElement }) => {
    const button = chip(canvasElement)
    expect(button.textContent).toContain('Loops · 2')
    expect(button.getAttribute('aria-label')).toBe('2 loops in this chat')
  },
}

// A 390px composer: the short form carries the chip, the bar never wraps, and
// the full summary stays in the accessible name.
export const Narrow: Story = {
  render: () => chipIn([loop('alpha')], { width: 390 }),
  play: async ({ canvasElement }) => {
    const button = chip(canvasElement)
    expect(button.querySelector('.chat-loop-chip__label--short')?.textContent).toBe('Loop · 5m')
    expect(button.getAttribute('aria-label')).toContain('every 5 min')
    expect(button.getBoundingClientRect().height).toBeGreaterThanOrEqual(32)
  },
}

// Opened: the menu carries Open in Automations and Stop loop for the loop, and
// is portalled to the document, so the play reads it from there.
export const MenuOpen: Story = {
  render: () => chipIn([loop('alpha')]),
  play: async ({ canvasElement, userEvent }) => {
    const button = chip(canvasElement)
    await userEvent.click(button)
    const items = Array.from(document.querySelectorAll<HTMLElement>('[role="menuitem"]')).map(el => el.textContent?.trim())
    expect(items).toEqual(['Open in Automations', 'Stop loop'])
    expect(button.getAttribute('aria-expanded')).toBe('true')
  },
}

// Keyboard: the chip takes focus with a visible ring, and Enter opens its menu.
export const KeyboardFocus: Story = {
  render: () => chipIn([loop('alpha')]),
  play: async ({ canvasElement, userEvent }) => {
    const button = chip(canvasElement)
    button.focus()
    expect(document.activeElement).toBe(button)
    await userEvent.keyboard('{Enter}')
    expect(button.getAttribute('aria-expanded')).toBe('true')
  },
}

// An archived chat keeps its loops in the record but offers no chip.
export const Archived: Story = {
  render: () => chipIn([loop('alpha')], { archived: true }),
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('button.chat-loop-chip')).toBeNull()
  },
}
