// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsServer from '../settings/SettingsServer.vue'
import type { RoutineSettings } from '../../lib/types'

const DEFAULTS = {
  server_defaults: { pwa_host: '0.0.0.0', log_level: 'info', log_levels: ['debug', 'info', 'warning', 'error'] },
  server_running: { pwa_host: '0.0.0.0', log_level: 'info' },
}

function mountCard(extra: Partial<RoutineSettings> | null, saveRoutines = vi.fn(() => Promise.resolve())) {
  const routines = extra === null ? null : ({ ...DEFAULTS, ...extra } as RoutineSettings)
  const wrapper = mount(SettingsServer, {
    props: { routines, routinesSaving: false, saveRoutines },
  })
  return { wrapper, saveRoutines }
}

describe('SettingsServer', () => {
  it('saves loopback-only as the bind address', async () => {
    const { wrapper, saveRoutines } = mountCard({ pwa_host: '' })
    const select = wrapper.findAll('select')[0]
    expect((select.element as HTMLSelectElement).value).toBe('')

    await select.setValue('127.0.0.1')
    expect(saveRoutines).toHaveBeenCalledWith({ pwa_host: '127.0.0.1' })
  })

  it('says a saved bind address waits for a restart', () => {
    const { wrapper } = mountCard({ pwa_host: '127.0.0.1' })
    expect(wrapper.text()).toContain('Restart Ciaobot to listen on 127.0.0.1')
  })

  it('keeps a custom address imported from .env selectable', () => {
    const { wrapper } = mountCard({
      pwa_host: '100.64.0.7',
      server_running: { pwa_host: '100.64.0.7', log_level: 'info' },
    })
    const select = wrapper.findAll('select')[0].element as HTMLSelectElement
    expect(select.value).toBe('100.64.0.7')
    expect(wrapper.text()).not.toContain('Restart Ciaobot to listen')
  })

  it('toggles developer mode', async () => {
    const { wrapper, saveRoutines } = mountCard({ dev_mode: false })
    const toggle = wrapper.get('[role="switch"]')
    expect(toggle.attributes('aria-checked')).toBe('false')

    await toggle.trigger('click')
    expect(saveRoutines).toHaveBeenCalledWith({ dev_mode: true })
  })

  it('saves the source checkout and the log level', async () => {
    const { wrapper, saveRoutines } = mountCard({ app_repo: '' })
    await wrapper.get('input[type="text"]').setValue('/src/ciaobot')
    await wrapper.get('.checkout-row button').trigger('click')
    expect(saveRoutines).toHaveBeenCalledWith({ app_repo: '/src/ciaobot' })

    await wrapper.findAll('select')[1].setValue('debug')
    expect(saveRoutines).toHaveBeenCalledWith({ log_level: 'debug' })
  })

  it('is disabled until the settings have loaded', () => {
    const { wrapper } = mountCard(null)
    expect(wrapper.get('[role="switch"]').attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('select')[0].attributes('disabled')).toBeDefined()
  })
})
