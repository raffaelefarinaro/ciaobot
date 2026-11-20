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
// Whitespace-only input is not a value: the submit button is disabled, so
// neither a click nor Enter in the field fires the form's submit event, and the
// request keeps waiting for an answer.
export const Whitespace: Story = {
  args: { value: '   ' },
  play: async ({ canvas, userEvent }) => {
    const input = await waitFor(() => canvas.getByLabelText('Project name')) as HTMLInputElement
    expect(input.value).toBe('   ')
    const submit = canvas.getByRole('button', { name: 'Create' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    const form = input.form!
    let submitted = 0
    form.addEventListener('submit', () => { submitted += 1 }, true)
    await userEvent.click(submit)
    input.focus()
    await userEvent.keyboard('{Enter}')
    expect(submitted).toBe(0)
    expect(input.value).toBe('   ')
    // role="status" is pruned from the accessibility tree while the modal is
    // open, so the host's answer line is reached by text.
    expect(canvas.getByText(/Waiting for an answer/).textContent).toBe('Result: Waiting for an answer')
    expect(canvas.getByRole('dialog')).toBe(form)
  },
}
export const Prefilled: Story = { args: { title: 'Rename project', confirmLabel: 'Rename', value: 'Kitchen renovation' } }
export const Populated: Story = { args: { value: '  Team workshop  ' } }
// Disabled is the production validation state, not an invented disabled prop.
export const Disabled: Story = { args: { value: '   ' } }
export const Narrow: Story = { args: { ...Prefilled.args, narrow: true } }
