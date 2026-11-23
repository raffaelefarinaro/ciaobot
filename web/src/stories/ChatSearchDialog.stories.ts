import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, userEvent, waitFor } from 'storybook/test'
import { defineComponent, h, ref } from 'vue'
import ChatSearchDialog from '../components/ChatSearchDialog.vue'

function renderSearchDialog() {
  return defineComponent({
    setup() {
      const open = ref(false)
      return () => h('div', [
        h('button', { type: 'button', onClick: () => { open.value = true } }, 'Search chats'),
        h(ChatSearchDialog, {
          open: open.value,
          workspace: 'storybook-alpha',
          onClose: () => { open.value = false },
          onSelect: () => { open.value = false },
        }),
      ])
    },
  })
}

const meta = {
  id: 'chat-search-dialog',
  title: 'Chat/Search chats dialog',
  tags: ['!autodocs'],
  parameters: { docs: { disable: true }, controls: { disable: true } },
} satisfies Meta
export default meta
type Story = StoryObj<typeof meta>

export const KeyboardDismissal: Story = {
  render: () => renderSearchDialog(),
  play: async () => {
    const opener = screen.getByRole('button', { name: 'Search chats' })
    await userEvent.click(opener)
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Search chats' })).toBeTruthy())
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(opener)
  },
}
