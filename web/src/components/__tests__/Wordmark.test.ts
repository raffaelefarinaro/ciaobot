// @vitest-environment jsdom

import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import Wordmark from '../Wordmark.vue'

const ON_DARK = /brand\/ciaobot-wordmark-light\.png$/
const ON_LIGHT = /brand\/ciaobot-wordmark\.png$/

/** The theme observer runs asynchronously, so let it see the class change. */
const settle = () => new Promise(resolve => setTimeout(resolve, 0))

describe('Wordmark', () => {
  afterEach(() => {
    document.documentElement.classList.remove('theme-light')
  })

  it('renders one image named Ciaobot, with the artwork ratio reserved', () => {
    const wrapper = mount(Wordmark)

    const images = wrapper.findAll('img')
    expect(images).toHaveLength(1)
    const img = images[0]
    expect(img.attributes('alt')).toBe('Ciaobot')
    expect(img.attributes('decoding')).toBe('async')
    expect(img.attributes('width')).toBe('1402')
    expect(img.attributes('height')).toBe('324')
    expect(wrapper.text()).toBe('')
  })

  it('shows the off-white artwork on the dark default theme', () => {
    const wrapper = mount(Wordmark)
    expect(wrapper.attributes('src')).toMatch(ON_DARK)
  })

  it('switches the single image to the ink artwork when the theme-light class is set', async () => {
    const wrapper = mount(Wordmark)
    expect(wrapper.attributes('src')).toMatch(ON_DARK)

    document.documentElement.classList.add('theme-light')
    await settle()
    await wrapper.vm.$nextTick()
    expect(wrapper.attributes('src')).toMatch(ON_LIGHT)

    document.documentElement.classList.remove('theme-light')
    await settle()
    await wrapper.vm.$nextTick()
    expect(wrapper.attributes('src')).toMatch(ON_DARK)
  })

  it('maps each size to its own class, and the class carries the rendered height', () => {
    for (const size of ['lg', 'md', 'sm'] as const) {
      const wrapper = mount(Wordmark, { props: { size } })
      expect(wrapper.classes()).toContain('wordmark')
      expect(wrapper.classes()).toContain(`wordmark--${size}`)
    }
  })
})
