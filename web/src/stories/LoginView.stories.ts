import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import LoginView from '../components/LoginView.vue'
import {
  LOGIN_PASSWORD,
  LOGIN_SETUP_DONE_PATH,
  LOGIN_WORKSPACE,
  bootstrapStatus,
  held,
  inspectAnswer,
  inspectRoute,
  json,
  listDirsRoute,
  listing,
  providersSetupNeeded,
  renderLoginState,
  route,
  settled,
  setupStatus,
  statusRoute,
  wizardRoutes,
} from './loginFixture'

/**
 * LoginView (the `components-login-view` inventory row, #3310): the route
 * composition behind `/login` — the standard password form, the first-run
 * setup wizard and its server-backed folder picker, and the restarting
 * screen. It mounts with a fresh Pinia through #3267's `mountConnected` and
 * probes `GET /api/setup-status` on mount, so every story declares exactly the
 * routes its flow performs and anything else fails closed. The login and
 * setup writes a story exercises are held or failed on purpose: a successful
 * credential write would navigate the preview away, so LoginView never posts
 * credentials for real here. Synthetic names and text only.
 */

const meta = {
  id: 'login-view',
  title: 'Startup, offline and login/LoginView',
  component: LoginView,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof LoginView>
export default meta
type Story = StoryObj<typeof meta>

const shellTitle = (canvasElement: HTMLElement) => canvasElement.querySelector('.login-shell-title')
const tokenInput = (canvasElement: HTMLElement) => canvasElement.querySelector('#login-token') as HTMLInputElement
const loginSubmit = (canvasElement: HTMLElement) => canvasElement.querySelector('.login-submit') as HTMLButtonElement
const setupPassword = (canvasElement: HTMLElement) => canvasElement.querySelector('#setup-password') as HTMLInputElement
const setupPasswordConfirm = (canvasElement: HTMLElement) => canvasElement.querySelector('#setup-password-confirm') as HTMLInputElement
const setupWorkspace = (canvasElement: HTMLElement) => canvasElement.querySelector('#setup-workspace') as HTMLInputElement
const finishButton = (canvasElement: HTMLElement) =>
  [...canvasElement.querySelectorAll('button')].find((button) => button.classList.contains('prompt-submit')) as HTMLButtonElement

async function wizardReady(canvasElement: HTMLElement): Promise<void> {
  await waitFor(() => expect(canvasElement.querySelector('.setup-wizard')).toBeTruthy())
  await settled(canvasElement)
}

/** Fills and confirms the synthetic password the wizard asks for. */
async function fillPasswordFields(canvasElement: HTMLElement, eve: { clear(el: Element): Promise<void>; type(el: Element, text: string): Promise<void> }): Promise<void> {
  await eve.clear(setupPassword(canvasElement))
  await eve.type(setupPassword(canvasElement), LOGIN_PASSWORD)
  await eve.clear(setupPasswordConfirm(canvasElement))
  await eve.type(setupPasswordConfirm(canvasElement), LOGIN_PASSWORD)
}

export const Login: Story = {
  // A healthy non-bootstrap machine: the standard one-field password form.
  parameters: { fixture: { routes: [statusRoute(() => json(setupStatus()))] } },
  render: () => renderLoginState({ label: 'standard login' }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    expect(canvasElement.textContent).toContain('Enter your password')
    expect(shellTitle(canvasElement)?.textContent).toContain('ciaobot@local · log in')
    expect(tokenInput(canvasElement).value).toBe('')
    // The empty field blocks the submit: the control, not just its styling.
    expect(loginSubmit(canvasElement).disabled).toBe(true)
    expect(canvasElement.textContent).toContain('Forgot your password?')
    // The workshop runs on a loopback hostname, so the PWA host block renders.
    const signin = canvasElement.querySelector('.login-host-signin') as HTMLButtonElement
    expect(signin).toBeTruthy()
    expect(signin.textContent?.trim()).toBe('Sign in on this computer')
  },
}

export const LoginError: Story = {
  // The only write a login story ever succeeds at performing is its failure.
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(setupStatus())),
        route('POST', '/api/auth', () => json({ error: 'invalid token' }, 401)),
      ],
    },
  },
  render: () => renderLoginState({ label: 'failed login attempt' }),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.type(tokenInput(canvasElement), LOGIN_PASSWORD)
    expect(loginSubmit(canvasElement).disabled).toBe(false)
    await userEvent.click(loginSubmit(canvasElement))
    await waitFor(() => expect(canvasElement.querySelector('#login-error')).toBeTruthy())
    expect(canvasElement.querySelector('.login-error')?.textContent).toBe('That password did not work. Check it and try again.')
    expect(tokenInput(canvasElement).getAttribute('aria-invalid')).toBe('true')
    expect(tokenInput(canvasElement).getAttribute('aria-describedby')).toBe('login-error')
    // The attempt is over: the form is usable again, on the same branch.
    expect(tokenInput(canvasElement).disabled).toBe(false)
    expect(loginSubmit(canvasElement).disabled).toBe(false)
  },
}

