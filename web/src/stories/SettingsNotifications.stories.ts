import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { onUnmounted } from 'vue'
import SettingsNotifications from '../components/settings/SettingsNotifications.vue'

// The workshop has no service worker and no push subscription, so the owner
// reads "off on this device" and makes no request. No routes are declared here,
// so any request would fail the story's fixture fence.

/**
 * Pins `Notification.permission` for the mount and restores it on teardown. The
 * browser runner's own permission is not a fixed value, so the story states it.
 */
function withPermission(value: NotificationPermission) {
  return () => ({
    components: { SettingsNotifications },
    setup() {
      const original = Object.getOwnPropertyDescriptor(Notification, 'permission')
      Object.defineProperty(Notification, 'permission', { configurable: true, get: () => value })
      onUnmounted(() => {
        if (original) Object.defineProperty(Notification, 'permission', original)
        else Reflect.deleteProperty(Notification, 'permission')
      })
      return {}
    },
    template: '<SettingsNotifications />',
  })
}

const meta = {
  id: 'settings-notifications',
  title: 'Settings general/SettingsNotifications',
  component: SettingsNotifications,
  render: withPermission('default'),
} satisfies Meta<typeof SettingsNotifications>
export default meta
type Story = StoryObj<typeof meta>

export const Off: Story = {
  play: async ({ canvas }) => {
    expect(await canvas.findByText('Off on this device')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Enable on this device' })).toBeEnabled()
    expect(canvas.queryByRole('alert')).toBeNull()
  },
}

export const PermissionDenied: Story = {
  render: withPermission('denied'),
  play: async ({ canvas }) => {
    expect(await canvas.findByText('Blocked by the system')).toBeTruthy()
    expect(canvas.getByText(/Turn them on in System Settings/)).toBeTruthy()
    expect(canvas.queryByRole('button', { name: 'Enable on this device' })).toBeNull()
  },
}

export const Narrow: Story = {
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  play: async ({ canvas }) => {
    const button = await canvas.findByRole('button', { name: 'Enable on this device' })
    expect(button.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
