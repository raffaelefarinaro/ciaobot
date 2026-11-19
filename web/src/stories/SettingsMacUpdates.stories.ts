import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { onUnmounted } from 'vue'
import SettingsMacUpdates from '../components/settings/SettingsMacUpdates.vue'
import { hasMacAppSettings, type MacUpdateSettings } from '../lib/macAppSettings'

// The row reads the Mac app's bridge. The workshop has none, so each story
// installs a stub `ciaobotAppSettings` handler for its mount that answers `get`
// with the state the story pins, and removes it on teardown. Actions are covered
// by the component's unit tests, not by the workshop.

/** Installs a stub bridge that answers with `updates` (null: an app with no
 *  updates block) and restores the page's own `webkit` value on teardown. */
function withBridge(updates: MacUpdateSettings | null) {
  return () => ({
    components: { SettingsMacUpdates },
    setup() {
      const g = globalThis as { webkit?: unknown }
      const original = g.webkit
      const reply = { v: 1, notifications: null, updates }
      g.webkit = { messageHandlers: { ciaobotAppSettings: { postMessage: async () => reply } } }
      onUnmounted(() => {
        if (original === undefined) delete g.webkit
        else g.webkit = original
      })
      return {}
    },
    template: '<div style="max-width: 640px"><SettingsMacUpdates /></div>',
  })
}

const meta = {
  id: 'settings-mac-updates',
  title: 'Settings general/SettingsMacUpdates',
  component: SettingsMacUpdates,
  render: withBridge({ available: true, automatic: true, canCheck: true, reason: '' }),
} satisfies Meta<typeof SettingsMacUpdates>
export default meta
type Story = StoryObj<typeof meta>

export const InstallsAutomatically: Story = {
  play: async ({ canvas }) => {
    expect(await canvas.findByText('Installs updates automatically')).toBeTruthy()
    expect(canvas.getByRole('switch', { name: 'Install Updates Automatically' })).toHaveAttribute('aria-checked', 'true')
    expect(canvas.getByRole('button', { name: 'Check for Updates…' })).toBeEnabled()
  },
}

export const AsksBeforeInstalling: Story = {
  render: withBridge({ available: true, automatic: false, canCheck: true, reason: '' }),
  play: async ({ canvas }) => {
    expect(await canvas.findByText('Asks before installing updates')).toBeTruthy()
    expect(canvas.getByRole('switch', { name: 'Install Updates Automatically' })).toHaveAttribute('aria-checked', 'false')
  },
}

export const Checking: Story = {
  render: withBridge({ available: true, automatic: true, canCheck: false, reason: '' }),
  play: async ({ canvas }) => {
    expect(await canvas.findByText('Checking for updates')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Check for Updates…' })).toBeDisabled()
  },
}

export const Unavailable: Story = {
  render: withBridge({ available: false, automatic: false, canCheck: false, reason: 'Updates are unavailable in a debug build.' }),
  play: async ({ canvas }) => {
    expect(await canvas.findByText('Updates are unavailable')).toBeTruthy()
    expect(canvas.getByText('Updates are unavailable in a debug build.')).toBeTruthy()
    expect(canvas.getByRole('switch', { name: 'Install Updates Automatically' })).toBeDisabled()
    expect(canvas.getByRole('button', { name: 'Check for Updates…' })).toBeDisabled()
  },
}

export const Narrow: Story = {
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  play: async ({ canvas }) => {
    const button = await canvas.findByRole('button', { name: 'Check for Updates…' })
    // The 390px wrapper is the frame: the button must sit inside it, not overflow.
    const frame = (button.closest('[style*="max-width: 390px"]') as HTMLElement).getBoundingClientRect()
    const box = button.getBoundingClientRect()
    expect(box.right).toBeLessThanOrEqual(frame.right)
    expect(box.height).toBeGreaterThanOrEqual(44)
  },
}

/** An app whose bridge answers with no updates block. */
export const NoUpdatesBlock: Story = {
  render: withBridge(null),
  play: async ({ canvasElement }) => {
    // The row renders nothing and the card keeps its static text.
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(canvasElement.textContent?.trim()).toBe('')
  },
}

/** A page with no app bridge at all: the handler is absent, not merely silent. */
export const NoBridge: Story = {
  render: () => ({
    components: { SettingsMacUpdates },
    setup() {
      const g = globalThis as { webkit?: unknown }
      const original = g.webkit
      delete g.webkit
      onUnmounted(() => {
        if (original !== undefined) g.webkit = original
      })
      return {}
    },
    template: '<div style="max-width: 640px"><SettingsMacUpdates /></div>',
  }),
  play: async ({ canvasElement }) => {
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(hasMacAppSettings()).toBe(false)
    expect(canvasElement.textContent?.trim()).toBe('')
  },
}
