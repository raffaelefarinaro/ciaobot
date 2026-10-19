// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { nextTick } from 'vue'
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

  it('names agents and each live run with a whole-minute age', () => {
    const one = mountBar({ agentCount: 1 })
    expect(one.get('.composer-activity-seg--agents').text()).toBe('1 agent')
    expect(one.get('.composer-activity-seg--agents').find('.activity-glyph--agent').exists()).toBe(true)
    one.unmount()

    const two = mountBar({ agentCount: 2, runs: [run('r1', 'ITTA preview', 319_000)] })
    expect(two.get('.composer-activity-seg--agents').text()).toBe('2 agents')
    const seg = two.get('.composer-activity-seg--run')
    expect(seg.get('.composer-activity-label').text()).toBe('ITTA preview')
    expect(seg.get('.composer-activity-elapsed').text()).toBe('5m')
    expect(seg.find('.activity-glyph--run').exists()).toBe(true)
    two.unmount()
  })

  it('says how a finished run ended and opens Chat details from it', async () => {
    const failed = mountBar({ finished: run('r1', '', 0, { status: 'error', exit_code: 1 }) })
    const seg = failed.get('.composer-activity-seg--outcome')
    expect(seg.classes()).toContain('composer-activity-seg--error')
    expect(seg.get('.composer-activity-label').text()).toBe('uv sync failed (exit 1)')
    expect(seg.find('.activity-glyph--run-failed').exists()).toBe(true)
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
    expect(wrapper.get('.composer-activity-seg--run').attributes('aria-expanded')).toBe('false')

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

  it('folds to glyph and count on a narrow bar', async () => {
    vi.stubGlobal('ResizeObserver', class {
      private cb: ResizeObserverCallback
      constructor(cb: ResizeObserverCallback) { this.cb = cb }
      observe() { this.cb([{ contentRect: { width: 150 } } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver) }
      disconnect() {}
      unobserve() {}
    })
    const wrapper = mountBar({
      agentCount: 2,
      runs: [run('r1', 'one', 1000), run('r2', 'two', 1000), run('r3', 'three', 1000)],
    })
    await nextTick()
    expect(wrapper.find('.composer-activity-label').exists()).toBe(false)
    expect(wrapper.findAll('.composer-activity-count').map(c => c.text())).toEqual(['2', '3'])
    expect(wrapper.get('.composer-activity-seg--runs').attributes('title')).toBe('3 background runs')
    wrapper.unmount()
  })

  it('announces runs starting and ending, never their age', async () => {
    const wrapper = mountBar({ runs: [run('r1', 'ITTA preview', 60_000)] })
    expect(wrapper.get('[role="status"]').text()).toBe('Running in the background: ITTA preview')
    await wrapper.setProps({ runs: [], finished: run('r1', 'ITTA preview', 60_000, { status: 'cancelled' }) })
    expect(wrapper.get('[role="status"]').text()).toBe('Background run: ITTA preview stopped')
    wrapper.unmount()
  })
})
