import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import SettingsEngineLogin from '../components/settings/SettingsEngineLogin.vue'
import type { FixtureRoute } from './fixtureBoundary'
import { connectedRender, json, neverSettles } from './settingsGeneralFixture'

const LOGIN = '/api/service/login'
const REFUSED = 'storybook-alpha refused the change'
const READ_FAILED = 'storybook-alpha read refused'

interface LoginStatus {
  platform: string
  supported: boolean
  installed: boolean | null
  enabled: boolean | null
  can_change: boolean
  reason: string
  setup_command: string | null
}

const online: LoginStatus = {
  platform: 'storybook-alpha',
  supported: true,
  installed: true,
  enabled: true,
  can_change: true,
  reason: '',
  setup_command: null,
}
const status = (patch: Partial<LoginStatus> = {}): LoginStatus => ({ ...online, ...patch })

type Answer = () => Response | Promise<never>

/**
 * The declared `GET`/`PATCH /api/service/login` routes. Reads answer in order,
 * the last one repeating; `reset` restarts the order on every mount.
 */
function loginFixture(reads: Answer[], write?: Answer) {
  let count = 0
  const routes: FixtureRoute[] = [{
    method: 'GET',
    path: LOGIN,
    respond: () => {
      const answer = reads[Math.min(count, reads.length - 1)]!
      count++
      return answer()
    },
  }]
  if (write) routes.push({ method: 'PATCH', path: LOGIN, respond: write })
  return { routes, reset: () => { count = 0 } }
}

function loginStory(reads: Answer[], write?: Answer, width?: number): Pick<Story, 'parameters' | 'render'> {
  const { routes, reset } = loginFixture(reads, write)
  return {
    parameters: { fixture: { routes } },
    render: () => connectedRender({ component: SettingsEngineLogin, reset, width }),
  }
}

const meta = {
  id: 'settings-engine-login',
  title: 'Settings general/SettingsEngineLogin',
  component: SettingsEngineLogin,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof SettingsEngineLogin>
export default meta
type Story = StoryObj<typeof meta>

const ENABLE = 'Enable at sign-in'
const DISABLE = 'Disable at sign-in'

// The first read never settles, so the owner stays on its pending line.
export const Checking: Story = {
  ...loginStory([neverSettles]),
  play: async ({ canvas }) => {
    expect(await canvas.findByText(/Checking this host/)).toBeTruthy()
    expect(canvas.queryByRole('alert')).toBeNull()
  },
}

export const LoadFailed: Story = {
  ...loginStory([() => json({ error: READ_FAILED }, 500)]),
  play: async ({ canvas, userEvent }) => {
    const alert = await canvas.findByRole('alert')
    expect(alert.textContent).toContain(READ_FAILED)
    await userEvent.click(canvas.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(canvas.getByRole('alert').textContent).toContain(READ_FAILED))
  },
}

export const Enabled: Story = {
  ...loginStory([() => json(status())]),
  play: async ({ canvas }) => {
    expect(await canvas.findByText('Starts when I sign in')).toBeTruthy()
    expect(canvas.getByRole('button', { name: DISABLE })).toBeEnabled()
  },
}

// The one write: the PATCH answer is the re-read, and the row becomes it.
export const Disabled: Story = {
  ...loginStory(
    [() => json(status({ enabled: false }))],
    () => json(status({ enabled: true })),
  ),
  play: async ({ canvas, userEvent }) => {
    expect(await canvas.findByText('Does not start when I sign in')).toBeTruthy()
    await userEvent.click(canvas.getByRole('button', { name: ENABLE }))
    expect(await canvas.findByText('The engine service is now set to start when you sign in.')).toBeTruthy()
    expect(canvas.getByText('Starts when I sign in')).toBeTruthy()
  },
}

export const NotInstalled: Story = {
  ...loginStory([() => json(status({ installed: false, enabled: null, setup_command: 'storybook-alpha service setup' }))]),
  play: async ({ canvas }) => {
    expect(await canvas.findByText('Not installed for this user')).toBeTruthy()
    expect(canvas.getByText('Set the service up by running this on this computer:')).toBeTruthy()
    expect(canvas.getByText('storybook-alpha service setup').tagName).toBe('CODE')
    expect(canvas.queryByRole('button', { name: ENABLE })).toBeNull()
  },
}

export const UnsupportedPlatform: Story = {
  ...loginStory([() => json(status({ supported: false, installed: null, enabled: null, can_change: false }))]),
  play: async ({ canvas }) => {
    expect(await canvas.findByText('Not read on this platform')).toBeTruthy()
    expect(canvas.getByText('This platform has no service backend Ciaobot can read or change.')).toBeTruthy()
    expect(canvas.queryByRole('button')).toBeNull()
  },
}

// The machine did not say whether the service starts: that is never drawn as a position.
export const UnknownPosition: Story = {
  ...loginStory([() => json(status({ enabled: null, can_change: false, reason: 'storybook-alpha did not report a position' }))]),
  play: async ({ canvas }) => {
    expect(await canvas.findByText('Unknown')).toBeTruthy()
    expect(canvas.getByText('storybook-alpha did not report a position')).toBeTruthy()
    expect(canvas.queryByRole('button')).toBeNull()
  },
}

export const ReadOnly: Story = {
  ...loginStory([() => json(status({ can_change: false }))]),
  play: async ({ canvas }) => {
    expect(await canvas.findByText('Starts when I sign in')).toBeTruthy()
    expect(canvas.queryByRole('button')).toBeNull()
  },
}

export const WritePending: Story = {
  ...loginStory([() => json(status({ enabled: false }))], neverSettles),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: ENABLE }))
    const button = canvas.getByRole('button', { name: ENABLE })
    expect(button.textContent).toContain('Working')
    expect(button).toBeDisabled()
    expect(button.getAttribute('aria-busy')).toBe('true')
  },
}

