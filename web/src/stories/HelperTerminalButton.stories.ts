import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import HelperTerminalButton from '../components/needs-you/HelperTerminalButton.vue'
import type { FixtureConfig } from './fixtureBoundary'
import { renderBody } from './needsYouFixture'
import { statusRoute } from './terminalFixtures'

// Synthetic helper session id: the engine started a sign-in CLI for this chat.
const SESSION = 'storybook-session'
const props = (sessionId: string) => ({ chatId: 'storybook-alpha', sessionId })

// Showing the terminal reads its status once; the engine reports it unavailable here.
const unavailable: FixtureConfig = {
  routes: [statusRoute({ available: false, reason: 'disabled', unlock_expires_in: 0 })],
}

const meta = {
  id: 'helper-terminal-button',
  title: 'Needs-you stable bodies/HelperTerminalButton',
  component: HelperTerminalButton,
  args: props(SESSION),
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof HelperTerminalButton>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  render: () => renderBody(HelperTerminalButton, props(SESSION)),
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('button', { name: 'Show sign-in terminal' })).toBeEnabled()
  },
}

// Without a helper session the body renders nothing, so there is no control to show.
export const Hidden: Story = {
  render: () => renderBody(HelperTerminalButton, props('')),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.queryByRole('button')).toBeNull())
  },
}

export const Show: Story = {
  parameters: { fixture: unavailable },
  render: () => renderBody(HelperTerminalButton, props(SESSION)),
  play: async ({ canvas, userEvent }) => {
    const button = await canvas.findByRole('button', { name: 'Show sign-in terminal' })
    await userEvent.click(button)
    // The one declared status read answers; the control stays put and nothing else is requested.
    expect(await canvas.findByRole('button', { name: 'Show sign-in terminal' })).toBeEnabled()
    expect(canvas.queryByRole('alert')).toBeNull()
  },
}

export const KeyboardFocus: Story = {
  render: () => renderBody(HelperTerminalButton, props(SESSION)),
  play: async ({ canvas, userEvent }) => {
    const button = await canvas.findByRole('button', { name: 'Show sign-in terminal' })
    await userEvent.tab()
    expect(document.activeElement).toBe(button)
    expect(getComputedStyle(button).outlineStyle).toBe('solid')
  },
}

export const Narrow: Story = {
  render: () => renderBody(HelperTerminalButton, props(SESSION), { narrow: true }),
  play: async ({ canvas }) => {
    const button = await canvas.findByRole('button', { name: 'Show sign-in terminal' })
    expect(button.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
