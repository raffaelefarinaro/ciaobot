// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsSwitch from '../settings/SettingsSwitch.vue'

describe('SettingsSwitch', () => {
  it.each([true, false])('renders the native controlled %s state', async (checked) => {
    const wrapper = mount(SettingsSwitch, { props: { checked } })
    const button = wrapper.get('button.settings-switch')
    expect(wrapper.element).toBe(button.element)
    expect(button.attributes()).toMatchObject({ type: 'button', role: 'switch', 'aria-checked': String(checked) })
    expect(button.text()).toBe(checked ? 'On' : 'Off')
    expect(button.attributes('aria-busy')).toBeUndefined()
    expect(button.find('.switch-track').attributes('aria-hidden')).toBe('true')
    await button.trigger('click')
    expect(wrapper.emitted('toggle')).toEqual([[]])
    expect(button.attributes('aria-checked')).toBe(String(checked))
    await wrapper.setProps({ checked: !checked })
    expect(button.text()).toBe(checked ? 'Off' : 'On')
  })

  it('guards disabled activation, including native programmatic dispatch', async () => {
    const wrapper = mount(SettingsSwitch, { props: { checked: true, disabled: true } })
    const button = wrapper.get('button')
    expect((button.element as HTMLButtonElement).disabled).toBe(true)
    await button.trigger('click')
    button.element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(wrapper.emitted('toggle')).toBeUndefined()
  })

  it('shows busy without inventing a disabled state or suppressing activation', async () => {
    const wrapper = mount(SettingsSwitch, { props: { checked: false, busy: true } })
    const button = wrapper.get('button')
    expect(button.text()).toBe('…')
    expect(button.attributes('aria-busy')).toBe('true')
    expect((button.element as HTMLButtonElement).disabled).toBe(false)
    await button.trigger('click')
    expect(wrapper.emitted('toggle')).toEqual([[]])
    await wrapper.setProps({ busy: false })
    expect(button.attributes('aria-busy')).toBeUndefined()
    expect(button.text()).toBe('Off')
  })

  it('forwards ordinary attributes and native click listeners to the button', async () => {
    const onClick = vi.fn()
    const wrapper = mount(SettingsSwitch, {
      props: { checked: true },
      attrs: { 'aria-label': 'Answer check', 'aria-labelledby': 'label', 'aria-describedby': 'description', class: 'mcp-toggle', id: 'switch', onClick },
    })
    const button = wrapper.get('button')
    expect(button.attributes()).toMatchObject({ 'aria-label': 'Answer check', 'aria-labelledby': 'label', 'aria-describedby': 'description', id: 'switch' })
    expect(button.classes()).toContain('mcp-toggle')
    await button.trigger('click')
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})
