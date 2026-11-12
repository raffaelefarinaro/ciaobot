// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import ItemTitleRow from '../ItemTitleRow.vue'
import ItemHeaderMenu from '../ItemHeaderMenu.vue'

describe('ItemTitleRow', () => {
  it('is close, then the title, and widens for a rail', async () => {
    const wrapper = mount(ItemTitleRow, {
      props: { closeLabel: 'Close task', rail: true },
      slots: { default: '<span class="pane-title">Plan</span>' },
    })
    const close = wrapper.get('button.close-btn')
    expect(close.classes()).toContain('btn-icon')
    expect(close.attributes('aria-label')).toBe('Close task')
    expect(close.attributes('title')).toBe('Close task')
    expect(close.find('svg').exists()).toBe(true)
    expect(close.text()).toBe('')
    expect(wrapper.classes()).toEqual(expect.arrayContaining(['item-title-row', 'item-title-row--rail']))
    expect(wrapper.get('.pane-title').text()).toBe('Plan')
    await close.trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('hides close on a phone, and the hosts fold their actions with the shared CSS', () => {
    const here = join(__dirname, '..')
    expect(readFileSync(join(here, 'ItemTitleRow.vue'), 'utf8')).toMatch(/@media \(max-width: 767px\)\s*\{\s*\.close-btn \{ display: none; \}/)
    const fold = readFileSync(join(here, 'headerFold.css'), 'utf8')
    expect(fold).toMatch(/min-width: 768px[^}]*\.fold-phone/)
    expect(fold).toMatch(/max-width: 767px[^}]*\.fold-wide/)
    for (const host of ['SchedulePanel.vue', 'ProjectView.vue', 'TaskWorkspaceSurface.vue']) {
      const source = readFileSync(join(here, host), 'utf8')
      expect(source, host).toContain('<style scoped src="./headerFold.css">')
      expect(source, host).toContain('<ItemTitleRow')
    }
    // ProjectView folded its actions with a media-query listener; it is CSS now.
    expect(readFileSync(join(here, 'ProjectView.vue'), 'utf8')).not.toContain('matchMedia(')
    expect(readFileSync(join(here, 'ChatPanel.vue'), 'utf8')).toContain('<ItemTitleRow')
  })
})

describe('ItemHeaderMenu', () => {
  it('names its trigger, and shows it only on a phone when asked', () => {
    const wrapper = mount(ItemHeaderMenu, { props: { label: 'More task actions', phoneOnly: true }, global: { plugins: [createPinia()] } })
    const trigger = wrapper.get('button.item-menu-trigger')
    expect(trigger.attributes('aria-label')).toBe('More task actions')
    expect(trigger.classes()).toContain('item-menu-trigger--phone')
    const plain = mount(ItemHeaderMenu, { props: { label: 'More automation actions' }, global: { plugins: [createPinia()] } })
    expect(plain.get('button.item-menu-trigger').classes()).not.toContain('item-menu-trigger--phone')
  })
})
