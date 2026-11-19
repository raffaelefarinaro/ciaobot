import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import FileViewerModal from '../components/FileViewerModal.vue'
import {
  SB_CSV_HEADERED,
  SB_CSV_PATH,
  SB_CHAT_ID,
  SB_TEXT_CONTENT,
  SB_TEXT_PATH,
  playStores,
  renderFileViewerState,
  saveFileRoute,
} from './fileViewerFixture'

/**
 * The `.fv-body` inline family (#3302, slice B17): the preview-and-editor body
 * of FileViewerModal.vue, driven through the real Edit → type → Save/Cancel
 * flow. Each story renders the production dialog inside `mountConnected`; the
 * fixed backdrop covers the page, so plays act on the stores through
 * `playStores`. The preview half of this family is claimed by the
 * `file-viewer-modal` stories.
 */

const meta = {
  id: 'file-modal-preview-editor',
  title: 'File viewers/File modal preview and editor',
  component: FileViewerModal,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof FileViewerModal>
export default meta
type Story = StoryObj<typeof meta>

/** A chat-scoped text file the modal offers Edit for. */
const editableSeed = {
  isOpen: true,
  path: SB_TEXT_PATH,
  content: SB_TEXT_CONTENT,
  chatId: SB_CHAT_ID,
  loadToken: 1,
}

export const Editing: Story = {
  render: () => renderFileViewerState('text file switched into the textarea editor', editableSeed),
  play: async ({ canvasElement, userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Edit' }))
    const textarea = await waitFor(() => {
      const el = screen.getByRole('dialog').querySelector('.fv-edit-textarea')
      expect(el).toBeTruthy()
      return el as HTMLTextAreaElement
    })
    await waitFor(() => expect(document.activeElement).toBe(textarea))
    // The buffer starts from the on-disk content.
    expect(textarea.value).toBe(SB_TEXT_CONTENT)
    await event.type(textarea, 'An appended edit')
    expect(textarea.value).toBe(`${SB_TEXT_CONTENT}An appended edit`)
    expect(playStores(canvasElement).viewer.isDirty).toBe(true)
    // Cancel discards the draft and returns the preview without writing.
    await event.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByText('first line')).toBeTruthy())
    expect(screen.queryByText('An appended edit')).toBeNull()
    expect(playStores(canvasElement).viewer.editing).toBe(false)
    expect(screen.getByText(/Fixture requests: 0\./)).toBeTruthy()
  },
}

export const EditingCsv: Story = {
  render: () => renderFileViewerState('CSV file switched into the table editor', {
    isOpen: true,
    path: SB_CSV_PATH,
    content: SB_CSV_HEADERED,
    chatId: SB_CHAT_ID,
    loadToken: 1,
  }),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Edit' }))
    // The CSV editor is the lazy CsvViewer chunk in editing mode.
    await waitFor(() => expect(screen.getByText('+ Row')).toBeTruthy())
    const metaBar = await waitFor(() => screen.getByText('2 rows · 2 cols'))
    expect(metaBar).toBeTruthy()
    // Header cells are editable inputs with real header names.
    expect(screen.getByLabelText('Column 1 header')).toBeTruthy()
    expect(screen.getByLabelText('Column 2 header')).toBeTruthy()
    expect(screen.getByLabelText('Row 1, fruit')).toBeTruthy()
    await event.click(screen.getByRole('button', { name: '+ Row' }))
    await waitFor(() => expect(screen.getByText('3 rows · 2 cols')).toBeTruthy())
    // Each data row carries its delete control.
    expect(screen.getByRole('button', { name: 'Delete row 3' })).toBeTruthy()
  },
}

export const Saving: Story = {
  parameters: { fixture: { routes: [saveFileRoute('held')] } },
  render: () => renderFileViewerState('editor save held in flight', editableSeed),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Edit' }))
    await waitFor(() => {
      const el = screen.getByRole('dialog').querySelector('.fv-edit-textarea')
      expect(el).toBeTruthy()
    })
    await event.click(screen.getByRole('button', { name: 'Save' }))
    // Both actions are actually blocked while the POST is held.
    await waitFor(() => expect(screen.getByText('Saving…')).toBeTruthy())
    const save = screen.getByRole('button', { name: 'Saving…' })
    expect(save.hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'Cancel' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByText(/Fixture requests: [1-9]/)).toBeTruthy()
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const SaveError: Story = {
  parameters: { fixture: { routes: [saveFileRoute(500)] } },
  render: () => renderFileViewerState('editor save answered with 500', editableSeed),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Edit' }))
    await waitFor(() => {
      const el = screen.getByRole('dialog').querySelector('.fv-edit-textarea')
      expect(el).toBeTruthy()
    })
    await event.click(screen.getByRole('button', { name: 'Save' }))
    // The failed write surfaces in the editor and the controls unblock.
    await waitFor(() => expect(screen.getByText('Save failed (HTTP 500).')).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Save' }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}
