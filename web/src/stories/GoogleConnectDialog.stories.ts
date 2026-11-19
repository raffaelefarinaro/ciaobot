import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import type { ConcreteComponent } from 'vue'
import GoogleConnectDialog from '../components/settings/GoogleConnectDialog.vue'
import type { GwsIntegrationSettings } from '../lib/types'
import type { FixtureRoute } from './fixtureBoundary'
import { resetHostTerminalForTests } from '../composables/useHostTerminal'
import { mountConnected } from './mountConnected'
import {
  GWS_AUTH_URL,
  GWS_PROFILE_ID,
  gwsIntegration,
  gwsProfile,
  googleConnectHost,
  type GwsSeed,
} from './gwsFixture'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** The two-step bootstrap fixture: register the profile, then read it back. */
const REGISTER_THEN_READ: GwsIntegrationSettings = gwsIntegration([
  gwsProfile({ configured: false, client_secret_present: false, credentials_present: false }),
], true)
/** The payload after a pasted code was accepted. */
const EXCHANGED: GwsIntegrationSettings = gwsIntegration([gwsProfile()], true)

/** The install step's HostTerminalButton reads the host terminal capability on mount; the fixture has no host terminal. */
const HOST_TERMINAL_ROUTE: FixtureRoute = {
  method: 'GET',
  path: '/api/host/terminal',
  respond: () => json({ available: false, reason: 'unavailable' }),
}

/** Declared reads: a bootstrap Check-again registers then reads the integration. */
function declaredRoutes(reads: GwsIntegrationSettings | null = null): FixtureRoute[] {
  const routes: FixtureRoute[] = [HOST_TERMINAL_ROUTE]
  if (reads) {
    routes.push({ method: 'GET', path: '/api/integrations/gws', respond: () => json(reads) })
    routes.push({ method: 'POST', path: '/api/integrations/gws/profiles/add', respond: () => json(reads) })
  }
  return routes
}

const CANCEL_ROUTES: FixtureRoute[] = [
  { method: 'POST', path: '/api/integrations/gws/relogin/cancel', respond: () => json({}) },
]
const EXCHANGE_ROUTES: FixtureRoute[] = [
  { method: 'POST', path: '/api/integrations/gws/exchange', respond: () => json(EXCHANGED) },
]

