import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, fn, waitFor } from 'storybook/test'
import SettingsServer from '../components/settings/SettingsServer.vue'
import type { RoutineSettings } from '../lib/types'

/**
 * SettingsServer (the `components-settings-settings-server` inventory row,
 * #3307): the Network access and Developer cards, selected by `section`. It
 * takes props only, so these stories render the production component with
 * synthetic routine settings and a spy in place of persistence.
 */

const baseRoutines: RoutineSettings = {
  model_options: { anthropic: [] },
  pwa_host: '',
  log_level: '',
  app_repo: '',
  server_defaults: {
    pwa_host: '0.0.0.0',
    log_level: 'info',
    log_levels: ['debug', 'info', 'warning', 'error'],
  },
  server_running: { pwa_host: '0.0.0.0', log_level: 'info' },
}

const meta = {
  id: 'settings-server',
  title: 'Settings access and memory/SettingsServer',
  component: SettingsServer,
  tags: ['!autodocs'],
  args: { section: 'network', routines: baseRoutines, routinesSaving: false },
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
  render: (args) => ({
    components: { SettingsServer },
    setup() {
      return { args }
    },
    template: '<SettingsServer v-bind="args" />',
  }),
} satisfies Meta<typeof SettingsServer>
export default meta
type Story = StoryObj<typeof meta>

const cardSelect = (canvasElement: HTMLElement) => canvasElement.querySelector('select') as HTMLSelectElement

const developerToggle = (canvasElement: HTMLElement) =>
  canvasElement.querySelector('.developer-toggle') as HTMLButtonElement

export const NetworkDefault: Story = {
  args: { saveRoutines: fn() },
  play: async ({ args, canvasElement, userEvent }) => {
    const select = cardSelect(canvasElement)
    expect(select.value).toBe('')
    expect(select.disabled).toBe(false)
    expect(canvasElement.querySelector('[role="status"]')).toBeNull()
    await userEvent.selectOptions(select, '127.0.0.1')
    expect(args.saveRoutines).toHaveBeenCalledWith({ pwa_host: '127.0.0.1' })
  },
}

export const NetworkRestartPending: Story = {
  args: {
    saveRoutines: fn(),
    routines: { ...baseRoutines, pwa_host: '127.0.0.1' },
  },
  play: async ({ canvasElement }) => {
    expect(cardSelect(canvasElement).value).toBe('127.0.0.1')
    await waitFor(() => expect(canvasElement.querySelector('[role="status"]')?.textContent)
      .toContain('Restart Ciaobot to listen on 127.0.0.1'))
  },
}

export const NetworkCustomHost: Story = {
  args: {
    saveRoutines: fn(),
    routines: {
      ...baseRoutines,
      pwa_host: '192.0.2.44',
      server_running: { pwa_host: '192.0.2.44', log_level: 'info' },
    },
  },
  play: async ({ canvasElement }) => {
    const select = cardSelect(canvasElement)
    expect(select.value).toBe('192.0.2.44')
    expect([...select.options].map((option) => option.textContent?.trim())).toContain('Custom address: 192.0.2.44')
    expect(canvasElement.querySelector('[role="status"]')).toBeNull()
  },
}

export const NetworkUnloaded: Story = {
  args: { saveRoutines: fn(), routines: null },
  play: async ({ canvasElement }) => {
    expect(cardSelect(canvasElement).disabled).toBe(true)
  },
}

export const DeveloperClosed: Story = {
  args: { section: 'developer', saveRoutines: fn() },
  play: async ({ canvasElement, userEvent }) => {
    const toggle = developerToggle(canvasElement)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(canvasElement.querySelector('#developer-panel')).toBeNull()
    await userEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(canvasElement.querySelector('#developer-panel')).toBeTruthy()
  },
}

export const DeveloperOpen: Story = {
  args: { section: 'developer', saveRoutines: fn() },
  play: async ({ args, canvasElement, userEvent }) => {
    await userEvent.click(developerToggle(canvasElement))
    expect(canvasElement.querySelector('[role="switch"]')).toBeNull()
    const save = [...canvasElement.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Save') as HTMLButtonElement
    expect(save.disabled).toBe(true)
    const checkout = canvasElement.querySelector('#developer-panel input[type="text"]') as HTMLInputElement
    await userEvent.type(checkout, '/src/ciaobot')
    await userEvent.click(save)
    expect(args.saveRoutines).toHaveBeenCalledWith({ app_repo: '/src/ciaobot' })
  },
}

export const DeveloperLogLevelPending: Story = {
  args: {
    section: 'developer',
    saveRoutines: fn(),
    routines: { ...baseRoutines, log_level: 'debug' },
  },
  play: async ({ canvasElement, userEvent }) => {
    await userEvent.click(developerToggle(canvasElement))
    await waitFor(() => expect(canvasElement.querySelector('[role="status"]')?.textContent)
      .toContain('Restart Ciaobot to log at debug'))
  },
}

export const DeveloperSaving: Story = {
  args: { section: 'developer', saveRoutines: fn(), routinesSaving: true },
  play: async ({ args, canvasElement }) => {
    expect(developerToggle(canvasElement).getAttribute('aria-expanded')).toBe('false')
    // The toggle itself stays usable while saving; only the persisted controls lock.
    const panelToggle = developerToggle(canvasElement)
    panelToggle.click()
    await waitFor(() => expect(canvasElement.querySelector('#developer-panel')).toBeTruthy())
    const select = cardSelect(canvasElement)
    expect(select.disabled).toBe(true)
    const save = [...canvasElement.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Save') as HTMLButtonElement
    expect(save.disabled).toBe(true)
    save.click()
    expect(args.saveRoutines).not.toHaveBeenCalled()
  },
}
