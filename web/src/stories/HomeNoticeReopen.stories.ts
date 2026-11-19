import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount } from 'vue'
import HomeNoticeReopen from '../components/HomeNoticeReopen.vue'
import { resetHomeNoticeWindows, useHomeNoticeWindows } from '../composables/useHomeNoticeWindows'
import { narrowStyle } from './homeWindowsFixture'

const keys = ['storybook:one', 'storybook:two'] as const

function renderReopen(closed: number, narrow = false) {
  return defineComponent({
    setup() {
      resetHomeNoticeWindows()
      const notices = useHomeNoticeWindows()
      notices.setAvailable('storybook', [...keys].slice(0, closed))
      for (const key of keys.slice(0, closed)) notices.close(key)
      onBeforeUnmount(() => resetHomeNoticeWindows())
      return () => h('div', narrow ? { style: narrowStyle } : {}, [
        h(HomeNoticeReopen),
        h('p', { role: 'status', 'aria-label': 'Harness ledger' }, `Closed notices in the harness: ${notices.closedCount.value}.`),
      ])
    },
  })
}

const meta = {
  id: 'home-notice-reopen',
  title: 'Home windows/HomeNoticeReopen',
  component: HomeNoticeReopen,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof HomeNoticeReopen>
export default meta
type Story = StoryObj<typeof meta>

function ledger(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas']) {
  return canvas.getByRole('status', { name: 'Harness ledger' })
}

export const Hidden: Story = {
  render: () => renderReopen(0),
  play: async ({ canvas }) => {
    expect(canvas.queryByRole('button', { name: /closed notice/ })).toBeNull()
    expect(ledger(canvas).textContent).toContain('Closed notices in the harness: 0')
  },
}

export const Singular: Story = {
  render: () => renderReopen(1),
  play: async ({ canvas, userEvent }) => {
    const reopen = canvas.getByRole('button', { name: 'Show 1 closed notice' })
    expect(reopen.getAttribute('data-home-notices-reopen')).toBe('')
    await userEvent.click(reopen)
    await waitFor(() => expect(canvas.queryByRole('button', { name: /closed notice/ })).toBeNull())
    expect(ledger(canvas).textContent).toContain('Closed notices in the harness: 0')
  },
}

export const Plural: Story = {
  render: () => renderReopen(2),
  play: async ({ canvas, userEvent }) => {
    const reopen = canvas.getByRole('button', { name: 'Show 2 closed notices' })
    await userEvent.click(reopen)
    await waitFor(() => expect(canvas.queryByRole('button', { name: /closed notice/ })).toBeNull())
    expect(ledger(canvas).textContent).toContain('Closed notices in the harness: 0')
  },
}

export const KeyboardFocus: Story = {
  render: () => renderReopen(1, true),
  play: async ({ canvas, userEvent }) => {
    const reopen = canvas.getByRole('button', { name: 'Show 1 closed notice' })
    for (let step = 0; step < 6 && document.activeElement !== reopen; step++) await userEvent.tab()
    expect(document.activeElement).toBe(reopen)
    expect(getComputedStyle(reopen).outlineWidth).toBe('2px')
    expect(reopen.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(canvas.queryByRole('button', { name: /closed notice/ })).toBeNull())
  },
}
