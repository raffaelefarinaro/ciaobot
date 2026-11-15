// @vitest-environment jsdom

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import Wordmark from '../Wordmark.vue'

describe('Wordmark', () => {
  it('renders Ciaobot with both o letters replaced by accent dots and retains its accessible name', () => {
    const wrapper = mount(Wordmark)

    expect(wrapper.attributes('aria-label')).toBe('Ciaobot')
    expect(wrapper.attributes('role')).toBe('img')
    expect(wrapper.findAll('.wordmark-dot')).toHaveLength(2)
    expect(wrapper.findAll('.wordmark-dot').every((dot) => dot.attributes('aria-hidden') === 'true')).toBe(true)
    expect(wrapper.text()).not.toContain('o')
  })

  it('supports legacy size variants while allowing non-brand terminal labels', () => {
    const small = mount(Wordmark, { props: { size: 'sm', text: 'restarting' } })
    const large = mount(Wordmark, { props: { size: 'lg', text: 'ciaobot setup' } })

    expect(small.classes()).toContain('wordmark--sm')
    expect(small.text()).toContain('restarting')
    expect(large.classes()).toContain('wordmark--lg')
    expect(large.findAll('.wordmark-dot')).toHaveLength(2)
    expect(large.attributes('aria-label')).toBe('ciaobot setup')
  })
})