export const LoginSubmitting: Story = {
  // The write is held, so the busy form is observable: both controls blocked.
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(setupStatus())),
        route('POST', '/api/auth', () => held()),
      ],
    },
  },
  render: () => renderLoginState({ label: 'held login write' }),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.type(tokenInput(canvasElement), LOGIN_PASSWORD)
    await userEvent.click(loginSubmit(canvasElement))
    await waitFor(() => expect(loginSubmit(canvasElement).textContent?.trim()).toBe('Logging in…'))
    expect(loginSubmit(canvasElement).disabled).toBe(true)
    expect(tokenInput(canvasElement).disabled).toBe(true)
  },
}

export const SetupProbeFailed: Story = {
  // Loading contract, initial failure: the probe answer is caught and the
  // page keeps the standard login form (identical render, failed request).
  parameters: { fixture: { routes: [statusRoute(() => json({ error: 'probe failed' }, 500))] } },
  render: () => renderLoginState({ label: 'failed setup probe' }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    expect(canvasElement.textContent).toContain('Enter your password')
    expect(loginSubmit(canvasElement).disabled).toBe(true)
    expect(canvasElement.textContent).toMatch(/Fixture requests: [1-9]/)
    expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  },
}

export const HostSignIn: Story = {
  // The busy host sign-in: the write is held, so the aria-disabled control
  // and its waiting status are observable. Its success path (an auth poll and
  // a navigation) must never happen in the workshop.
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(setupStatus())),
        route('POST', '/api/auth/host-sign-in', () => held()),
      ],
    },
  },
  render: () => renderLoginState({ label: 'held host sign-in' }),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    const signin = canvasElement.querySelector('.login-host-signin') as HTMLButtonElement
    expect(signin.getAttribute('aria-disabled')).toBeNull()
    await userEvent.click(signin)
    await waitFor(() => expect(signin.getAttribute('aria-disabled')).toBe('true'))
    expect(signin.textContent?.trim()).toBe('Opening…')
    expect(canvasElement.querySelector('.login-host-status')?.textContent).toBe('')
  },
}

export const HostSignInFallback: Story = {
  // No desktop opener here: the write answers with its reason, and the status
  // line shows the terminal command the user runs instead.
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(setupStatus())),
        route('POST', '/api/auth/host-sign-in', () => json({ error: 'no desktop', reason: 'no_desktop' }, 500)),
      ],
    },
  },
  render: () => renderLoginState({ label: 'host sign-in without a desktop opener' }),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    const signin = canvasElement.querySelector('.login-host-signin') as HTMLButtonElement
    await userEvent.click(signin)
    await waitFor(() => expect(canvasElement.querySelector('.login-host-command')).toBeTruthy())
    expect(canvasElement.querySelector('.login-host-command')?.textContent).toBe('ciao setup-url --workspace ~/Ciaobot')
    expect(canvasElement.querySelector('.login-host-status')?.textContent).toContain('Ciaobot could not open a browser here.')
    // The attempt is over: the control is usable again.
    expect(signin.getAttribute('aria-disabled')).toBeNull()
    expect(signin.textContent?.trim()).toBe('Sign in on this computer')
  },
}

