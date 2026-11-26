import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { defineComponent, h, ref } from 'vue'
import WorkspaceMoveDialog from '../components/WorkspaceMoveDialog.vue'
import { held, json, route } from './settingsAccessFixture'

const meta = { id: 'workspace-move-dialog', title: 'Chat/WorkspaceMoveDialog', component: WorkspaceMoveDialog, args: { open: true, workspaceRoot: '/synthetic/ciao' } } satisfies Meta<typeof WorkspaceMoveDialog>
export default meta
type Story = StoryObj<typeof meta>
const listing = { path: '/synthetic', display_path: '/synthetic', parent: '/', dirs: [] }
const dirs = (respond: () => Response | Promise<never>) => route('GET', '/api/workspace-move/dirs?path=%2Fsynthetic', respond)
const plan = (respond: () => Response | Promise<never>) => route('POST', '/api/workspace-move/plan', respond)
export const Loading: Story = {
  parameters: { fixture: { routes: [dirs(held)] } },
  play: async () => { expect(screen.getByRole('button', { name: 'Move' }).hasAttribute('disabled')).toBe(true) },
}
export const Empty: Story = {
  parameters: { fixture: { routes: [dirs(() => json(listing)), plan(() => json({ source: '/synthetic/ciao', target: '/synthetic/ciao', ok: false, warnings: [], refusals: ['Choose a different destination'] }))] } },
  play: async () => { await waitFor(() => expect(screen.getByText('No folders here')).toBeTruthy()) },
}
export const Failed: Story = {
  parameters: { fixture: { routes: [dirs(() => json({ error: 'Synthetic folder error' }, 503))] } },
  play: async () => { await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Synthetic folder error')) },
}
export const Refused: Story = {
  parameters: { fixture: { routes: [dirs(() => json(listing)), plan(() => json({ source: '/synthetic/ciao', target: '/synthetic/ciao', ok: false, warnings: ['Synthetic warning'], refusals: ['Synthetic refusal'] }))] } },
  play: async () => { await waitFor(() => expect(screen.getByText('Synthetic refusal')).toBeTruthy()); expect(screen.getByText('Synthetic warning')).toBeTruthy(); expect(screen.getByRole('button', { name: 'Move' }).hasAttribute('disabled')).toBe(true) },
}
export const Planning: Story = {
  parameters: { fixture: { routes: [dirs(() => json(listing)), plan(held)] } },
  play: async () => { await waitFor(() => expect(screen.getByText('No folders here')).toBeTruthy()); expect(screen.getByRole('button', { name: 'Move' }).hasAttribute('disabled')).toBe(true) },
}
export const StartFailed: Story = {
  parameters: { fixture: { routes: [dirs(() => json(listing)), plan(() => json({ source: '/synthetic/ciao', target: '/synthetic/new-ciao', ok: true, warnings: [], refusals: [] })), route('POST', '/api/workspace-move', () => json({ error: 'Synthetic move failure' }, 503))] } },
  play: async ({ userEvent }) => {
    await userEvent.clear(screen.getByLabelText('Folder name'))
    await userEvent.type(screen.getByLabelText('Folder name'), 'new-ciao')
    await waitFor(() => expect((screen.getByRole('button', { name: 'Move' }) as HTMLButtonElement).disabled).toBe(false))
    await userEvent.click(screen.getByRole('button', { name: 'Move' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Synthetic move failure'))
  },
}
export const Starting: Story = {
  parameters: { fixture: { routes: [dirs(() => json(listing)), plan(() => json({ source: '/synthetic/ciao', target: '/synthetic/new-ciao', ok: true, warnings: [], refusals: [] })), route('POST', '/api/workspace-move', held)] } },
  play: async ({ userEvent }) => {
    await userEvent.clear(screen.getByLabelText('Folder name'))
    await userEvent.type(screen.getByLabelText('Folder name'), 'new-ciao')
    await waitFor(() => expect((screen.getByRole('button', { name: 'Move' }) as HTMLButtonElement).disabled).toBe(false))
    await userEvent.click(screen.getByRole('button', { name: 'Move' }))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Starting…' }) as HTMLButtonElement).disabled).toBe(true))
  },
}
const moveOwner = defineComponent({ setup() { const target = ref(''); return () => [h(WorkspaceMoveDialog, { open: true, workspaceRoot: '/synthetic/ciao', onStarted: (value: string) => { target.value = value } }), h('p', { 'data-testid': 'started-target' }, target.value)] } })
export const Started: Story = {
  parameters: { fixture: { routes: [dirs(() => json(listing)), plan(() => json({ source: '/synthetic/ciao', target: '/synthetic/new-ciao', ok: true, warnings: [], refusals: [] })), route('POST', '/api/workspace-move', () => json({}))] } },
  render: () => moveOwner,
  play: async ({ canvas, userEvent }) => {
    await userEvent.clear(screen.getByLabelText('Folder name'))
    await userEvent.type(screen.getByLabelText('Folder name'), 'new-ciao')
    await waitFor(() => expect((screen.getByRole('button', { name: 'Move' }) as HTMLButtonElement).disabled).toBe(false))
    await userEvent.click(screen.getByRole('button', { name: 'Move' }))
    await waitFor(() => expect(canvas.getByTestId('started-target').textContent).toBe('/synthetic/new-ciao'))
  },
}
export const Browse: Story = {
  parameters: { fixture: { routes: [dirs(() => json({ ...listing, dirs: [{ name: 'Archive', path: '/synthetic/archive' }] })), route('GET', '/api/workspace-move/dirs?path=%2Fsynthetic%2Farchive', () => json({ path: '/synthetic/archive', display_path: '/synthetic/archive', parent: '/synthetic', dirs: [] })), plan(() => json({ source: '/synthetic/ciao', target: '/synthetic/archive/ciao', ok: true, warnings: [], refusals: [] }))] } },
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByRole('button', { name: 'Archive' })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(screen.getByText('/synthetic/archive')).toBeTruthy())
    expect(screen.getByText('No folders here')).toBeTruthy()
  },
}
export const BrowseUp: Story = {
  parameters: { fixture: { routes: [dirs(() => json(listing)), route('GET', '/api/workspace-move/dirs?path=%2F', () => json({ path: '/', display_path: '/', parent: null, dirs: [{ name: 'synthetic', path: '/synthetic' }] })), plan(() => json({ source: '/synthetic/ciao', target: '/ciao', ok: true, warnings: [], refusals: [] }))] } },
  play: async ({ userEvent }) => {
    await waitFor(() => expect((screen.getByRole('button', { name: 'Up' }) as HTMLButtonElement).disabled).toBe(false))
    await userEvent.click(screen.getByRole('button', { name: 'Up' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'synthetic' })).toBeTruthy())
    expect((screen.getByRole('button', { name: 'Up' }) as HTMLButtonElement).disabled).toBe(true)
  },
}
