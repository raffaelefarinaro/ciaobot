// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount, RouterLinkStub } from '@vue/test-utils'
import InjectedPromptCard from '../InjectedPromptCard.vue'
import type { InjectedPrompt } from '../../lib/injectedPrompt'

const runPrompt: InjectedPrompt = {
  kind: 'background_run',
  count: 1,
  runs: [{
    label: 'nightly', runId: 'bg-1', verdict: 'failed', exitCode: 3,
    error: 'exit 3', logPath: '/logs/bg-1.log', tail: ['one', 'two'],
  }],
}

function mountCard(props: Record<string, unknown>) {
  return mount(InjectedPromptCard, {
    props: props as never,
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
}

describe('InjectedPromptCard', () => {
  it('is collapsed by default and expands with aria-expanded', async () => {
    const wrapper = mountCard({ prompt: runPrompt, raw: '=== raw' })
    const toggle = wrapper.get('.injected-card-toggle')
    expect(toggle.element.tagName).toBe('BUTTON')
    expect(toggle.attributes('type')).toBe('button')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.injected-card-body').exists()).toBe(false)
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    const body = wrapper.get('.injected-card-body')
    expect(toggle.attributes('aria-controls')).toBe(body.attributes('id'))
  })

  it("shows each run's status pill, log link and tail", async () => {
    const wrapper = mountCard({ prompt: runPrompt, raw: '=== raw' })
    await wrapper.get('.injected-card-toggle').trigger('click')
    expect(wrapper.get('.injected-card-body .injected-pill--failed').text()).toBe('failed')
    expect(wrapper.get('a.file-link').attributes('data-file-path')).toBe('/logs/bg-1.log')
    const tail = wrapper.get('pre.injected-card-tail')
    expect(tail.text()).toBe('one\ntwo')
    expect(tail.attributes('tabindex')).toBe('0')
  })

  it('keeps the full prompt behind its own toggle', async () => {
    const wrapper = mountCard({ prompt: runPrompt, raw: '=== raw' })
    await wrapper.get('.injected-card-toggle').trigger('click')
    expect(wrapper.find('pre.injected-card-raw').exists()).toBe(false)
    await wrapper.get('.injected-card-raw-toggle').trigger('click')
    expect(wrapper.get('pre.injected-card-raw').text()).toBe('=== raw')
    const rawId = wrapper.get('pre.injected-card-raw').attributes('id')
    expect(rawId).toBeTruthy()
    expect(wrapper.get('.injected-card-raw-toggle').attributes('aria-controls')).toBe(rawId)
  })

  it('links an automation turn and keeps command output collapsed', async () => {
    const wrapper = mountCard({
      prompt: {
        kind: 'schedule',
        command: { command: './digest.sh', exitCode: 0, timedOut: false, output: 'all good' },
        prompt: 'Summarise.',
      },
      raw: 'raw',
      schedule: { id: 'sched-1', title: 'Nightly' },
    })
    const link = wrapper.getComponent(RouterLinkStub)
    expect(link.props('to')).toBe('/schedules/sched-1')
    expect(link.text()).toBe('Nightly')
    expect(wrapper.get('.injected-card-head').text()).toContain('Automation')
    await wrapper.get('.injected-card-toggle').trigger('click')
    expect(wrapper.find('.injected-card-output').exists()).toBe(false)
    await wrapper.get('.injected-card-detail-toggle').trigger('click')
    expect(wrapper.get('.injected-card-output').text()).toBe('all good')
  })

  it('keeps a multi-line command readable in the output toggle', async () => {
    const wrapper = mountCard({
      prompt: {
        kind: 'schedule',
        command: { command: 'cd repo\n./digest.sh', exitCode: 0, timedOut: false, output: 'ok' },
        prompt: 'Summarise.',
      },
      raw: 'raw',
    })
    await wrapper.get('.injected-card-toggle').trigger('click')
    expect(wrapper.get('.injected-card-command').element.textContent).toBe('cd repo\n./digest.sh')
  })

  it('names the webhook chat and hides the event until asked', async () => {
    const wrapper = mountCard({
      prompt: { kind: 'webhook', instructions: 'Triage it.', event: 'build failed' },
      raw: 'raw',
      chatTitle: 'Nightly build',
    })
    const head = wrapper.get('.injected-card-toggle').text()
    expect(head).toContain('Webhook')
    expect(head).toContain('· Nightly build')
    await wrapper.get('.injected-card-toggle').trigger('click')
    expect(wrapper.find('.injected-card-output').exists()).toBe(false)
    await wrapper.get('.injected-card-detail-toggle').trigger('click')
    expect(wrapper.get('.injected-card-output').text()).toBe('build failed')
  })

  it('shows the send time', () => {
    const wrapper = mountCard({ prompt: runPrompt, raw: 'raw', timestamp: '2026-10-19T10:00:00Z' })
    expect(wrapper.get('time.injected-card-time').attributes('datetime')).toBe('2026-10-19T10:00:00Z')
  })

  // A retry is a turn the reader asked to run again, so it reads as a notice
  // like the other engine-written turns, and the request it repeats stays one
  // disclosure away instead of being a second copy of the same words.
  it('draws a retry as a collapsed notice with the request behind a disclosure', async () => {
    const wrapper = mountCard({ prompt: { kind: 'retry' }, raw: 'Check the video' })
    const card = wrapper.get('.injected-card')
    expect(card.classes()).toContain('injected-card--retry')
    expect(card.attributes('aria-label')).toBe('Retry started')
    // Collapsed by default: no body, no disclosure, nothing to read by accident.
    expect(wrapper.find('.injected-card-body').exists()).toBe(false)
    expect(wrapper.find('details.injected-card-original').exists()).toBe(false)

    await wrapper.get('.injected-card-toggle').trigger('click')
    expect(wrapper.get('.injected-card-toggle').attributes('aria-expanded')).toBe('true')
    // The note is duration-free and adds what the header does not: the same
    // card has to read correctly while the attempt runs, after it settles and
    // after it failed, because a reloaded turn keeps the same copy.
    expect(wrapper.get('.injected-card-note').text()).toBe(
      'The request above was sent again unchanged.',
    )
    const details = wrapper.get('details.injected-card-original')
    expect(wrapper.get('details.injected-card-original summary').text()).toBe('Show original request')
    // The disclosure starts closed, and the native element is the control, so
    // it is keyboard operable without a custom handler.
    expect(details.attributes('open')).toBeUndefined()
    expect(details.element.tagName).toBe('DETAILS')
    // The retry carries its own disclosure: the generic full-prompt toggle
    // would show the same words twice.
    expect(wrapper.find('.injected-card-raw-toggle').exists()).toBe(false)
    expect(wrapper.get('details.injected-card-original .injected-card-prose').text())
      .toContain('Check the video')
  })

  it('renders the repeated request as text, not as markup', async () => {
    const wrapper = mountCard({
      prompt: { kind: 'retry' },
      raw: '<img src=x onerror="alert(1)">',
    })
    await wrapper.get('.injected-card-toggle').trigger('click')
    const details = wrapper.get('details.injected-card-original')
    expect(details.find('img').exists()).toBe(false)
    expect(details.text()).toContain('<img src=x onerror="alert(1)">')
  })
})
