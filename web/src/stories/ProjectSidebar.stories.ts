import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import { defineComponent, h } from 'vue'
import ProjectSidebar from '../components/ProjectSidebar.vue'
import { useProjectStore } from '../stores/projects'
import { renderSeeded } from './homeIntakeFixture'
import { json } from './homeWindowsFixture'

const routes = [
  { method: 'GET' as const, path: '/api/housekeeping', respond: () => json({ actions: [] }) },
  { method: 'GET' as const, path: '/api/workspace-health', respond: () => json({}) },
  { method: 'GET' as const, path: '/api/update-tasks?workspace=storybook-alpha', respond: () => json({ tasks: [] }) },
  { method: 'GET' as const, path: '/api/tasks?workspace=storybook-alpha', respond: () => json({ tasks: [] }) },
]
function fixture(collapsed: boolean) { return defineComponent({ render: () => h(ProjectSidebar, { collapsed }) }) }
const meta = { id: 'connected-project-sidebar', title: 'Connected/Project sidebar', component: ProjectSidebar, tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true }, fixture: { routes } } } satisfies Meta<typeof ProjectSidebar>
export default meta
type Story = StoryObj<typeof meta>
export const Expanded: Story = { render: () => renderSeeded(fixture(false), { label: 'Expanded sidebar' }), play: async ({ canvas }) => { expect(await canvas.findByRole('button', { name: 'Collapse sidebar' })).toBeTruthy(); expect(canvas.getByText('General')).toBeTruthy() } }
export const Collapsed: Story = { render: () => renderSeeded(fixture(true), { label: 'Collapsed sidebar' }), play: async ({ canvas }) => { expect(await canvas.findByRole('button', { name: 'Open sidebar' })).toBeTruthy() } }
export const Loading: Story = { render: () => renderSeeded(fixture(false), { label: 'Initial loading', projects: false }), play: async ({ canvasElement }) => { await waitFor(() => expect(canvasElement.querySelector('.chats-scroll-area .sidebar-skeleton')).toBeTruthy()) } }
export const Empty: Story = { render: () => renderSeeded(fixture(false), { label: 'Empty workspace', projects: false, seed: pinia => { useProjectStore(pinia).bootstrapped = true } }), play: async ({ canvas }) => { expect(await canvas.findByRole('button', { name: 'Collapse sidebar' })).toBeTruthy() } }
export const WorkspaceMenu: Story = { render: () => renderSeeded(fixture(false), { label: 'Workspace menu' }), play: async ({ canvas }) => { const trigger = await canvas.findByRole('button', { name: /workspace/i }); await userEvent.click(trigger); expect(trigger.getAttribute('aria-expanded')).toBe('true') } }
