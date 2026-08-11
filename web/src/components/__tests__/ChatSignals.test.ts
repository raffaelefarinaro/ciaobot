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

  it('renders the working ring and background-agent count', () => {
    const { store } = seed()
    store.projectStreaming = { 'chat-1': true }
    const working = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card' } })
    expect(working.find('.chat-signal--working').exists()).toBe(true)
    expect(working.text()).toContain('working')

    store.projectStreaming = {}
    store.backgroundAgents = { 'chat-1': 3 }
    const agents = mount(ChatSignals, { props: { chatId: 'chat-1', density: 'card' } })
    expect(agents.find('.chat-signal--agents').exists()).toBe(true)
    expect(agents.text()).toContain('3 agents')
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

  it('keeps quiet and unread chats free of a mark', () => {
    const { store } = seed()
    store.chats[0].last_activity_at = '2026-08-11T12:00:00Z'
    store.chats[0].last_read_at = '2026-08-10T12:00:00Z'
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1' } })
    expect(wrapper.findAll('.chat-signal')).toHaveLength(0)
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

  it('exposes a reduced-motion hook on the pulsing core', () => {
    const { store } = seed()
    store.projectStreaming = { 'chat-1': true }
    const wrapper = mount(ChatSignals, { props: { chatId: 'chat-1' } })
    expect(wrapper.find('.chat-signal-core--pulse').exists()).toBe(true)
  })
})
