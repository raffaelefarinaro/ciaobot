// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import TurnVerificationBadge from '../TurnVerificationBadge.vue'
import type { TurnVerification } from '../../lib/types'

const mixed: TurnVerification = {
  supported: 1,
  unsupported: 1,
  general: 1,
  claims: [
    { text: 'Lisbon is the capital', status: 'supported', source: 'notes/geo.md' },
    { text: 'The bridge opened in 1966', status: 'unsupported', source: '' },
    { text: 'Water boils at 100 C', status: 'general', source: '' },
  ],
  sources: ['notes/geo.md'],
}

describe('TurnVerificationBadge', () => {
  it('renders nothing when the answer drew only on general knowledge', () => {
    const wrapper = mount(TurnVerificationBadge, {
      props: { verification: { supported: 0, unsupported: 0, general: 2, claims: [], sources: [] } },
    })
    expect(wrapper.find('.vault-check').exists()).toBe(false)
  })

  it('shows the count with the warning tone when a claim is not in the notes', () => {
    const wrapper = mount(TurnVerificationBadge, { props: { verification: mixed } })
    const toggle = wrapper.get('button.vault-check-toggle')
    expect(toggle.text()).toContain('Vault 1/2')
    expect(toggle.classes()).toContain('vault-check-toggle--warn')
    expect(toggle.attributes('aria-expanded')).toBe('false')
  })

  it('lists each claim with its status in words, source only on the supported one', async () => {
    const wrapper = mount(TurnVerificationBadge, { props: { verification: mixed } })
    await wrapper.get('button.vault-check-toggle').trigger('click')
    expect(wrapper.get('button.vault-check-toggle').attributes('aria-expanded')).toBe('true')
    const items = wrapper.findAll('li.vault-check-claim')
    expect(items).toHaveLength(3)
    expect(items.map(li => li.get('.vault-check-status').text())).toEqual([
      'Supported',
      'Not in your notes',
      'General knowledge',
    ])
    expect(items[0].find('.vault-check-source').text()).toBe('notes/geo.md')
    expect(items[1].find('.vault-check-source').exists()).toBe(false)
    expect(items[2].find('.vault-check-source').exists()).toBe(false)
  })
})
