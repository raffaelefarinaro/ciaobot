import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { useProjectStore } from '../stores/projects'
import OnboardingFinishStage from '../components/onboarding/OnboardingFinishStage.vue'
import { held, json, route, settled, stagePanel, WORKSPACE } from './onboardingStagesFixture'
const props = { context: { target: { kind: 'installation' as const }, workspace: WORKSPACE, revision: 1, provisioned: true }, busy: false }
const meta = { id: 'onboarding-finish-stage', title: 'Onboarding runtime/Finish', component: OnboardingFinishStage, args: props, tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } } } satisfies Meta<typeof OnboardingFinishStage>
export default meta
type Story = StoryObj<typeof meta>
const render = () => stagePanel({ label: 'finish', component: OnboardingFinishStage, props })
const paths = ['/api/workspaces', '/api/setup-status', '/api/local/backup/setup-prompt', '/api/settings/providers', '/api/proposals/handover?workspace=' + WORKSPACE, '/api/schedules']
const answers = [{ workspaces: [{ name: WORKSPACE }] }, { workspace_root: '/storybook/alpha' }, { context: {} }, { connections: {} }, { handover: 'off' }, []]
// The production project store navigates through the app router after creating a chat.
// Its asynchronous auth guard can still be in flight as another story mounts.
const authRoute = () => route('GET', '/api/auth/check', () => json({ authenticated: true }))
const routes = (mode: 'loaded' | 'held' | 'error') => [authRoute(), ...paths.map((path, i) => route('GET', path, mode === 'held' ? held : () => json(mode === 'error' ? {} : answers[i], mode === 'error' ? 503 : 200)))]
export const Loading: Story = { parameters: { fixture: { routes: routes('held') } }, render, play: async ({ canvasElement }) => { await settled(canvasElement); expect(canvasElement.textContent).not.toContain('Nothing needs attention') } }
export const Empty: Story = { parameters: { fixture: { routes: routes('loaded') } }, render, play: async ({ canvasElement }) => { await settled(canvasElement); expect(canvasElement.textContent).toContain('Nothing needs attention'); expect(canvasElement.querySelector<HTMLButtonElement>('button[type=submit]')!.disabled).toBe(true) } }
export const ReadFailure: Story = { parameters: { fixture: { routes: routes('error') } }, render, play: async ({ canvasElement }) => { await settled(canvasElement); await waitFor(() => expect(canvasElement.textContent).toContain('Could not read the folder')) } }
export const NoProject: Story = { parameters: { fixture: { routes: routes('loaded') } }, render, play: async ({ canvasElement, userEvent }) => { await settled(canvasElement); await userEvent.type(canvasElement.querySelector('textarea')!, 'A synthetic task'); await userEvent.click(canvasElement.querySelector<HTMLButtonElement>('button[type=submit]')!); await waitFor(() => expect(canvasElement.querySelector('[role=alert]')?.textContent).toContain('No project is available')); expect(canvasElement.querySelector('textarea')!.value).toBe('A synthetic task') } }

export const Attention: Story = { parameters: { fixture: { routes: [authRoute(), ...paths.map((path, i) => route('GET', path, () => json(i === 0 ? { workspaces: [{ name: WORKSPACE, provider_overrides: { claude: { model: 'storybook-model' } } }] } : i === 2 ? { context: { has_remote: true } } : answers[i])))] } }, render, play: async ({ canvasElement }) => { await settled(canvasElement); expect(canvasElement.textContent).toContain('off-machine backup is not verified'); expect(canvasElement.textContent).toContain('successful reply is not yet verified') } }

