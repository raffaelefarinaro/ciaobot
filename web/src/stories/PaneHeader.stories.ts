import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h } from 'vue'
import PaneHeader from '../components/PaneHeader.vue'
import { assertQuietFence, connectedOwnerStory, seedInbox } from './notificationInboxFixture'
import { headerHistory } from './headerHistoryFixture'

const meta = { id: 'pane-header', title: 'Chat/PaneHeader', component: PaneHeader } satisfies Meta<typeof PaneHeader>
export default meta
type Story = StoryObj<typeof meta>
const header = (withTitle: boolean) => defineComponent({ render: () => h(PaneHeader, { pageTag: 'settings', phoneOverflow: withTitle }, withTitle ? { title: () => h('h1', 'Example chat'), actions: () => h('button', 'Action') } : {}) })
export const Page: Story = {
  render: () => connectedOwnerStory({ component: header(false), seed: pinia => seedInbox(pinia), label: 'pane header' }),
  play: async ({ canvas }) => { await assertQuietFence(canvas); expect(canvas.getByRole('heading', { name: 'settings' })).toBeTruthy() },
}
export const Titled: Story = {
  render: () => connectedOwnerStory({ component: header(true), seed: pinia => seedInbox(pinia), label: 'pane header' }),
  play: async ({ canvas }) => { await assertQuietFence(canvas); expect(canvas.getByRole('heading', { name: 'Example chat' })).toBeTruthy(); expect(canvas.getByRole('button', { name: 'Action' })).toBeTruthy() },
}
export const Phone: Story = {
  globals: { viewport: { value: 'narrow' } },
  render: () => connectedOwnerStory({ component: header(true), seed: pinia => seedInbox(pinia), label: 'pane header' }),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    const sidebar = canvas.getByRole('button', { name: 'Open sidebar' })
    await userEvent.tab()
    expect(document.activeElement).toBe(sidebar)
    expect(sidebar.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
  },
}
export const History: Story = {
  globals: { viewport: { value: 'narrow' } },
  render: () => headerHistory(header(false)),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByTestId('history-ready').textContent).toBe('ready'))
    await userEvent.click(canvas.getByRole('button', { name: 'Back to Home' }))
    await waitFor(() => expect(canvas.getByTestId('history-route').textContent).toBe('/'))
  },
}