function renderConnect(
  mode: 'connect' | 'reauth',
  seed: GwsSeed = {},
  startOpen = true,
  narrow = false,
) {
  return defineComponent({
    setup() {
      // The host-terminal capability is cached per page; reset it so each
      // story's install step makes exactly one declared read.
      resetHostTerminalForTests()
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountConnected>>()
      const error = ref('')
      onMounted(() => {
        if (!host.value) return
        try {
          fixture.value = mountConnected(host.value!, {
            component: googleConnectHost(GoogleConnectDialog, mode, seed, startOpen),
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

const meta = {
  id: 'google-connect-dialog',
  title: 'Settings appearance and Google/GoogleConnectDialog',
  component: GoogleConnectDialog as unknown as ConcreteComponent<ConnectArgs>,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<ConnectArgs>
export default meta
type Story = StoryObj<typeof meta>

const ledger = () => screen.getByText(/Fixture requests: [0-9]+\. Violations: [0-9]+\./)
const quiet = async (requests: number) => {
  await waitFor(() => expect(ledger().textContent).toBe(`Fixture requests: ${requests}. Violations: 0.`))
  await new Promise(resolve => setTimeout(resolve, 500))
  expect(screen.queryByText(/Story network violation/)).toBeNull()
}
const connectDialog = () => screen.getByRole('dialog', { name: 'Connect Google account' })
const reauthDialog = () => screen.getByRole('dialog', { name: 'Sign in to Google again' })
const signinButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Sign in with Google' })

// The stories render the owner through the fixture host below; the production
// props are host state, not story args.
type ConnectArgs = { open: boolean; workspace: string; mode: 'connect' | 'reauth' }

export const Closed: Story = {
  parameters: { fixture: { routes: declaredRoutes() } },
  render: () => renderConnect('connect', { integration: gwsIntegration([], false) }, false),
  play: async () => {
    await waitFor(() => expect(ledger()).toBeTruthy())
    expect(screen.queryByRole('dialog')).toBeNull()
    await quiet(0)
  },
}

export const Default: Story = {
  parameters: { fixture: { routes: declaredRoutes() } },
  render: () => renderConnect('connect', { integration: gwsIntegration([], false) }),
  play: async ({ userEvent }) => {
    const sheet = await waitFor(() => connectDialog())
    // Without a profile the display name is an editable, focused field.
    const name = screen.getByLabelText<HTMLInputElement>('Display name')
    await waitFor(() => expect(document.activeElement).toBe(name))
    expect(name.matches(':focus-visible')).toBe(true)
    // Step 1: gws is not installed, so the install step shows with its hint.
    expect(sheet.textContent).toContain('Install the gws CLI')
    expect(sheet.textContent).toContain('@googleworkspace/cli')
    // The terminal install is host-only: HostTerminalButton renders nothing without a host terminal.
    expect(screen.queryByRole('button', { name: 'Install in Terminal' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Install in Chat' })).not.toBeNull()
    expect(screen.queryByRole('button', { name: 'Check again' })).not.toBeNull()
    // Step 2: the upload panel is the default OAuth client mode.
    const upload = screen.getByRole<HTMLInputElement>('radio', { name: 'Upload my own client_secret.json' })
    expect(upload.checked).toBe(true)
    expect(screen.queryByRole('button', { name: 'Upload client_secret.json' })).not.toBeNull()
    // Step 3 is blocked until an OAuth client exists, and Done explains why.
    expect((signinButton() as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('Upload an OAuth client first.')).toBeTruthy()
    expect(screen.getByText('Done is available once you are signed in and linked.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Done' }) as HTMLButtonElement).disabled).toBe(true)
    // No profile yet, so the storage hint derives from the workspace.
    expect(sheet.textContent).toContain('secrets/gws-storybook-alpha/')
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await quiet(1)
  },
}

export const InstallProblem: Story = {
  parameters: { fixture: { routes: declaredRoutes() } },
  render: () => renderConnect('connect', {
    integration: gwsIntegration([], false, 'Synthetic gws install problem'),
  }),
  play: async () => {
    const sheet = await waitFor(() => connectDialog())
    const alert = await waitFor(() => screen.getByRole('alert'))
    expect(alert.textContent).toBe('Synthetic gws install problem')
    expect(sheet.textContent).toContain('Install the gws CLI')
    await quiet(1)
  },
}

export const InstallDone: Story = {
  parameters: { fixture: { routes: declaredRoutes() } },
  render: () => renderConnect('connect', { integration: gwsIntegration([]) }),
  play: async () => {
    const sheet = await waitFor(() => connectDialog())
    // The install step is replaced by its checklist entry.
    expect(screen.getByText('gws CLI installed')).toBeTruthy()
    expect(sheet.textContent).not.toContain('Install the gws CLI')
    // The OAuth client step is still pending: the checklist has one entry.
    expect(screen.queryByText('OAuth client present')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Upload client_secret.json' })).not.toBeNull()
    expect((signinButton() as HTMLButtonElement).disabled).toBe(true)
    await quiet(0)
  },
}

export const ClientDoneReady: Story = {
  parameters: { fixture: { routes: declaredRoutes() } },
  render: () => renderConnect('connect', {
    integration: gwsIntegration([gwsProfile({ configured: false, credentials_present: false })]),
  }),
  play: async () => {
    const sheet = await waitFor(() => connectDialog())
    expect(screen.getByText('gws CLI installed')).toBeTruthy()
    expect(screen.getByText('OAuth client present')).toBeTruthy()
    expect(screen.getByLabelText('Replace file: client_secret.json')).toBeTruthy()
    // The stored display name replaces the input, and sign-in is offered.
    expect(screen.queryByLabelText('Display name')).toBeNull()
    expect((signinButton() as HTMLButtonElement).disabled).toBe(false)
    expect(screen.getByRole('button', { name: 'Paste a code instead' })).toBeTruthy()
    // The storage hint now comes from the profile, and Done is still blocked.
    expect(sheet.textContent).toContain(`secrets/gws-${GWS_PROFILE_ID}/`)
    expect((screen.getByRole('button', { name: 'Done' }) as HTMLButtonElement).disabled).toBe(true)
    await quiet(0)
  },
}

export const ClientGcloud: Story = {
  parameters: { fixture: { routes: declaredRoutes(REGISTER_THEN_READ) } },
  render: () => renderConnect('connect', { integration: gwsIntegration([], false) }),
  play: async ({ userEvent }) => {
    const sheet = await waitFor(() => connectDialog())
    await userEvent.click(screen.getByRole<HTMLInputElement>('radio', { name: 'Create one with gcloud' }))
    // The gcloud panel shows the derived setup command.
    expect(sheet.textContent).toContain(`ciao gws ${GWS_PROFILE_ID} auth setup`)
    // Two Check again buttons: the install step's comes first, the gcloud panel's last.
    const checkAgain = screen.getAllByRole('button', { name: 'Check again' })
    expect(checkAgain.length).toBe(2)
    // Check again registers the profile, then reads the integration.
    await userEvent.click(checkAgain[checkAgain.length - 1])
    await waitFor(() => expect(screen.getByText('Workspace Google account')).toBeTruthy())
    expect(screen.queryByLabelText('Display name')).toBeNull()
    await waitFor(() => expect(screen.getByText('gws CLI installed')).toBeTruthy())
    await quiet(3)
  },
}

export const Connected: Story = {
  parameters: { fixture: { routes: declaredRoutes() } },
  render: () => renderConnect('connect', {
    integration: gwsIntegration([gwsProfile()]),
    linked: true,
  }),
  play: async ({ userEvent }) => {
    const sheet = await waitFor(() => connectDialog())
    expect(sheet.textContent).toContain('Connected as account@example.test. Linked to storybook-alpha')
    // A connected account does not re-offer the sign-in buttons.
    expect(screen.queryByRole('button', { name: 'Paste a code instead' })).toBeNull()
    expect((screen.getByRole('button', { name: 'Done' }) as HTMLButtonElement).disabled).toBe(false)
    expect(screen.queryByText('Done is available once you are signed in and linked.')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await quiet(0)
  },
}

export const ExpiredReauth: Story = {
  parameters: { fixture: { routes: declaredRoutes() } },
  render: () => renderConnect('reauth', {
    integration: gwsIntegration([gwsProfile({ needs_relogin: true, token_error: 'Synthetic token revoked' })]),
  }),
  play: async () => {
    const sheet = await waitFor(() => reauthDialog())
    expect(sheet.textContent).toContain('This login expired or was revoked (Synthetic token revoked).')
    // Re-authentication offers the same sign-in buttons and a blocked Done.
    expect((signinButton() as HTMLButtonElement).disabled).toBe(false)
    expect(screen.getByRole('button', { name: 'Paste a code instead' })).toBeTruthy()
    expect(screen.getByText('Done is available once you are signed in and linked.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Done' }) as HTMLButtonElement).disabled).toBe(true)
    await quiet(0)
  },
}

export const ReloginPending: Story = {
  parameters: { fixture: { routes: CANCEL_ROUTES } },
  render: () => renderConnect('connect', {
    integration: gwsIntegration([gwsProfile({ configured: false })]),
    reloginPending: true,
  }),
  play: async ({ userEvent }) => {
    await waitFor(() => connectDialog())
    await waitFor(() => expect(screen.getByText('Waiting for the browser')).toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Sign in with Google' })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Stop sign-in' }))
    // The cancel write ends the wait and restores the sign-in buttons.
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Sign in with Google' })).not.toBeNull())
    await quiet(1)
  },
}

export const ReloginPendingBlocked: Story = {
  parameters: { fixture: { routes: CANCEL_ROUTES } },
  render: () => renderConnect('connect', {
    integration: gwsIntegration([gwsProfile({ configured: false })]),
    reloginPending: true,
    authUrls: { [GWS_PROFILE_ID]: GWS_AUTH_URL },
  }),
  play: async () => {
    await waitFor(() => connectDialog())
    await waitFor(() => expect(screen.getByText('Waiting for the browser')).toBeTruthy())
    // A blocked tab surfaces the authorization page as a link instead.
    const link = screen.getByRole<HTMLAnchorElement>('link', { name: 'Open authorization page' })
    expect(link.getAttribute('href')).toBe(GWS_AUTH_URL)
    await quiet(0)
  },
}

export const PasteCodeFlow: Story = {
  parameters: { fixture: { routes: EXCHANGE_ROUTES } },
  render: () => renderConnect('connect', {
    integration: gwsIntegration([gwsProfile({ configured: false })]),
    linked: true,
    authUrls: { [GWS_PROFILE_ID]: GWS_AUTH_URL },
    redirectUrls: { [GWS_PROFILE_ID]: '' },
  }),
  play: async ({ userEvent }) => {
    const sheet = await waitFor(() => connectDialog())
    expect(sheet.textContent).toContain('copy the full redirect URL')
    const input = screen.getByLabelText<HTMLInputElement>('Redirect URL or authorization code')
    expect((screen.getByRole('button', { name: 'Complete Sign-In' }) as HTMLButtonElement).disabled).toBe(true)
    await userEvent.type(input, 'https://oauth.example.test/redirect?code=storybook')
    expect((screen.getByRole('button', { name: 'Complete Sign-In' }) as HTMLButtonElement).disabled).toBe(false)
    await userEvent.click(screen.getByRole('button', { name: 'Complete Sign-In' }))
    // The exchange answers a configured profile: the flow box closes and the
    // connected line with the workspace link replaces it.
    await waitFor(() => expect(sheet.textContent).toContain('Connected as account@example.test. Linked to storybook-alpha'))
    expect(screen.queryByLabelText('Redirect URL or authorization code')).toBeNull()
    await quiet(1)
  },
}

export const Narrow: Story = {
  parameters: { fixture: { routes: declaredRoutes() } },
  render: () => renderConnect('reauth', {
    integration: gwsIntegration([gwsProfile({ needs_relogin: true })]),
  }, true, true),
  play: async () => {
    await waitFor(() => reauthDialog())
    expect((screen.getByText(/Fixture requests:/).parentElement as HTMLElement).style.maxWidth).toBe('390px')
    await quiet(0)
  },
}
