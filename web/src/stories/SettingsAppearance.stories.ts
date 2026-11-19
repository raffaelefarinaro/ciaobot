import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, ref } from 'vue'
import SettingsAppearance from '../components/settings/SettingsAppearance.vue'
import { DEFAULT_PAIRS, type AppearancePayload } from '../lib/appearance'
import { activeFixtureBoundary, type FixtureRoute } from './fixtureBoundary'
import { appearanceFixturePayload, seedAppearance } from './appearanceFixture'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** What the engine-shaped GET answers and what a successful PATCH replies with. */
type AppearanceFixture = {
  get: 'ok' | 'error'
  seeded: AppearancePayload
  /** What a successful PATCH (a choice or a Reset) answers with. */
  patch?: AppearancePayload
  /** A failing PATCH instead. */
  patchError?: boolean
}

function payload(overrides: Partial<AppearancePayload>, revision = 'storybook-1'): AppearancePayload {
  return appearanceFixturePayload({ revision, ...overrides })
}

const SEED_CUSTOM_BOTH = { custom_colors: { dark: { ...DEFAULT_PAIRS.dark }, light: { ...DEFAULT_PAIRS.light } } }
const SEED_CUSTOM_DARK = { custom_colors: { dark: { ...DEFAULT_PAIRS.dark } } }

function appearanceRoutes(fixture: AppearanceFixture): FixtureRoute[] {
  return [
    {
      method: 'GET',
      path: '/api/settings/appearance',
      respond: () => fixture.get === 'error'
        ? json({ error: 'Synthetic appearance read failed' }, 500)
        : json(fixture.seeded),
    },
    {
      method: 'PATCH',
      path: '/api/settings/appearance',
      respond: () => fixture.patchError
        ? json({ error: 'Synthetic appearance refusal' }, 500)
        : json(fixture.patch ?? fixture.seeded),
    },
  ]
}

function renderAppearance(fixture: AppearanceFixture, narrow = false) {
  return defineComponent({
    setup() {
      const restore = seedAppearance(fixture.get === 'ok' ? fixture.seeded : {})
      const host = ref<HTMLElement>()
      const error = ref('')
      const boundary = activeFixtureBoundary()
      onBeforeUnmount(() => {
        try { restore() } finally { error.value = '' }
      })
      return () => h('div', narrow ? { style: 'max-width: 390px' } : {}, [
        h('p', { role: 'status' }, `Fixture requests: ${boundary?.requests.length ?? 0}. Violations: ${boundary?.violations.length ?? 0}.`),
        h('div', { ref: host }, [
          h(SettingsAppearance),
        ]),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(boundary?.violations ?? []).map(message => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

const meta = {
  id: 'settings-appearance',
  title: 'Settings appearance and Google/SettingsAppearance',
  component: SettingsAppearance,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof SettingsAppearance>
export default meta
type Story = StoryObj<typeof meta>

/** The boundary status line: the ledger must stay empty for every read and write. */
const fenceQuiet = async () => {
  await waitFor(() => expect(screen.getAllByText(/Fixture requests: 1/).length).toBeGreaterThan(0))
  await new Promise(resolve => setTimeout(resolve, 500))
  expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  expect(screen.queryByText(/Story network violation/)).toBeNull()
}

export const Default: Story = {
  parameters: { fixture: { routes: appearanceRoutes({ get: 'ok', seeded: payload({}) }) } },
  render: () => renderAppearance({ get: 'ok', seeded: payload({}) }),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('radiogroup', { name: 'Text contrast' })).toBeTruthy())
    expect(canvas.getByRole('radio', { name: 'Normal' }).getAttribute('aria-checked')).toBe('true')
    expect(canvas.getByRole('radio', { name: 'System' }).getAttribute('aria-checked')).toBe('true')
    expect(canvas.getByRole('button', { name: 'Customise…' })).toBeTruthy()
    expect(canvas.queryByRole('button', { name: 'Reset' })).toBeNull()
    expect(canvas.getByText('Choose your own background and text colours.')).toBeTruthy()
    expect(canvas.queryByRole('alert')).toBeNull()
    await fenceQuiet()
  },
}

export const CustomColoursBoth: Story = {
  parameters: { fixture: { routes: appearanceRoutes({ get: 'ok', seeded: payload(SEED_CUSTOM_BOTH) }) } },
  render: () => renderAppearance({ get: 'ok', seeded: payload(SEED_CUSTOM_BOTH) }),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Using your own colours for both themes.')).toBeTruthy())
    const reset = canvas.getByRole('button', { name: 'Reset' })
    expect((reset as HTMLButtonElement).disabled).toBe(false)
    await fenceQuiet()
  },
}

export const CustomColoursOneTheme: Story = {
  parameters: { fixture: { routes: appearanceRoutes({ get: 'ok', seeded: payload(SEED_CUSTOM_DARK) }) } },
  render: () => renderAppearance({ get: 'ok', seeded: payload(SEED_CUSTOM_DARK) }),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Using your own colours for the dark theme.')).toBeTruthy())
    expect(canvas.getByRole('button', { name: 'Reset' })).toBeTruthy()
    await fenceQuiet()
  },
}

export const SoftIgnored: Story = {
  parameters: { fixture: { routes: appearanceRoutes({ get: 'ok', seeded: payload({ text_contrast: 'soft', ...SEED_CUSTOM_DARK }) }) } },
  render: () => renderAppearance({ get: 'ok', seeded: payload({ text_contrast: 'soft', ...SEED_CUSTOM_DARK }) }),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(
      'Soft text contrast has no effect while custom colours are on for this theme.',
    )).toBeTruthy())
    expect(canvas.getByRole('radio', { name: 'Soft' }).getAttribute('aria-checked')).toBe('true')
    expect(canvas.getByRole('button', { name: 'Reset' })).toBeTruthy()
    await fenceQuiet()
  },
}

