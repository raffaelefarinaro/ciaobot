import type { Meta, StoryObj } from '@storybook/vue3-vite'
import SidebarListSkeleton from '../components/SidebarListSkeleton.vue'

const meta = {
  id: 'foundation-sidebar-list-skeleton',
  title: 'Foundation/Sidebar list skeleton',
  component: SidebarListSkeleton,
} satisfies Meta<typeof SidebarListSkeleton>
export default meta
type Story = StoryObj<typeof meta>

export const Loading: Story = { args: { label: 'Loading automations', groups: 1, rows: 3 } }
export const Nested: Story = { args: { label: 'Loading chats', groups: 2, rows: 2, nested: true } }
