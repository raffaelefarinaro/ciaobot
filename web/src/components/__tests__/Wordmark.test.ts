// @vitest-environment jsdom

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import Wordmark from '../Wordmark.vue'

describe('Wordmark', () => {
  it('renders the supplied brand artwork for both themes under the accessible name Ciaobot', () => {
    const wrapper = mount(Wordmark)

    expect(wrapper.attributes('role')).toBe('img')
    expect(wrapper.attributes('aria-label')).toBe('Ciaobot')

    const images = wrapper.findAll('img')
    expect(images).toHaveLength(2)
    const onDark = wrapper.find('.wordmark-img--on-dark')
    const onLight = wrapper.find('.wordmark-img--on-light')
    expect(onDark.attributes('src')).toMatch(/brand\/ciaobot-wordmark-light\.png$/)
    expect(onLight.attributes('src')).toMatch(/brand\/ciaobot-wordmark\.png$/)
    for (const img of images) {
      expect(img.attributes('decoding')).toBe('async')
      expect(img.attributes('aria-hidden')).toBe('true')
    }
  })

  it('renders no text or accent dots: the image is the whole mark', () => {
    const wrapper = mount(Wordmark)

    expect(wrapper.text()).toBe('')
    expect(wrapper.find('.wordmark-dot').exists()).toBe(false)
  })

  it('maps each size to an image height and reserves the matching width for the 1402:324 artwork', () => {
    const expected = { lg: [28, 121], md: [22, 95], sm: [16, 69] } as const
    for (const size of ['lg', 'md', 'sm'] as const) {
      const wrapper = mount(Wordmark, { props: { size } })
      const [height, width] = expected[size]
      expect(wrapper.classes()).toContain(`wordmark--${size}`)
      const img = wrapper.find('.wordmark-img--on-dark')
      expect(img.attributes('height')).toBe(String(height))
      expect(img.attributes('width')).toBe(String(width))
    }
  })
})
