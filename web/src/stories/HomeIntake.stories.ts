import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import HomeIntake from '../components/HomeIntake.vue'
import { assertFenceQuiet } from './homeWindowsFixture'
import {
  expectAttachmentRoundTrip,
  intakeEmptyWorkspace,
  intakeRoutes,
  intakeWorkspace,
  openModelPicker,
  promptBox,
  renderSeeded,
  setQueryForMount,
} from './homeIntakeFixture'

const meta = {
  id: 'home-intake',
  title: 'Home intake and review/HomeIntake',
  component: HomeIntake,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof HomeIntake>
export default meta
type Story = StoryObj<typeof meta>

// Every play below asserts the fence ledger reports no violation, so a request
// the story did not declare fails the story instead of passing silently.

export const Default: Story = {
  parameters: { fixture: { routes: intakeRoutes() } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic default composer', switcher: true }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    expect(promptBox(canvas).value).toBe('')
    expect(promptBox(canvas).disabled).toBe(false)
    expect(canvas.getByRole('button', { name: 'New chat' })).toBeTruthy()
    expect(canvas.getByRole('button', { name: /Choose a project for the new chat — default General/ })).toBeTruthy()
  },
}

export const Draft: Story = {
  parameters: { fixture: { routes: intakeRoutes() } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic unsent draft', switcher: true }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    await userEvent.type(promptBox(canvas), 'Synthetic alpha draft')
    expect(canvas.getByRole('button', { name: 'Send' })).toBeTruthy()
    // A workspace switch keeps each scope's unsent prompt: the other workspace starts empty.
    await userEvent.selectOptions(canvas.getByLabelText('Fixture workspace'), intakeEmptyWorkspace)
    await waitFor(() => expect(promptBox(canvas).value).toBe(''))
    await userEvent.selectOptions(canvas.getByLabelText('Fixture workspace'), intakeWorkspace)
    await waitFor(() => expect(promptBox(canvas).value).toBe('Synthetic alpha draft'))
  },
}

export const Attachments: Story = {
  parameters: { fixture: { routes: intakeRoutes() } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic staged attachment' }),
  play: async ({ canvas, canvasElement }) => {
    await assertFenceQuiet(canvas)
    await expectAttachmentRoundTrip(canvas, canvasElement)
  },
}

// The share-error alert reads `?share-error=` from the page URL on mount.
// The workshop sets that query for this mount only and restores the URL after.
export const ActionError: Story = {
  parameters: { fixture: { routes: intakeRoutes() } },
  render: () => renderSeeded(HomeIntake, {
    label: 'Synthetic share error',
    beforeMount: () => setQueryForMount('share-error', 'size'),
  }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    const alert = canvas.getByText('The shared file is too large (20 MB limit).')
    expect(alert.getAttribute('role')).toBe('alert')
  },
}

export const ModelError: Story = {
  parameters: { fixture: { routes: intakeRoutes({ models: 'failed' }) } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic model list failure' }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    await openModelPicker(canvas)
    await waitFor(() => expect(canvas.getByRole('alert', { name: 'New chat models' }).textContent).toContain('Could not load models.'))
    expect(canvas.getByRole('button', { name: 'Retry new chat models' })).toBeTruthy()
  },
}

export const ModelsLoading: Story = {
  parameters: { fixture: { routes: intakeRoutes({ models: 'loading' }) } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic model list loading' }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    await openModelPicker(canvas)
    await waitFor(() => expect(canvas.getByRole('status', { name: 'New chat models' }).textContent).toContain('Loading new chat models…'))
  },
}

export const ModelsLoaded: Story = {
  parameters: { fixture: { routes: intakeRoutes({ models: 'loaded' }) } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic model list loaded' }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    await openModelPicker(canvas)
    await canvas.findByRole('option', { name: /beta/i })
    await waitFor(() => expect(canvas.getByRole('status', { name: 'New chat models' }).textContent).toContain('New chat models loaded.'))
  },
}

export const ModelsEmpty: Story = {
  parameters: { fixture: { routes: intakeRoutes({ models: 'empty' }) } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic model list empty' }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    await openModelPicker(canvas)
    await waitFor(() => expect(canvas.getByRole('status', { name: 'New chat models' }).textContent).toContain('No models listed; availability is unknown.'))
  },
}

// The create call never answers: the start button is busy and the prompt is locked.
export const Starting: Story = {
  parameters: { fixture: { routes: intakeRoutes({ starting: true }) } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic start in flight' }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    await userEvent.type(promptBox(canvas), 'Synthetic start prompt')
    await userEvent.click(canvas.getByRole('button', { name: 'Send' }))
    const busy = await canvas.findByRole('button', { name: 'Opening…' }) as HTMLButtonElement
    expect(busy.disabled).toBe(true)
    expect(promptBox(canvas).disabled).toBe(true)
  },
}

export const Narrow: Story = {
  parameters: { fixture: { routes: intakeRoutes() } },
  render: () => renderSeeded(HomeIntake, { label: 'Synthetic composer at 390px', narrow: true }),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    const form = canvas.getByRole('form', { name: 'Start a new chat' })
    expect(form.getBoundingClientRect().width <= 390).toBe(true)
    expect(canvas.getByRole('button', { name: 'New chat' })).toBeTruthy()
  },
}
