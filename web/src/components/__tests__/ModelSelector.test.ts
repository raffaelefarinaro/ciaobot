// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
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

/** Give the host a real rect and the popover a real size, as jsdom reports 0. */
function stubGeometry(
  wrapper: ReturnType<typeof mountSelector>,
  rect: { left: number; right: number; top: number; bottom: number },
  size: { width: number; height: number },
) {
  const host = wrapper.find('.model-selector').element as HTMLElement
  host.getBoundingClientRect = () => ({
    ...rect,
    width: rect.right - rect.left,
    height: rect.bottom - rect.top,
    x: rect.left,
    y: rect.top,
    toJSON: () => ({}),
  }) as DOMRect
  const pop = wrapper.find('.model-selector__popover').element as HTMLElement
  Object.defineProperty(pop, 'offsetWidth', { value: size.width, configurable: true })
  Object.defineProperty(pop, 'offsetHeight', { value: size.height, configurable: true })
}

describe('ModelSelector placement', () => {
  // The popover used to be absolutely positioned inside the selector, so
  // `.chat-panel`'s `overflow: hidden` sliced it off at the pane edge in a
  // split view. It is viewport-positioned now, and clamped so it also cannot
  // simply hang off-screen instead.
  it('keeps a menu wider than its pane inside the viewport', async () => {
    window.innerWidth = 1200
    window.innerHeight = 800
    const wrapper = mountSelector({ triggerless: true, placement: 'bottom-end' })
    await flushPromises()
    // Trigger sits 300px from the left edge; a 400px menu aligned to its right
    // edge would start at -100.
    stubGeometry(wrapper, { left: 280, right: 300, top: 40, bottom: 70 }, { width: 400, height: 200 })
    window.dispatchEvent(new Event('resize'))
    await nextTick()

    const style = (wrapper.find('.model-selector__popover').element as HTMLElement).style
    expect(style.position).toBe('')          // comes from the stylesheet
    expect(parseFloat(style.left)).toBe(8)   // clamped to the viewport margin
    expect(parseFloat(style.top)).toBe(74)   // just under the trigger
    expect(wrapper.find('.model-selector__popover').classes())
      .toContain('model-selector__popover--placed')
  })

  it('flips above the trigger when there is no room below', async () => {
    window.innerWidth = 1200
    window.innerHeight = 800
    const wrapper = mountSelector({ triggerless: true, placement: 'bottom-end' })
    await flushPromises()
    stubGeometry(wrapper, { left: 600, right: 900, top: 600, bottom: 640 }, { width: 400, height: 300 })
    window.dispatchEvent(new Event('resize'))
    await nextTick()

    const style = (wrapper.find('.model-selector__popover').element as HTMLElement).style
    expect(parseFloat(style.top)).toBe(296)  // 600 - 300 - 4
    expect(parseFloat(style.left)).toBe(500) // right-aligned: 900 - 400
  })
})

describe('ModelSelector', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('exposes the full trigger label as a title, since the label ellipsizes', () => {
    expect(
      mountSelector({ multiple: true, modelValue: [], emptyPlaceholder: 'Automatic default (a, b)' })
        .find('.model-selector__trigger').attributes('title'),
    ).toBe('Automatic default (a, b)')
    expect(
      mountSelector({ multiple: true, modelValue: ['haiku', 'glm-5.2:cloud'] })
        .find('.model-selector__trigger').attributes('title'),
    ).toBe('haiku, glm-5.2:cloud')
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
