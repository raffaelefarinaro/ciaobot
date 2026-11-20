import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import DialogStoryHost from './DialogStoryHost.vue'

const meta = {
  id: 'confirm-dialog',
  title: 'Dialogs/Confirm dialog',
  component: DialogStoryHost,
  tags: ['!autodocs'],
  parameters: { docs: { disable: true } },
  args: {
    kind: 'confirm', title: 'Archive chat', message: 'Archive this chat? You can reopen it from Archived.',
    confirmLabel: 'Archive',
  },
} satisfies Meta<typeof DialogStoryHost>
export default meta
type Story = StoryObj<typeof meta>

// The host opens the dialog on mount; Escape resolves the request as a cancel.
// Expected text is literal on purpose: changing the story's initial args (title,
// message or confirm label) must fail this play, not follow the change.
export const Normal: Story = {
  play: async ({ canvas, userEvent }) => {
    const dialog = await waitFor(() => canvas.getByRole('dialog', { name: 'Archive chat' }))
    expect(dialog.textContent).toContain('Archive this chat? You can reopen it from Archived.')
    expect(canvas.getByRole('button', { name: 'Archive' }).textContent?.trim()).toBe('Archive')
    // role="status" is pruned from the accessibility tree while the modal is
    // open, so the host's answer line is reached by text.
    const status = canvas.getByText(/Waiting for an answer/)
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(status.textContent).toContain('Result: false'))
    await waitFor(() => expect(canvas.queryByRole('dialog')).toBeNull())
  },
}
export const Destructive: Story = {
  args: { title: 'Delete task', message: 'Delete this task permanently? This cannot be undone.', confirmLabel: 'Delete', destructive: true },
}
export const Reason: Story = {
  args: { ...Destructive.args, withReason: true },
}
export const LongContent: Story = {
  args: {
    title: 'Archive the planning conversation and its follow-up notes?',
    message: 'This conversation includes the planning notes for the next workshop and the decisions shared with the team.\n\nArchiving removes it from the active chat list. The conversation and its attachments remain available in Archived, and you can reopen it whenever you need the context.',
    confirmLabel: 'Archive conversation',
  },
}
export const Narrow: Story = { args: { ...Reason.args, narrow: true } }