export const Narrow: Story = {
  // The 390px viewport is the story viewport for a full-page mount.
  parameters: {
    viewport: { defaultViewport: 'narrow' },
    fixture: { routes: [statusRoute(() => json(setupStatus()))] },
  },
  render: () => renderLoginState({ label: 'standard login in a 390px viewport' }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    const shell = canvasElement.querySelector('.login-shell') as HTMLElement
    const width = shell.getBoundingClientRect().width
    expect(width).toBeGreaterThan(0)
    expect(width).toBeLessThanOrEqual(window.innerWidth)
    expect(canvasElement.textContent).toContain('Enter your password')
    expect(loginSubmit(canvasElement)).toBeTruthy()
  },
}

export const BootstrapSetup: Story = {
  // The non-native first-run wizard: run note, tour, workspace field with its
  // probe, the ok provider card, and a blocked Finish until the form is done.
  parameters: {
    fixture: {
      routes: wizardRoutes(LOGIN_WORKSPACE, () => inspectAnswer({ macos_access_note: 'macOS may ask to confirm access to this folder before Ciaobot reads it.' })),
    },
  },
  render: () => renderLoginState({ label: 'first-run setup wizard' }),
  play: async ({ canvasElement }) => {
    await wizardReady(canvasElement)
    expect(shellTitle(canvasElement)?.textContent).toContain('ciaobot@local · first-run setup')
    expect(canvasElement.querySelector('.setup-run-note')).toBeTruthy()
    expect(canvasElement.querySelector('.setup-tour')).toBeTruthy()
    expect(setupWorkspace(canvasElement).value).toBe(LOGIN_WORKSPACE)
    expect(canvasElement.querySelectorAll('.workspace-chip')).toHaveLength(0)
    expect((canvasElement.querySelector('#setup-workspace-name') as HTMLInputElement).value).toBe('personal')
    // The mount probe's note renders under the field.
    expect(canvasElement.querySelector('#setup-workspace-macos-access-note')?.textContent).toContain('confirm access to this folder')
    const badge = canvasElement.querySelector('.provider-status-card .badge')
    expect(badge?.className).toContain('badge--success')
    expect(badge?.textContent?.trim()).toBe('[ok] Ready')
    expect((canvasElement.querySelector('input[type="radio"][value="claude"]') as HTMLInputElement).checked).toBe(true)
    // The form is incomplete: the submit is actually blocked.
    expect(finishButton(canvasElement).disabled).toBe(true)
  },
}

export const BootstrapNative: Story = {
  // native_bootstrap: no shell bar, run note or tour, and the probe answers
  // about the installer's suggested folder.
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(bootstrapStatus({ native_bootstrap: true, suggested_workspace: '~/Ciaobot' }))),
        inspectRoute('~/Ciaobot', () => inspectAnswer()),
      ],
    },
  },
  render: () => renderLoginState({ label: 'native first-run setup' }),
  play: async ({ canvasElement }) => {
    await wizardReady(canvasElement)
    const shell = canvasElement.querySelector('.login-shell')
    expect(shell?.className).toContain('native-bootstrap')
    expect(shellTitle(canvasElement)).toBeNull()
    expect(canvasElement.querySelector('.setup-tour')).toBeNull()
    expect(canvasElement.querySelector('.setup-run-note')).toBeNull()
    expect(setupWorkspace(canvasElement).value).toBe('~/Ciaobot')
    expect(canvasElement.querySelector('#setup-workspace-name')).toBeTruthy()
    expect(finishButton(canvasElement).disabled).toBe(true)
  },
}

