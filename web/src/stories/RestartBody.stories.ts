import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import RestartBody from '../components/needs-you/RestartBody.vue'
import { CHAT_ID, chatAction, renderBody, settingsDestinations } from './needsYouFixture'

// The body only navigates: the restart itself is started from the Settings page it opens.
const NOTE = 'Ciaobot changed a synthetic setting that applies after a restart.'
const props = (params: Record<string, string>) => ({
  chatId: CHAT_ID,
  action: chatAction('open', 'restart', params),
})
const options = { routerRoutes: settingsDestinations }

const meta = {
  id: 'restart-body',
  title: 'Needs-you stable bodies/RestartBody',
  component: RestartBody,
  args: props({ reason: 'config', note: NOTE }),
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof RestartBody>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  render: () => renderBody(RestartBody, props({ reason: 'config', note: NOTE }), options),
  play: async ({ canvas }) => {
    expect(await canvas.findByText("Agent's reason:")).toBeTruthy()
    expect(canvas.getByText(NOTE)).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Open Settings' })).toBeEnabled()
  },
}

export const Update: Story = {
  render: () => renderBody(RestartBody, props({ reason: 'update' }), options),
  play: async ({ canvas, userEvent }) => {
    expect(await canvas.findByRole('button', { name: 'Open Updates' })).toBeEnabled()
    expect(canvas.queryByText("Agent's reason:")).toBeNull()
    await userEvent.click(canvas.getByRole('button', { name: 'Open Updates' }))
    expect(await canvas.findByRole('status')).toBeTruthy()
    expect(canvas.getByRole('status').textContent).toBe('Settings page')
  },
}

// A reason the PWA does not know gets the generic Settings copy and the advanced restart place.
export const UnknownReason: Story = {
  render: () => renderBody(RestartBody, props({ reason: 'other', note: NOTE }), options),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Open Settings' }))
    expect(await canvas.findByRole('status')).toBeTruthy()
    expect(canvas.getByRole('status').textContent).toBe('Advanced settings page')
  },
}

export const NoNote: Story = {
  render: () => renderBody(RestartBody, props({ reason: 'config' }), options),
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('button', { name: 'Open Settings' })).toBeEnabled()
    expect(canvas.queryByText("Agent's reason:")).toBeNull()
  },
}

export const KeyboardActivate: Story = {
  render: () => renderBody(RestartBody, props({ reason: 'config', note: NOTE }), options),
  play: async ({ canvas, userEvent }) => {
    const button = await canvas.findByRole('button', { name: 'Open Settings' })
    await userEvent.tab()
    expect(document.activeElement).toBe(button)
    expect(getComputedStyle(button).outlineStyle).toBe('solid')
    await userEvent.keyboard('{Enter}')
    expect(await canvas.findByRole('status')).toBeTruthy()
    expect(canvas.getByRole('status').textContent).toBe('Advanced settings page')
  },
}

export const Narrow: Story = {
  render: () => renderBody(RestartBody, props({ reason: 'config', note: NOTE }), { ...options, narrow: true }),
  play: async ({ canvas }) => {
    const button = await canvas.findByRole('button', { name: 'Open Settings' })
    expect(button.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
