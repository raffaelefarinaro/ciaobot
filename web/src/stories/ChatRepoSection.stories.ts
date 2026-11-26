import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import ChatRepoSection from '../components/ChatRepoSection.vue'
import { held, json, route } from './settingsAccessFixture'

const repo = { path: '/repos/ciao', branch: 'develop', dirty: false, missing: false }
const meta = {
  id: 'chat-repo-section',
  title: 'Chat/ChatRepoSection',
  component: ChatRepoSection,
  args: { repo, location: null, chatId: 'story-chat', eligible: false },
} satisfies Meta<typeof ChatRepoSection>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  play: async ({ canvas }) => {
    expect(canvas.getByRole('heading', { name: 'Repository' })).toBeTruthy()
    expect(canvas.getByText('/repos/ciao')).toBeTruthy()
    expect(canvas.getByText('clean')).toBeTruthy()
    expect(canvas.queryByRole('button', { name: 'View changes' })).toBeNull()
  },
}

export const Missing: Story = {
  args: { repo: { ...repo, missing: true } },
  play: async ({ canvas }) => {
    expect(canvas.getByRole('status').textContent).toContain('linked folder is missing')
    expect(canvas.queryByRole('button', { name: 'Copy path' })).toBeNull()
  },
}
export const Dirty: Story = {
  args: { repo: { ...repo, dirty: true } },
  play: async ({ canvas }) => { expect(canvas.getByText('uncommitted changes')).toBeTruthy() },
}

export const Worktree: Story = {
  args: { location: { path: '/worktrees/ciao', branch: 'feature', is_worktree: true }, eligible: true },
  play: async ({ canvas }) => {
    expect(canvas.getByText('/worktrees/ciao')).toBeTruthy()
    expect(canvas.getByText('worktree')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'View changes' })).toBeTruthy()
  },
}

const changes = (respond: () => Response | Promise<never>) => ({ fixture: { routes: [route('GET', '/api/chats/story-chat/worktree-changes', respond)] } })
const openChanges: Story['play'] = async ({ canvas, userEvent }) => {
  await userEvent.click(canvas.getByRole('button', { name: 'View changes' }))
}

export const Loading: Story = {
  args: { eligible: true }, parameters: changes(held),
  play: async (context) => {
    await openChanges!(context)
    expect(context.canvas.getByRole('status').textContent).toContain('Loading checkout changes')
  },
}
export const Error: Story = {
  args: { eligible: true }, parameters: changes(() => json({ error: 'Unavailable' }, 503)),
  play: async (context) => {
    await openChanges!(context)
    await waitFor(() => expect(context.canvas.getByRole('alert').textContent).toContain('Could not read checkout changes'))
    expect(context.canvas.getByRole('button', { name: 'Retry' })).toBeTruthy()
  },
}
export const EmptyChanges: Story = {
  args: { eligible: true }, parameters: changes(() => json({ state: 'ready', source: 'linked_checkout', staged: '', unstaged: '', untracked: [], truncated: false })),
  play: async (context) => {
    await openChanges!(context)
    await waitFor(() => expect(context.canvas.getByRole('status').textContent).toContain('No uncommitted changes'))
  },
}
export const Changes: Story = {
  args: { eligible: true }, parameters: changes(() => json({ state: 'ready', source: 'worktree', staged: '+ staged', unstaged: '- old', untracked: ['new.txt'], truncated: true })),
  play: async (context) => {
    await openChanges!(context)
    await waitFor(() => expect(context.canvas.getByText('+ staged')).toBeTruthy())
    expect(context.canvas.getByText('- old')).toBeTruthy()
    expect(context.canvas.getByText('new.txt')).toBeTruthy()
    expect(context.canvas.getByRole('status').textContent).toContain('truncated')
  },
}
export const Unavailable: Story = {
  args: { eligible: true }, parameters: changes(() => json({ state: 'unavailable', reason: 'unsupported_checkout' })),
  play: async (context) => {
    await openChanges!(context)
    await waitFor(() => expect(context.canvas.getByRole('status').textContent).toContain('cannot be read safely'))
  },
}
