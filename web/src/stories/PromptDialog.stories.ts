import type { Meta, StoryObj } from '@storybook/vue3-vite'
import DialogStoryHost from './DialogStoryHost.vue'

const meta = {
  id: 'prompt-dialog',
  title: 'Dialogs/Prompt dialog',
  component: DialogStoryHost,
  tags: ['!autodocs'],
  parameters: { docs: { disable: true } },
  args: { kind: 'prompt', title: 'New project', message: 'Project name', confirmLabel: 'Create', value: '' },
} satisfies Meta<typeof DialogStoryHost>
export default meta
type Story = StoryObj<typeof meta>

export const Empty: Story = {}
export const Whitespace: Story = { args: { value: '   ' } }
export const Prefilled: Story = { args: { title: 'Rename project', confirmLabel: 'Rename', value: 'Kitchen renovation' } }
export const Populated: Story = { args: { value: '  Team workshop  ' } }
// Disabled is the production validation state, not an invented disabled prop.
export const Disabled: Story = { args: { value: '   ' } }
export const Narrow: Story = { args: { ...Prefilled.args, narrow: true } }
