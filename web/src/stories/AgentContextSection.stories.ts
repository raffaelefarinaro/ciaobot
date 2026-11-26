import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import AgentContextSection from '../components/AgentContextSection.vue'
import { held, json, route } from './settingsAccessFixture'
import { connectedOwnerStory, seedInbox } from './notificationInboxFixture'

const prompt = (respond: () => Response | Promise<never>) => ({ fixture: { routes: [route('GET', '/api/system-prompt', respond)] } })
const meta = {
  id: 'agent-context-section', title: 'Chat/AgentContextSection', component: AgentContextSection,
  args: { project: null, contextPct: 42, compacting: false, compactDisabled: false, revealKey: 1 },
  parameters: prompt(() => json({ text: 'Synthetic system prompt' })),
} satisfies Meta<typeof AgentContextSection>
export default meta
type Story = StoryObj<typeof meta>
export const Default: Story = {
  play: async ({ canvas, userEvent }) => {
    expect(canvas.getByRole('meter').getAttribute('aria-valuenow')).toBe('42')
    const details = (await waitFor(() => canvas.getByText('Always loaded'))).closest('details')!
    if (!details.open) await userEvent.click(canvas.getByText('Always loaded'))
    await waitFor(() => expect(canvas.getByRole('button', { name: /Ciaobot system prompt/ })).toBeTruthy())
    await userEvent.click(canvas.getByRole('button', { name: /Ciaobot system prompt/ }))
    expect(canvas.getByText('Synthetic system prompt')).toBeTruthy()
  },
}
export const Loading: Story = {
  parameters: prompt(held),
  play: async ({ canvas }) => { expect(canvas.getByText('Always loaded')).toBeTruthy() },
}
export const Failed: Story = {
  parameters: prompt(() => json({ error: 'Unavailable' }, 503)),
  play: async ({ canvas, userEvent }) => {
    const details = (await waitFor(() => canvas.getByText('Always loaded'))).closest('details')!
    if (!details.open) await userEvent.click(canvas.getByText('Always loaded'))
    await waitFor(() => expect(canvas.getByText('Could not be read just now')).toBeTruthy())
    expect(canvas.getByRole('button', { name: 'Retry' })).toBeTruthy()
  },
}
export const Compacting: Story = {
  args: { compacted: true, compacting: true, compactDisabled: true, contextPct: null },
  play: async ({ canvas }) => { expect((canvas.getByRole('button', { name: 'Compacting…' }) as HTMLButtonElement).disabled).toBe(true) },
}
export const ProjectGuide: Story = {
  args: { project: { project_id: 'example', name: 'Example project', workspace: 'storybook', context: 'Synthetic brief', created_at: '2026-01-01', order: 0, vault_folder: 'example', repo: { path: '/repos/example', branch: 'main', dirty: false, missing: false, instructions: { name: 'AGENTS.md', chars: 80 } } } },
  parameters: { fixture: { routes: [route('GET', '/api/system-prompt', () => json({ text: 'Synthetic system prompt' })), route('GET', '/api/workspace-file?exact=1&path=storybook%2FAGENTS.md', () => new Response('Synthetic guide'))] } },
  render: (args) => connectedOwnerStory({ component: AgentContextSection, props: args, seed: pinia => seedInbox(pinia), label: 'agent context' }),
  play: async ({ canvas, userEvent }) => {
    const details = (await waitFor(() => canvas.getByText('Always loaded'))).closest('details')!
    if (!details.open) await userEvent.click(canvas.getByText('Always loaded'))
    await waitFor(() => expect(canvas.getByText('Workspace guide · read every turn')).toBeTruthy())
    expect(canvas.getByText('Synthetic brief')).toBeTruthy()
    expect(canvas.getByText('Repository instructions')).toBeTruthy()
  },
}
export const RepositoryInstructionsNotFound: Story = {
  args: { project: { project_id: 'example', name: 'Example project', workspace: 'storybook', context: '', created_at: '2026-01-01', order: 0, vault_folder: 'example', repo: { path: '/repos/example', branch: 'main', dirty: false, missing: false, instructions: null } } },
  parameters: { fixture: { routes: [route('GET', '/api/system-prompt', () => json({ text: '' })), route('GET', '/api/workspace-file?exact=1&path=storybook%2FAGENTS.md', () => new Response('Synthetic guide'))] } },
  render: (args) => connectedOwnerStory({ component: AgentContextSection, props: args, seed: pinia => seedInbox(pinia), label: 'agent context' }),
  play: async ({ canvas, canvasElement, userEvent }) => {
    const details = (await waitFor(() => canvas.getByText('Always loaded'))).closest('details')!
    if (!details.open) await userEvent.click(canvas.getByText('Always loaded'))
    const row = await waitFor(() => {
      const found = canvasElement.querySelector('.agent-context-repo[role="status"]')
      expect(found).toBeTruthy()
      return found
    })
    expect(row!.textContent).toContain('Repository instructions')
    expect(row!.textContent).toContain('not found')
  },
}
export const GuideFailed: Story = {
  args: { project: { project_id: 'example', name: 'Example project', workspace: 'storybook', context: '', created_at: '2026-01-01', order: 0, vault_folder: 'example' } },
  parameters: { fixture: { routes: [route('GET', '/api/system-prompt', () => json({ text: '' })), route('GET', '/api/workspace-file?exact=1&path=storybook%2FAGENTS.md', () => json({ error: 'unavailable' }, 503)), route('GET', '/api/workspace-file?exact=1&path=storybook%2FCLAUDE.md', () => json({ error: 'unavailable' }, 503))] } },
  render: (args) => connectedOwnerStory({ component: AgentContextSection, props: args, seed: pinia => seedInbox(pinia), label: 'agent context' }),
  play: async ({ canvas, userEvent }) => {
    const details = (await waitFor(() => canvas.getByText('Always loaded'))).closest('details')!
    if (!details.open) await userEvent.click(canvas.getByText('Always loaded'))
    await waitFor(() => expect(canvas.getByText('Workspace guide')).toBeTruthy())
    expect(canvas.getByText(/Could not be read just now:/)).toBeTruthy()
  },
}
let clipboardText = ''
export const CopyPrompt: Story = {
  render: (args) => connectedOwnerStory({
    component: AgentContextSection, props: args, seed: pinia => seedInbox(pinia), label: 'agent context',
    prepare: () => {
      clipboardText = ''
      const original = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (text: string) => { clipboardText = text } } })
      return () => { if (original) Object.defineProperty(navigator, 'clipboard', original); else Reflect.deleteProperty(navigator, 'clipboard') }
    },
  }),
  play: async ({ canvas, userEvent }) => {
    const details = (await waitFor(() => canvas.getByText('Always loaded'))).closest('details')!
    if (!details.open) await userEvent.click(canvas.getByText('Always loaded'))
    await waitFor(() => expect(canvas.getByRole('button', { name: /Ciaobot system prompt/ })).toBeTruthy())
    await userEvent.click(canvas.getByRole('button', { name: /Ciaobot system prompt/ }))
    await userEvent.click(canvas.getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Copied' })).toBeTruthy())
    expect(clipboardText).toBe('Synthetic system prompt')
  },
}
let guideAttempts = 0
export const GuideRetry: Story = {
  args: { project: { project_id: 'example', name: 'Example project', workspace: 'storybook', context: '', created_at: '2026-01-01', order: 0, vault_folder: 'example' } },
  parameters: { fixture: { routes: [route('GET', '/api/system-prompt', () => json({ text: '' })), route('GET', '/api/workspace-file?exact=1&path=storybook%2FAGENTS.md', () => ++guideAttempts === 1 ? json({ error: 'unavailable' }, 503) : new Response('Synthetic guide')), route('GET', '/api/workspace-file?exact=1&path=storybook%2FCLAUDE.md', () => json({ error: 'unavailable' }, 503))] } },
  render: (args) => { guideAttempts = 0; return connectedOwnerStory({ component: AgentContextSection, props: args, seed: pinia => seedInbox(pinia), label: 'agent context' }) },
  play: async ({ canvas, userEvent }) => {
    const details = (await waitFor(() => canvas.getByText('Always loaded'))).closest('details')!
    if (!details.open) await userEvent.click(canvas.getByText('Always loaded'))
    await waitFor(() => expect(canvas.getByText(/Could not be read just now:/)).toBeTruthy())
    await userEvent.click(canvas.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(canvas.getByText('Workspace guide · read every turn')).toBeTruthy())
  },
}
