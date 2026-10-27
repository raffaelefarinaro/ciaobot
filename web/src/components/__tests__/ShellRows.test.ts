// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
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
  vi.useRealTimers()
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

  it('stops by bare task id, shows Stopping and ignores a second press', async () => {
    const stopTask = vi.fn().mockResolvedValue(undefined)
    const wrapper = mountRows({ stopTask })
    const stop = wrapper.get('.shell-row-stop')
    expect(stop.text()).toBe('Stop')
    expect(stop.attributes('aria-label')).toBe('Stop cmd b1')
    await stop.trigger('click')
    await flushPromises()
    expect(stopTask).toHaveBeenCalledWith('b1')
    expect(stop.text()).toBe('Stopping…')
    expect(stop.attributes('aria-disabled')).toBe('true')
    expect(stop.attributes('disabled')).toBeUndefined()
    await stop.trigger('click')
    expect(stopTask).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('notice')?.[0]).toEqual(['Stopping cmd b1'])
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

  it('shows an error and re-enables Stop when stopping fails', async () => {
    const wrapper = mountRows({ stopTask: vi.fn().mockRejectedValue(new Error('x')) })
    await wrapper.get('.shell-row-stop').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('Could not stop')
    expect(wrapper.get('.shell-row-stop').text()).toBe('Stop')
    expect(wrapper.get('.shell-row-stop').attributes('aria-disabled')).toBeUndefined()
    expect(wrapper.emitted('notice')?.at(-1)).toEqual([''])
    wrapper.unmount()
  })

  it('releases the Stopping lock after 15 s so Stop can be retried', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const stopTask = vi.fn().mockResolvedValue(undefined)
    const wrapper = mountRows({ stopTask })
    await wrapper.get('.shell-row-stop').trigger('click')
    await flushPromises()
    vi.advanceTimersByTime(15_000)
    await flushPromises()
    expect(wrapper.get('.shell-row-stop').text()).toBe('Stop')
    await wrapper.get('.shell-row-stop').trigger('click')
    expect(stopTask).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('drops the Stopping state and its notice when the row leaves the poll', async () => {
    const wrapper = mountRows()
    await wrapper.get('.shell-row-stop').trigger('click')
    await flushPromises()
    await wrapper.setProps({ rows: [] })
    expect(wrapper.find('.shell-row').exists()).toBe(false)
    expect(wrapper.emitted('notice')?.at(-1)).toEqual([''])
    wrapper.unmount()
  })

  it('keeps Stop a 44px touch target on a coarse pointer', async () => {
    const { readFileSync } = await import('node:fs')
    const { join } = await import('node:path')
    const source = readFileSync(join(__dirname, '..', 'ShellRows.vue'), 'utf8')
    expect(source).toMatch(/@media \(pointer: coarse\)[\s\S]*\.shell-row-stop[\s\S]*var\(--touch\)/)
  })
})
