// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsRadioGroup from '../settings/SettingsRadioGroup.vue'

const options = [
  { value: 'dark', label: 'Dark' },
  { value: 'light', label: 'Light' },
  { value: 'system', label: 'System', fontFamily: 'var(--font-sans)' },
]
const wrappers: ReturnType<typeof mount>[] = []
function render(modelValue: string | null = 'light') {
  const wrapper = mount(SettingsRadioGroup, { attachTo: document.body, props: { label: 'Theme', options, modelValue } })
  wrappers.push(wrapper)
  return wrapper
}
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()) })

describe('SettingsRadioGroup', () => {
  it('labels the group and radios, with only the checked option in the tab order', () => {
    const wrapper = render()
    expect(wrapper.get('[role="radiogroup"]').attributes('aria-label')).toBe('Theme')
    const radios = wrapper.findAll('[role="radio"]')
    expect(radios.map((radio) => radio.text())).toEqual(['Dark', 'Light', 'System'])
    expect(radios.map((radio) => radio.attributes('type'))).toEqual(['button', 'button', 'button'])
    expect(radios.map((radio) => radio.attributes('aria-checked'))).toEqual(['false', 'true', 'false'])
    expect(radios.map((radio) => radio.attributes('tabindex'))).toEqual(['-1', '0', '-1'])
    expect(radios[1].classes()).toContain('active')
    expect(radios[0].attributes('style')).toBeUndefined()
    expect((radios[2].element as HTMLButtonElement).style.fontFamily).toBe('var(--font-sans)')
  })

  it('emits clicks without changing parent-owned selection, then follows prop updates', async () => {
    const wrapper = render()
    await wrapper.findAll('[role="radio"]')[0].trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([['dark']])
    expect(wrapper.findAll('[aria-checked="true"]').map((radio) => radio.text())).toEqual(['Light'])
    await wrapper.setProps({ modelValue: 'dark' })
    expect(wrapper.findAll('[aria-checked="true"]').map((radio) => radio.text())).toEqual(['Dark'])
    expect(wrapper.findAll('[tabindex="0"]').map((radio) => radio.text())).toEqual(['Dark'])
  })

  it('starts null selection at the first tab stop without claiming a checked option', async () => {
    const wrapper = render(null)
    expect(wrapper.findAll('[aria-checked="true"]')).toHaveLength(0)
    expect(wrapper.findAll('.active')).toHaveLength(0)
    expect(wrapper.findAll('[tabindex="0"]').map((radio) => radio.text())).toEqual(['Dark'])
    await wrapper.get('[role="radiogroup"]').trigger('keydown', { key: 'ArrowRight' })
    expect(document.activeElement?.textContent?.trim()).toBe('Light')
    expect(wrapper.emitted('update:modelValue')).toEqual([['light']])
  })

  it.each([
    ['ArrowRight', 2, 0], ['ArrowDown', 2, 0],
    ['ArrowLeft', 0, 2], ['ArrowUp', 0, 2],
    ['ArrowRight', 0, 1], ['ArrowDown', 0, 1],
    ['ArrowLeft', 2, 1], ['ArrowUp', 2, 1],
    ['Home', 2, 0], ['End', 0, 2],
  ])('%s from %i focuses and emits option %i, including wraparound', async (key, from, to) => {
    const wrapper = render()
    const radios = wrapper.findAll('[role="radio"]')
    ;(radios[from].element as HTMLButtonElement).focus()
    const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
    radios[from].element.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(radios[to].element)
    expect(wrapper.emitted('update:modelValue')).toEqual([[options[to].value]])
    // Focus, not the parent's checked value, is the navigation origin.
    expect(radios[1].attributes('aria-checked')).toBe('true')
    await wrapper.setProps({ modelValue: options[to].value })
    expect(wrapper.findAll('[tabindex="0"]')[0].element).toBe(radios[to].element)
  })

  it.each(['altKey', 'ctrlKey', 'metaKey'])('leaves %s chords to the host', (modifier) => {
    const wrapper = render()
    const radio = wrapper.findAll('[role="radio"]')[1]
    ;(radio.element as HTMLButtonElement).focus()
    for (const key of ['ArrowRight', 'ArrowLeft', 'ArrowUp', 'ArrowDown', 'Home', 'End']) {
      const event = new KeyboardEvent('keydown', { key, [modifier]: true, bubbles: true, cancelable: true })
      radio.element.dispatchEvent(event)
      expect(event.defaultPrevented).toBe(false)
      expect(document.activeElement).toBe(radio.element)
    }
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('does not consume unrelated keys', () => {
    const wrapper = render()
    for (const key of ['Tab', 'Escape', 'a']) {
      const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
      wrapper.element.dispatchEvent(event)
      expect(event.defaultPrevented).toBe(false)
    }
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })
})