// A 409 carries the machine's current position, which replaces the row; the write
// is reported beside it with a re-read as the only retry.
export const WriteRefused: Story = {
  ...loginStory(
    [() => json(status({ enabled: false }))],
    () => json({ ...status({ enabled: false }), error: REFUSED }, 409),
  ),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: ENABLE }))
    const alert = await canvas.findByRole('alert')
    expect(alert.textContent).toContain(REFUSED)
    expect(canvas.getByText('Does not start when I sign in')).toBeTruthy()
    expect(alert.querySelector('button')?.textContent).toContain('Check again')
  },
}

// A failed re-read keeps the last known row and says so.
export const RefreshFailed: Story = {
  ...loginStory(
    [() => json(status({ enabled: false })), () => json({ error: READ_FAILED }, 500)],
    () => json({ ...status({ enabled: false }), error: REFUSED }, 409),
  ),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: ENABLE }))
    await userEvent.click(await canvas.findByRole('button', { name: 'Check again' }))
    expect(await canvas.findByText(/Could not refresh: storybook-alpha read refused/)).toBeTruthy()
    expect(canvas.getByText(/Showing the last known state/)).toBeTruthy()
    expect(canvas.getByText('Does not start when I sign in')).toBeTruthy()
  },
}

// A re-read over a row that is already on screen shows no pending line: the
// row stays and the write's alert is cleared. Recorded in the owner's loading detail.
export const RefreshPending: Story = {
  ...loginStory(
    [() => json(status({ enabled: false })), neverSettles],
    () => json({ ...status({ enabled: false }), error: REFUSED }, 409),
  ),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: ENABLE }))
    await userEvent.click(await canvas.findByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(canvas.queryByRole('alert')).toBeNull())
    expect(canvas.getByText('Does not start when I sign in')).toBeTruthy()
    expect(canvas.queryByText(/Checking/)).toBeNull()
  },
}

export const Narrow: Story = {
  ...loginStory([() => json(status())], undefined, 390),
  play: async ({ canvas }) => {
    const button = await canvas.findByRole('button', { name: DISABLE })
    expect(button.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