export const Customise: Story = {
  parameters: { fixture: { routes: appearanceRoutes({ get: 'ok', seeded: payload({}) }) } },
  render: () => renderAppearance({ get: 'ok', seeded: payload({}) }),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Customise…' })).toBeTruthy())
    await userEvent.click(canvas.getByRole('button', { name: 'Customise…' }))
    const dialog = await waitFor(() => screen.getByRole('dialog', { name: 'Custom colours' }))
    expect(dialog.textContent).toContain('Dark theme')
    expect(dialog.textContent).toContain('Light theme')
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Custom colours' })).toBeNull())
    // The row itself keeps its controls.
    expect(canvas.getByRole('button', { name: 'Customise…' })).toBeTruthy()
  },
}

export const ResetCustomColours: Story = {
  parameters: { fixture: { routes: appearanceRoutes({ get: 'ok', seeded: payload(SEED_CUSTOM_BOTH), patch: payload({}, 'storybook-2') }) } },
  render: () => renderAppearance({ get: 'ok', seeded: payload(SEED_CUSTOM_BOTH), patch: payload({}, 'storybook-2') }),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Reset' })).toBeTruthy())
    await userEvent.click(canvas.getByRole('button', { name: 'Reset' }))
    await waitFor(() => expect(canvas.queryByRole('button', { name: 'Reset' })).toBeNull())
    expect(canvas.getByText('Choose your own background and text colours.')).toBeTruthy()
    await waitFor(() => expect(screen.getByText(/Fixture requests: 2/)).toBeTruthy())
    await new Promise(resolve => setTimeout(resolve, 500))
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const TextContrastSoft: Story = {
  parameters: { fixture: { routes: appearanceRoutes({ get: 'ok', seeded: payload({}), patch: payload({ text_contrast: 'soft' }, 'storybook-2') }) } },
  render: () => renderAppearance({ get: 'ok', seeded: payload({}), patch: payload({ text_contrast: 'soft' }, 'storybook-2') }),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('radiogroup', { name: 'Text contrast' })).toBeTruthy())
    const soft = canvas.getByRole('radio', { name: 'Soft' })
    await userEvent.click(soft)
    expect(document.activeElement).toBe(soft)
    await waitFor(() => expect(soft.getAttribute('aria-checked')).toBe('true'))
    expect(canvas.queryByText(/Soft text contrast has no effect/)).toBeNull()
    expect(canvas.queryByRole('alert')).toBeNull()
    await waitFor(() => expect(screen.getByText(/Fixture requests: 2/)).toBeTruthy())
    await new Promise(resolve => setTimeout(resolve, 500))
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const SaveFailure: Story = {
  parameters: { fixture: { routes: appearanceRoutes({ get: 'ok', seeded: payload({}), patchError: true }) } },
  render: () => renderAppearance({ get: 'ok', seeded: payload({}), patchError: true }),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('radiogroup', { name: 'Text contrast' })).toBeTruthy())
    await userEvent.click(canvas.getByRole('radio', { name: 'Soft' }))
    const alert = await waitFor(() => screen.getByRole('alert'))
    expect(alert.textContent).toBe('Synthetic appearance refusal')
    // The rejected choice stays on the engine's value.
    expect(canvas.getByRole('radio', { name: 'Normal' }).getAttribute('aria-checked')).toBe('true')
    expect(canvas.getByRole('radio', { name: 'Soft' }).getAttribute('aria-checked')).toBe('false')
    await waitFor(() => expect(screen.getByText(/Fixture requests: 2/)).toBeTruthy())
  },
}

export const LoadFailure: Story = {
  parameters: { fixture: { routes: appearanceRoutes({ get: 'error', seeded: payload({}) }) } },
  render: () => renderAppearance({ get: 'error', seeded: payload({}) }),
  play: async ({ canvas }) => {
    const alert = await waitFor(() => screen.getByRole('alert'))
    expect(alert.textContent).toBe('Could not load appearance settings.')
    // Before the first engine answer the groups show the shipped defaults.
    expect(canvas.getByRole('radio', { name: 'Normal' }).getAttribute('aria-checked')).toBe('true')
    await fenceQuiet()
  },
}

export const Narrow: Story = {
  parameters: { fixture: { routes: appearanceRoutes({ get: 'ok', seeded: payload(SEED_CUSTOM_BOTH) }) } },
  render: () => renderAppearance({ get: 'ok', seeded: payload(SEED_CUSTOM_BOTH) }, true),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('radiogroup', { name: 'Body font' })).toBeTruthy())
    expect(canvas.getByRole('button', { name: 'Reset' })).toBeTruthy()
    expect((canvas.getByText(/Fixture requests:/).parentElement as HTMLElement).style.maxWidth).toBe('390px')
    await fenceQuiet()
  },
}
