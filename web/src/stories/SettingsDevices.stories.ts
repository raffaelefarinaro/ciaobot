import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import SettingsDevices from '../components/settings/SettingsDevices.vue'
import { held, json, ownerPanel, route, settled } from './settingsAccessFixture'

/**
 * SettingsDevices (the `components-settings-settings-devices` inventory row,
 * #3307): the "Other devices" card. It reads `/api/addresses` once on mount
 * through the #3267 boundary, so every story declares that one read and
 * nothing else. Addresses are synthetic (documentation and reserved ranges).
 */
const meta = {
  id: 'settings-devices',
  title: 'Settings access and memory/SettingsDevices',
  component: SettingsDevices,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof SettingsDevices>
export default meta
type Story = StoryObj<typeof meta>

const trusted = {
  url: 'https://storybook-alpha.example.invalid:8443/',
  kind: 'trusted',
  source: 'tailscale',
  secure: true,
  loopback: false,
}
const lan = { url: 'http://192.0.2.10:8443/', kind: 'lan', secure: false, loopback: false }
const loopback = { url: 'http://127.0.0.1:8443/', kind: 'loopback', secure: false, loopback: true }

const listing = (addresses: unknown[]) => ({ port: 8443, addresses })

const addresses = (respond: () => Response | Promise<never>) => [route('GET', '/api/addresses', respond)]

const panel = () => ownerPanel({ label: 'SettingsDevices', component: SettingsDevices, clipboard: true })

export const Default: Story = {
  parameters: { fixture: { routes: addresses(() => json(listing([trusted, lan, loopback]))) } },
  render: panel,
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelectorAll('.device-row').length).toBe(3))
    expect(canvasElement.querySelector('.device-row[data-kind="trusted"]')).toBeTruthy()
    expect(canvasElement.querySelector('.device-row[data-kind="loopback"] .device-qr')).toBeNull()
    expect(canvasElement.textContent).toContain('via Tailscale Serve')
    expect(canvasElement.textContent).not.toContain('For the full app on other devices')
  },
}

export const NoTailscale: Story = {
  parameters: { fixture: { routes: addresses(() => json(listing([lan, loopback]))) } },
  render: panel,
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelectorAll('.device-row').length).toBe(2))
    expect(canvasElement.textContent).toContain('For the full app on other devices')
    expect(canvasElement.textContent).toContain('tailscale serve --bg')
  },
}

export const QrOpen: Story = {
  parameters: { fixture: { routes: addresses(() => json(listing([trusted, lan, loopback]))) } },
  render: panel,
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelectorAll('.device-row').length).toBe(3))
    const qr = canvasElement.querySelector('.device-row[data-kind="trusted"] .device-actions button:nth-child(2)') as HTMLButtonElement
    expect(qr.getAttribute('aria-expanded')).toBe('false')
    await userEvent.click(qr)
    expect(qr.getAttribute('aria-expanded')).toBe('true')
    expect(qr.textContent?.trim()).toBe('Hide QR')
    expect(canvasElement.querySelector('.device-qr-code svg')).toBeTruthy()
  },
}

export const Copied: Story = {
  parameters: { fixture: { routes: addresses(() => json(listing([trusted, lan, loopback]))) } },
  render: panel,
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelectorAll('.device-row').length).toBe(3))
    const copy = canvasElement.querySelector('.device-row[data-kind="lan"] .device-actions button') as HTMLButtonElement
    expect(copy.textContent?.trim()).toBe('Copy')
    await userEvent.click(copy)
    await waitFor(() => expect(copy.textContent?.trim()).toBe('Copied'))
  },
}

export const Loading: Story = {
  parameters: { fixture: { routes: addresses(held) } },
  render: panel,
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Looking up addresses'))
    expect(canvasElement.querySelector('.device-list')).toBeNull()
    expect(canvasElement.querySelector('[role="alert"]')).toBeNull()
  },
}

export const Failed: Story = {
  parameters: {
    fixture: { routes: addresses(() => json({ error: 'Synthetic address lookup refused' }, 500)) },
  },
  render: panel,
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Could not read addresses: Synthetic address lookup refused'))
    expect(canvasElement.querySelector('.device-list')).toBeNull()
  },
}

export const Empty: Story = {
  parameters: { fixture: { routes: addresses(() => json(listing([]))) } },
  render: panel,
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('.device-list')).toBeTruthy())
    expect(canvasElement.querySelectorAll('.device-row').length).toBe(0)
    expect(canvasElement.textContent).toContain('For the full app on other devices')
  },
}
