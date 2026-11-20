import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import HomeIntake from '../components/HomeIntake.vue'
import { assertFenceQuiet } from './homeWindowsFixture'
import {
  expectAttachmentRoundTrip,
  intakeRoutes,
  openModelPicker,
  promptBox,
  renderSeeded,
} from './homeIntakeFixture'

// The composer row (inline, owned by HomeIntake.vue's form). These stories
// render the same production owner; they assert the form's own controls.
const meta = {
  id: 'home-intake-composer',
  title: 'Home intake and review/Home intake composer',
  component: HomeIntake,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof HomeIntake>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  parameters: { fixture: { routes: intakeRoutes() } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic composer form' }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    expect(canvas.getByRole('form', { name: 'Start a new chat' })).toBeTruthy()
    expect(canvas.queryByRole('button', { name: 'Send' })).toBeNull()
    expect(canvas.getByRole('button', { name: 'New chat' })).toBeTruthy()
  },
}

export const Prompt: Story = {
  parameters: { fixture: { routes: intakeRoutes() } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic typed prompt' }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    await userEvent.type(promptBox(canvas), 'Synthetic prompt text')
    // With text, the send control is named Send and advertises its chord.
    const send = canvas.getByRole('button', { name: 'Send' })
    expect(send.getAttribute('title')).toMatch(/^Send \(/)
    expect(canvas.queryByRole('button', { name: 'New chat' })).toBeNull()
  },
}

// No project in the workspace: the project chip and the send control are blocked.
export const Project: Story = {
  parameters: { fixture: { routes: intakeRoutes() } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic workspace without a project', projects: false }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    const chip = canvas.getByRole('button', { name: /Choose a project for the new chat/ }) as HTMLButtonElement
    expect(chip.disabled).toBe(true)
    const submit = canvas.getByRole('button', { name: 'New chat' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
  },
}

export const Model: Story = {
  parameters: { fixture: { routes: intakeRoutes({ models: 'loaded' }) } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic model picker' }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    await openModelPicker(canvas)
    await userEvent.click(await canvas.findByRole('option', { name: /beta/i }))
    await waitFor(() => expect(canvas.getByRole('button', { name: /Model for the new chat — storybook-model-beta/ })).toBeTruthy())
  },
}

export const Attachments: Story = {
  parameters: { fixture: { routes: intakeRoutes() } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic composer attachments' }),
  play: async ({ canvas, canvasElement }) => {
    await assertFenceQuiet(canvas)
    await expectAttachmentRoundTrip(canvas, canvasElement)
  },
}