export const BootstrapDetectedWorkspaces: Story = {
  // The probe found existing workspaces: chips replace the name field, and
  // the fixture's cloud warning renders its warn note.
  parameters: {
    fixture: {
      routes: wizardRoutes(LOGIN_WORKSPACE, () => inspectAnswer({
        existing_workspaces: ['storybook-personal', 'story-work'],
        cloud_sync_warning: 'Live-syncing this folder with a cloud drive is not supported.',
      })),
    },
  },
  render: () => renderLoginState({ label: 'setup with detected workspaces' }),
  play: async ({ canvasElement }) => {
    await wizardReady(canvasElement)
    const chips = [...canvasElement.querySelectorAll('.workspace-chip')]
    expect(chips.some((chip) => chip.textContent === 'storybook-personal')).toBe(true)
    expect(chips.some((chip) => chip.textContent === 'story-work')).toBe(true)
    expect(canvasElement.textContent).toContain('Found 2 workspaces')
    expect(canvasElement.querySelector('#setup-workspace-name')).toBeNull()
    expect(canvasElement.textContent).toContain('Live-syncing this folder with a cloud drive is not supported.')
  },
}

export const BootstrapFolderInspecting: Story = {
  // The probe is held: the checking hint stands in for both the chips and the
  // name field, with no skeleton. See the row's loading detail.
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(bootstrapStatus())),
        inspectRoute(LOGIN_WORKSPACE, () => held()),
      ],
    },
  },
  render: () => renderLoginState({ label: 'held folder inspection' }),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.textContent).toContain('Checking the folder for existing workspaces…'))
    expect(canvasElement.querySelectorAll('.workspace-chip')).toHaveLength(0)
    expect(canvasElement.querySelector('#setup-workspace-name')).toBeNull()
    await settled(canvasElement)
  },
}

export const BootstrapProviderSetup: Story = {
  // A provider not installed: error badge, the command box with Copy (the
  // stubbed clipboard) and the PATH step, install link, auto-refresh hint and
  // the app_path wording. The radio swaps the card to the other provider.
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(bootstrapStatus({ provider_ready: false, providers: providersSetupNeeded() }))),
        inspectRoute(LOGIN_WORKSPACE, () => inspectAnswer()),
      ],
    },
  },
  render: () => renderLoginState({ label: 'provider setup card', clipboard: true }),
  play: async ({ canvasElement, userEvent }) => {
    await wizardReady(canvasElement)
    const badge = canvasElement.querySelector('.provider-status-card .badge')
    expect(badge?.className).toContain('badge--error')
    expect(badge?.textContent?.trim()).toBe('[!] Not Installed')
    expect(canvasElement.textContent).toContain('The desktop app is installed, but Ciaobot drives the CLI. Install it in your Terminal:')
    expect(canvasElement.textContent).toContain('provider-cli login')
    expect(canvasElement.textContent).toContain('Then make it findable in new terminals:')
    const link = canvasElement.querySelector('.install-link') as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe('https://install.example.test/provider')
    expect(canvasElement.textContent).toContain('This check refreshes on its own')
    const firstCopy = canvasElement.querySelectorAll('.command-row button')[0] as HTMLButtonElement
    await userEvent.click(firstCopy)
    await waitFor(() => expect(firstCopy.textContent?.trim()).toBe('Copied!'))
    // The opencode radio swaps the card's row to that provider's answer.
    const opencode = canvasElement.querySelector('input[type="radio"][value="opencode"]') as HTMLInputElement
    await userEvent.click(opencode)
    await waitFor(() => expect(canvasElement.textContent).toContain('opencode login'))
    expect(canvasElement.textContent).not.toContain('Then make it findable in new terminals:')
    // The provider is not ready and no key fallback is chosen: busy blocked.
    expect(finishButton(canvasElement).disabled).toBe(true)
  },
}

