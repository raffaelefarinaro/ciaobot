import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
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
// Whitespace-only input is not a value: submission stays disabled and the
// request keeps waiting for an answer.
export const Whitespace: Story = {
  args: { value: '   ' },
  play: async ({ canvas, userEvent }) => {
    const input = await waitFor(() => canvas.getByLabelText('Project name')) as HTMLInputElement
    expect(input.value).toBe('   ')
    const submit = await waitFor(() => canvas.getByRole('button', { name: 'Create' })) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    await userEvent.click(submit)
    expect(submit.disabled).toBe(true)
    // role="status" is pruned from the accessibility tree while the modal is
    // open, so the host's answer line is reached by text.
    expect(canvas.getByText(/Waiting for an answer/)).not.toBeNull()
    expect(canvas.queryByRole('dialog')).not.toBeNull()
  },
}
export const Prefilled: Story = { args: { title: 'Rename project', confirmLabel: 'Rename', value: 'Kitchen renovation' } }
export const Populated: Story = { args: { value: '  Team workshop  ' } }
// Disabled is the production validation state, not an invented disabled prop.
export const Disabled: Story = { args: { value: '   ' } }
export const Narrow: Story = { args: { ...Prefilled.args, narrow: true } }
