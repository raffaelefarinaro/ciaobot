import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import CommandBody from '../components/needs-you/CommandBody.vue'
import { CHAT_ID, chatAction, pendingWrite, renderBody } from './needsYouFixture'
import { statusRoute } from './terminalFixtures'
import type { FixtureConfig } from './fixtureBoundary'

const COMMAND = 'echo storybook-alpha'
const props = (state: Parameters<typeof chatAction>[0]) => ({
  chatId: CHAT_ID,
  action: chatAction(state, 'command', { command: COMMAND }),
})

// The body never runs a command: it types into the chat's terminal or copies.
// The terminal reports itself unavailable, so a Type it click falls back to copy.
const unavailableTerminal: FixtureConfig = {
  routes: [statusRoute({ available: false, reason: 'disabled', unlock_expires_in: 0 })],
}

const meta = {
  id: 'command-body',
  title: 'Needs-you stable bodies/CommandBody',
  component: CommandBody,
  args: props('open'),
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof CommandBody>
export default meta
type Story = StoryObj<typeof meta>

export const Open: Story = {
  render: () => renderBody(CommandBody, props('open')),
  play: async ({ canvas }) => {
    expect(await canvas.findByText(/Nothing runs until you press Enter/)).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Type it in a terminal' })).toBeEnabled()
    expect(canvas.getByRole('button', { name: 'Copy' })).toBeEnabled()
    expect(canvas.queryByRole('button', { name: 'I ran it' })).toBeNull()
    expect(canvas.getByRole('region', { name: 'Suggested command' }).textContent?.trim()).toBe(COMMAND)
  },
}

export const Working: Story = {
  parameters: { fixture: { routes: [pendingWrite('start')] } },
  render: () => renderBody(CommandBody, props('working')),
  play: async ({ canvas, userEvent }) => {
    const ran = await canvas.findByRole('button', { name: 'I ran it' })
    expect(canvas.queryByRole('button', { name: 'Type it in a terminal' })).toBeNull()
    await userEvent.click(ran)
    // The marking request is held: the control is blocked, not just styled.
    await waitFor(() => expect(canvas.getByRole('button', { name: 'I ran it' })).toBeDisabled())
    expect(canvas.getByRole('button', { name: 'Copy' })).toBeEnabled()
  },
}

export const Done: Story = {
  render: () => renderBody(CommandBody, props('done')),
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('region', { name: 'Suggested command' })).toBeTruthy()
    expect(canvas.queryByRole('button')).toBeNull()
    expect(canvas.queryByText(/Nothing runs until you press Enter/)).toBeNull()
  },
}

export const Failed: Story = {
  render: () => renderBody(CommandBody, props('failed')),
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('button', { name: 'Type it in a terminal' })).toBeEnabled()
    expect(canvas.getByRole('button', { name: 'Copy' })).toBeEnabled()
    expect(canvas.queryByRole('button', { name: 'I ran it' })).toBeNull()
    expect(canvas.queryByText(/Nothing runs until you press Enter/)).toBeNull()
  },
}

export const Copied: Story = {
  render: () => renderBody(CommandBody, props('open'), { clipboard: 'ok' }),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Copy' }))
    expect(await canvas.findByText('Copied.')).toBeTruthy()
    expect(canvas.getByRole('status').textContent).toBe('Copied.')
    expect(canvas.getByRole('button', { name: 'I ran it' })).toBeEnabled()
  },
}

export const CopyFailed: Story = {
  render: () => renderBody(CommandBody, props('open'), { clipboard: 'fail' }),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Copy' }))
    expect(await canvas.findByText('The command could not be copied. Try again.')).toBeTruthy()
    expect(canvas.getByRole('alert').textContent).toContain('could not be copied')
    expect(canvas.queryByRole('button', { name: 'I ran it' })).toBeNull()
  },
}

export const TypeFallback: Story = {
  parameters: { fixture: unavailableTerminal },
  render: () => renderBody(CommandBody, props('open'), { clipboard: 'ok' }),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Type it in a terminal' }))
    expect(await canvas.findByText(/The terminal was not ready, so the command was copied/)).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'I ran it' })).toBeEnabled()
  },
}

export const Typing: Story = {
  parameters: {
    fixture: {
      routes: [{ method: 'GET', path: '/api/terminal/status', respond: () => new Promise<never>(() => {}) }],
    },
  },
  render: () => renderBody(CommandBody, props('open')),
  play: async ({ canvas, userEvent }) => {
    const type = await canvas.findByRole('button', { name: 'Type it in a terminal' })
    await userEvent.click(type)
    // The terminal read is held, so the control is blocked while the command is being typed.
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Type it in a terminal' })).toBeDisabled())
    expect(canvas.getByRole('button', { name: 'Copy' })).toBeEnabled()
  },
}

export const KeyboardFocus: Story = {
  render: () => renderBody(CommandBody, props('open')),
  play: async ({ canvas, userEvent }) => {
    const text = await canvas.findByRole('region', { name: 'Suggested command' })
    await userEvent.tab()
    expect(document.activeElement).toBe(text)
    expect(getComputedStyle(text).outlineStyle).toBe('solid')
  },
}

export const Narrow: Story = {
  render: () => renderBody(CommandBody, props('open'), { narrow: true }),
  play: async ({ canvas }) => {
    const text = await canvas.findByRole('region', { name: 'Suggested command' })
    expect(text.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    expect(canvas.getByRole('button', { name: 'Copy' })).toBeEnabled()
  },
}
