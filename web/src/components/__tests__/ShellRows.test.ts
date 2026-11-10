// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import type { RunningSubagent } from '../../lib/types'
import ShellRows from '../ShellRows.vue'

const NOW = Date.parse('2026-10-06T10:10:00Z')

function shell(id: string, over: Partial<RunningSubagent> = {}): RunningSubagent {
  return {
    agent_id: `task:${id}`, description: `cmd ${id}`, subagent_type: 'shell',
    started_at: new Date(NOW - 125_000).toISOString(), stoppable: true, ...over,
  }
}

function mountRows(props: Record<string, unknown> = {}) {
  return mount(ShellRows, {
    attachTo: document.body,
    props: { rows: [shell('b1')], now: NOW, variant: 'rail', stopTask: vi.fn().mockResolvedValue(undefined), ...props },
  })
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('ShellRows', () => {
  it('lists each shell with its label and whole-minute age, never as a link', () => {
    const wrapper = mountRows({ rows: [shell('b1'), shell('b2', { started_at: '', description: '' })] })
    const labels = wrapper.findAll('.shell-row-label').map(l => l.text())
    expect(labels[0]).toBe('cmd b1')
    expect(wrapper.findAll('.shell-row-age').map(a => a.text())).toEqual(['2m'])
    expect(wrapper.find('a').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders the rail label as plain text and the popover label as a button that opens details', async () => {
    const rail = mountRows()
    expect(rail.find('button.shell-row-main').exists()).toBe(false)
    rail.unmount()

    const pop = mountRows({ variant: 'popover' })
    await pop.get('button.shell-row-main').trigger('click')
    expect(pop.emitted('open')).toHaveLength(1)
    pop.unmount()
  })

  it('draws a rail row in the row grammar', () => {
    const wrapper = mountRows()
    expect(wrapper.find('.cd-row.shell-row').exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'ActivityGlyph' }).props('kind')).toBe('run')
    expect(wrapper.get('.cd-primary').text()).toBe('cmd b1')
    expect(wrapper.get('.cd-detail').text()).toBe('Shell · 2m')
    expect(wrapper.find('.cd-trail .shell-row-stop').exists()).toBe(true)
    wrapper.unmount()
  })

  it('names a Monitor row by its kind', () => {
    const wrapper = mountRows({ rows: [shell('m1', { subagent_type: 'monitor' })] })
    expect(wrapper.get('.cd-detail').text()).toBe('Monitor · 2m')
    wrapper.unmount()
  })

  it("puts a stop error in the rail row's detail line", () => {
    const wrapper = mountRows({ errors: { b1: 'Could not stop' } })
    const alert = wrapper.get('[role="alert"]')
    expect(alert.classes()).toContain('cd-detail')
    expect(alert.classes()).toContain('cd-detail--error')
    expect(wrapper.text()).not.toContain('Shell ·')
    expect(alert.element.closest('.cd-row')?.querySelector('.shell-row-stop')).not.toBeNull()
    wrapper.unmount()
  })

  it('leaves the popover rows outside the row grammar', () => {
    const wrapper = mountRows({ variant: 'popover' })
    expect(wrapper.find('.cd-row').exists()).toBe(false)
    expect(wrapper.find('button.shell-row-main').exists()).toBe(true)
    wrapper.unmount()
  })

  it('asks for a stop by bare task id and holds no stop state of its own', async () => {
    const stopTask = vi.fn().mockResolvedValue(undefined)
    const wrapper = mountRows({ stopTask })
    const stop = wrapper.get('.shell-row-stop')
    expect(stop.text()).toBe('Stop')
    expect(stop.attributes('aria-label')).toBe('Stop cmd b1')
    await stop.trigger('click')
    expect(stopTask).toHaveBeenCalledWith('b1')
    expect(stop.text()).toBe('Stop')
    wrapper.unmount()
  })

  it('shows Stopping from its stopping prop, keeps focusable (aria-disabled) and ignores presses', async () => {
    const stopTask = vi.fn()
    const wrapper = mountRows({ stopTask, stopping: { b1: true } })
    const stop = wrapper.get('.shell-row-stop')
    expect(stop.text()).toBe('Stopping…')
    expect(stop.attributes('aria-disabled')).toBe('true')
    expect(stop.attributes('disabled')).toBeUndefined()
    await stop.trigger('click')
    expect(stopTask).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('offers no Stop without a stop action or for a row that is not stoppable', () => {
    const none = mountRows({ stopTask: undefined })
    expect(none.find('.shell-row-stop').exists()).toBe(false)
    none.unmount()
    const fixed = mountRows({ rows: [shell('b1', { stoppable: false })] })
    expect(fixed.find('.shell-row-stop').exists()).toBe(false)
    expect(fixed.find('.shell-row-age').exists()).toBe(true)
    fixed.unmount()
  })

  it('shows a row error from its errors prop', () => {
    const wrapper = mountRows({ errors: { b1: 'Could not stop this process. Try again.' } })
    expect(wrapper.get('[role="alert"]').text()).toContain('Could not stop')
    expect(wrapper.get('.shell-row-stop').text()).toBe('Stop')
    wrapper.unmount()
  })

  it('keeps Stop a 44px touch target on a coarse pointer', async () => {
    const { readFileSync } = await import('node:fs')
    const { join } = await import('node:path')
    const source = readFileSync(join(__dirname, '..', 'ShellRows.vue'), 'utf8')
    expect(source).toMatch(/@media \(pointer: coarse\)[\s\S]*\.shell-row-stop[\s\S]*var\(--touch\)/)
  })
})
