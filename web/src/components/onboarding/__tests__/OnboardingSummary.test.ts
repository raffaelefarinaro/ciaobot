// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import OnboardingSummary from '../OnboardingSummary.vue'

describe('OnboardingSummary', () => {
  it('renders observed, deferred, and unknown values as plain text', () => {
    const wrapper = mount(OnboardingSummary, { props: { loading: false, items: [
      { id: 'location', label: 'Folder', value: 'Configured', state: 'ready' },
      { id: 'backup', label: 'Backup', value: 'Not set up', state: 'deferred' },
      { id: 'care', label: 'Care', value: '<b>Unread</b>', state: 'unknown' },
    ] } })
    expect(wrapper.text()).toContain('Configured')
    expect(wrapper.text()).toContain('<b>Unread</b>')
    expect(wrapper.find('b').exists()).toBe(false)
    expect(wrapper.text()).toContain('deferred')
  })
})