export const TaskAccepted: Story = {
  parameters: { fixture: { routes: [...routes('loaded'), route('POST', '/api/projects/storybook-project/chats', () => json({ chat_id: 'storybook-chat', project_id: 'storybook-project', title: 'New chat' })), route('POST', '/api/chats/storybook-chat/read', () => json({}))], sockets: [{ path: '/ws/chat/storybook-chat', script: (socket: { serverOpen: () => void }) => socket.serverOpen() }] } },
  render: () => stagePanel({ label: 'finish', component: OnboardingFinishStage, props, seed: pinia => { const store = useProjectStore(pinia); store.projects = [{ project_id: 'storybook-project', name: 'Storybook project', workspace: WORKSPACE, order: 0 }] as typeof store.projects; store.activeWorkspace = WORKSPACE } }),
  play: async ({ canvasElement, userEvent }) => { await settled(canvasElement); await userEvent.type(canvasElement.querySelector('textarea')!, 'A synthetic task'); await userEvent.click(canvasElement.querySelector<HTMLButtonElement>('button[type=submit]')!); await waitFor(() => expect(canvasElement.textContent).toContain('Task sent in a new chat.')); expect(canvasElement.querySelector('textarea')!.value).toBe(''); const { router } = await import('../router'); await waitFor(() => expect(router.currentRoute.value.params.chatId).toBe('storybook-chat')); expect(canvasElement.textContent).toContain('Violations: 0.') },
}

const seedProject = (pinia: import('pinia').Pinia) => { const store = useProjectStore(pinia); store.projects = [{ project_id: 'storybook-project', name: 'Storybook project', workspace: WORKSPACE, order: 0 }] as typeof store.projects; store.activeWorkspace = WORKSPACE }
export const Sending: Story = {
  parameters: { fixture: { routes: [...routes('loaded'), route('POST', '/api/projects/storybook-project/chats', held)] } },
  render: () => stagePanel({ label: 'finish', component: OnboardingFinishStage, props, seed: seedProject }),
  play: async ({ canvasElement, userEvent }) => { await settled(canvasElement); await userEvent.type(canvasElement.querySelector('textarea')!, 'A synthetic task'); await userEvent.click(canvasElement.querySelector<HTMLButtonElement>('button[type=submit]')!); await waitFor(() => expect(canvasElement.textContent).toContain('Starting…')); expect(canvasElement.querySelector<HTMLTextAreaElement>('textarea')!.disabled).toBe(true); expect(canvasElement.querySelector<HTMLButtonElement>('button[type=submit]')!.disabled).toBe(true); expect(canvasElement.querySelector('textarea')!.value).toBe('A synthetic task'); expect(canvasElement.textContent).toContain('Violations: 0.') },
}
export const SendRefused: Story = {
  parameters: { fixture: { routes: [...routes('loaded'), route('POST', '/api/projects/storybook-project/chats', () => json({ chat_id: 'storybook-chat', project_id: 'storybook-project', title: 'New chat' })), route('POST', '/api/chats/storybook-chat/read', () => json({}))], sockets: [{ path: '/ws/chat/storybook-chat', script: (socket: { serverOpen: () => void }) => socket.serverOpen() }] } },
  render: () => stagePanel({ label: 'finish', component: OnboardingFinishStage, props, seed: pinia => { seedProject(pinia); const store = useProjectStore(pinia); store.activeQuestions['storybook-chat'] = [{ id: 'storybook-question', question: 'Choose', header: 'Question', multiSelect: false, allowOther: false, isSecret: false, requestId: 'storybook-request', options: [] }] } }),
  play: async ({ canvasElement, userEvent }) => { await settled(canvasElement); await userEvent.type(canvasElement.querySelector('textarea')!, 'A synthetic task'); await userEvent.click(canvasElement.querySelector<HTMLButtonElement>('button[type=submit]')!); await waitFor(() => expect(canvasElement.querySelector('[role=alert]')?.textContent).toContain('Your text is still here')); expect(canvasElement.querySelector('textarea')!.value).toBe('A synthetic task'); expect(canvasElement.textContent).not.toContain('Task sent in a new chat.'); const { router } = await import('../router'); await waitFor(() => expect(router.currentRoute.value.params.chatId).toBe('storybook-chat')); expect(canvasElement.textContent).toContain('Violations: 0.') },
}
