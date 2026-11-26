import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref } from 'vue'
import NewChatPicker from '../components/NewChatPicker.vue'
import { openNewChatPicker, pendingNewChat } from '../lib/newChat'
import { connectedOwnerStory, seedInbox } from './notificationInboxFixture'
import { useProjectStore } from '../stores/projects'

const meta = { id: 'new-chat-picker', title: 'Chat/NewChatPicker', component: NewChatPicker } satisfies Meta<typeof NewChatPicker>
export default meta
type Story = StoryObj<typeof meta>
const owner = defineComponent({ setup() { const result = ref('pending'); onMounted(() => { void openNewChatPicker().then(value => { result.value = value ?? 'cancelled' }) }); onBeforeUnmount(() => pendingNewChat.value?.resolve(null)); return () => [h(NewChatPicker), h('p', { 'data-testid': 'picker-result' }, result.value)] } })
const picker = (empty: boolean) => connectedOwnerStory({ component: owner, seed: pinia => { seedInbox(pinia); if (empty) useProjectStore(pinia).projects = [] }, label: 'new chat picker' })
export const Default: Story = {
  render: () => picker(false),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByRole('listbox', { name: 'Choose a project for the new chat' })).toBeTruthy())
    expect(screen.getAllByRole('option').length).toBeGreaterThan(0)
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
  },
}
export const Empty: Story = {
  render: () => picker(true),
  play: async () => { await waitFor(() => expect(screen.getByRole('listbox')).toBeTruthy()); expect(screen.queryAllByRole('option')).toHaveLength(0) },
}
export const Choose: Story = {
  render: () => picker(false),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(screen.getAllByRole('option').length).toBeGreaterThan(0))
    await userEvent.click(screen.getByRole('option', { name: /Synthetic project storybook-p1/ }))
    await waitFor(() => expect(canvas.getByTestId('picker-result').textContent).toBe('storybook-p1'))
  },
}
export const SwitchWorkspace: Story = {
  render: () => picker(false),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByRole('option', { name: /storybook-p1/ })).toBeTruthy())
    await userEvent.keyboard('2')
    await waitFor(() => expect(screen.getByRole('option', { name: /storybook-p2/ })).toBeTruthy())
    expect(screen.queryByRole('option', { name: /storybook-p1/ })).toBeNull()
  },
}
export const GeneralAndMemory: Story = {
  render: () => connectedOwnerStory({
    component: owner,
    seed: pinia => {
      seedInbox(pinia)
      const store = useProjectStore(pinia)
      const project = store.projects[0]
      project.name = 'General'
      project.is_auto = true
      store.projects.push({ ...project, project_id: 'memory', name: 'Memory', kind: 'memory' })
    },
    label: 'new chat picker',
  }),
  play: async () => {
    const option = await waitFor(() => screen.getByRole('option', { name: /General/ }))
    expect(option.getAttribute('aria-selected')).toBe('true')
    expect(option.querySelector('.newchat-badge')?.textContent).toBe('General')
    expect(screen.queryByRole('option', { name: /Memory/ })).toBeNull()
  },
}
