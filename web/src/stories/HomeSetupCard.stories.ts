import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import HomeSetupCard from '../components/HomeSetupCard.vue'
import { SETUP_CARD_DISMISSED_KEY } from '../lib/pwaPlatform'
import { listenForInstallPrompt, probeInstalledApp } from '../lib/installPrompt'
import type { FixtureRoute } from './fixtureBoundary'
import { assertFenceQuiet, json, renderHome } from './homeWindowsFixture'

type SetupFixture = {
  standalone?: boolean
  push?: boolean
  pushEnabled?: boolean
  permission?: NotificationPermission
  denyOnRequest?: boolean
  relatedApps?: boolean
  iphone?: boolean
  dismissed?: boolean
  installPrompt?: boolean
  pushKeyHeld?: boolean
  subscribeError?: boolean
}

type Environment = {
  ua: string
  maxTouchPoints: number
  secure: boolean
}

const defaultEnvironment: Environment = {
  ua: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36',
  maxTouchPoints: 0,
  secure: true,
}

let installChoice: ((choice: { outcome: 'accepted' | 'dismissed' }) => void) | null = null

function define(target: object, key: PropertyKey, value: unknown, restores: Array<() => void>): void {
  const original = Object.getOwnPropertyDescriptor(target, key)
  Object.defineProperty(target, key, { configurable: true, writable: true, value })
  restores.push(() => {
    if (original) Object.defineProperty(target, key, original)
    else Reflect.deleteProperty(target, key)
  })
}

function remove(target: object, key: PropertyKey, restores: Array<() => void>): void {
  const original = Object.getOwnPropertyDescriptor(target, key)
  Reflect.deleteProperty(target, key)
  restores.push(() => {
    if (original) Object.defineProperty(target, key, original)
  })
}

function stubEnvironment(fixture: SetupFixture): () => void {
  const restores: Array<() => void> = []
  define(window, 'matchMedia', (query: string) => ({
    matches: !!fixture.standalone && query.includes('display-mode: standalone'),
    media: query,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
  }), restores)
  define(window, 'isSecureContext', defaultEnvironment.secure, restores)
  define(navigator, 'userAgent', fixture.iphone
    ? 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile Safari/604.1'
    : defaultEnvironment.ua, restores)
  define(navigator, 'maxTouchPoints', defaultEnvironment.maxTouchPoints, restores)

  const hadDismissal = localStorage.getItem(SETUP_CARD_DISMISSED_KEY)
  if (fixture.dismissed) localStorage.setItem(SETUP_CARD_DISMISSED_KEY, '1')
  else localStorage.removeItem(SETUP_CARD_DISMISSED_KEY)
  restores.push(() => {
    if (hadDismissal === null) localStorage.removeItem(SETUP_CARD_DISMISSED_KEY)
    else localStorage.setItem(SETUP_CARD_DISMISSED_KEY, hadDismissal)
  })

  const subscription = {
    endpoint: 'synthetic',
    toJSON: () => ({ endpoint: 'synthetic' }),
    unsubscribe: async () => true,
  }
  const registration = {
    pushManager: {
      getSubscription: async () => (fixture.pushEnabled ? subscription : null),
      subscribe: async () => subscription,
    },
  }
  if (fixture.push) {
    define(navigator, 'serviceWorker', {
      getRegistration: async () => registration,
      ready: Promise.resolve(registration),
    }, restores)
    define(window, 'PushManager', class SyntheticPushManager {}, restores)
    class SyntheticNotification {
      static permission = fixture.permission ?? 'default'
      static async requestPermission(): Promise<NotificationPermission> {
        return fixture.denyOnRequest ? 'denied' : SyntheticNotification.permission
      }
    }
    define(window, 'Notification', SyntheticNotification, restores)
  } else {
    remove(navigator, 'serviceWorker', restores)
    remove(window, 'PushManager', restores)
    remove(window, 'Notification', restores)
  }
  define(navigator, 'getInstalledRelatedApps', async () => fixture.relatedApps ? [{ platform: 'webapp' }] : [], restores)
  // main.ts normally does this once at boot; the workshop has no app bootstrap.
  void probeInstalledApp()

  if (fixture.installPrompt) {
    listenForInstallPrompt()
    const event = new Event('beforeinstallprompt') as Event & {
      prompt: () => Promise<void>
      userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
    }
    event.prompt = async () => {}
    event.userChoice = new Promise(resolve => { installChoice = resolve })
    window.dispatchEvent(event)
    restores.push(() => { installChoice = null })
  }
  return () => restores.reverse().forEach(restore => restore())
}

