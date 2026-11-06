// @vitest-environment jsdom

import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import SettingsServer from '../settings/SettingsServer.vue'
import type { RoutineSettings } from '../../lib/types'

function mountDeveloper(saveRoutines = vi.fn(async () => {})) {
  const routines = {
    dev_mode: false,
    app_repo: '',
    log_level: '',
    server_defaults: { pwa_host: '0.0.0.0', log_level: 'info', log_levels: ['debug', 'info', 'warning', 'error'] },
  } as unknown as RoutineSettings
  const wrapper = mount(SettingsServer, {
    props: { section: 'developer', routines, routinesSaving: false, saveRoutines },
    attachTo: document.body,
  })
  return { wrapper, saveRoutines }
}

describe('Settings → Advanced → Developer', () => {
  it('is collapsed on arrival: its fields are not in the page', () => {
    const { wrapper } = mountDeveloper()
    const toggle = wrapper.get('button.developer-toggle')
    expect(toggle.text()).toBe('Developer')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('[role="switch"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Source checkout')
    expect(wrapper.text()).not.toContain('Log level')
    expect(wrapper.find('input').exists()).toBe(false)
    expect(wrapper.find('select').exists()).toBe(false)
    wrapper.unmount()
  })

  it('reveals Developer mode, Source checkout and Log level when expanded', async () => {
    const { wrapper } = mountDeveloper()
    const toggle = wrapper.get('button.developer-toggle')
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(toggle.attributes('aria-controls')).toBe('developer-panel')
    expect(wrapper.find('#developer-panel').exists()).toBe(true)
    expect(wrapper.get('[role="switch"]').attributes('aria-label')).toBe('Developer mode')
    expect(wrapper.text()).toContain('Source checkout')
    expect(wrapper.text()).toContain('Log level')
    wrapper.unmount()
  })

  it('collapses again and drops the fields', async () => {
    const { wrapper } = mountDeveloper()
    const toggle = wrapper.get('button.developer-toggle')
    await toggle.trigger('click')
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('#developer-panel').exists()).toBe(false)
    wrapper.unmount()
  })

  it('is operable from the keyboard: the toggle is a real button', async () => {
    const { wrapper } = mountDeveloper()
    const toggle = wrapper.get('button.developer-toggle')
    expect(toggle.element.tagName).toBe('BUTTON')
    expect(toggle.attributes('type')).toBe('button')
    // The heading still names the card.
    expect(wrapper.get('h2').text()).toBe('Developer')
    wrapper.unmount()
  })

  it('saves from the expanded fields exactly as before', async () => {
    const { wrapper, saveRoutines } = mountDeveloper()
    await wrapper.get('button.developer-toggle').trigger('click')
    await wrapper.get('[role="switch"]').trigger('click')
    expect(saveRoutines).toHaveBeenCalledWith({ dev_mode: true })
    await wrapper.get('input').setValue('/Users/me/ciaobot')
    await nextTick()
    await wrapper.get('.checkout-row button').trigger('click')
    expect(saveRoutines).toHaveBeenCalledWith({ app_repo: '/Users/me/ciaobot' })
    wrapper.unmount()
  })
})
