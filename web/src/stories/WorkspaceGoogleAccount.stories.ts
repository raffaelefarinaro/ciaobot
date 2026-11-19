import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor, within } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import WorkspaceGoogleAccount from '../components/settings/WorkspaceGoogleAccount.vue'
import type { FixtureRoute } from './fixtureBoundary'
import { mountConnected } from './mountConnected'
import { useProjectStore } from '../stores/projects'
import {
  GWS_WORKSPACE,
  gwsHost,
  gwsIntegration,
  gwsProfile,
  type GwsSeed,
} from './gwsFixture'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** The integration read the Retry button makes; the failure variant answers 500. */
function gwsReadRoutes(mode: 'ok' | 'error'): FixtureRoute[] {
  return [{
    method: 'GET',
    path: '/api/integrations/gws',
    respond: () => mode === 'ok'
      ? json(gwsIntegration([]), 200)
      : json({ error: 'Synthetic integration read refused' }, 500),
  }]
}

/** The only write this slice exercises: the held account removal that shows the busy menu. */
const REMOVE_HELD: FixtureRoute[] = [{
  method: 'POST',
  path: '/api/integrations/gws/profiles/remove',
  respond: () => new Promise<never>(() => {}),
}]

function renderAccount(seed: GwsSeed, extras: Parameters<typeof gwsHost>[2] = [], narrow = false) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountConnected>>()
      const error = ref('')
      onMounted(() => {
        if (!host.value) return
        try {
          fixture.value = mountConnected(host.value!, {
            component: gwsHost(WorkspaceGoogleAccount, seed, extras),
            seed: pinia => { useProjectStore(pinia).activeWorkspace = GWS_WORKSPACE },
          })
          fixture.value.app.config.errorHandler = (err) => { error.value = String(err) }
        } catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', narrow ? { style: 'max-width: 390px' } : {}, [
        h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
        h('div', { ref: host }),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map(message => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

const menuButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Actions for Google account' })

const meta = {
  id: 'workspace-google-account',
  title: 'Settings appearance and Google/WorkspaceGoogleAccount',
  component: WorkspaceGoogleAccount,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof WorkspaceGoogleAccount>
export default meta
type Story = StoryObj<typeof meta>

const ledger = () => screen.getByText(/Fixture requests: [0-9]+\. Violations: [0-9]+\./)
const quiet = async (requests: number) => {
  await waitFor(() => expect(ledger().textContent).toBe(`Fixture requests: ${requests}. Violations: 0.`))
  await new Promise(resolve => setTimeout(resolve, 500))
  expect(screen.queryByText(/Story network violation/)).toBeNull()
}

export const Loading: Story = {
  parameters: { fixture: { routes: [] } },
  render: () => renderAccount({ integration: null, loaded: false }),
  play: async () => {
    // The generic first-load placeholder; the parent owns the integration read.
    const status = await waitFor(() => screen.getByRole('status', { name: 'Loading Google account' }))
    expect(status).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Connect Google account' })).toBeNull()
    await quiet(0)
  },
}

export const LoadError: Story = {
  parameters: { fixture: { routes: gwsReadRoutes('ok') } },
  render: () => renderAccount({
    integration: null,
    loaded: true,
    loadError: 'Failed to load Google Workspace integration: Synthetic failure',
  }),
  play: async ({ userEvent }) => {
    const initial = await waitFor(() => screen.getByText('Failed to load Google Workspace integration: Synthetic failure'))
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
    // Retry answers a fresh read: the row recovers to the no-account branch.
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Connect Google account' })).toBeTruthy())
    expect(initial).toBeTruthy()
    await quiet(1)
  },
}

export const RefreshFailure: Story = {
  parameters: { fixture: { routes: gwsReadRoutes('error') } },
  render: () => renderAccount({
    integration: null,
    loaded: true,
    loadError: 'Failed to load Google Workspace integration: Synthetic failure',
  }),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByText(
      'Failed to load Google Workspace integration: Synthetic integration read refused',
    )).toBeTruthy())
    await quiet(1)
  },
}

export const None: Story = {
  parameters: { fixture: { routes: [] } },
  render: () => renderAccount({ integration: gwsIntegration([]) }),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText(
      'No Google account is connected to this workspace. Chats and schedules here run without Google access.',
    )).toBeTruthy())
    const connect = screen.getByRole('button', { name: 'Connect Google account' })
    await userEvent.click(connect)
    const sheet = await waitFor(() => screen.getByRole('dialog', { name: 'Connect Google account' }))
    // Closing the dialog from its footer hands focus back to the row's opener.
    // The header's icon Close comes first; the footer's text Close is last.
    const closes = within(sheet).getAllByRole('button', { name: 'Close' })
    await userEvent.click(closes[closes.length - 1])
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(connect)
    await quiet(0)
  },
}

export const Connected: Story = {
  parameters: { fixture: { routes: [] } },
  render: () => renderAccount({
    integration: gwsIntegration([gwsProfile({ workspaces: ['storybook-alpha', 'storybook-beta'] })]),
    linked: true,
  }),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText('Connected')).toBeTruthy())
    expect(screen.getByText('Workspace Google account')).toBeTruthy()
    expect(screen.getByText('account@example.test')).toBeTruthy()
    const trigger = menuButton()
    expect((trigger as HTMLButtonElement).disabled).toBe(false)
    await userEvent.click(trigger)
    await waitFor(() => expect(screen.getByRole('menuitem', { name: 'Re-authenticate' })).toBeTruthy())
    expect(screen.getByRole('menuitem', { name: 'Advanced / headless setup' })).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: 'Stop using in this workspace' })).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: 'Disconnect and remove' })).toBeTruthy()
    // Escape is the keyboard close path for the row's menu.
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('menuitem', { name: 'Re-authenticate' })).toBeNull())
    await quiet(0)
  },
}

