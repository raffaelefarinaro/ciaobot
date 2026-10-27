// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { nextTick } from 'vue'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { BackgroundRunSummary, RunningSubagent } from '../../lib/types'
import ComposerActivity from '../ComposerActivity.vue'

const NOW = Date.parse('2026-10-06T10:10:00Z')

function run(id: string, label: string, startedAgoMs: number, over: Partial<BackgroundRunSummary> = {}): BackgroundRunSummary {
  return { run_id: id, label, cmd: ['uv', 'sync'], started_at: new Date(NOW - startedAgoMs).toISOString(), status: 'running', exit_code: null, ...over }
}

const AGENT: RunningSubagent = { agent_id: 'a1', description: 'Explore repo layout', subagent_type: 'Explore' }

function mountBar(props: Record<string, unknown> = {}) {
  return mount(ComposerActivity, {
    attachTo: document.body,
    props: { chatId: 'c1', agentCount: 0, agents: [], runs: [], finished: null, now: NOW, ...props },
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
}

// Reka renders the popover content in its own layer; read it through document.
const popover = () => document.querySelector<HTMLElement>('.composer-activity-popover')
const texts = (sel: string) => Array.from(document.querySelectorAll(sel)).map(el => el.textContent?.trim())

async function openAgents(wrapper: ReturnType<typeof mountBar>) {
  await wrapper.get('.composer-activity-seg--agents').trigger('click')
  await flushPromises()
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('ComposerActivity', () => {
  it('renders nothing but the live region when nothing is running', () => {
    const wrapper = mountBar()
    expect(wrapper.find('.composer-activity').exists()).toBe(true)
    expect(wrapper.findAll('.composer-activity-seg')).toHaveLength(0)
    expect(wrapper.get('[role="status"]').text()).toBe('')
    wrapper.unmount()
  })

  it('counts agents and shells, naming a single run only in its tooltip', () => {
    const one = mountBar({ agentCount: 1 })
    expect(one.get('.composer-activity-seg--agents').text()).toBe('1 agent')
    expect(one.get('.composer-activity-seg--agents').find('.activity-glyph--working').exists()).toBe(true)
    one.unmount()

    const two = mountBar({ agentCount: 2, runs: [run('r1', 'ITTA preview', 319_000)] })
    expect(two.get('.composer-activity-seg--agents').text()).toBe('2 agents')
    const seg = two.get('.composer-activity-seg--runs')
    expect(seg.get('.composer-activity-label').text()).toBe('1 shell')
    expect(seg.attributes('title')).toBe('Background run: ITTA preview')
    expect(seg.find('.activity-glyph--run').exists()).toBe(true)
    two.unmount()
  })

  it('draws a successful run outcome as a run glyph in the ok tone', () => {
    const ok = mountBar({ finished: run('r1', '', 0, { status: 'ok' }) })
    const seg = ok.get('.composer-activity-seg--outcome')
    expect(seg.classes()).toContain('composer-activity-seg--ok')
    expect(seg.find('.activity-glyph--run').exists()).toBe(true)
    ok.unmount()
  })

  it('keeps run outcomes off the semantic palette', () => {
    const source = readFileSync(join(__dirname, '..', 'ComposerActivity.vue'), 'utf8')
    expect(source).not.toMatch(/var\(--(success|error|warning)/)
  })

  it('says how a finished run ended and opens Chat details from it', async () => {
    const failed = mountBar({ finished: run('r1', '', 0, { status: 'error', exit_code: 1 }) })
    const seg = failed.get('.composer-activity-seg--outcome')
    expect(seg.classes()).toContain('composer-activity-seg--error')
    expect(seg.get('.composer-activity-label').text()).toBe('uv sync failed (exit 1)')
    expect(seg.find('.activity-glyph--alert').exists()).toBe(true)
    await seg.trigger('click')
    expect(failed.emitted('open-details')).toHaveLength(1)
    failed.unmount()

    const ok = mountBar({ finished: run('r1', '', 0, { status: 'ok' }) })
    const done = ok.get('.composer-activity-seg--outcome')
    expect(done.classes()).toContain('composer-activity-seg--ok')
    expect(done.get('.composer-activity-label').text()).toBe('uv sync finished')
    ok.unmount()
  })

  it('opens one popover listing agents and processes', async () => {
    const wrapper = mountBar({ agentCount: 1, agents: [AGENT], runs: [run('r1', 'ITTA preview', 60_000)] })
    await openAgents(wrapper)

    const agents = wrapper.get('.composer-activity-seg--agents')
    expect(agents.attributes('aria-expanded')).toBe('true')
    expect(agents.attributes('aria-haspopup')).toBe('dialog')
    expect(wrapper.get('.composer-activity-seg--runs').attributes('aria-expanded')).toBe('false')

    const pop = popover()
    expect(pop).not.toBeNull()
    expect(pop?.getAttribute('role')).toBe('dialog')
    expect(pop?.id).toBe(agents.attributes('aria-controls'))
    expect(texts('.composer-activity-pop-head')).toEqual(['Agents', 'Processes'])
    expect(wrapper.getComponent(RouterLinkStub).props('to')).toBe('/chat/c1/subagent/a1')
    expect(document.querySelector('.composer-activity-pop-foot')?.textContent?.trim()).toBe('Open Chat details →')
    wrapper.unmount()
  })

  it('shows a loading note when the count is ahead of the poll', async () => {
    const wrapper = mountBar({ agentCount: 2, agents: [] })
    await openAgents(wrapper)
    expect(document.querySelector('.composer-activity-pop-note')?.textContent?.trim()).toBe('Details loading…')
    wrapper.unmount()
  })

  it('opens Chat details from a process row and the footer, closing the popover', async () => {
    const wrapper = mountBar({ agentCount: 1, agents: [AGENT], runs: [run('r1', 'ITTA preview', 60_000)] })
    await openAgents(wrapper)
    document.querySelector<HTMLElement>('button.composer-activity-pop-item')?.click()
    await flushPromises()
    expect(wrapper.emitted('open-details')).toHaveLength(1)
    expect(popover()).toBeNull()

    await openAgents(wrapper)
    document.querySelector<HTMLElement>('.composer-activity-pop-foot')?.click()
    await flushPromises()
    expect(wrapper.emitted('open-details')).toHaveLength(2)
    wrapper.unmount()
  })

  it('closes on Escape, claims the key, and returns focus to the trigger', async () => {
    const recorded: boolean[] = []
    const onKey = (e: KeyboardEvent) => recorded.push(e.defaultPrevented)
    window.addEventListener('keydown', onKey)
    const wrapper = mountBar({ agentCount: 1, agents: [AGENT] })
    await openAgents(wrapper)
    expect(popover()).not.toBeNull()

    popover()?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await flushPromises()

    expect(popover()).toBeNull()
    expect(document.activeElement).toBe(wrapper.get('.composer-activity-seg--agents').element)
    // Either the window listener never fired (propagation stopped) or it saw a
    // claimed event: ChatLayout must not close the chat.
    expect(recorded.every(Boolean)).toBe(true)
    window.removeEventListener('keydown', onKey)
    wrapper.unmount()
  })

  it('closes on a pointerdown outside without stealing focus', async () => {
    const wrapper = mountBar({ agentCount: 1, agents: [AGENT] })
    await openAgents(wrapper)
    expect(popover()).not.toBeNull()

    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0 }))
    await flushPromises()
    await nextTick()
    expect(popover()).toBeNull()
    expect(document.activeElement).not.toBe(wrapper.get('.composer-activity-seg--agents').element)
    wrapper.unmount()
  })

  // The stub reports the width of whatever element is observed; the component
  // must observe the bar (its parent), not itself.
  function stubBarWidth(width: number, observed: Element[] = []) {
    vi.stubGlobal('ResizeObserver', class {
      private cb: ResizeObserverCallback
      constructor(cb: ResizeObserverCallback) { this.cb = cb }
      observe(el: Element) {
        observed.push(el)
        this.cb([{ contentRect: { width } } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver)
      }
      disconnect() {}
      unobserve() {}
    })
  }
  const threeRuns = () => [run('r1', 'one', 1000), run('r2', 'two', 1000), run('r3', 'three', 1000)]

  it('folds to glyph and count on a narrow bar', async () => {
    const observed: Element[] = []
    stubBarWidth(340, observed)
    const wrapper = mountBar({ agentCount: 2, runs: threeRuns() })
    await nextTick()
    expect(observed[0]).toBe(wrapper.get('.composer-activity').element.parentElement)
    expect(wrapper.find('.composer-activity-label').exists()).toBe(false)
    expect(wrapper.findAll('.composer-activity-count').map(c => c.text())).toEqual(['2', '3'])
    expect(wrapper.get('.composer-activity-seg--runs').attributes('title')).toBe('3 background runs')
    expect(wrapper.get('.composer-activity').classes()).toContain('composer-activity--compact')
    wrapper.unmount()
  })

  it('keeps the labels on a wide bar', async () => {
    stubBarWidth(900)
    const wrapper = mountBar({ agentCount: 2, runs: threeRuns() })
    await nextTick()
    expect(wrapper.get('.composer-activity').classes()).not.toContain('composer-activity--compact')
    expect(wrapper.get('.composer-activity-seg--runs .composer-activity-label').text()).toBe('3 shells')
    expect(wrapper.get('.composer-activity-seg--agents .composer-activity-label').text()).toBe('2 agents')
    wrapper.unmount()
  })

  it('closes without moving focus when the part that opened it goes away', async () => {
    const wrapper = mountBar({ runs: [run('r1', 'one', 1000), run('r2', 'two', 1000)] })
    const first = wrapper.get('.composer-activity-seg--runs')
    await first.trigger('click')
    await flushPromises()
    expect(popover()).not.toBeNull()
    const other = document.createElement('button')
    document.body.appendChild(other)
    other.focus()

    await wrapper.setProps({ runs: [] })
    await flushPromises()
    expect(popover()).toBeNull()
    expect(document.activeElement).toBe(other)
    wrapper.unmount()
  })

  it('leaves Escape alone while the popover is closed', () => {
    const seen: boolean[] = []
    const onKey = (e: KeyboardEvent) => seen.push(e.defaultPrevented)
    window.addEventListener('keydown', onKey)
    const wrapper = mountBar({ agentCount: 1, agents: [AGENT] })
    wrapper.get('.composer-activity').element.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }),
    )
    window.removeEventListener('keydown', onKey)
    expect(seen).toEqual([false])
    wrapper.unmount()
  })

  it('announces runs starting and ending, never their age', async () => {
    const wrapper = mountBar({ runs: [run('r1', 'ITTA preview', 60_000)] })
    expect(wrapper.get('[role="status"]').text()).toBe('Running in the background: ITTA preview')
    await wrapper.setProps({ runs: [], finished: run('r1', 'ITTA preview', 60_000, { status: 'cancelled' }) })
    expect(wrapper.get('[role="status"]').text()).toBe('Background run: ITTA preview stopped')
    wrapper.unmount()
  })

  it('announces a shell-only process', () => {
    const shell: RunningSubagent = { agent_id: 'shell:sh1', description: 'npm run dev', subagent_type: 'shell' }
    const wrapper = mountBar({ agents: [shell] })
    expect(wrapper.get('[role="status"]').text()).toBe('Running in the background: npm run dev')
    wrapper.unmount()
  })

  it('counts a shell row beside a run in the announcement', () => {
    const shell: RunningSubagent = { agent_id: 'shell:sh1', description: 'npm run dev', subagent_type: 'shell' }
    const wrapper = mountBar({ runs: [run('r1', 'ITTA preview', 60_000)], agents: [shell] })
    expect(wrapper.get('[role="status"]').text()).toBe('Running in the background: ITTA preview and 1 more')
    wrapper.unmount()
  })

  it('lists a shell row under Processes, as a non-link, and keeps it out of the agent count', async () => {
    const shell: RunningSubagent = { agent_id: 'shell:sh1', description: 'sleep 20', subagent_type: 'shell' }
    const wrapper = mountBar({ agentCount: 1, agents: [AGENT, shell] })
    expect(wrapper.get('.composer-activity-seg--agents').text()).toBe('1 agent')
    expect(wrapper.get('.composer-activity-seg--runs').text()).toBe('1 shell')
    await openAgents(wrapper)
    expect(texts('.composer-activity-pop-head')).toEqual(['Agents', 'Processes'])
    expect(texts('a.composer-activity-pop-item .composer-activity-pop-label')).toEqual(['Explore repo layout'])
    const shellItem = Array.from(document.querySelectorAll<HTMLElement>('.composer-activity-pop-item'))
      .find(el => el.textContent?.includes('sleep 20'))
    expect(shellItem?.tagName).toBe('BUTTON')
    wrapper.unmount()
  })

  it('lists a monitor row under Processes with its elapsed time and a Stop button', async () => {
    const monitor: RunningSubagent = {
      agent_id: 'task:m1', description: 'tail -f app.log', subagent_type: 'monitor',
      started_at: new Date(NOW - 125_000).toISOString(), stoppable: true,
    }
    const stopTask = vi.fn().mockResolvedValue(undefined)
    const wrapper = mountBar({ agents: [monitor], stopTask })
    expect(wrapper.get('.composer-activity-seg--runs').text()).toBe('1 shell')
    await wrapper.get('.composer-activity-seg--runs').trigger('click')
    await flushPromises()
    expect(texts('.composer-activity-pop-head')).toEqual(['Processes'])
    expect(texts('.composer-activity-pop-label')).toEqual(['tail -f app.log'])
    expect(texts('.composer-activity-pop-item small')).toEqual(['2m'])
    const stop = document.querySelector<HTMLButtonElement>('.composer-activity-pop-stop')
    expect(stop?.textContent?.trim()).toBe('Stop')
    expect(stop?.getAttribute('aria-label')).toBe('Stop tail -f app.log')
    stop?.click()
    await flushPromises()
    expect(stopTask).toHaveBeenCalledWith('m1')
    expect(document.querySelector('.composer-activity-pop-stop')?.textContent?.trim()).toBe('Stopping…')
    wrapper.unmount()
  })

  it('offers no Stop for a row that is not stoppable, and still shows its age', async () => {
    const shell: RunningSubagent = {
      agent_id: 'task:b1', description: 'sleep 600', subagent_type: 'shell',
      started_at: new Date(NOW - 3_725_000).toISOString(), stoppable: false,
    }
    const wrapper = mountBar({ agents: [shell], stopTask: vi.fn() })
    await wrapper.get('.composer-activity-seg--runs').trigger('click')
    await flushPromises()
    expect(document.querySelector('.composer-activity-pop-stop')).toBeNull()
    expect(texts('.composer-activity-pop-item small')).toEqual(['1h 2m'])
    wrapper.unmount()
  })

  it('shows an error and re-enables Stop when stopping fails', async () => {
    const shell: RunningSubagent = {
      agent_id: 'task:b1', description: 'sleep 600', subagent_type: 'shell',
      started_at: new Date(NOW).toISOString(), stoppable: true,
    }
    const wrapper = mountBar({ agents: [shell], stopTask: vi.fn().mockRejectedValue(new Error('x')) })
    await wrapper.get('.composer-activity-seg--runs').trigger('click')
    await flushPromises()
    document.querySelector<HTMLButtonElement>('.composer-activity-pop-stop')?.click()
    await flushPromises()
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('Could not stop')
    expect(document.querySelector<HTMLButtonElement>('.composer-activity-pop-stop')?.disabled).toBe(false)
    wrapper.unmount()
  })
})
