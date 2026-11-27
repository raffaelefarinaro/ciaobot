import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { ref } from 'vue'
import ModelSelector, { type ModelSection } from '../components/ModelSelector.vue'
import PermissionModeMenu from '../components/PermissionModeMenu.vue'

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

// A realistic composer catalog: Claude tiers and ids, then opencode routes with
// long provider-prefixed ids. Synthetic names only.
const CLAUDE_MODELS = ['default', 'opus', 'sonnet', 'haiku', 'claude-opus-4-1', 'claude-sonnet-4-5', 'claude-haiku-3-5', 'claude-3-opus-20240229']
const OPENCODE_MODELS = [
  'openrouter/anthropic/claude-sonnet-4.5-with-an-unusually-long-model-identifier',
  'openai/gpt-5.6', 'openai/gpt-5.5-mini', 'ollama-cloud/glm-5.3-flash', 'ollama-cloud/kimi-k2.7-code:cloud',
  'google/gemini-3-pro', 'deepseek/deepseek-v4', 'mistral/devstral-2',
]
const MANY_SECTIONS: ModelSection[] = [
  { key: 'anthropic', label: 'Claude (Anthropic)', models: CLAUDE_MODELS, modelBadges: { sonnet: ['Default'] }, modelLabels: { default: 'Default', opus: 'Opus', sonnet: 'Sonnet', haiku: 'Haiku' } },
  { key: 'opencode', label: 'opencode', models: OPENCODE_MODELS, modelBadges: { 'openai/gpt-5.6': ['Default'] } },
]

/** The composer's picker: the triggerless popover with the Permission mode menu in its footer. */
const composerRender: Story['render'] = (args) => ({
  components: { ModelSelector, PermissionModeMenu },
  setup() {
    const mode = ref('auto')
    return { args, mode, modes: ['normal', 'auto', 'bypass', 'plan'] }
  },
  template: `<div style="position: fixed; left: 24px; bottom: 24px; width: 200px; height: 36px">
    <ModelSelector v-bind="args">
      <template #footer>
        <div style="display: grid; grid-template-columns: max-content minmax(0, 1fr); align-items: center; gap: 8px 12px">
          <span id="story-mode-label" style="font-size: var(--text-sm); color: var(--fg2)">Permission mode</span>
          <PermissionModeMenu :model-value="mode" :modes="modes" labelled-by="story-mode-label" @select="mode = $event" />
        </div>
      </template>
    </ModelSelector>
  </div>`,
})

export const ComposerManyModels: Story = {
  args: { triggerless: true, placement: 'top-start', modelValue: 'sonnet', sections: MANY_SECTIONS },
  render: composerRender,
  play: async ({ canvas }) => {
    expect(canvas.getByText('Claude (Anthropic)')).toBeTruthy()
    expect(canvas.getByText('opencode')).toBeTruthy()
    expect(canvas.getByRole('option', { name: /Sonnet/ }).getAttribute('aria-selected')).toBe('true')
    // The list is the roomy part: it holds far more than a hundred pixels of rows.
    const list = document.querySelector<HTMLElement>('.model-selector__list')!
    expect(list.getBoundingClientRect().height).toBeGreaterThan(240)
  },
}
export const ComposerLight: Story = {
  ...ComposerManyModels,
  globals: { theme: 'light' },
}
export const ComposerLongNames: Story = {
  args: { triggerless: true, placement: 'top-start', modelValue: OPENCODE_MODELS[0], sections: [MANY_SECTIONS[1]] },
  render: composerRender,
  play: async ({ canvas }) => {
    const option = canvas.getByRole('option', { name: /unusually-long/ })
    expect(option.getAttribute('title')).toBe(OPENCODE_MODELS[0])
    const popover = document.querySelector<HTMLElement>('.model-selector__popover')!
    expect(popover.scrollWidth).toBeLessThanOrEqual(popover.clientWidth)
  },
}
export const ComposerFilteredEmpty: Story = {
  args: { triggerless: true, placement: 'top-start', modelValue: 'sonnet', sections: MANY_SECTIONS },
  render: composerRender,
  play: async ({ canvas, userEvent }) => {
    await userEvent.type(canvas.getByPlaceholderText('Search models'), 'no-such-model')
    expect(canvas.getByText('No models match "no-such-model"')).toBeTruthy()
    // The footer controls stay usable with an empty list.
    expect(canvas.getByRole('button', { name: /Permission mode/ })).toBeTruthy()
  },
}
export const ComposerPhoneSheet: Story = {
  globals: { viewport: { value: 'narrow' } },
  args: { triggerless: true, mobileSheet: true, placement: 'top-start', modelValue: 'sonnet', sections: MANY_SECTIONS },
  render: composerRender,
  play: async ({ canvas }) => {
    expect(canvas.getByText('Choose model')).toBeTruthy()
    const popover = document.querySelector<HTMLElement>('.model-selector__popover')!
    expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(window.innerWidth)
    expect(popover.scrollWidth).toBeLessThanOrEqual(popover.clientWidth)
  },
}
export const ComposerPhoneSheetLight: Story = {
  ...ComposerPhoneSheet,
  globals: { viewport: { value: 'narrow' }, theme: 'light' },
}
export const TypeToFilterFromList: Story = {
  args: { triggerless: true, modelValue: 'sonnet', sections: MANY_SECTIONS },
  play: async ({ canvas, userEvent }) => {
    const search = canvas.getByPlaceholderText('Search models')
    await userEvent.click(search)
    await userEvent.keyboard('{ArrowDown}')
    expect(document.activeElement?.getAttribute('role')).toBe('option')
    await userEvent.keyboard('g')
    await waitFor(() => expect(document.activeElement).toBe(search))
    expect((search as HTMLInputElement).value).toBe('g')
    await userEvent.keyboard('{ArrowDown}{End}')
    expect(document.activeElement?.getAttribute('role')).toBe('option')
  },
}
