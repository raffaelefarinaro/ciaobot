import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, ref } from 'vue'
import type { ConcreteComponent } from 'vue'
import CustomColorsDialog from '../components/settings/CustomColorsDialog.vue'
import { DEFAULT_PAIRS, type AppearancePayload } from '../lib/appearance'
import { activeFixtureBoundary, type FixtureRoute } from './fixtureBoundary'
import { appearanceFixturePayload, seedAppearance } from './appearanceFixture'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/**
 * What the fixture seeds into the appearance singleton and what a write
 * answers with. The dialog has no GET of its own: it reads the seeded
 * singleton; only Apply and Reset write.
 */
type ColorsFixture = {
  open?: boolean
  seed?: AppearancePayload
  write?: 'held' | 'error' | AppearancePayload
}

function colorsRoutes(fixture: ColorsFixture): FixtureRoute[] {
  const write = fixture.write
  if (write === undefined) return []
  return [{
    method: 'PATCH',
    path: '/api/settings/appearance',
    respond: () => write === 'held'
      ? new Promise<never>(() => {})
      : write === 'error'
        ? json({ error: 'Synthetic custom colours refusal' }, 500)
        : json(write),
  }]
}

function renderColors(fixture: ColorsFixture, narrow = false) {
  return defineComponent({
    setup() {
      const open = ref(fixture.open ?? true)
      const restore = seedAppearance(fixture.seed ?? {})
      const boundary = activeFixtureBoundary()
      onBeforeUnmount(restore)
      return () => h('div', narrow ? { style: 'max-width: 390px' } : {}, [
        h('p', { role: 'status' }, `Dialog ${open.value ? 'open' : 'closed'}`),
        h('p', { role: 'status' }, `Fixture requests: ${boundary?.requests.length ?? 0}. Violations: ${boundary?.violations.length ?? 0}.`),
        h(CustomColorsDialog, {
          open: open.value,
          'onUpdate:open': (value: boolean) => { open.value = value },
        }),
        ...(boundary?.violations ?? []).map(message => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

const noErrorsLedger = () => {
  expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  expect(screen.queryByText(/Story network violation/)).toBeNull()
}

const dialog = () => screen.getByRole('dialog', { name: 'Custom colours' })
const hexField = (name: string) => screen.getByLabelText<HTMLInputElement>(name)

// The stories render the owner through the wrapper below; the open prop is
// wrapper state, not story args.
type DialogArgs = { open: boolean }

const meta = {
  id: 'custom-colors-dialog',
  title: 'Settings appearance/CustomColorsDialog',
  component: CustomColorsDialog as unknown as ConcreteComponent<DialogArgs>,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<DialogArgs>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  render: () => renderColors({}),
  play: async () => {
    await waitFor(() => expect(screen.getByText('Dialog open')).toBeTruthy())
    const card = dialog()
    expect(card.textContent).toContain('Dark theme')
    expect(card.textContent).toContain('Light theme')
    // Both seeded pairs are the readable theme defaults, so both sections are.
    expect(screen.getAllByText(/:1 Readable/)).toHaveLength(2)
    expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Reset' }) as HTMLButtonElement).disabled).toBe(false)
    noErrorsLedger()
  },
}

export const Closed: Story = {
  render: () => renderColors({ open: false }),
  play: async () => {
    await waitFor(() => expect(screen.getByText('Dialog closed')).toBeTruthy())
    expect(screen.queryByRole('dialog', { name: 'Custom colours' })).toBeNull()
    noErrorsLedger()
  },
}

export const SeededCustomColours: Story = {
  render: () => renderColors({
    seed: appearanceFixturePayload({
      custom_colors: { dark: { ...DEFAULT_PAIRS.dark }, light: { ...DEFAULT_PAIRS.light } },
    }),
  }),
  play: async () => {
    await waitFor(() => expect(screen.getByText('Dialog open')).toBeTruthy())
    // The saved pair wins over the theme's own colours in both sections.
    expect(hexField('Dark theme background hex').value).toBe(DEFAULT_PAIRS.dark.bg)
    expect(hexField('Light theme text hex').value).toBe(DEFAULT_PAIRS.light.fg)
    expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(true)
    noErrorsLedger()
  },
}

export const EditedReadable: Story = {
  render: () => renderColors({}),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText('Dialog open')).toBeTruthy())
    const field = hexField('Light theme background hex')
    await userEvent.clear(field)
    await userEvent.type(field, '#eaeaf4')
    expect(field.value).toBe('#eaeaf4')
    const card = dialog()
    expect(card.querySelector<HTMLElement>('.colors-status.hint--warn')).toBeNull()
    expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(false)
    noErrorsLedger()
  },
}

export const Unreadable: Story = {
  render: () => renderColors({}),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText('Dialog open')).toBeTruthy())
    const field = hexField('Dark theme background hex')
    await userEvent.clear(field)
    // A light background against the dark theme's light text is too close.
    await userEvent.type(field, '#e0e0e8')
    const card = dialog()
    const status = card.querySelectorAll<HTMLElement>('.colors-status')[0]
    expect(status.classList.contains('hint--warn')).toBe(true)
    expect(status.textContent).toContain('needs at least 4.5:1')
    // The refused pair must not be savable.
    expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(true)
    noErrorsLedger()
  },
}

export const InvalidHex: Story = {
  render: () => renderColors({}),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText('Dialog open')).toBeTruthy())
    const field = hexField('Light theme background hex')
    await userEvent.clear(field)
    await userEvent.type(field, 'nope')
    // Typed text is kept until it parses; the swatch falls back to the seed.
    expect(field.value).toBe('nope')
    expect(hexField('Light theme background colour').value).toBe(DEFAULT_PAIRS.light.bg)
    const card = dialog()
    // The light section is the second status line.
    expect(card.querySelectorAll<HTMLElement>('.colors-status')[1]?.textContent ?? '').toContain(
      'Enter colours like #1a1a2e.',
    )
    expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(true)
    noErrorsLedger()
  },
}

