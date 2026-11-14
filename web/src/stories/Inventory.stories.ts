import type { Meta, StoryObj } from '@storybook/vue3-vite'
import InventoryBrowser from './InventoryBrowser.vue'
import { inventory } from './inventory'

const meta = {
  id: 'inventory',
  title: 'Inventory/UI inventory',
  component: InventoryBrowser,
} satisfies Meta<typeof InventoryBrowser>
export default meta

type Story = StoryObj<typeof meta>
export const All: Story = { args: { entries: inventory } }
