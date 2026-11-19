import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { ref } from 'vue'
import SettingsRestartResume from '../components/settings/SettingsRestartResume.vue'
import type { RoutineSettings } from '../lib/types'

// Synthetic engine routine settings: the one key this card reads and writes.
const routines = (resume_after_restart?: boolean) => ({ resume_after_restart }) as unknown as RoutineSettings

const meta = {
  id: 'settings-restart-resume',
  title: 'Settings general/SettingsRestartResume',
  component: SettingsRestartResume,
  args: { routines: routines(true), routinesSaving: false, saveRoutines: async () => {} },
  render: (args) => ({
    components: { SettingsRestartResume },
    setup() {
      // The parent (SettingsView) owns the settings store; this host keeps a
      // local copy so a toggle is visible as the controlled prop changes.
      const current = ref<RoutineSettings | null>(args.routines)
      const saving = ref(args.routinesSaving)
      async function saveRoutines(patch: Record<string, unknown>) {
        current.value = { ...current.value, ...patch } as RoutineSettings
      }
      return { current, saving, saveRoutines }
    },
    template: '<SettingsRestartResume :routines="current" :routines-saving="saving" :save-routines="saveRoutines" />',
  }),
} satisfies Meta<typeof SettingsRestartResume>
export default meta
type Story = StoryObj<typeof meta>

export const On: Story = {
  play: async ({ canvas, userEvent }) => {
    const toggle = canvas.getByRole('switch', { name: 'Continue interrupted chats after a restart' })
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    expect(toggle).toBeEnabled()
    await userEvent.click(toggle)
    expect(toggle.getAttribute('aria-checked')).toBe('false')
  },
}

export const Off: Story = {
  args: { routines: routines(false) },
  play: async ({ canvas, userEvent }) => {
    const toggle = canvas.getByRole('switch', { name: 'Continue interrupted chats after a restart' })
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    await userEvent.click(toggle)
    expect(toggle.getAttribute('aria-checked')).toBe('true')
  },
}

// Before the engine's settings arrive, the owner has no routines and must not
// accept a write it cannot base on them.
export const NoSettings: Story = {
  args: { routines: null },
  play: async ({ canvas, userEvent }) => {
    const toggle = canvas.getByRole('switch', { name: 'Continue interrupted chats after a restart' })
    expect(toggle).toBeDisabled()
    expect(toggle.getAttribute('aria-checked')).toBe('true')
    await userEvent.click(toggle)
    expect(toggle.getAttribute('aria-checked')).toBe('true')
  },
}

export const Saving: Story = {
  args: { routinesSaving: true },
  play: async ({ canvas, userEvent }) => {
    const toggle = canvas.getByRole('switch', { name: 'Continue interrupted chats after a restart' })
    expect(toggle).toBeDisabled()
    await userEvent.click(toggle)
    expect(toggle.getAttribute('aria-checked')).toBe('true')
  },
}

export const KeyboardToggle: Story = {
  play: async ({ canvas, userEvent }) => {
    await userEvent.tab()
    const toggle = canvas.getByRole('switch', { name: 'Continue interrupted chats after a restart' })
    expect(document.activeElement).toBe(toggle)
    await userEvent.keyboard(' ')
    expect(toggle.getAttribute('aria-checked')).toBe('false')
  },
}

export const Narrow: Story = {
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  play: async ({ canvas }) => {
    const toggle = canvas.getByRole('switch', { name: 'Continue interrupted chats after a restart' })
    expect(toggle.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
