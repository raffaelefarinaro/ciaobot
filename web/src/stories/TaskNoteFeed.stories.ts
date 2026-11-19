import { defineComponent, h, ref, type VNode } from 'vue'
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import TaskNoteFeed from '../components/TaskNoteFeed.vue'
import type { TaskUpdate } from '../lib/types'

/**
 * The progress-note feed of the task editor and the chat-side peek (the
 * `components-task-note-feed` inventory row, #3298). Props only: the notes
 * arrive from the owner, so no fixture is declared. Each story renders the
 * production component; the editing and actions slots are supplied by a
 * synthetic host that stands in for the owner's own handlers.
 */
const meta = {
  id: 'task-note-feed',
  title: 'Task notes and peek/TaskNoteFeed',
  component: TaskNoteFeed,
  args: { updates: [] },
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof TaskNoteFeed>
export default meta
type Story = StoryObj<typeof meta>

const agentNote: TaskUpdate = {
  id: 'sb-note-agent',
  recorded_at: '2026-03-01T10:00:00+00:00',
  actor: 'agent',
  text: 'Synthetic agent note with a reference to the storybook-alpha run.',
  edited_at: null,
  attempt_id: 'sb-note-attempt',
  chat_id: 'sb-note-chat',
}

const userNote: TaskUpdate = {
  id: 'sb-note-user',
  recorded_at: '2026-03-01T09:00:00+00:00',
  actor: 'user',
  text: 'Synthetic user note with a [link](https://storybook-alpha.example/notes).',
  edited_at: null,
  attempt_id: '',
  chat_id: '',
}

/** Newest first, as the owner passes them. */
const notes: TaskUpdate[] = [agentNote, userNote]

/** Feed with no slots: the plain rendering. */
function feed(updates: TaskUpdate[], maxWidth = 640) {
  return () => h('div', { style: `max-width: ${maxWidth}px` }, [
    h(TaskNoteFeed, { updates }),
  ])
}

/**
 * A feed whose owner-side handlers are real state: the actions slot's Edit
 * button opens the editor slot on its note, and the editor's Done editing
 * closes it again.
 */
function editableFeed(updates: TaskUpdate[], initialEditing = '') {
  return defineComponent({
    name: 'TaskNoteFeedStoryHost',
    setup() {
      const editing = ref(initialEditing)
      return () => h('div', { style: 'max-width: 640px' }, [
        h(TaskNoteFeed, { updates, editingId: editing.value }, {
          actions: ({ note }: { note: TaskUpdate }): VNode => h('button', {
            type: 'button',
            class: 'btn-small task-note-edit',
            onClick: () => { editing.value = note.id },
          }, 'Edit'),
          editor: ({ note }: { note: TaskUpdate }): VNode => h('div', { class: 'task-note-editor' }, [
            h('label', { class: 'task-note-editor-label' }, 'Edit note'),
            h('textarea', { class: 'task-note-editor-field', value: note.text, rows: 3 }),
            h('button', {
              type: 'button',
              class: 'btn-small',
              onClick: () => { editing.value = '' },
            }, 'Done editing'),
          ]),
        }),
      ])
    },
  })
}

export const Default: Story = {
  render: feed(notes),
  play: async ({ canvasElement }) => {
    const items = await waitFor(() => {
      const found = canvasElement.querySelectorAll<HTMLElement>('.task-note')
      expect(found).toHaveLength(2)
      return found
    })
    // Newest first: the agent's note leads, and each names who wrote it.
    expect([...items].map((item) => item.dataset.actor)).toEqual(['agent', 'user'])
    expect(items[0]!.querySelector('.task-note-author')!.textContent).toBe('Agent')
    expect(items[1]!.querySelector('.task-note-author')!.textContent).toBe('You')
    // The note's links are live anchors, not escaped text.
    const link = items[1]!.querySelector<HTMLAnchorElement>('.task-note-text a')
    expect(link?.getAttribute('href')).toBe('https://storybook-alpha.example/notes')
    // Without an actions slot the meta line holds no button.
    expect(canvasElement.querySelector('.task-note-edit')).toBeNull()
    expect(items[0]!.querySelector('.task-note-when')!.textContent).not.toContain('edited')
  },
}

/** The default feed at the 390px phone width, with a long unbroken token that must wrap. */
const longToken = `storybook-alpha-${'a'.repeat(120)}`
const narrowNotes: TaskUpdate[] = [
  { ...agentNote, text: longToken },
  userNote,
]

export const Narrow: Story = {
  render: feed(narrowNotes, 390),
  play: async ({ canvasElement }) => {
    const items = await waitFor(() => {
      const found = canvasElement.querySelectorAll<HTMLElement>('.task-note')
      expect(found).toHaveLength(2)
      return found
    })
    // The long token wraps inside the bubble: no horizontal overflow.
    const bubble = items[0]!
    expect(bubble.scrollWidth).toBeLessThanOrEqual(bubble.clientWidth + 1)
    expect(bubble.querySelector('.task-note-text')!.textContent).toContain(longToken)
  },
}

export const Edited: Story = {
  render: feed([{ ...userNote, id: 'sb-note-edited', edited_at: '2026-03-01T11:00:00+00:00', text: 'Synthetic reworded note.' }]),
  play: async ({ canvasElement }) => {
    const when = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.task-note .task-note-when')
      expect(found).toBeTruthy()
      return found!
    })
    // The mark sits after the time, in the same meta line.
    expect(when.textContent).toContain(' · edited')
  },
}

export const NoRecordedTime: Story = {
  render: feed([{ ...userNote, id: 'sb-note-untimed', recorded_at: '' }]),
  play: async ({ canvasElement }) => {
    const when = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.task-note .task-note-when')
      expect(found).toBeTruthy()
      return found!
    })
    expect(when.textContent).toBe('No time recorded')
  },
}

export const WithActions: Story = {
  render: () => h(editableFeed(notes)),
  play: async ({ canvasElement }) => {
    const buttons = await waitFor(() => {
      const found = canvasElement.querySelectorAll<HTMLButtonElement>('.task-note-edit')
      expect(found).toHaveLength(2)
      return found
    })
    // The action sits in the meta line of each note, not in its text.
    for (const button of buttons) expect(button.closest('.task-note-meta')).toBeTruthy()
    await userEvent.click(buttons[1]!)
    // The button's owner handler opened the editor on the second note.
    await waitFor(() => expect(canvasElement.querySelector('.task-note-editor')).toBeTruthy())
  },
}

export const Editing: Story = {
  render: () => h(editableFeed(notes, userNote.id)),
  play: async ({ canvasElement }) => {
    const editor = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLTextAreaElement>('.task-note-editor-field')
      expect(found).toBeTruthy()
      return found!
    })
    // The note being edited shows its field in place of the rendered text.
    expect(editor.value).toBe(userNote.text)
    const edited = canvasElement.querySelector<HTMLElement>('.task-note[data-actor="user"]')!
    expect(edited.querySelector('.task-note-text')).toBeNull()
    // The other note keeps its rendered text.
    expect(canvasElement.querySelector('.task-note[data-actor="agent"] .task-note-text')).toBeTruthy()
    await userEvent.click(canvasElement.querySelector<HTMLButtonElement>('.task-note-editor button')!)
    await waitFor(() => {
      expect(canvasElement.querySelector('.task-note-editor')).toBeNull()
      expect(canvasElement.querySelector('.task-note[data-actor="user"] .task-note-text')).toBeTruthy()
    })
  },
}