function setupRoutes(fixture: SetupFixture): FixtureRoute[] {
  return [
    { method: 'GET', path: '/api/push/public-key', respond: () => fixture.pushKeyHeld
      ? new Promise<never>(() => {})
      : json({ public_key: 'c3ludGhldGljLWtleQ' }) },
    {
      method: 'POST',
      path: '/api/push/subscribe',
      respond: () => fixture.subscribeError
        ? json({ error: 'Synthetic push subscribe failed' }, 500)
        : json({ ok: true }),
    },
  ]
}

function render(fixture: SetupFixture, narrow = false) {
  return renderHome(HomeSetupCard, {
    label: 'Synthetic browser setup fixture. No app window or desktop marker is read here.',
    narrow,
    beforeMount: () => stubEnvironment(fixture),
  })
}

const meta = {
  id: 'home-setup-card',
  title: 'Home windows/HomeSetupCard',
  component: HomeSetupCard,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof HomeSetupCard>
export default meta
type Story = StoryObj<typeof meta>

async function cardOf(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas']) {
  return waitFor(() => {
    const found = canvas.queryByRole('region', { name: 'Set up this device' })
    expect(found).toBeTruthy()
    return found!
  })
}

export const CompleteHidden: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true, pushEnabled: true, relatedApps: true }) } },
  render: () => render({ push: true, pushEnabled: true, relatedApps: true }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas, 0)
    expect(canvas.queryByRole('region', { name: 'Set up this device' })).toBeNull()
  },
}

export const DismissedHidden: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true, dismissed: true }) } },
  render: () => render({ push: true, dismissed: true }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas, 0)
    expect(canvas.queryByRole('region', { name: 'Set up this device' })).toBeNull()
  },
}

export const InstallPrompt: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true, installPrompt: true }) } },
  render: () => render({ push: true, installPrompt: true }),
  play: async ({ canvas, userEvent }) => {
    const card = await cardOf(canvas)
    await assertFenceQuiet(canvas, 0)
    const install = canvas.getByRole('button', { name: 'Install' }) as HTMLButtonElement
    await userEvent.click(install)
    // promptInstall clears canPromptInstall before awaiting userChoice, so the
    // real blocking state is the control being withdrawn, not a disabled button.
    await waitFor(() => expect(canvas.queryByRole('button', { name: 'Install' })).toBeNull())
    installChoice?.({ outcome: 'accepted' })
    await waitFor(() => expect(card.textContent).toContain('Installed'))
  },
}

export const InstallInstructions: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true }) } },
  render: () => render({ push: true }),
  play: async ({ canvas, userEvent }) => {
    const card = await cardOf(canvas)
    await assertFenceQuiet(canvas, 0)
    const link = canvas.getByRole('link', { name: 'How to install' })
    expect(link.getAttribute('href')).toBe('/settings#install-app')
    await userEvent.click(link)
    await waitFor(() => expect(canvas.getByText(/Workshop destination: \/settings#install-app/)).toBeTruthy())
    expect(card.textContent).toContain('Turn on notifications')
  },
}

export const InstalledOnDevice: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true, relatedApps: true }) } },
  render: () => render({ push: true, relatedApps: true }),
  play: async ({ canvas }) => {
    const card = await cardOf(canvas)
    await assertFenceQuiet(canvas, 0)
    expect(card.textContent).toContain('Installed on this device. Open it from its icon instead of this tab.')
    expect(canvas.getByRole('button', { name: 'Enable' })).toBeTruthy()
  },
}

export const PushEnabled: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true, pushEnabled: true }) } },
  render: () => render({ push: true, pushEnabled: true }),
  play: async ({ canvas }) => {
    const card = await cardOf(canvas)
    await assertFenceQuiet(canvas, 0)
    expect(card.textContent).toContain('On for this device')
    expect(canvas.queryByRole('button', { name: 'Enable' })).toBeNull()
  },
}

