import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import PinnedFilePanel from '../components/PinnedFilePanel.vue'
import {
  SB_CSV_HEADERED,
  SB_CSV_PATH,
  workspaceFileRoute,
} from './fileViewerFixture'
import {
  SB_CSV_BASE,
  SB_NOTE_CONTENT,
  SB_NOTE_PATH,
  assertQuietFence,
  renderPinnedFilePanel,
} from './pinsFixture'
import { saveFileRoute } from './fileViewerFixture'

/**
 * The `.pfp-body` inline family (#3303, slice B18): the preview-and-editor body
 * of PinnedFilePanel.vue, driven through the real Edit → type → Save/Cancel
 * flow. Each story renders the production panel inside `mountConnected`; the
 * plays act on the panel through its own header Edit control. The preview half
 * of this family is proven by the pinned-file-panel component row's covered
 * states (default/markdown/document-properties/csv/html stories render exactly
 * this body).
 */

const meta = {
  id: 'pinned-file-preview-editor',
  title: 'Pins and comments/Pinned file preview and editor',
  component: PinnedFilePanel,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof PinnedFilePanel>
export default meta
type Story = StoryObj

export const Editing: Story = {
  parameters: { fixture: { routes: [workspaceFileRoute(SB_NOTE_PATH, SB_NOTE_CONTENT)] } },
  render: () => renderPinnedFilePanel('text tile switched into the textarea editor', { path: SB_NOTE_PATH }),
  play: async ({ canvas, canvasElement, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('first note line')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Edit' }))
    const textarea = await waitFor(() => {
      const el = canvasElement.querySelector('.pfp-edit-textarea') as HTMLTextAreaElement
      expect(el).toBeTruthy()
      return el
    })
    // The buffer starts from the on-disk content and the caret lands in it.
    await waitFor(() => expect(document.activeElement).toBe(textarea))
    expect(textarea.value).toBe(SB_NOTE_CONTENT)
    await event.type(textarea, ' an appended edit')
    expect(textarea.value).toBe(`${SB_NOTE_CONTENT} an appended edit`)
    // Cancel discards the draft and restores the preview without a write.
    await event.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByText('first note line')).toBeTruthy())
    expect(screen.queryByText('first note line an appended edit')).toBeNull()
    expect(canvasElement.querySelector('.pfp-edit-textarea')).toBeNull()
    // The only read was the initial one; canceling wrote nothing.
    expect(screen.getByText('Fixture requests: 1. Violations: 0.')).toBeTruthy()
  },
}

export const EditingCsv: Story = {
  parameters: { fixture: { routes: [workspaceFileRoute(SB_CSV_PATH, SB_CSV_HEADERED)] } },
  render: () => renderPinnedFilePanel('CSV tile switched into the table editor', { path: SB_CSV_PATH }),
  play: async ({ canvas, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('Click a cell to comment')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Edit' }))
    // The CSV editor is the lazy CsvViewer chunk in editing mode.
    await waitFor(() => expect(screen.getByText('+ Row')).toBeTruthy())
    expect(screen.getByText('2 rows · 2 cols')).toBeTruthy()
    expect(screen.getByLabelText('Column 1 header')).toBeTruthy()
    expect(screen.getByLabelText('Row 1, fruit')).toBeTruthy()
    await event.click(screen.getByRole('button', { name: '+ Row' }))
    await waitFor(() => expect(screen.getByText('3 rows · 2 cols')).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Delete row 3' })).toBeTruthy()
    // Cancel returns the read-only table without writing.
    await event.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByText('Click a cell to comment')).toBeTruthy())
  },
}

export const Saving: Story = {
  parameters: { fixture: { routes: [workspaceFileRoute(SB_NOTE_PATH, SB_NOTE_CONTENT), saveFileRoute('held')] } },
  render: () => renderPinnedFilePanel('editor save held in flight', { path: SB_NOTE_PATH }),
  play: async ({ canvas, canvasElement, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('first note line')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Edit' }))
    await waitFor(() => expect(canvasElement.querySelector('.pfp-edit-textarea')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Save' }))
    // Both actions are actually blocked while the POST is held.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Saving…' })).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Saving…' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'Cancel' }).hasAttribute('disabled')).toBe(true)
    await waitFor(() => expect(screen.getByText(/Fixture requests: [2-9]/)).toBeTruthy())
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const SaveError: Story = {
  parameters: {
    fixture: { routes: [workspaceFileRoute(SB_NOTE_PATH, SB_NOTE_CONTENT), saveFileRoute(500)] },
  },
  render: () => renderPinnedFilePanel('editor save answered with 500', { path: SB_NOTE_PATH }),
  play: async ({ canvas, canvasElement, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('first note line')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Edit' }))
    await waitFor(() => expect(canvasElement.querySelector('.pfp-edit-textarea')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Save' }))
    // The failed write surfaces in the editor and the controls unblock without writing.
    await waitFor(() => expect(screen.getByText('Save failed (HTTP 500).')).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Save' }).hasAttribute('disabled')).toBe(false)
    // A failed save keeps the editor open with the buffer intact (the pre body stays away).
    expect(canvasElement.querySelector('.pfp-edit-textarea')).toBeTruthy()
    expect(canvasElement.querySelector('.pfp-pre')).toBeNull()
  },
}

export const CsvSave: Story = {
  parameters: {
    fixture: { routes: [workspaceFileRoute(SB_CSV_PATH, SB_CSV_BASE), saveFileRoute(200)] },
  },
  render: () => renderPinnedFilePanel('CSV editor saved back to the tile', { path: SB_CSV_PATH }),
  play: async ({ canvas, canvasElement, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('Click a cell to comment')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Edit' }))
    await waitFor(() => expect(screen.getByText('+ Row')).toBeTruthy())
    // A real cell edit is what the save POST carries: the appended row is
    // empty, so type into the first data cell and save that change.
    await event.type(screen.getByLabelText('Row 1, item'), 'gamma')
    await waitFor(() => expect(document.querySelector('.csv-meta')?.textContent).toBe('2 rows · 2 cols'))
    await event.click(screen.getByRole('button', { name: 'Save' }))
    // The adopted buffer is the read-only table again, with the typed cell.
    await waitFor(() => expect(screen.queryByText('+ Row')).toBeNull())
    await waitFor(() => expect(document.querySelector('.csv-meta-bar')?.textContent).toContain('2 rows · 2 cols'))
    expect(canvasElement.querySelector('.pfp-body')?.textContent).toContain('gamma')
  },
}
