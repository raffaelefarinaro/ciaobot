import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import HomeWelcomeCard from '../components/HomeWelcomeCard.vue'
import type { FixtureRoute } from './fixtureBoundary'
import { assertFenceQuiet, homeWorkspace, json, held, renderHome } from './homeWindowsFixture'

type WelcomeFixture = 'on-host' | 'remote' | 'hidden' | 'password-busy' | 'password-error' | 'folder-busy' | 'folder-error'

const pending = {
  pending: true,
  workspace_root: `/synthetic/${homeWorkspace}`,
  provider: 'storybook-provider',
  provider_label: 'Synthetic provider',
  platform: 'darwin',
  on_host: true,
}

function welcomeRoutes(fixture: WelcomeFixture): FixtureRoute[] {
  return [
    {
      method: 'GET',
      path: '/api/welcome',
      respond: () => json(fixture === 'hidden' ? { ...pending, pending: false }
        : fixture === 'remote' ? { ...pending, on_host: false } : pending),
    },
    {
      method: 'GET',
      path: '/api/welcome/password',
      respond: () => fixture === 'password-busy'
        ? held()
        : fixture === 'password-error'
          ? json({ error: 'Synthetic password unavailable' }, 500)
          : json({ password: 'storybook-secret' }),
    },
    {
      method: 'POST',
      path: '/api/welcome/open-folder',
      respond: () => fixture === 'folder-busy' ? held() : fixture === 'folder-error'
        ? json({ error: 'Synthetic folder unavailable' }, 500)
        : json({ ok: true }),
    },
    { method: 'POST', path: '/api/welcome/dismiss', respond: () => json({ ok: true }) },
  ]
}

function clipboardStub(): () => void {
  const original = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: async () => {} },
  })
  return () => {
    if (original) Object.defineProperty(navigator, 'clipboard', original)
    else Reflect.deleteProperty(navigator, 'clipboard')
  }
}

const meta = {
  id: 'home-welcome-card',
  title: 'Home windows/HomeWelcomeCard',
  component: HomeWelcomeCard,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof HomeWelcomeCard>
export default meta
type Story = StoryObj<typeof meta>

const label = 'Synthetic welcome status fixture (no real password or path).'

async function loadedCard(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas']) {
  const card = await waitFor(() => {
    const found = canvas.queryByRole('region', { name: 'Your Ciaobot' })
    expect(found).toBeTruthy()
    return found!
  })
  return card
}

export const Hidden: Story = {
  parameters: { fixture: { routes: welcomeRoutes('hidden') } },
  render: () => renderHome(HomeWelcomeCard, { label }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    expect(canvas.queryByRole('region', { name: 'Your Ciaobot' })).toBeNull()
  },
}

export const OnHost: Story = {
  parameters: { fixture: { routes: welcomeRoutes('on-host') } },
  render: () => renderHome(HomeWelcomeCard, { label }),
  play: async ({ canvas, userEvent }) => {
    const card = await loadedCard(canvas)
    await assertFenceQuiet(canvas)
    expect(card.textContent).toContain(`/synthetic/${homeWorkspace}`)
    expect(canvas.getByRole('button', { name: 'Show in Finder' })).toBeTruthy()
    const show = canvas.getByRole('button', { name: 'Show' })
    expect(show.getAttribute('aria-pressed')).toBe('false')
    expect(card.textContent).toContain('Password hidden')
    await userEvent.click(show)
    await waitFor(() => expect(show.getAttribute('aria-pressed')).toBe('true'))
    expect(card.textContent).toContain('storybook-secret')
    await userEvent.click(show)
    await waitFor(() => expect(show.getAttribute('aria-pressed')).toBe('false'))
    expect(card.textContent).not.toContain('storybook-secret')
  },
}

export const Remote: Story = {
  parameters: { fixture: { routes: welcomeRoutes('remote') } },
  render: () => renderHome(HomeWelcomeCard, { label }),
  play: async ({ canvas }) => {
    const card = await loadedCard(canvas)
    await assertFenceQuiet(canvas)
    expect(card.textContent).toContain('The password is shown on the computer running Ciaobot.')
    expect(canvas.queryByRole('button', { name: 'Show' })).toBeNull()
    expect(canvas.queryByRole('button', { name: 'Copy' })).toBeNull()
    expect(canvas.queryByRole('button', { name: 'Show in Finder' })).toBeNull()
  },
}

export const Copied: Story = {
  parameters: { fixture: { routes: welcomeRoutes('on-host') } },
  render: () => renderHome(HomeWelcomeCard, { label, beforeMount: clipboardStub }),
  play: async ({ canvas, userEvent }) => {
    await loadedCard(canvas)
    await assertFenceQuiet(canvas)
    const copy = canvas.getByRole('button', { name: 'Copy' })
    await userEvent.click(copy)
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Copied' })).toBeTruthy())
    expect(canvas.getByText('Password copied')).toBeTruthy()
  },
}

export const PasswordBusy: Story = {
  parameters: { fixture: { routes: welcomeRoutes('password-busy') } },
  render: () => renderHome(HomeWelcomeCard, { label }),
  play: async ({ canvas, userEvent }) => {
    await loadedCard(canvas)
    await assertFenceQuiet(canvas)
    const show = canvas.getByRole('button', { name: 'Show' }) as HTMLButtonElement
    await userEvent.click(show)
    await waitFor(() => expect(show.disabled).toBe(true))
    expect(show.textContent?.trim()).toBe('Show')
    expect(canvas.queryByRole('alert')).toBeNull()
  },
}

export const PasswordError: Story = {
  parameters: { fixture: { routes: welcomeRoutes('password-error') } },
  render: () => renderHome(HomeWelcomeCard, { label }),
  play: async ({ canvas, userEvent }) => {
    await loadedCard(canvas)
    await assertFenceQuiet(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'Show' }))
    // The owner replaces the transport error with its own host-down wording.
    await waitFor(() => expect(canvas.getByRole('alert').textContent)
      .toContain('Could not get the password. Check that Ciaobot is running and try again.'))
    expect(canvas.getByRole('button', { name: 'Show' }).getAttribute('aria-pressed')).toBe('false')
  },
}

