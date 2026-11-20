// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import StartupView from '../StartupView.vue'

describe('StartupView', () => {
  it('names the connection without inventing boot progress before a status arrives', async () => {
    const wrapper = mount(StartupView, { props: { phases: [], overallReady: false } })
    expect(wrapper.get('h1').text()).toBe('Connecting to Ciaobot')
    expect(wrapper.text()).toContain('Checking the connection to your workspace.')
    expect(wrapper.find('.startup-phases').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('0%')

    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('skip')).toHaveLength(1)
  })

  it('shows only phases actually reported by the engine, with readable statuses', () => {
    const wrapper = mount(StartupView, {
      props: {
        overallReady: false,
        phases: [
          { name: 'refresh_vault_index', status: 'done', message: '', started_at: null, finished_at: null },
          { name: 'update_skills', status: 'in_progress', message: '', started_at: null, finished_at: null },
        ],
      },
    })
    expect(wrapper.text()).toContain('Getting your workspace ready.')
    expect(wrapper.get('.startup-phases').text()).toContain('Preparing your notesReady')
    expect(wrapper.get('.startup-phases').text()).toContain('Preparing skillsIn progress')
    expect(wrapper.find('.startup-wait').exists()).toBe(false)
  })

  it('shows a skipped provider as neutral Skipped, never as Needs attention', () => {
    const wrapper = mount(StartupView, {
      props: {
        overallReady: true,
        phases: [
          { name: 'connect_codex', status: 'skipped', message: 'Codex and its ACP adapter are not both installed on this machine.', started_at: null, finished_at: null },
        ],
      },
    })
    const phase = wrapper.get('.startup-phase')
    expect(phase.classes()).toContain('is-skipped')
    expect(phase.get('.startup-phase-status').text()).toBe('Skipped')
    expect(wrapper.text()).not.toContain('Needs attention')
  })
})
