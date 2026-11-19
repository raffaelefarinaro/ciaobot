import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import CallbackPasteField from '../components/needs-you/CallbackPasteField.vue'
import { ACTION_ID, ACTION_PATH, CHAT_ID, pendingWrite, refusedWrite, renderBody } from './needsYouFixture'
import type { FixtureRoute } from './fixtureBoundary'

// Synthetic loopback address: the shape the engine accepts, with no real code or state.
const CALLBACK = 'http://localhost:53682/callback?code=storybook-code&state=storybook-state'
const HINT = 'After you approve, your browser shows a page that cannot load. Copy its full address and paste it here.'
const props = { chatId: CHAT_ID, actionId: ACTION_ID, hint: HINT }

const accepted: FixtureRoute = {
  method: 'POST',
  path: `${ACTION_PATH}/callback`,
  respond: () => new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
}

const meta = {
  id: 'callback-paste-field',
  title: 'Needs-you stable bodies/CallbackPasteField',
  component: CallbackPasteField,
  args: props,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof CallbackPasteField>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  render: () => renderBody(CallbackPasteField, props),
  play: async ({ canvas }) => {
    const input = await canvas.findByRole('textbox')
    expect((input as HTMLInputElement).value).toBe('')
    expect(canvas.getByRole('button', { name: 'Finish sign-in' })).toBeDisabled()
  },
}

export const Ready: Story = {
  render: () => renderBody(CallbackPasteField, props),
  play: async ({ canvas, userEvent }) => {
    const input = await canvas.findByRole('textbox')
    await userEvent.type(input, CALLBACK)
    expect(canvas.getByRole('button', { name: 'Finish sign-in' })).toBeEnabled()
    expect(canvas.queryByRole('alert')).toBeNull()
  },
}

// The address is held in flight: the input is read-only and the submit is blocked.
export const Pending: Story = {
  parameters: { fixture: { routes: [pendingWrite('callback')] } },
  render: () => renderBody(CallbackPasteField, props),
  play: async ({ canvas, userEvent }) => {
    const input = (await canvas.findByRole('textbox')) as HTMLInputElement
    await userEvent.type(input, CALLBACK)
    await userEvent.click(canvas.getByRole('button', { name: 'Finish sign-in' }))
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Sending…' })).toBeDisabled())
    expect(input.hasAttribute('readonly')).toBe(true)
  },
}

// The engine refuses the address; the field says why and clears the value.
export const Refused: Story = {
  parameters: { fixture: { routes: [refusedWrite('callback', 'state_mismatch')] } },
  render: () => renderBody(CallbackPasteField, props),
  play: async ({ canvas, userEvent }) => {
    const input = (await canvas.findByRole('textbox')) as HTMLInputElement
    await userEvent.type(input, CALLBACK)
    await userEvent.click(canvas.getByRole('button', { name: 'Finish sign-in' }))
    expect(await canvas.findByText('That address belongs to a different sign-in. Start again.')).toBeTruthy()
    expect(canvas.getByRole('alert').getAttribute('id')).toBeTruthy()
    expect(input.value).toBe('')
    expect(input.getAttribute('aria-describedby')).toBe(canvas.getByRole('alert').id)
  },
}

export const Sent: Story = {
  parameters: { fixture: { routes: [accepted] } },
  render: () => renderBody(CallbackPasteField, props),
  play: async ({ canvas, userEvent }) => {
    const input = (await canvas.findByRole('textbox')) as HTMLInputElement
    await userEvent.type(input, CALLBACK)
    await userEvent.click(canvas.getByRole('button', { name: 'Finish sign-in' }))
    await waitFor(() => expect(input.value).toBe(''))
    expect(canvas.queryByRole('alert')).toBeNull()
    expect(canvas.getByRole('button', { name: 'Finish sign-in' })).toBeDisabled()
  },
}

export const KeyboardFocus: Story = {
  render: () => renderBody(CallbackPasteField, props),
  play: async ({ canvas, userEvent }) => {
    const input = await canvas.findByRole('textbox')
    await userEvent.tab()
    expect(document.activeElement).toBe(input)
    expect(getComputedStyle(input).outlineStyle).toBe('solid')
  },
}

export const Narrow: Story = {
  render: () => renderBody(CallbackPasteField, props, { narrow: true }),
  play: async ({ canvas }) => {
    const input = await canvas.findByRole('textbox')
    expect(input.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