export const BootstrapAdvanced: Story = {
  // The Advanced disclosure: Port, Python Path and the .env fallback checkbox.
  parameters: {
    fixture: {
      routes: wizardRoutes(LOGIN_WORKSPACE, () => inspectAnswer()),
    },
  },
  render: () => renderLoginState({ label: 'setup advanced section' }),
  play: async ({ canvasElement, userEvent }) => {
    await wizardReady(canvasElement)
    const toggle = canvasElement.querySelector('#setup-advanced-toggle') as HTMLButtonElement
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(canvasElement.querySelector('.advanced-options')).toBeNull()
    await userEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect((canvasElement.querySelector('#setup-port') as HTMLInputElement).value).toBe('8443')
    expect((canvasElement.querySelector('#setup-python') as HTMLInputElement).value).toBe('')
    const fallback = canvasElement.querySelector('#setup-api-fallback') as HTMLInputElement
    expect(fallback.checked).toBe(false)
    await userEvent.click(fallback)
    expect(fallback.checked).toBe(true)
    // Still incomplete (no password): the submit stays blocked.
    expect(finishButton(canvasElement).disabled).toBe(true)
    await userEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(canvasElement.querySelector('.advanced-options')).toBeNull()
  },
}

export const BootstrapPasswordHint: Story = {
  // passwordProblem: too short, then mismatched, then matching unblocks.
  parameters: {
    fixture: {
      routes: wizardRoutes(LOGIN_WORKSPACE, () => inspectAnswer()),
    },
  },
  render: () => renderLoginState({ label: 'password validation hints' }),
  play: async ({ canvasElement, userEvent }) => {
    await wizardReady(canvasElement)
    await userEvent.type(setupPassword(canvasElement), 'ab')
    await waitFor(() => expect(canvasElement.textContent).toContain('Use at least 4 characters.'))
    await userEvent.type(setupPasswordConfirm(canvasElement), 'abcde')
    expect(canvasElement.textContent).toContain('Use at least 4 characters.')
    await userEvent.clear(setupPassword(canvasElement))
    await userEvent.type(setupPassword(canvasElement), LOGIN_PASSWORD)
    await waitFor(() => expect(canvasElement.textContent).toContain('The two passwords do not match.'))
    expect(canvasElement.textContent).not.toContain('Use at least 4 characters.')
    expect(finishButton(canvasElement).disabled).toBe(true)
    await userEvent.clear(setupPasswordConfirm(canvasElement))
    await userEvent.type(setupPasswordConfirm(canvasElement), LOGIN_PASSWORD)
    await waitFor(() => expect(canvasElement.textContent).not.toContain('The two passwords do not match.'))
    expect(finishButton(canvasElement).disabled).toBe(false)
    expect(finishButton(canvasElement).textContent?.trim()).toBe('Finish Setup')
  },
}

export const WorkspacePicker: Story = {
  // Browse… opens the server-backed picker, steps into a folder, and
  // "Use this folder" writes the field and re-probes the chosen folder.
  parameters: {
    fixture: {
      routes: [
        ...wizardRoutes(LOGIN_WORKSPACE, () => inspectAnswer()),
        listDirsRoute(LOGIN_WORKSPACE, () => json(listing(LOGIN_WORKSPACE, [{ name: 'storybook-alpha', path: '~/storybook-alpha' }]))),
        listDirsRoute('~/storybook-alpha', () => json(listing('~/storybook-alpha'))),
        inspectRoute('~/storybook-alpha', () => inspectAnswer()),
      ],
    },
  },
  render: () => renderLoginState({ label: 'workspace folder picker' }),
  play: async ({ canvasElement, userEvent }) => {
    await wizardReady(canvasElement)
    await userEvent.click(canvasElement.querySelector('#setup-workspace-browse') as HTMLButtonElement)
    await waitFor(() => expect(canvasElement.querySelector('.picker-modal')).toBeTruthy())
    expect((canvasElement.querySelector('.picker-path') as HTMLElement).textContent).toBe(LOGIN_WORKSPACE)
    const up = [...canvasElement.querySelectorAll('button')].find((button) => button.textContent === '↑ Up') as HTMLButtonElement
    expect(up.disabled).toBe(true)
    await waitFor(() => expect(canvasElement.querySelector('.picker-dir')).toBeTruthy())
    await userEvent.click(canvasElement.querySelector('.picker-dir') as HTMLButtonElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('No subfolders here — create one below'))
    const select = [...canvasElement.querySelectorAll('button')].find((button) => button.textContent?.startsWith('Use this folder')) as HTMLButtonElement
    expect(select.textContent?.trim()).toBe('Use this folder: storybook-alpha')
    expect(select.disabled).toBe(false)
    await userEvent.click(select)
    await waitFor(() => expect(canvasElement.querySelector('.picker-modal')).toBeNull())
    expect(setupWorkspace(canvasElement).value).toBe('~/storybook-alpha')
  },
}