export const Saving: Story = {
  parameters: { fixture: { routes: colorsRoutes({ write: 'held' }) } },
  render: () => renderColors({}),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText('Dialog open')).toBeTruthy())
    const field = hexField('Dark theme background hex')
    await userEvent.clear(field)
    await userEvent.type(field, '#1a2a3a')
    await userEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(screen.getByText(/Fixture requests: 1/)).toBeTruthy())
    // The held write blocks Apply, Reset and closing.
    expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Reset' }) as HTMLButtonElement).disabled).toBe(true)
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: 'Custom colours' })).not.toBeNull()
    noErrorsLedger()
  },
}

export const SaveFailure: Story = {
  parameters: { fixture: { routes: colorsRoutes({ write: 'error' }) } },
  render: () => renderColors({}),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText('Dialog open')).toBeTruthy())
    const field = hexField('Dark theme background hex')
    await userEvent.clear(field)
    await userEvent.type(field, '#1a2a3a')
    await userEvent.click(screen.getByRole('button', { name: 'Apply' }))
    const alert = await waitFor(() => screen.getByRole('alert'))
    expect(alert.textContent).toBe('Synthetic custom colours refusal')
    // The dialog stays open and the controls recover.
    expect(screen.getByRole('dialog', { name: 'Custom colours' })).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(false)
  },
}

const APPLY_PATCH = appearanceFixturePayload({
  custom_colors: { light: { bg: '#eaeaf4', fg: DEFAULT_PAIRS.light.fg } },
  revision: 'storybook-2',
})

export const Apply: Story = {
  parameters: { fixture: { routes: colorsRoutes({ write: APPLY_PATCH }) } },
  render: () => renderColors({}),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText('Dialog open')).toBeTruthy())
    const field = hexField('Light theme background hex')
    await userEvent.clear(field)
    await userEvent.type(field, '#eaeaf4')
    await userEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(screen.getByText('Dialog closed')).toBeTruthy())
    expect(screen.queryByRole('dialog', { name: 'Custom colours' })).toBeNull()
    await waitFor(() => expect(screen.getByText(/Fixture requests: 1/)).toBeTruthy())
    noErrorsLedger()
  },
}

export const Reset: Story = {
  parameters: {
    fixture: {
      routes: colorsRoutes({ write: appearanceFixturePayload({ revision: 'storybook-2' }) }),
    },
  },
  render: () => renderColors({
    seed: appearanceFixturePayload({ custom_colors: { dark: { ...DEFAULT_PAIRS.dark } } }),
  }),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText('Dialog open')).toBeTruthy())
    expect(hexField('Dark theme background hex').value).toBe(DEFAULT_PAIRS.dark.bg)
    await userEvent.click(screen.getByRole('button', { name: 'Reset' }))
    await waitFor(() => expect(screen.getByText('Dialog closed')).toBeTruthy())
    expect(screen.queryByRole('dialog', { name: 'Custom colours' })).toBeNull()
    await waitFor(() => expect(screen.getByText(/Fixture requests: 1/)).toBeTruthy())
    noErrorsLedger()
  },
}

export const Cancel: Story = {
  render: () => renderColors({}),
  play: async ({ userEvent }) => {
    await waitFor(() => expect(screen.getByText('Dialog open')).toBeTruthy())
    expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(false)
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.getByText('Dialog closed')).toBeTruthy())
    expect(screen.queryByRole('dialog', { name: 'Custom colours' })).toBeNull()
    noErrorsLedger()
  },
}

export const Narrow: Story = {
  render: () => renderColors({}, true),
  play: async () => {
    await waitFor(() => expect(screen.getByText('Dialog open')).toBeTruthy())
    const card = dialog()
    expect(card.textContent).toContain('Dark theme')
    expect(card.textContent).toContain('Light theme')
    expect((screen.getByText(/Fixture requests:/).parentElement as HTMLElement).style.maxWidth).toBe('390px')
    noErrorsLedger()
  },
}
