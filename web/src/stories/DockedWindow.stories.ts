import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import DockedWindow from '../components/DockedWindow.vue'

const meta = {
  id: 'foundation-docked-window',
  title: 'Foundation/Docked window',
  component: DockedWindow,
  args: { title: 'Recent runs' },
} satisfies Meta<typeof DockedWindow>
export default meta
type Story = StoryObj<typeof meta>

// A rail window as the Automations Recent runs and Home's At a glance use it:
// a 44px title bar over hairline rows, the last one closed by the window's edge.
export const WithRows: Story = {
  render: (args) => ({
    components: { DockedWindow },
    setup: () => ({ args }),
    template: `<div style="width: 280px">
      <DockedWindow v-bind="args">
        <div class="docked-list docked-list--end">
          <button type="button" class="docked-row"><span class="docked-row-title">Workspace care (work)</span><span class="docked-row-detail">Fri 13:00 · 2h ago</span></button>
          <button type="button" class="docked-row"><span class="docked-row-title">Check whether the OpenAI plugin submission is accepted, and summarise what changed since the last check</span><span class="docked-row-detail">Fri 10:00 · 5h ago</span></button>
          <button type="button" class="docked-row"><span class="docked-row-title">Morning action briefing</span><span class="docked-row-detail">Fri 08:00 · 7h ago</span></button>
        </div>
      </DockedWindow>
    </div>`,
  }),
  play: async ({ canvas }) => {
    expect(canvas.getByRole('region', { name: 'Recent runs' })).toBeTruthy()
    expect(canvas.getAllByRole('button')).toHaveLength(3)
  },
}
