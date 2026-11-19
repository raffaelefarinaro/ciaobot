import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, provide, ref, type Component } from 'vue'
import SettingsClaudeTrim from '../components/settings/SettingsClaudeTrim.vue'
import { WORKSPACE_ADMIN_KEY, type WorkspaceAdminContext } from '../composables/useWorkspaceAdmin'
import type { ClaudeTrimOption } from '../lib/types'
import { useProjectStore } from '../stores/projects'
import { connectedRender } from './settingsGeneralFixture'

// Synthetic catalogue: the design group is flat (two rows) and comes first so
// the keyboard story reaches a switch first; scheduling has four rows, so it sits
// behind a disclosure and is hidden by default.
const option = (name: string, group: string, label: string, defaultTrimmed = false): ClaudeTrimOption => ({
  key: `tool:${name}`,
  kind: 'tool',
  name,
  label,
  reason: `Synthetic reason for ${label.toLowerCase()}.`,
  group,
  default_trimmed: defaultTrimmed,
})
const CATALOGUE: ClaudeTrimOption[] = [
  option('storybook-alpha-design', 'design', 'Storybook alpha design tool'),
  option('storybook-beta-design', 'design', 'Storybook beta design tool'),
  option('storybook-cron-a', 'scheduling', 'Storybook cron A schedule', true),
  option('storybook-cron-b', 'scheduling', 'Storybook cron B schedule', true),
  option('storybook-cron-c', 'scheduling', 'Storybook cron C schedule', true),
  option('storybook-cron-d', 'scheduling', 'Storybook cron D schedule', true),
]

interface Form {
  name: string
  claude_trim: Record<string, boolean>
  disallowed_tools: string
}

const SCHEDULING_SUMMARY = /Scheduling · \d+ of 4 hidden/

/** The workspace admin SettingsView provides, reduced to what the card reads. */
function adminFor(form: Form | null, saving: string | null): WorkspaceAdminContext {
  return {
    activeWorkspaceForm: ref(form),
    workspacesSaving: ref(saving),
  } as unknown as WorkspaceAdminContext
}

/** Provides the admin to the production card, as the Workspace overview does in the app. */
function withAdmin(admin: WorkspaceAdminContext): Component {
  return defineComponent({
    setup() {
      provide(WORKSPACE_ADMIN_KEY, admin)
      return () => h(SettingsClaudeTrim)
    },
  })
}

interface Scenario {
  form: Form | null
  saving?: string | null
  options?: ClaudeTrimOption[]
  width?: number
}

/** Each render builds a fresh form, so a toggle in one mount never leaks into the next. */
function trimStory(scenario: () => Scenario): Pick<Story, 'render'> {
  return {
    render: () => {
      const current = scenario()
      return connectedRender({
        component: withAdmin(adminFor(current.form, current.saving ?? null)),
        seed: pinia => { useProjectStore(pinia).claudeTrimOptions = current.options ?? CATALOGUE },
        width: current.width,
      })
    },
  }
}

const meta = {
  id: 'settings-claude-trim',
  title: 'Settings general/SettingsClaudeTrim',
  component: SettingsClaudeTrim,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof SettingsClaudeTrim>
export default meta
type Story = StoryObj<typeof meta>

const emptyForm = (): Form => ({ name: 'storybook-alpha', claude_trim: {}, disallowed_tools: '' })

// The card renders nothing without a catalogue: the same as a catalogue still loading.
export const NoOptions: Story = {
  ...trimStory(() => ({ form: emptyForm(), options: [] })),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.queryByText('Claude context trimming')).toBeNull())
    expect(canvas.queryByRole('switch')).toBeNull()
  },
}

// Without an active workspace form the card has nothing to edit, so it renders nothing.
export const NoWorkspaceForm: Story = {
  ...trimStory(() => ({ form: null })),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.queryByText('Claude context trimming')).toBeNull())
    expect(canvas.queryByRole('switch')).toBeNull()
  },
}

export const Default: Story = {
  ...trimStory(() => ({ form: emptyForm() })),
  play: async ({ canvas }) => {
    expect(await canvas.findByText(SCHEDULING_SUMMARY)).toBeTruthy()
    expect(canvas.getByText('Scheduling · 4 of 4 hidden')).toBeTruthy()
    // The collapsed scheduling rows stay in the tree: four trimmed, two flat rows On.
    expect(canvas.getAllByRole('switch')).toHaveLength(6)
    expect(canvas.getByRole('switch', { name: 'Storybook alpha design tool' }).getAttribute('aria-checked')).toBe('true')
    expect(canvas.getByRole('switch', { name: 'Storybook beta design tool' }).getAttribute('aria-checked')).toBe('true')
    for (const toggle of canvas.getAllByRole('switch', { name: /Storybook cron/ })) expect(toggle.getAttribute('aria-checked')).toBe('false')
    expect(canvas.getByRole('button', { name: 'Reset to defaults' })).toBeDisabled()
  },
}

// A saved override shows the trimmed row Off, and the reset control is live.
export const Edited: Story = {
  ...trimStory(() => ({ form: { ...emptyForm(), claude_trim: { 'tool:storybook-alpha-design': true } } })),
  play: async ({ canvas, userEvent }) => {
    const alpha = await canvas.findByRole('switch', { name: 'Storybook alpha design tool' })
    expect(alpha.getAttribute('aria-checked')).toBe('false')
    const beta = canvas.getByRole('switch', { name: 'Storybook beta design tool' })
    await userEvent.click(beta)
    expect(beta.getAttribute('aria-checked')).toBe('false')
    expect(canvas.getByRole('button', { name: 'Reset to defaults' })).toBeEnabled()
  },
}

// One scheduling row turned on: the group note explains the consequence, and
// the disclosure shows the switch On.
export const SchedulingUnlocked: Story = {
  ...trimStory(() => ({ form: { ...emptyForm(), claude_trim: { 'tool:storybook-cron-a': false } } })),
  play: async ({ canvas, userEvent }) => {
    const summary = await canvas.findByText('Scheduling · 3 of 4 hidden')
    await userEvent.click(summary)
    expect(canvas.getByText(/Ciaobot's own schedules replace these/)).toBeTruthy()
    expect(canvas.getByRole('switch', { name: 'Storybook cron A schedule' }).getAttribute('aria-checked')).toBe('true')
  },
}

// Saving the workspace disables every switch and the reset control.
export const Saving: Story = {
  ...trimStory(() => ({ form: emptyForm(), saving: 'storybook-alpha' })),
  play: async ({ canvas, userEvent }) => {
    const alpha = await canvas.findByRole('switch', { name: 'Storybook alpha design tool' })
    expect(alpha).toBeDisabled()
    expect(canvas.getByRole('button', { name: 'Reset to defaults' })).toBeDisabled()
    await userEvent.click(alpha)
    expect(alpha.getAttribute('aria-checked')).toBe('true')
  },
}

export const KeyboardToggle: Story = {
  ...trimStory(() => ({ form: emptyForm() })),
  play: async ({ canvas, userEvent }) => {
    const alpha = await canvas.findByRole('switch', { name: 'Storybook alpha design tool' })
    await userEvent.tab()
    expect(document.activeElement).toBe(alpha)
    await userEvent.keyboard(' ')
    expect(alpha.getAttribute('aria-checked')).toBe('false')
  },
}

export const Narrow: Story = {
  ...trimStory(() => ({ form: emptyForm(), width: 390 })),
  play: async ({ canvas }) => {
    const alpha = await canvas.findByRole('switch', { name: 'Storybook alpha design tool' })
    expect(alpha.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