export const WorkspacePickerCreate: Story = {
  // An empty folder offers the create block: the POST steps into the new
  // folder, and Cancel closes without changing the field.
  parameters: {
    fixture: {
      routes: [
        ...wizardRoutes(LOGIN_WORKSPACE, () => inspectAnswer()),
        listDirsRoute(LOGIN_WORKSPACE, () => json(listing(LOGIN_WORKSPACE, [{ name: 'storybook-alpha', path: '~/storybook-alpha' }]))),
        listDirsRoute('~/storybook-alpha', () => json(listing('~/storybook-alpha'))),
        route('POST', '/api/setup/mkdir', () => json(listing('~/storybook-alpha', [{ name: 'storybook-beta', path: '~/storybook-alpha/storybook-beta' }], '~/storybook-alpha'))),
        listDirsRoute('~/storybook-alpha/storybook-beta', () => json(listing('~/storybook-alpha/storybook-beta'))),
      ],
    },
  },
  render: () => renderLoginState({ label: 'picker create folder' }),
  play: async ({ canvasElement, userEvent }) => {
    await wizardReady(canvasElement)
    await userEvent.click(canvasElement.querySelector('#setup-workspace-browse') as HTMLButtonElement)
    await waitFor(() => expect((canvasElement.querySelector('.picker-path') as HTMLElement).textContent).toBe(LOGIN_WORKSPACE))
    await userEvent.click(canvasElement.querySelector('.picker-dir') as HTMLButtonElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('No subfolders here — create one below'))
    await userEvent.type(canvasElement.querySelector('.picker-new input') as HTMLInputElement, 'storybook-beta')
    const create = [...canvasElement.querySelectorAll('button')].find((button) => button.textContent === 'Create folder') as HTMLButtonElement
    expect(create.disabled).toBe(false)
    await userEvent.click(create)
    await waitFor(() => expect((canvasElement.querySelector('.picker-path') as HTMLElement).textContent).toBe('~/storybook-alpha/storybook-beta'))
    // Creating never selects: the typed folder stays in the field.
    expect(setupWorkspace(canvasElement).value).toBe(LOGIN_WORKSPACE)
    const cancel = [...canvasElement.querySelectorAll('button')].find((button) => button.textContent === 'Cancel') as HTMLButtonElement
    await userEvent.click(cancel)
    await waitFor(() => expect(canvasElement.querySelector('.picker-modal')).toBeNull())
    expect(setupWorkspace(canvasElement).value).toBe(LOGIN_WORKSPACE)
  },
}

export const PickerError: Story = {
  // The folder listing fails for the field's folder and for home: the picker
  // records the error line and stays open, with nothing selectable.
  parameters: {
    fixture: {
      routes: [
        ...wizardRoutes(LOGIN_WORKSPACE, () => inspectAnswer()),
        listDirsRoute(LOGIN_WORKSPACE, () => json({ error: 'storybook listing failed' }, 500)),
        listDirsRoute(undefined, () => json({ error: 'storybook listing failed' }, 500)),
      ],
    },
  },
  render: () => renderLoginState({ label: 'picker listing failure' }),
  play: async ({ canvasElement, userEvent }) => {
    await wizardReady(canvasElement)
    await userEvent.click(canvasElement.querySelector('#setup-workspace-browse') as HTMLButtonElement)
    await waitFor(() => expect(canvasElement.querySelector('.picker-modal .line--error')?.textContent).toContain('storybook listing failed'))
    const select = canvasElement.querySelector('.picker-select') as HTMLButtonElement
    expect(select.disabled).toBe(true)
    expect(canvasElement.querySelector('.picker-modal')).toBeTruthy()
  },
}

