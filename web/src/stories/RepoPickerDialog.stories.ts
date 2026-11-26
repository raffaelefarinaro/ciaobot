import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { defineComponent, h, ref } from 'vue'
import RepoPickerDialog from '../components/RepoPickerDialog.vue'
import { connectedOwnerStory, seedInbox } from './notificationInboxFixture'
import { held, json, route } from './settingsAccessFixture'

const meta = { id: 'repo-picker-dialog', title: 'Chat/RepoPickerDialog', component: RepoPickerDialog, args: { open: true, projectId: 'storybook-project', currentPath: null } } satisfies Meta<typeof RepoPickerDialog>
export default meta
type Story = StoryObj<typeof meta>
const fixture = (respond: () => Response | Promise<never>) => ({ fixture: { routes: [route('GET', '/api/repos/candidates', respond)] } })
const renderDialog = () => connectedOwnerStory({ component: RepoPickerDialog, props: { open: true, projectId: 'storybook-project', currentPath: null }, seed: pinia => seedInbox(pinia), label: 'repository picker' })
export const Loading: Story = {
  parameters: fixture(held), render: renderDialog,
  play: async () => { await waitFor(() => expect(screen.getByText('Looking for repositories…')).toBeTruthy()) },
}
export const Empty: Story = {
  parameters: fixture(() => json({ candidates: [] })), render: renderDialog,
  play: async () => { await waitFor(() => expect(screen.getByText(/No git repositories found/)).toBeTruthy()) },
}
export const Failed: Story = {
  parameters: fixture(() => json({ error: 'Synthetic read failed' }, 503)), render: renderDialog,
  play: async () => { await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Synthetic read failed')); expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy() },
}
export const Candidates: Story = {
  parameters: fixture(() => json({ candidates: [{ name: 'example', path: '/repos/example' }] })), render: renderDialog,
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText('/repos/example')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /example/ }))
    expect((screen.getByLabelText('Repository folder') as HTMLInputElement).value).toBe('/repos/example')
    expect((screen.getByRole('button', { name: 'Link' }) as HTMLButtonElement).disabled).toBe(false)
  },
}
export const SaveFailed: Story = {
  parameters: { fixture: { routes: [route('GET', '/api/repos/candidates', () => json({ candidates: [] })), route('PATCH', '/api/projects/storybook-project', () => json({ error: 'Not a repository' }, 400))] } },
  render: renderDialog,
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText(/No git repositories found/)).toBeTruthy())
    await userEvent.type(screen.getByLabelText('Repository folder'), '/synthetic/not-git')
    await userEvent.click(screen.getByRole('button', { name: 'Link' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Not a repository'))
    expect(screen.getByRole('dialog')).toBeTruthy()
  },
}
export const Saving: Story = {
  parameters: { fixture: { routes: [route('GET', '/api/repos/candidates', () => json({ candidates: [] })), route('PATCH', '/api/projects/storybook-project', held)] } },
  render: renderDialog,
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText(/No git repositories found/)).toBeTruthy())
    await userEvent.type(screen.getByLabelText('Repository folder'), '/synthetic/example')
    await userEvent.click(screen.getByRole('button', { name: 'Link' }))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Saving…' }) as HTMLButtonElement).disabled).toBe(true))
  },
}
export const ChangeExisting: Story = {
  args: { currentPath: '/repos/existing' },
  parameters: fixture(() => json({ candidates: [] })),
  render: () => connectedOwnerStory({ component: RepoPickerDialog, props: { open: true, projectId: 'storybook-project', currentPath: '/repos/existing' }, seed: pinia => seedInbox(pinia), label: 'repository picker' }),
  play: async () => {
    await waitFor(() => expect(screen.getByText('Change repository')).toBeTruthy())
    expect((screen.getByLabelText('Repository folder') as HTMLInputElement).value).toBe('/repos/existing')
    expect((screen.getByRole('button', { name: 'Change' }) as HTMLButtonElement).disabled).toBe(true)
  },
}
const saveOwner = defineComponent({
  setup() {
    const events = ref<string[]>([])
    return () => [h(RepoPickerDialog, { open: true, projectId: 'storybook-project', currentPath: null, onLinked: () => events.value.push('linked'), onClose: () => events.value.push('close') }), h('p', { 'data-testid': 'save-events' }, events.value.join(','))]
  },
})
export const Save: Story = {
  parameters: { fixture: { routes: [route('GET', '/api/repos/candidates', () => json({ candidates: [] })), route('PATCH', '/api/projects/storybook-project', () => json({ project_id: 'storybook-project', repo: { path: '/repos/example' } }))] } },
  render: () => connectedOwnerStory({ component: saveOwner, seed: pinia => seedInbox(pinia), label: 'repository picker' }),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(screen.getByText(/No git repositories found/)).toBeTruthy())
    await userEvent.type(screen.getByLabelText('Repository folder'), '/repos/example')
    await userEvent.click(screen.getByRole('button', { name: 'Link' }))
    await waitFor(() => expect(canvas.getByTestId('save-events').textContent).toBe('linked,close'))
  },
}
let candidateAttempts = 0
export const Retry: Story = {
  parameters: fixture(() => ++candidateAttempts === 1 ? json({ error: 'Synthetic read failed' }, 503) : json({ candidates: [{ name: 'example', path: '/repos/example' }] })),
  render: () => { candidateAttempts = 0; return renderDialog() },
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByText('/repos/example')).toBeTruthy())
  },
}
export const SelectionWhileSaving: Story = {
  parameters: { fixture: { routes: [route('GET', '/api/repos/candidates', () => json({ candidates: [{ name: 'first', path: '/repos/first' }, { name: 'second', path: '/repos/second' }] })), route('PATCH', '/api/projects/storybook-project', held)] } },
  render: renderDialog,
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByRole('button', { name: /first/ })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /first/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Link' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Saving…' })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /second/ }))
    expect((screen.getByLabelText('Repository folder') as HTMLInputElement).value).toBe('/repos/second')
    expect((screen.getByRole('button', { name: 'Saving…' }) as HTMLButtonElement).disabled).toBe(true)
  },
}
