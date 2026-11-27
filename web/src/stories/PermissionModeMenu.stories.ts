import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { ref } from 'vue'
import PermissionModeMenu from '../components/PermissionModeMenu.vue'
import type { BridgeMode } from '../lib/permissionModes'

const MODES: BridgeMode[] = ['normal', 'auto', 'bypass', 'plan']

const meta = {
  id: 'permission-mode-menu', title: 'Chat/PermissionModeMenu', component: PermissionModeMenu,
  args: { modelValue: 'auto', modes: MODES, labelledBy: 'story-mode-label' },
  render: (args) => ({
    components: { PermissionModeMenu },
    setup() {
      const mode = ref(args.modelValue)
      return { args, mode }
    },
    template: `<div style="padding: 160px 24px 24px; max-width: 420px; display: grid; grid-template-columns: max-content minmax(0, 1fr); align-items: center; gap: 12px">
      <span id="story-mode-label" style="font-size: var(--text-sm); color: var(--fg2)">Permission mode</span>
      <PermissionModeMenu v-bind="args" :model-value="mode" @select="mode = $event" />
    </div>`,
  }),
} satisfies Meta<typeof PermissionModeMenu>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  play: async ({ canvas }) => {
    const button = canvas.getByRole('button', { name: /Permission mode Auto/ })
    expect(button.textContent).toContain('allow safe work, ask on risky')
  },
}
export const PickWithKeyboard: Story = {
  play: async ({ canvas, userEvent }) => {
    const button = canvas.getByRole('button', { name: /Permission mode/ })
    button.focus()
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(screen.getAllByRole('menuitemradio')).toHaveLength(4))
    expect(screen.getByRole('menuitemradio', { name: /Auto/ }).getAttribute('aria-checked')).toBe('true')
    await userEvent.keyboard('{End}{Enter}')
    await waitFor(() => expect(screen.queryAllByRole('menuitemradio')).toHaveLength(0))
    await waitFor(() => expect(document.activeElement).toBe(button))
    expect(button.textContent).toContain('Plan')
  },
}
export const EscapeKeepsMode: Story = {
  play: async ({ canvas, userEvent }) => {
    const button = canvas.getByRole('button', { name: /Permission mode/ })
    await userEvent.click(button)
    await waitFor(() => expect(screen.getAllByRole('menuitemradio')).toHaveLength(4))
    await userEvent.keyboard('{ArrowDown}{Escape}')
    await waitFor(() => expect(screen.queryAllByRole('menuitemradio')).toHaveLength(0))
    await waitFor(() => expect(document.activeElement).toBe(button))
    expect(button.textContent).toContain('Auto')
  },
}
export const Unknown: Story = {
  args: { modelValue: 'future-mode' },
  play: async ({ canvas }) => { expect(canvas.getByRole('button', { name: /Unknown/ })).toBeTruthy() },
}
export const Disabled: Story = {
  args: { disabled: true },
  play: async ({ canvas, userEvent }) => {
    const button = canvas.getByRole('button', { name: /Permission mode/ })
    expect(button.getAttribute('aria-disabled')).toBe('true')
    await userEvent.click(button)
    expect(screen.queryAllByRole('menuitemradio')).toHaveLength(0)
  },
}
export const Light: Story = {
  globals: { theme: 'light' },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { name: /Permission mode/ }))
    await waitFor(() => expect(screen.getAllByRole('menuitemradio')).toHaveLength(4))
  },
}
export const Narrow: Story = {
  globals: { viewport: { value: 'narrow' } },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { name: /Permission mode/ }))
    await waitFor(() => expect(screen.getAllByRole('menuitemradio')).toHaveLength(4))
    for (const item of screen.getAllByRole('menuitemradio')) {
      expect(item.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
      expect(item.getBoundingClientRect().right).toBeLessThanOrEqual(window.innerWidth)
    }
  },
}