export const Restarting: Story = {
  // The RESTARTING branch, reached the way production reaches it: the wizard
  // completes and its finish write answers. The wizard's poll keeps running.
  parameters: {
    fixture: {
      routes: [
        ...wizardRoutes(LOGIN_WORKSPACE, () => inspectAnswer()),
        route('POST', LOGIN_SETUP_DONE_PATH, () => json({ ok: true })),
      ],
    },
  },
  render: () => renderLoginState({ label: 'restarting after setup' }),
  play: async ({ canvasElement, userEvent }) => {
    await wizardReady(canvasElement)
    await fillPasswordFields(canvasElement, userEvent)
    expect(finishButton(canvasElement).disabled).toBe(false)
    await userEvent.click(finishButton(canvasElement))
    await waitFor(() => expect(canvasElement.querySelector('.restarting-body')).toBeTruthy())
    expect(shellTitle(canvasElement)?.textContent).toContain('ciaobot@local · restarting')
    expect(canvasElement.querySelector('.line--banner')).toBeTruthy()
    expect(canvasElement.textContent).toContain('// ciaobot is loading config')
    expect(canvasElement.textContent).toContain('finishing setup and moving to the background service.')
    expect(canvasElement.textContent).toContain('When it comes back, log in with the password you just set.')
    expect(canvasElement.querySelector('.spinner-container .caret')).toBeTruthy()
    expect(canvasElement.querySelector('.setup-wizard')).toBeNull()
  },
}

export const RestartingNative: Story = {
  // The native restarting copy, also through the finish write.
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(bootstrapStatus({ native_bootstrap: true, suggested_workspace: '~/Ciaobot' }))),
        inspectRoute('~/Ciaobot', () => inspectAnswer()),
        route('POST', LOGIN_SETUP_DONE_PATH, () => json({ ok: true })),
      ],
    },
  },
  render: () => renderLoginState({ label: 'restarting after native setup' }),
  play: async ({ canvasElement, userEvent }) => {
    await wizardReady(canvasElement)
    await fillPasswordFields(canvasElement, userEvent)
    await userEvent.click(finishButton(canvasElement))
    await waitFor(() => expect(canvasElement.querySelector('.restarting-body')).toBeTruthy())
    expect(canvasElement.textContent).toContain('Setup saved. Ciaobot is starting on this Mac.')
    expect(canvasElement.textContent).toContain('When it comes back, log in with the password you just set.')
    expect(shellTitle(canvasElement)).toBeNull()
    expect(canvasElement.querySelector('.line--banner')).toBeNull()
  },
}

export const FinishError: Story = {
  // The finish write failing: the wizard records its error line and stays
  // usable, with no restart.
  parameters: {
    fixture: {
      routes: [
        ...wizardRoutes(LOGIN_WORKSPACE, () => inspectAnswer()),
        route('POST', LOGIN_SETUP_DONE_PATH, () => json({ error: 'setup finish failed' }, 500)),
      ],
    },
  },
  render: () => renderLoginState({ label: 'failed setup finish' }),
  play: async ({ canvasElement, userEvent }) => {
    await wizardReady(canvasElement)
    await fillPasswordFields(canvasElement, userEvent)
    await userEvent.click(finishButton(canvasElement))
    await waitFor(() => expect(canvasElement.textContent).toContain('setup finish failed'))
    expect(canvasElement.querySelector('.line--error .prompt--err')).toBeTruthy()
    expect(canvasElement.querySelector('.restarting-body')).toBeNull()
    expect(finishButton(canvasElement).disabled).toBe(false)
    expect(finishButton(canvasElement).textContent?.trim()).toBe('Finish Setup')
  },
}
