// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
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
  taskStore.loops = [] as unknown as typeof taskStore.loops
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
  })

  // Working is the transcript's own activity pulse now, with no text label: the
  // tier heading and sidebar context already say "working", so the chip was
  // saying it twice. The accessible name still carries it.
  it('renders the working pulse without a redundant label', () => {
    const { store } = seed()
    store.projectStreaming = { 'chat-1': true }
    const working = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card' } })
    expect(working.find('.chat-signal--working').exists()).toBe(true)
    expect(working.find('.chat-signal--working .activity-spinner').exists()).toBe(true)
    expect(working.text()).toBe('')
    expect(working.find('.chat-signal--working').attributes('aria-label')).toBe('Working')
  })

  it('shows a count only when more than one agent is running', () => {
    const { store } = seed()
    store.backgroundAgents = { 'chat-1': 3 }
    const many = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card' } })
    expect(many.find('.chat-signal--agents').exists()).toBe(true)
    expect(many.find('.chat-signal-count').text()).toBe('3')
    expect(many.find('.chat-signal--agents').attributes('aria-label')).toBe('3 agents running')

    store.backgroundAgents = { 'chat-1': 1 }
    const one = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card' } })
    expect(one.find('.chat-signal-count').exists()).toBe(false)
    expect(one.find('.chat-signal--agents').attributes('aria-label')).toBe('1 agent running')
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
    expect(wrapper.find('.chat-signal--agents').attributes('aria-label')).toBe('2 agents running')
    expect(wrapper.find('.chat-signal-count').text()).toBe('2')
  })

  it('renders loop and retry modifiers with their accessible labels', () => {
    const { store, taskStore } = seed()
    store.chats[0].retry = { status: 'pending' } as never
    taskStore.loops = [{ web_chat_id: 'chat-1', running: false, loop_id: 'loop-1' }] as unknown as typeof taskStore.loops
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'row' } })
    expect(wrapper.find('.chat-signal--retry').attributes('aria-label')).toBe('Retry scheduled')
    expect(wrapper.find('.chat-signal--loop').classes()).toContain('stopped')
    expect(wrapper.findAll('.chat-signal')).toHaveLength(2)
  })

  it('marks an unread chat for attention without adding a count', () => {
    const { store } = seed()
    store.chats[0].last_activity_at = '2026-08-11T12:00:00Z'
    store.chats[0].last_read_at = '2026-08-10T12:00:00Z'
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1' } })
    expect(wrapper.find('.chat-signal--unread').exists()).toBe(true)
    expect(wrapper.find('.chat-signal--unread').attributes('aria-label')).toBe('Unread chat')
    expect(wrapper.find('.chat-signal-count').exists()).toBe(false)
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

  it('exposes a reduced-motion hook on the pulse', () => {
    const { store } = seed()
    store.projectStreaming = { 'chat-1': true }
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1' } })
    // .activity-spinner is the element the prefers-reduced-motion block targets.
    expect(wrapper.find('.activity-spinner').exists()).toBe(true)
  })
})
