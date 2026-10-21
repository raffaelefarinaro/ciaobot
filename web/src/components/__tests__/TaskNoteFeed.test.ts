// @vitest-environment jsdom

/**
 * The task's progress notes, newest first. Shared by the board's editor and the
 * chat-side task peek, so the feed reads the same in both places.
 */
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import TaskNoteFeed from '../TaskNoteFeed.vue'
import type { TaskUpdate } from '../../lib/types'

function note(overrides: Partial<TaskUpdate> = {}): TaskUpdate {
  return {
    id: 'n1',
    actor: 'agent',
    text: 'Checked the **index**',
    recorded_at: '2026-10-20T10:00:00Z',
    edited_at: null,
    ...overrides,
  } as TaskUpdate
}

describe('TaskNoteFeed', () => {
  it('renders one labelled note per update, in the order given', () => {
    const wrapper = mount(TaskNoteFeed, {
      props: {
        updates: [note({ id: 'a', actor: 'agent' }), note({ id: 'b', actor: 'user', text: 'Thanks' })],
      },
    })
    const items = wrapper.findAll('.task-note')
    expect(items).toHaveLength(2)
    expect(items[0].attributes('data-actor')).toBe('agent')
    expect(items[0].find('.task-note-author').text()).toBe('Agent')
    expect(items[1].attributes('data-actor')).toBe('user')
    expect(items[1].find('.task-note-author').text()).toBe('You')
    expect(wrapper.find('ol.task-notes').attributes('aria-label')).toBe('Progress notes, newest first')
  })

  it('swaps the editor slot in for the note being edited', () => {
    const wrapper = mount(TaskNoteFeed, {
      props: { updates: [note({ id: 'a' }), note({ id: 'b', text: 'Other' })], editingId: 'a' },
      slots: {
        editor: '<textarea class="probe-editor"></textarea>',
      },
    })
    const items = wrapper.findAll('.task-note')
    expect(items[0].find('.probe-editor').exists()).toBe(true)
    expect(items[0].find('.task-note-text').exists()).toBe(false)
    expect(items[1].find('.probe-editor').exists()).toBe(false)
    expect(items[1].find('.task-note-text').exists()).toBe(true)
  })

  it('renders the actions slot inside the note meta line', () => {
    const wrapper = mount(TaskNoteFeed, {
      props: { updates: [note({ id: 'a' })] },
      slots: {
        actions: '<button class="probe-action">Edit</button>',
      },
    })
    expect(wrapper.find('.task-note-meta .probe-action').exists()).toBe(true)
  })
})
