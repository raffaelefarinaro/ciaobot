import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import DeviceCodeBody from '../components/needs-you/DeviceCodeBody.vue'
import type { ChatActionState } from '../lib/types'
import { CHAT_ID, chatAction, pendingWrite, renderBody } from './needsYouFixture'

// Synthetic sign-in: a one-time code and a github.com verification link.
const CODE = 'ABCD-1234'
const VERIFY_URL = 'https://github.com/login/device'
const SESSION = 'storybook-session'

const params = { tool: 'gh', code: CODE, url: VERIFY_URL, session_id: SESSION }
const props = (state: ChatActionState, extra: Record<string, string> = {}) => ({
  chatId: CHAT_ID,
  action: chatAction(state, 'device_code', { ...params, ...extra }),
})

const meta = {
  id: 'device-code-body',
  title: 'Needs-you stable bodies/DeviceCodeBody',
  component: DeviceCodeBody,
  args: props('open', { code: '', url: '' }),
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof DeviceCodeBody>
export default meta
type Story = StoryObj<typeof meta>

export const Open: Story = {
  render: () => renderBody(DeviceCodeBody, props('open', { code: '', url: '' })),
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('button', { name: 'Start sign-in' })).toBeEnabled()
    expect(canvas.queryByRole('group', { name: 'One-time code' })).toBeNull()
    expect(canvas.queryByRole('button', { name: 'Copy' })).toBeNull()
  },
}

export const Starting: Story = {
  parameters: { fixture: { routes: [pendingWrite('start')] } },
  render: () => renderBody(DeviceCodeBody, props('open', { code: '', url: '' })),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Start sign-in' }))
    // The start request is held: the control shows the pending label and is blocked.
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Starting…' })).toBeDisabled())
  },
}

export const Failed: Story = {
  render: () => renderBody(DeviceCodeBody, props('failed', { code: '', url: '' })),
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('button', { name: 'Try again' })).toBeEnabled()
    expect(canvas.queryByRole('group', { name: 'One-time code' })).toBeNull()
  },
}

export const Waiting: Story = {
  render: () => renderBody(DeviceCodeBody, props('waiting')),
  play: async ({ canvas }) => {
    const group = await canvas.findByRole('group', { name: 'One-time code' })
    expect(group.textContent?.trim()).toBe(CODE)
    expect(canvas.getByRole('button', { name: 'Copy' })).toBeEnabled()
    expect(canvas.getByRole('link', { name: 'Open github.com' }).getAttribute('href')).toBe(VERIFY_URL)
    expect(canvas.getByRole('button', { name: 'Show sign-in terminal' })).toBeEnabled()
    expect(canvas.queryByRole('button', { name: 'Start sign-in' })).toBeNull()
  },
}

export const Copied: Story = {
  render: () => renderBody(DeviceCodeBody, props('waiting'), { clipboard: 'ok' }),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Copy' }))
    expect(await canvas.findByRole('button', { name: 'Copied' })).toBeEnabled()
    expect(canvas.queryByRole('button', { name: 'Copy' })).toBeNull()
  },
}

export const CopyFailed: Story = {
  render: () => renderBody(DeviceCodeBody, props('waiting'), { clipboard: 'fail' }),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Copy' }))
    // The body reports the refusal to its card and keeps the label as it was.
    expect(await canvas.findByText('The code could not be copied. Select it and copy it by hand.')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Copy' })).toBeEnabled()
    expect(canvas.queryByRole('button', { name: 'Copied' })).toBeNull()
  },
}

// A code the engine's pattern refuses is never shown or copied; the link still
// comes from the action's own URL.
export const WaitingWithoutCode: Story = {
  render: () => renderBody(DeviceCodeBody, props('waiting', { code: 'not a code' })),
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('link', { name: 'Open github.com' })).toBeTruthy()
    expect(canvas.queryByRole('group', { name: 'One-time code' })).toBeNull()
    expect(canvas.queryByRole('button', { name: 'Copy' })).toBeNull()
  },
}

export const Working: Story = {
  render: () => renderBody(DeviceCodeBody, props('working', { code: '', url: '' })),
  play: async ({ canvas }) => {
    expect(await canvas.findByRole('button', { name: 'Show sign-in terminal' })).toBeEnabled()
    expect(canvas.queryByRole('button', { name: 'Start sign-in' })).toBeNull()
    expect(canvas.queryByRole('group', { name: 'One-time code' })).toBeNull()
  },
}

export const Done: Story = {
  render: () => renderBody(DeviceCodeBody, props('done', { code: '', url: '' })),
  play: async ({ canvas }) => {
    // The card owns Continue; the body renders no control in the done state.
    await waitFor(() => expect(canvas.queryByRole('button')).toBeNull())
    expect(canvas.queryByRole('link')).toBeNull()
  },
}

export const KeyboardFocus: Story = {
  render: () => renderBody(DeviceCodeBody, props('open', { code: '', url: '' })),
  play: async ({ canvas, userEvent }) => {
    const start = await canvas.findByRole('button', { name: 'Start sign-in' })
    await userEvent.tab()
    expect(document.activeElement).toBe(start)
    expect(getComputedStyle(start).outlineStyle).toBe('solid')
  },
}

export const Narrow: Story = {
  render: () => renderBody(DeviceCodeBody, props('waiting'), { narrow: true }),
  play: async ({ canvas }) => {
    const group = await canvas.findByRole('group', { name: 'One-time code' })
    expect(group.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
