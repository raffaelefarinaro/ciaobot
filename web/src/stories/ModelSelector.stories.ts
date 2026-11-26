import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { ref } from 'vue'
import ModelSelector from '../components/ModelSelector.vue'

const meta = {
  id: 'model-selector', title: 'Chat/ModelSelector', component: ModelSelector,
  args: { sections: [{ key: 'local', label: 'Local', models: ['alpha', 'beta'] }], modelValue: 'alpha' },
} satisfies Meta<typeof ModelSelector>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { name: 'alpha' }))
    await waitFor(() => expect(screen.getByRole('listbox')).toBeTruthy())
    expect(screen.getByText('beta')).toBeTruthy()
  },
}
export const Empty: Story = {
  args: { sections: [], modelValue: '', triggerless: true },
  play: async ({ canvas }) => { expect(canvas.getByText('No models match ""')).toBeTruthy() },
}
export const Disabled: Story = {
  args: { disabled: true },
  play: async ({ canvas }) => { expect((canvas.getByRole('button', { name: 'alpha' }) as HTMLButtonElement).disabled).toBe(true) },
}
export const Multiple: Story = {
  args: { multiple: true, modelValue: ['alpha', 'beta'] },
  play: async ({ canvas }) => { expect(canvas.getByRole('button', { name: 'alpha, beta' })).toBeTruthy() },
}
export const Search: Story = {
  args: { triggerless: true },
  play: async ({ canvas, userEvent }) => {
    await userEvent.type(canvas.getByPlaceholderText('Search models'), 'beta')
    expect(canvas.getByRole('option', { name: 'beta' })).toBeTruthy()
    expect(canvas.queryByRole('option', { name: 'alpha' })).toBeNull()
    await userEvent.clear(canvas.getByPlaceholderText('Search models'))
    await userEvent.type(canvas.getByPlaceholderText('Search models'), 'missing')
    expect(canvas.getByText('No models match "missing"')).toBeTruthy()
  },
}
export const ProviderUnavailable: Story = {
  args: { triggerless: true, sections: [{ key: 'remote', label: 'Remote', models: ['beta'], disabled: true, hint: 'Sign in first' }] },
  play: async ({ canvas }) => {
    expect(canvas.getByText('Sign in first')).toBeTruthy()
    expect((canvas.getByRole('option', { name: 'beta' }) as HTMLButtonElement).disabled).toBe(true)
  },
}
export const Selection: Story = {
  render: (args) => ({
    components: { ModelSelector },
    setup() { const chosen = ref(''); return { args, chosen } },
    template: '<ModelSelector v-bind="args" @select="chosen = $event" /><p data-testid="chosen">{{ chosen }}</p>',
  }),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { name: 'alpha' }))
    await waitFor(() => expect(screen.getByRole('option', { name: 'beta' })).toBeTruthy())
    await userEvent.click(screen.getByRole('option', { name: 'beta' }))
    expect(canvas.getByTestId('chosen').textContent).toBe('beta')
  },
}
export const MobileSheet: Story = {
  globals: { viewport: { value: 'narrow' } },
  args: { triggerless: true, mobileSheet: true, sections: [{ key: 'local', label: 'Local', models: ['alpha'], modelBadges: { alpha: ['Fast'] } }] },
  play: async ({ canvas }) => {
    expect(canvas.getByText('Choose model')).toBeTruthy()
    expect(canvas.getByText('Fast')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Close model picker' })).toBeTruthy()
  },
}
export const Keyboard: Story = {
  play: async ({ canvas, userEvent }) => {
    await userEvent.tab()
    expect(document.activeElement).toBe(canvas.getByRole('button', { name: 'alpha' }))
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(screen.getByPlaceholderText('Search models')).toBe(document.activeElement))
    await userEvent.keyboard('{ArrowDown}')
    expect(document.activeElement?.getAttribute('role')).toBe('option')
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
  },
}
export const LabelsAndFilter: Story = {
  args: {
    triggerless: true, filterSection: 'local', modelValue: 'alpha',
    sections: [{ key: 'local', label: 'Local', models: ['alpha'], modelLabels: { alpha: 'Friendly model' }, modelBadges: { alpha: ['Fast'] } }, { key: 'remote', label: 'Remote', models: ['beta'] }],
  },
  play: async ({ canvas, userEvent }) => {
    const option = canvas.getByRole('option', { name: /Friendly model/ })
    expect(option.getAttribute('aria-selected')).toBe('true')
    expect(option.textContent).toContain('Fast')
    expect(canvas.queryByText('Remote')).toBeNull()
    await userEvent.type(canvas.getByPlaceholderText('Search models'), 'Friendly')
    expect(canvas.getByRole('option', { name: /Friendly model/ })).toBeTruthy()
  },
}
export const LabelledTrigger: Story = {
  args: { labelledBy: 'model-label', sections: [{ key: 'local', label: 'Local', models: ['alpha'], modelLabels: { alpha: 'Friendly model' } }] },
  render: (args) => ({ components: { ModelSelector }, setup: () => ({ args }), template: '<span id="model-label">Default model</span><ModelSelector v-bind="args" />' }),
  play: async ({ canvas }) => { expect(canvas.getByRole('button', { name: 'Default model Friendly model' })).toBeTruthy() },
}
