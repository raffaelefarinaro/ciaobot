import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import GwsAuthBody from '../components/needs-you/GwsAuthBody.vue'
import type { ChatActionState } from '../lib/types'
import { CHAT_ID, chatAction, pendingWrite, renderBody, settingsDestinations } from './needsYouFixture'

// Synthetic Google sign-in: a synthetic profile and a consent URL on the Google host.
const PROFILE = 'storybook-alpha'
const CONSENT_URL = 'https://accounts.google.com/o/oauth2/v2/auth?state=storybook-alpha'

const props = (state: ChatActionState, extra: Record<string, string> = {}, error = '') => ({
  chatId: CHAT_ID,
  action: chatAction(state, 'gws_auth', { profile: PROFILE, url: CONSENT_URL, ...extra }, error),
})

const meta = {
  id: 'gws-auth-body',
  title: 'Needs-you stable bodies/GwsAuthBody',
  component: GwsAuthBody,
  args: props('open', { url: '' }),
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof GwsAuthBody>
export default meta
type Story = StoryObj<typeof meta>

export const Open: Story = {
  render: () => renderBody(GwsAuthBody, props('open', { url: '' })),
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('button', { name: 'Sign in with Google' })).toBeEnabled()
    expect(canvas.queryByRole('link')).toBeNull()
  },
}

export const Starting: Story = {
  parameters: { fixture: { routes: [pendingWrite('start')] } },
  // The engine's computer opens a placeholder tab before the request; the stub keeps it inside the fixture.
  render: () => renderBody(GwsAuthBody, props('open', { url: '' }), { popup: true }),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Sign in with Google' }))
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Starting…' })).toBeDisabled())
  },
}

export const Failed: Story = {
  render: () => renderBody(GwsAuthBody, props('failed', { url: '' }, 'The sign-in was cancelled.')),
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('button', { name: 'Try again' })).toBeEnabled()
    expect(canvas.queryByRole('link', { name: 'Use manual connect in Settings' })).toBeNull()
  },
}

// The manual-connect link appears only when the engine's failure names the web app.
export const FailedManualConnect: Story = {
  render: () => renderBody(
    GwsAuthBody,
    props('failed', { url: '' }, 'Finish this sign-in in the web app.'),
    { routerRoutes: settingsDestinations },
  ),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('link', { name: 'Use manual connect in Settings' }))
    expect(await canvas.findByRole('status')).toBeTruthy()
    expect(canvas.getByRole('status').textContent).toBe('Workspace settings page')
  },
}

export const Waiting: Story = {
  render: () => renderBody(GwsAuthBody, props('waiting')),
  play: async ({ canvas }) => {
    const link = await canvas.findByRole('link', { name: 'Open Google sign-in' })
    expect(link.getAttribute('href')).toBe(CONSENT_URL)
    expect(link.getAttribute('rel')).toContain('noopener')
    expect(canvas.queryByRole('button')).toBeNull()
  },
}

// A waiting action whose URL is not a Google consent page offers no link at all.
export const WaitingWithoutConsent: Story = {
  render: () => renderBody(GwsAuthBody, props('waiting', { url: 'https://example.test/consent' })),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.queryByRole('link')).toBeNull())
    expect(canvas.queryByRole('button')).toBeNull()
  },
}

export const Working: Story = {
  render: () => renderBody(GwsAuthBody, props('working', { url: '' })),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.queryByRole('button')).toBeNull())
    expect(canvas.queryByRole('link')).toBeNull()
  },
}

export const Done: Story = {
  render: () => renderBody(GwsAuthBody, props('done', { url: '' })),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.queryByRole('button')).toBeNull())
    expect(canvas.queryByRole('link')).toBeNull()
  },
}

export const KeyboardFocus: Story = {
  render: () => renderBody(GwsAuthBody, props('open', { url: '' })),
  play: async ({ canvas, userEvent }) => {
    const primary = await canvas.findByRole('button', { name: 'Sign in with Google' })
    await userEvent.tab()
    expect(document.activeElement).toBe(primary)
    expect(getComputedStyle(primary).outlineStyle).toBe('solid')
  },
}

export const Narrow: Story = {
  render: () => renderBody(GwsAuthBody, props('open', { url: '' }), { narrow: true }),
  play: async ({ canvas }) => {
    const primary = await canvas.findByRole('button', { name: 'Sign in with Google' })
    expect(primary.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