export const Expired: Story = {
  parameters: { fixture: { routes: [] } },
  render: () => renderAccount({
    integration: gwsIntegration([gwsProfile({
      needs_relogin: true,
      token_error: 'Synthetic token revoked',
    })]),
    linked: true,
  }),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText('Sign-in expired')).toBeTruthy())
    expect(screen.getByText('Synthetic token revoked')).toBeTruthy()
    const reconnect = screen.getByRole('button', { name: 'Sign in again' })
    expect((reconnect as HTMLButtonElement).disabled).toBe(false)
    // The reauth dialog opens over the row; its footer Close returns focus.
    await userEvent.click(reconnect)
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Sign in to Google again' })).toBeTruthy())
    const sheet = await waitFor(() => screen.getByRole('dialog', { name: 'Sign in to Google again' }))
    // The header's icon Close comes first; the footer's text Close is last.
    const closes = within(sheet).getAllByRole('button', { name: 'Close' })
    await userEvent.click(closes[closes.length - 1])
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(reconnect)
    await quiet(0)
  },
}

export const AdvancedPanel: Story = {
  parameters: { fixture: { routes: [] } },
  render: () => renderAccount({
    integration: gwsIntegration([gwsProfile({ client_secret_present: false })]),
    linked: true,
  }),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(menuButton()).toBeTruthy())
    await userEvent.click(menuButton())
    await userEvent.click(screen.getByRole('menuitem', { name: 'Advanced / headless setup' }))
    const panel = await waitFor(() => screen.getByText('Create client').parentElement as HTMLElement)
    await waitFor(() => expect(panel.textContent).toContain(`ciao gws storybook-alpha auth setup`))
    expect(screen.getByText('Login').parentElement!.textContent).toContain('auth login --setup')
    expect(screen.getByText('Headless').parentElement!.textContent).toContain('auth login --headless')
    expect(screen.getByText('Stored in').parentElement!.textContent).toContain('secrets/gws-storybook-alpha/')
    await userEvent.click(screen.getByRole('button', { name: 'Hide' }))
    await waitFor(() => expect(screen.queryByText('Create client')).toBeNull())
    await quiet(0)
  },
}

export const AdvancedPanelSecret: Story = {
  parameters: { fixture: { routes: [] } },
  render: () => renderAccount({
    integration: gwsIntegration([gwsProfile({ client_secret_present: true })]),
    linked: true,
  }),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(menuButton()).toBeTruthy())
    await userEvent.click(menuButton())
    await userEvent.click(screen.getByRole('menuitem', { name: 'Advanced / headless setup' }))
    await waitFor(() => expect(screen.getByText('Login')).toBeTruthy())
    // An existing client hides the Create client block but keeps the commands.
    expect(screen.queryByText('Create client')).toBeNull()
    expect(screen.getByText('Headless')).toBeTruthy()
    await quiet(0)
  },
}

export const StopUsingConfirm: Story = {
  parameters: { fixture: { routes: [] } },
  render: () => renderAccount({
    integration: gwsIntegration([gwsProfile({ workspaces: ['storybook-alpha', 'storybook-beta'] })]),
    linked: true,
  }, [ConfirmDialog]),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(menuButton()).toBeTruthy())
    await userEvent.click(menuButton())
    await userEvent.click(screen.getByRole('menuitem', { name: 'Stop using in this workspace' }))
    const confirm = await waitFor(() => screen.getByRole('dialog', { name: 'Stop using this account?' }))
    expect(confirm.textContent).toContain('Stop using Workspace Google account in storybook-alpha?')
    expect(confirm.textContent).toContain('It stays connected for storybook-beta.')
    // Cancelling does not write: the workspace keeps its link.
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Stop using this account?' })).toBeNull())
    await quiet(0)
  },
}

export const Busy: Story = {
  parameters: { fixture: { routes: REMOVE_HELD } },
  render: () => renderAccount({
    integration: gwsIntegration([gwsProfile({ workspaces: ['storybook-alpha', 'storybook-beta'] })]),
    linked: true,
  }, [ConfirmDialog]),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(menuButton()).toBeTruthy())
    await userEvent.click(menuButton())
    await userEvent.click(screen.getByRole('menuitem', { name: 'Disconnect and remove' }))
    const confirm = await waitFor(() => screen.getByRole('dialog', { name: 'Remove Google account?' }))
    expect(confirm.textContent).toContain('Removing it deletes its stored credentials')
    await userEvent.click(screen.getByRole('button', { name: 'Remove account' }))
    await waitFor(() => expect(ledger().textContent).toBe('Fixture requests: 1. Violations: 0.'))
    // The held removal is the owner's busy state: the actions menu is blocked.
    expect((menuButton() as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByRole('dialog', { name: 'Remove Google account?' })).toBeNull()
  },
}

export const Narrow: Story = {
  parameters: { fixture: { routes: [] } },
  render: () => renderAccount({
    integration: gwsIntegration([gwsProfile({ workspaces: ['storybook-alpha', 'storybook-beta'] })]),
    linked: true,
  }, [], true),
  play: async () => {
    await waitFor(() => expect(screen.getByText('Connected')).toBeTruthy())
    const rowHead = screen.getByText('Connected').closest<HTMLElement>('.set-row-head')
    expect(rowHead).toBeTruthy()
    // Below ~300px of action width the actions wrap onto their own line.
    expect(getComputedStyle(rowHead as HTMLElement).flexWrap).toBe('wrap')
    await quiet(0)
  },
}
