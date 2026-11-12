// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AppIcon, { type AppIconName } from '../AppIcon.vue'

describe('AppIcon', () => {
  it('draws every Chat details icon as stroked shapes', () => {
    const names: AppIconName[] = ['terminal', 'arrow', 'chevron', 'skill', 'plug', 'folder', 'branch', 'code', 'copy', 'warning', 'task']
    for (const name of names) {
      const wrapper = mount(AppIcon, { props: { name } })
      expect(wrapper.get('svg.app-icon').attributes('aria-hidden'), name).toBe('true')
      expect(wrapper.element.children.length, name).toBeGreaterThan(0)
    }
  })
})