export const FolderBusy: Story = {
  parameters: { fixture: { routes: welcomeRoutes('folder-busy') } },
  render: () => renderHome(HomeWelcomeCard, { label }),
  play: async ({ canvas, userEvent }) => {
    await loadedCard(canvas)
    await assertFenceQuiet(canvas)
    const folder = canvas.getByRole('button', { name: 'Show in Finder' })
    await userEvent.click(folder)
    await waitFor(() => expect((folder as HTMLButtonElement).disabled).toBe(true))
    expect(folder.textContent?.trim()).toBe('Show in Finder')
    expect(canvas.queryByRole('alert')).toBeNull()
  },
}

export const FolderError: Story = {
  parameters: { fixture: { routes: welcomeRoutes('folder-error') } },
  render: () => renderHome(HomeWelcomeCard, { label }),
  play: async ({ canvas, userEvent }) => {
    await loadedCard(canvas)
    await assertFenceQuiet(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'Show in Finder' }))
    await waitFor(() => expect(canvas.getByRole('alert').textContent).toContain('Synthetic folder unavailable'))
  },
}

export const Dismissed: Story = {
  parameters: { fixture: { routes: welcomeRoutes('on-host') } },
  render: () => renderHome(HomeWelcomeCard, { label }),
  play: async ({ canvas, userEvent }) => {
    await loadedCard(canvas)
    await assertFenceQuiet(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'Dismiss this card on every device' }))
    await waitFor(() => expect(canvas.queryByRole('region', { name: 'Your Ciaobot' })).toBeNull())
    await waitFor(() => expect(canvas.getByRole('status', { name: 'Fixture ledger' }).textContent)
      .toMatch(/Fixture requests: [2-9]/))
  },
}

export const Narrow: Story = {
  parameters: { fixture: { routes: welcomeRoutes('on-host') } },
  render: () => renderHome(HomeWelcomeCard, { label, narrow: true }),
  play: async ({ canvas }) => {
    const card = await loadedCard(canvas)
    await assertFenceQuiet(canvas)
    expect(card.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    // Production raises these to 44px only under `(pointer: coarse)`; the
    // browser runner has a fine pointer, so assert the narrow layout here.
    expect(canvas.getByRole('button', { name: 'Show in Finder' }).getBoundingClientRect().height)
      .toBeGreaterThanOrEqual(32)
  },
}
