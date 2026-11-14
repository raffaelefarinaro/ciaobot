import type { Meta, StoryObj } from '@storybook/vue3-vite'
import SidebarRow from '../components/SidebarRow.vue'

const meta = {
  id: 'foundation-sidebar-row',
  title: 'Foundation/Sidebar row',
  component: SidebarRow,
  args: { text: 'Weekly review', meta: '09:00' },
} satisfies Meta<typeof SidebarRow>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {}
export const Active: Story = { args: { active: true } }
export const Unread: Story = { args: { unread: true } }
export const Muted: Story = { args: { muted: true } }
export const Pending: Story = { args: { pending: true } }
export const LongContent: Story = {
  args: { text: 'Weekly review of projects, outstanding decisions and next steps' },
  render: (args) => ({
    components: { SidebarRow },
    setup: () => ({ args }),
    template: '<div style="width: 240px"><SidebarRow v-bind="args" /></div>',
  }),
}
