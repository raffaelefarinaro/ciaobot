// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import ChatSignals from '../ChatSignals.vue'

function seed() {
  const store = useProjectStore()
  store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
  store.chats = [{
    chat_id: 'chat-1',
    project_id: 'project-1',
    title: 'Chat',
    archived: false,
    local: true,
  }] as unknown as typeof store.chats
  store.projectStreaming = {}
  store.backgroundAgents = {}
  const taskStore = useTaskStore()
  taskStore.schedules = [] as unknown as typeof taskStore.schedules
  return { store, taskStore }
}

describe('ChatSignals', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('renders the needs-you chip for a pending question', () => {
    const { store } = seed()
    store.chats[0].pending_question = JSON.stringify({ questions: [{ question: 'Which option?' }] })
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card', hue: 'cyan' } })
    expect(wrapper.find('.chat-signal--needs').exists()).toBe(true)
    expect(wrapper.text()).toContain('needs you')
    expect(wrapper.find('.chat-signal--needs').attributes('aria-label')).toBe('Needs your answer')
    expect(wrapper.find('.activity-glyph--alert').exists()).toBe(false)

    // At row density needs-you is the alert triangle, the one mark for "wants you".
    const row = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'row' } })
    expect(row.find('.chat-signal--needs .activity-glyph--alert').exists()).toBe(true)
    expect(row.find('.chat-signal-label').exists()).toBe(false)
  })

  // Working is the transcript's own activity pulse now, with no text label: the
  // tier heading and sidebar context already say "working", so the chip was
  // saying it twice. The accessible name still carries it.
  it('renders the working pulse without a redundant label', () => {
    const { store } = seed()
    store.projectStreaming = { 'chat-1': true }
    const working = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card' } })
    expect(working.find('.chat-signal--working').exists()).toBe(true)
    expect(working.find('.chat-signal--working .activity-glyph--working').exists()).toBe(true)
    expect(working.text()).toBe('')
    expect(working.find('.chat-signal--working').attributes('aria-label')).toBe('Working')
  })

  // A native shell is listed in the composer and Chat details, with Stop; the
  // sidebar row neither marks it nor counts it as an agent.
  it('draws no run marker for a chat whose only activity is a background shell', () => {
    const { store } = seed()
    store.runningSubagents = {
      'chat-1': [{ agent_id: 'task:b1', description: 'npm run dev', subagent_type: 'shell' }],
    } as unknown as typeof store.runningSubagents
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card' } })
    expect(wrapper.find('.chat-signal--runs').exists()).toBe(false)
    expect(wrapper.find('.chat-signal--working').exists()).toBe(false)
  })

  // Subagents are the chat's work: no mark of their own, just working, with
  // the label naming them.
  it('shows running agents as working, named only in the label', () => {
    const { store } = seed()
    store.backgroundAgents = { 'chat-1': 3 }
    const many = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card' } })
    expect(many.find('.chat-signal--working .activity-glyph--working').exists()).toBe(true)
    expect(many.find('.chat-signal-count').exists()).toBe(false)
    expect(many.find('.chat-signal--working').attributes('aria-label')).toBe('3 agents running')

    store.backgroundAgents = { 'chat-1': 1 }
    const one = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card' } })
    expect(one.find('.chat-signal--working').attributes('aria-label')).toBe('1 agent running')

    store.projectStreaming = { 'chat-1': true }
    const turn = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card' } })
    expect(turn.findAll('.activity-glyph--working')).toHaveLength(1)
    expect(turn.find('.chat-signal--working').attributes('aria-label')).toBe('Working')
  })

  // The poll and the watcher tick on different clocks, so a row must not read
  // idle just because the pushed count has not arrived yet.
  it('counts running subagents the watcher has not announced yet', () => {
    const { store } = seed()
    store.runningSubagents = {
      'chat-1': [
        { agent_id: 'a1', description: 'Sweep', status: 'running' },
        { agent_id: 'a2', description: 'Verify', status: 'running' },
      ],
    }
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card' } })
    expect(wrapper.find('.chat-signal--working').attributes('aria-label')).toBe('2 agents running')
  })

  it('renders the schedule clock and the retry alert with their accessible labels', () => {
    const { store, taskStore } = seed()
    store.chats[0].retry = { status: 'pending' } as never
    taskStore.schedules = [{
      schedule_id: 'sched-1',
      frequency: 'interval',
      interval_minutes: 10,
      web_chat_id: 'chat-1',
      enabled: false,
    }] as unknown as typeof taskStore.schedules
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'row' } })
    expect(wrapper.find('.chat-signal--retry').attributes('aria-label')).toBe('Retry scheduled')
    expect(wrapper.find('.chat-signal--retry .activity-glyph--alert').exists()).toBe(true)
    expect(wrapper.find('.chat-signal--schedule .schedule-glyph').exists()).toBe(true)
    expect(wrapper.find('.chat-signal--schedule').classes()).toContain('stopped')
    expect(wrapper.find('.chat-signal--schedule').attributes('aria-label')).toBe('An automation attached to this chat (paused)')
    expect(wrapper.findAll('.chat-signal')).toHaveLength(2)
  })

  it('says the run failed when a retry follows an error', () => {
    const { store } = seed()
    store.chats[0].retry = { status: 'pending' } as never
    store.chats[0].last_error = 'boom'
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'row' } })
    expect(wrapper.find('.chat-signal--retry').attributes('aria-label')).toBe('Run failed, retry scheduled')
    expect(wrapper.find('.chat-signal--retry').attributes('title')).toBe('Run failed, retry scheduled')
  })

  it('shows the schedule clock for a chat bound to a daily automation too', () => {
    const { taskStore } = seed()
    taskStore.schedules = [
      { schedule_id: 'sched-1', frequency: 'daily', web_chat_id: 'chat-1', enabled: true },
      { schedule_id: 'sched-2', frequency: 'interval', interval_minutes: 10, web_chat_id: 'chat-1', enabled: false },
    ] as unknown as typeof taskStore.schedules
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'row' } })
    const clock = wrapper.find('.chat-signal--schedule')
    expect(clock.classes()).not.toContain('stopped')
    expect(clock.attributes('aria-label')).toBe('2 automations running in this chat')
  })

  it('marks an unread chat for attention without adding a count', () => {
    const { store } = seed()
    store.chats[0].last_activity_at = '2026-08-11T12:00:00Z'
    store.chats[0].last_read_at = '2026-08-10T12:00:00Z'
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1' } })
    expect(wrapper.find('.chat-signal--unread').exists()).toBe(true)
    expect(wrapper.find('.chat-signal--unread').attributes('aria-label')).toBe('Unread chat')
    expect(wrapper.find('.chat-signal--unread svg').exists()).toBe(true)
    expect(wrapper.find('.chat-signal-count').exists()).toBe(false)
  })

  it('shows one dot, not two, for an unread chat that needs an answer', () => {
    const { store } = seed()
    store.chats[0].last_activity_at = '2026-08-11T12:00:00Z'
    store.chats[0].last_read_at = '2026-08-10T12:00:00Z'
    store.chats[0].pending_question = JSON.stringify({ questions: [{ question: 'Answer me' }] })
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'row' } })
    expect(wrapper.find('.chat-signal--needs').exists()).toBe(true)
    expect(wrapper.find('.chat-signal--unread').exists()).toBe(false)
  })

  it('gives needs-you precedence over working and keeps row density unlabeled', () => {
    const { store } = seed()
    store.chats[0].pending_question = JSON.stringify({ questions: [{ question: 'Answer me' }] })
    store.projectStreaming = { 'chat-1': true }
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'row' } })
    expect(wrapper.find('.chat-signal--needs').exists()).toBe(true)
    expect(wrapper.find('.chat-signal--working').exists()).toBe(false)
    expect(wrapper.text()).toBe('')
    expect(wrapper.find('.chat-signal--needs').classes()).toContain('chat-signal--needs')
  })

  it('shows the memory-pass needs-you chip', () => {
    // A pass that ended unclean is the one background job whose unfinished
    // state is the owner's problem, so it needs a signal of its own — and a
    // label, since a glyph is never the whole explanation.
    const { store } = seed()
    store.chats[0].helper = {
      kind: 'memory_pass',
      source_chat_id: 'chat-0',
      archive_path: 'chats/chat-0.md',
      doc_path: 'projects/general.md',
      source_title: 'Chat',
      source_project: 'General',
      state: 'attention',
      archive_policy: 'when_clean',
    } as unknown as NonNullable<typeof store.chats[0]['helper']>
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card' } })
    expect(wrapper.find('.chat-signal--memory').exists()).toBe(true)
    expect(wrapper.find('.chat-signal--memory').attributes('aria-label')).toBe('Memory pass needs you')
    expect(wrapper.find('.chat-signal--memory').attributes('title')).toBe('Memory pass needs you')
    // Stuck memory is a failure like any other: the alert triangle, not a glyph of its own.
    expect(wrapper.find('.chat-signal--memory .activity-glyph--alert').exists()).toBe(true)
  })

  it('leaves the memory chip off a pass that is still running or done', () => {
    const { store } = seed()
    for (const state of ['queued', 'running', 'done'] as const) {
      store.chats[0].helper = {
        kind: 'memory_pass',
        source_chat_id: 'chat-0',
        archive_path: 'chats/chat-0.md',
        doc_path: 'projects/general.md',
        source_title: 'Chat',
        source_project: 'General',
        state,
        archive_policy: 'when_clean',
      } as unknown as NonNullable<typeof store.chats[0]['helper']>
      const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1' } })
      expect(wrapper.find('.chat-signal--memory').exists()).toBe(false)
    }
  })

  it('keeps needs-you ranked above the memory chip', () => {
    const { store } = seed()
    store.chats[0].pending_question = JSON.stringify({ questions: [{ question: 'Answer me' }] })
    store.chats[0].helper = {
      kind: 'memory_pass',
      source_chat_id: 'chat-0',
      archive_path: 'chats/chat-0.md',
      doc_path: 'projects/general.md',
      source_title: 'Chat',
      source_project: 'General',
      state: 'attention',
      archive_policy: 'when_clean',
    } as unknown as NonNullable<typeof store.chats[0]['helper']>
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'row' } })
    expect(wrapper.find('.chat-signal--needs').exists()).toBe(true)
    expect(wrapper.find('.chat-signal--memory').exists()).toBe(false)
  })

  it('exposes a reduced-motion hook on the pulse', () => {
    const { store } = seed()
    store.projectStreaming = { 'chat-1': true }
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1' } })
    // .activity-glyph--working is the element ActivityGlyph's prefers-reduced-motion block targets.
    expect(wrapper.find('.activity-glyph--working').exists()).toBe(true)
    expect(wrapper.findAll('.activity-glyph--working .activity-glyph-pixel--still')).toHaveLength(5)
  })

  it('keeps status glyphs off the semantic palette', () => {
    const here = join(__dirname, '..')
    for (const file of ['ChatSignals.vue', 'ActivityGlyph.vue']) {
      const source = readFileSync(join(here, file), 'utf8')
      expect(source).not.toContain('var(--error')
      expect(source).not.toContain('var(--warning')
    }
  })
})