export const PushDenied: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true, permission: 'denied' }) } },
  render: () => render({ push: true, permission: 'denied' }),
  play: async ({ canvas }) => {
    const card = await cardOf(canvas)
    await assertFenceQuiet(canvas, 0)
    expect(card.textContent).toContain('Blocked. Allow notifications for this site in your system settings.')
    expect(canvas.queryByRole('button', { name: 'Enable' })).toBeNull()
  },
}

export const NeedsInstallFirst: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true, iphone: true }) } },
  render: () => render({ push: true, iphone: true }),
  play: async ({ canvas }) => {
    const card = await cardOf(canvas)
    await assertFenceQuiet(canvas, 0)
    expect(card.textContent).toContain('Install the app first; iPhone and iPad only notify from the Home Screen app.')
    expect(canvas.queryByRole('button', { name: 'Enable' })).toBeNull()
    expect(canvas.queryByText('Use on your phone')).toBeNull()
  },
}

export const PushUnavailable: Story = {
  parameters: { fixture: { routes: setupRoutes({}) } },
  render: () => render({}),
  play: async ({ canvas }) => {
    const card = await cardOf(canvas)
    await assertFenceQuiet(canvas, 0)
    expect(card.textContent).toContain('How to install')
    expect(card.textContent).not.toContain('Turn on notifications')
    expect(card.textContent).toContain('Use on your phone')
  },
}

export const PushEnableBusy: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true, permission: 'granted', pushKeyHeld: true }) } },
  render: () => render({ push: true, permission: 'granted', pushKeyHeld: true }),
  play: async ({ canvas, userEvent }) => {
    const card = await cardOf(canvas)
    await assertFenceQuiet(canvas, 0)
    const enable = canvas.getByRole('button', { name: 'Enable' })
    await userEvent.click(enable)
    // The owner keeps the label and only disables the control while the key read is held.
    await waitFor(() => expect((canvas.getByRole('button', { name: 'Enable' }) as HTMLButtonElement).disabled).toBe(true))
    expect(card.textContent).not.toContain('On for this device')
  },
}

export const PushEnableError: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true, permission: 'granted', subscribeError: true }) } },
  render: () => render({ push: true, permission: 'granted', subscribeError: true }),
  play: async ({ canvas, userEvent }) => {
    const card = await cardOf(canvas)
    await assertFenceQuiet(canvas, 0)
    await userEvent.click(canvas.getByRole('button', { name: 'Enable' }))
    await waitFor(() => expect(canvas.getByRole('alert').textContent)
      .toContain('Synthetic push subscribe failed'))
    expect(card.textContent).not.toContain('On for this device')
    expect(canvas.getByRole('button', { name: 'Enable' })).toHaveProperty('disabled', false)
  },
}

export const PhoneStep: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true }) } },
  render: () => render({ push: true }),
  play: async ({ canvas, userEvent }) => {
    const card = await cardOf(canvas)
    await assertFenceQuiet(canvas, 0)
    const summary = canvas.getByText('Use on your phone').closest('summary') as HTMLElement
    expect(summary).toBeTruthy()
    expect(summary.getAttribute('aria-expanded')).toBeNull()
    await userEvent.click(summary)
    await waitFor(() => expect(summary.closest('details')?.open).toBe(true))
    expect(canvas.getByRole('link', { name: 'Phone setup' })).toBeTruthy()
    expect(card.textContent).toContain('Install Tailscale on this computer and your phone')
  },
}

export const DismissAction: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true }) } },
  render: () => render({ push: true }),
  play: async ({ canvas, userEvent }) => {
    await cardOf(canvas)
    await assertFenceQuiet(canvas, 0)
    await userEvent.click(canvas.getByRole('button', { name: 'Dismiss setup reminder on this device' }))
    await waitFor(() => expect(canvas.queryByRole('region', { name: 'Set up this device' })).toBeNull())
    expect(localStorage.getItem(SETUP_CARD_DISMISSED_KEY)).toBe('1')
  },
}

export const Narrow: Story = {
  parameters: { fixture: { routes: setupRoutes({ push: true }) } },
  render: () => render({ push: true }, true),
  play: async ({ canvas }) => {
    const card = await cardOf(canvas)
    await assertFenceQuiet(canvas, 0)
    expect(card.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    expect(canvas.getByText('Use on your phone').closest('summary')!.getBoundingClientRect().height)
      .toBeGreaterThanOrEqual(32)
  },
}
