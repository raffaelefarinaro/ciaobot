// @vitest-environment jsdom

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import Wordmark from '../Wordmark.vue'

const here = dirname(fileURLToPath(import.meta.url))
const appCss = readFileSync(join(here, '../../styles/app.css'), 'utf8')
const dotRule = /\.wordmark-dot \{([^}]*)\}/.exec(appCss)?.[1] ?? ''

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

  // The dot stands in for the o, so it must match the monospace x-height and
  // the logo's orange, never the workspace accent (a neutral workspace's
  // accent is grey, which turned the startup and login wordmark into grey dots).
  it('draws the o as a brand-orange dot at x-height, not the workspace accent', () => {
    expect(dotRule).toMatch(/background:\s*var\(--brand-dot\)/)
    expect(dotRule).not.toMatch(/var\(--accent\)/)
    expect(dotRule).toMatch(/width:\s*0\.5[0-9]*em/)
    expect(dotRule).toMatch(/height:\s*0\.5[0-9]*em/)
    expect(appCss).toMatch(/:root\s*\{[^}]*--brand-dot:\s*#[0-9a-f]{6}/i)
  })
})
