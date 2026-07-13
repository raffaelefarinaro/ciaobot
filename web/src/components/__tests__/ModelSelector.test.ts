// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import ModelSelector, { type ModelSection } from '../ModelSelector.vue'

const SECTIONS: ModelSection[] = [
  { key: 'anthropic', label: 'Anthropic', models: ['haiku', 'sonnet', 'opus'] },
  { key: 'ollama_cloud', label: 'Ollama cloud', models: ['kimi-k2.7-code:cloud', 'glm-5.2:cloud'] },
  { key: 'ollama_local', label: 'Ollama local', models: ['llama3.1:latest'], badge: 'local' },
]

function mountSelector(props: Record<string, unknown> = {}) {
  return mount(ModelSelector, {
    props: {
      sections: SECTIONS,
      ...props,
    },
  })
}

describe('ModelSelector', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('opens on trigger click and renders sections', async () => {
    const wrapper = mountSelector()
    await wrapper.find('.model-selector__trigger').trigger('click')
    await flushPromises()

    const items = wrapper.findAll('.model-selector__item')
    expect(items.length).toBe(6)
    expect(items.map((el) => el.text())).toContain('glm-5.2:cloud')
  })

  it('emits the selected model in single mode and closes', async () => {
    const wrapper = mountSelector()
    await wrapper.find('.model-selector__trigger').trigger('click')
    await flushPromises()

    const item = wrapper.findAll('.model-selector__item').find((el) => el.text() === 'glm-5.2:cloud')
    await item!.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('update:modelValue')).toEqual([['glm-5.2:cloud']])
    expect(wrapper.emitted('select')).toEqual([['glm-5.2:cloud', 'ollama_cloud']])
    expect(wrapper.find('.model-selector__popover').exists()).toBe(false)
  })

  it('toggles selection in multiple mode', async () => {
    const wrapper = mountSelector({ multiple: true, modelValue: [] })
    await wrapper.find('.model-selector__trigger').trigger('click')
    await flushPromises()

    const items = wrapper.findAll('.model-selector__item')
    // SECTIONS order: haiku, sonnet, opus (Anthropic), kimi-k2.7-code:cloud, glm-5.2:cloud (Ollama cloud).
    const glm = items[4]
    const kimi = items[3]

    await glm.trigger('click')
    await flushPromises()
    // Parent would update modelValue; simulate it so the second click sees the prior selection.
    await wrapper.setProps({ modelValue: ['glm-5.2:cloud'] })
    await flushPromises()

    await kimi.trigger('click')
    await flushPromises()

    const emitted = wrapper.emitted('update:modelValue')
    expect(emitted!.length).toBe(2)
    expect(new Set(emitted![0]![0] as string[])).toEqual(new Set(['glm-5.2:cloud']))
    expect(new Set(emitted![1]![0] as string[])).toEqual(new Set(['glm-5.2:cloud', 'kimi-k2.7-code:cloud']))
    // Popover stays open in multi mode.
    expect(wrapper.find('.model-selector__popover').exists()).toBe(true)
  })

  it('filters sections by search query', async () => {
    const wrapper = mountSelector()
    await wrapper.find('.model-selector__trigger').trigger('click')
    await flushPromises()

    const input = wrapper.find('.model-selector__search')
    await input.setValue('glm')
    await flushPromises()
    await nextTick()

    const visibleSections = wrapper.findAll('.model-selector__section')
    expect(visibleSections.length).toBe(1)
    expect(visibleSections[0].find('.model-selector__section-label').text()).toBe('Ollama cloud')
    expect(wrapper.findAll('.model-selector__item').length).toBe(1)
  })

  it('shows empty state when search has no matches', async () => {
    const wrapper = mountSelector()
    await wrapper.find('.model-selector__trigger').trigger('click')
    await flushPromises()

    await wrapper.find('.model-selector__search').setValue('nope')
    await flushPromises()
    await nextTick()

    expect(wrapper.find('.model-selector__empty').exists()).toBe(true)
  })

  it('closes on Escape', async () => {
    const wrapper = mountSelector()
    await wrapper.find('.model-selector__trigger').trigger('click')
    await flushPromises()

    await wrapper.find('.model-selector__search').trigger('keydown', { key: 'Escape' })
    await flushPromises()

    expect(wrapper.find('.model-selector__popover').exists()).toBe(false)
  })

  it('renders disabled sections with hint', async () => {
    const sections: ModelSection[] = [
      { key: 'openrouter', label: 'OpenRouter', models: ['openai/gpt-5.1'], disabled: true, hint: 'Set API key' },
    ]
    const wrapper = mountSelector({ sections })
    await wrapper.find('.model-selector__trigger').trigger('click')
    await flushPromises()

    expect(wrapper.find('.model-selector__section--disabled').exists()).toBe(true)
    expect(wrapper.find('.model-selector__hint').text()).toBe('Set API key')
  })

  it('renders per-model badges', async () => {
    const sections: ModelSection[] = [
      {
        key: 'ollama',
        label: 'Ollama',
        models: ['llama3.1:latest'],
        modelBadges: { 'llama3.1:latest': ['local', 'Haiku'] },
      },
    ]
    const wrapper = mountSelector({ sections })
    await wrapper.find('.model-selector__trigger').trigger('click')
    await flushPromises()

    const badges = wrapper.findAll('.model-selector__item-badge').map((el) => el.text())
    expect(badges).toEqual(['local', 'Haiku'])
  })

  it('renders and searches a display label while emitting the stored model value', async () => {
    const sections: ModelSection[] = [{
      key: 'codex',
      label: 'OpenAI Codex',
      models: ['fable'],
      modelLabels: { fable: 'gpt-5.6-sol-ultra' },
      modelBadges: { fable: ['Fable'] },
    }]
    const wrapper = mountSelector({ sections })
    await wrapper.setProps({ modelValue: 'fable' })
    expect(wrapper.find('.model-selector__trigger').text()).toContain('gpt-5.6-sol-ultra')
    await wrapper.find('.model-selector__trigger').trigger('click')
    await wrapper.find('.model-selector__search').setValue('sol-ultra')
    await flushPromises()

    const item = wrapper.find('.model-selector__item')
    expect(item.text()).toContain('gpt-5.6-sol-ultra')
    expect(item.text()).toContain('Fable')
    await item.trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([['fable']])
  })

  it('uses explicit active models instead of modelValue when provided', async () => {
    const wrapper = mountSelector({
      modelValue: 'sonnet',
      activeModels: ['kimi-k2.7-code:cloud'],
    })
    await wrapper.find('.model-selector__trigger').trigger('click')
    await flushPromises()

    const activeModels = wrapper.findAll('.ms-item--active').map((el) => el.attributes('data-model'))
    expect(activeModels).toEqual(['kimi-k2.7-code:cloud'])
  })

  it('highlights only the exact explicit active model, not same-tier models from other providers', async () => {
    const sections: ModelSection[] = [
      { key: 'anthropic', label: 'Anthropic', models: ['haiku', 'sonnet', 'opus'] },
      {
        key: 'codex',
        label: 'OpenAI Codex',
        models: ['gpt-5.6-sol'],
        modelBadges: { 'gpt-5.6-sol': ['Opus'] },
      },
    ]
    const wrapper = mountSelector({ sections, activeModels: ['opus'] })
    await wrapper.find('.model-selector__trigger').trigger('click')
    await flushPromises()

    const activeModels = wrapper.findAll('.ms-item--active').map((el) => el.attributes('data-model'))
    expect(activeModels).toEqual(['opus'])
  })

  it('highlights a native model selected through its tier alias badge', async () => {
    const sections: ModelSection[] = [{
      key: 'codex',
      label: 'OpenAI Codex',
      models: ['gpt-5.6-sol'],
      modelBadges: { 'gpt-5.6-sol': ['Opus'] },
    }]
    const wrapper = mountSelector({ sections, modelValue: 'opus' })
    await wrapper.find('.model-selector__trigger').trigger('click')
    await flushPromises()

    expect(wrapper.find('.ms-item--active').attributes('data-model')).toBe('gpt-5.6-sol')
  })

  it('can render as a triggerless popup and emit close', async () => {
    const wrapper = mountSelector({ triggerless: true })
    await flushPromises()

    expect(wrapper.find('.model-selector__trigger').exists()).toBe(false)
    expect(wrapper.find('.model-selector__popover').exists()).toBe(true)

    await wrapper.find('.model-selector__search').trigger('keydown', { key: 'Escape' })
    await flushPromises()

    expect(wrapper.emitted('close')).toEqual([[]])
  })
})
