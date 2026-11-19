import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import PinnedFilePanel from '../components/PinnedFilePanel.vue'
import {
  SB_CSV_HEADERED,
  SB_CSV_PATH,
  SB_HTML_PATH,
  SB_HTML_SOURCE,
  SB_MD_CONTENT,
  SB_MD_PATH,
  chatFilePathRoute,
  workspaceFileRoute,
  workspaceFileStatusRoute,
} from './fileViewerFixture'
import {
  SB_CHAT_ID,
  SB_COMMENT_ID,
  SB_COMMENT_LINE,
  SB_COMMENT_NOTE,
  SB_FOLDER_PATH,
  SB_FOLDER_PIN,
  SB_MD_PLAIN_CONTENT,
  SB_MD_PLAIN_PATH,
  SB_NOTE_CONTENT,
  SB_NOTE_PATH,
  SB_NOTE_REFRESHED,
  SB_PPTX_PATH,
  SB_REPORT_CONTENT,
  SB_REPORT_PATH,
  assertQuietFence,
  folderListing,
  held,
  libreofficeStatusRoute,
  listRow,
  markdownPathsRoute,
  panelStores,
  renderPinnedFilePanel,
  workspaceDirChatRoute,
  workspaceFileThenRoute,
} from './pinsFixture'

/**
 * The production docked tile (#3303, slice B18): PinnedFilePanel.vue renders
 * inside `mountConnected` with a fresh Pinia and memory router, fenced by the
 * preview boundary from the routes each story declares. Synthetic fixtures
 * only. The one parent job a story cannot run — applying the pin-prop change
 * after a docked open's pin write lands — is a labelled button in the host
 * (`story-repin`); every other assertion in these plays is production code.
 */

const meta = {
  id: 'pinned-file-panel',
  title: 'Pins and comments/PinnedFilePanel',
  component: PinnedFilePanel,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof PinnedFilePanel>
export default meta
type Story = StoryObj

const FOLDER_ENTRIES = [
  { name: 'storybook-report.txt', path: SB_REPORT_PATH, type: 'file' as const, size: 38, mtime: 1700000000, ext: 'txt' },
  { name: 'storybook-sub', path: `${SB_FOLDER_PATH}/storybook-sub`, type: 'dir' as const, size: null, mtime: 1700000000, ext: '' },
]

export const Default: Story = {
  parameters: { fixture: { routes: [workspaceFileRoute(SB_NOTE_PATH, SB_NOTE_CONTENT)] } },
  render: () => renderPinnedFilePanel('loaded text file pinned as the chat tile', { path: SB_NOTE_PATH }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('first note line')).toBeTruthy())
    const title = canvasElement.querySelector('.chat-title') as HTMLElement
    expect(title?.textContent).toBe('storybook-note.txt')
    expect(title?.getAttribute('title')).toBe(SB_NOTE_PATH)
    // The folder hint renders after the filename (shortened); its content
    // collapses for a top-level folder, so assert the element, not the words.
    expect(canvasElement.querySelector('.pfp-dir')?.getAttribute('title')).toBe('/notes/')
    // The docked tile's close unpins (not closes), and Edit is offered on a loaded text kind.
    expect(screen.getByRole('button', { name: 'Unpin file' }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByRole('button', { name: 'Edit' }).hasAttribute('disabled')).toBe(false)
    const lines = canvasElement.querySelectorAll('.pre-line')
    expect(lines.length).toBe(3)
    expect(lines.item(1)?.textContent).toBe('second note line')
  },
}

export const Loading: Story = {
  parameters: { fixture: { routes: [{ method: 'GET', path: `/api/workspace-file?path=${encodeURIComponent(SB_NOTE_PATH)}`, respond: () => held() }] } },
  render: () => renderPinnedFilePanel('file read held in flight', { path: SB_NOTE_PATH }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    const skeleton = canvasElement.querySelector('.pfp-skeleton') as HTMLElement
    expect(skeleton).toBeTruthy()
    expect(skeleton.getAttribute('role')).toBe('status')
    expect(skeleton.getAttribute('aria-label')).toBe('Loading file')
    expect(skeleton.getAttribute('aria-busy')).toBe('true')
    await waitFor(() => expect(screen.getByText(/Fixture requests: [1-9]/)).toBeTruthy())
    // Edit is actually blocked while the read is in flight, and no body replaced it.
    expect(screen.getByRole('button', { name: 'Edit' }).hasAttribute('disabled')).toBe(true)
    expect(canvasElement.querySelector('.pfp-error')).toBeNull()
    expect(canvasElement.querySelector('.pre-line')).toBeNull()
  },
}

export const Error: Story = {
  parameters: { fixture: { routes: [workspaceFileStatusRoute(SB_NOTE_PATH, 404)] } },
  render: () => renderPinnedFilePanel('file read answered with 404', { path: SB_NOTE_PATH }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('File not found.')).toBeTruthy())
    expect(canvasElement.querySelector('.pfp-error')?.textContent).toBe('File not found.')
    // Edit stays blocked on the failure; the tile is still closable.
    expect(screen.getByRole('button', { name: 'Edit' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'Unpin file' }).hasAttribute('disabled')).toBe(false)
    expect(canvasElement.querySelector('.pfp-skeleton')).toBeNull()
  },
}

export const Folder: Story = {
  parameters: {
    fixture: { routes: [workspaceDirChatRoute(SB_FOLDER_PATH, SB_CHAT_ID, folderListing(SB_FOLDER_PATH, FOLDER_ENTRIES))] },
  },
  render: () => renderPinnedFilePanel('folder pin showing the docked browser', { path: SB_FOLDER_PIN }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    // The real FolderBrowser chunk (VueFinder) mounts over the one declared listing.
    await waitFor(() => expect(screen.getAllByText('storybook-report.txt').length).toBeGreaterThan(0))
    expect(canvasElement.querySelector('.pfp-body')?.classList.contains('pfp-body-folder')).toBe(true)
    expect(canvasElement.querySelector('.chat-title')?.textContent).toBe('storybook-drafts')
    // Folder tiles swap the wording, offer no Edit and read no file.
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Unpin folder' })).toBeTruthy()
  },
}

export const ReturnFolder: Story = {
  parameters: {
    fixture: {
      routes: [
        workspaceDirChatRoute(SB_FOLDER_PATH, SB_CHAT_ID, folderListing(SB_FOLDER_PATH, FOLDER_ENTRIES)),
        chatFilePathRoute(SB_CHAT_ID, SB_REPORT_PATH, SB_REPORT_PATH),
        workspaceFileRoute(SB_REPORT_PATH, SB_REPORT_CONTENT),
      ],
    },
  },
  render: () => renderPinnedFilePanel('a file docked out of the folder browser and its Back control', {
    path: SB_FOLDER_PIN,
    repinTo: SB_REPORT_PATH,
    seed: { pins: { [SB_CHAT_ID]: SB_FOLDER_PIN }, dock: SB_CHAT_ID },
  }),
  play: async ({ canvas, canvasElement, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getAllByText('storybook-report.txt').length).toBeGreaterThan(0))
    // Opening a pinnable file from the docked browser writes the pin instead of
    // the viewer: the dock resolves through the declared file-path read.
    await event.dblClick(listRow('storybook-report.txt'))
    await waitFor(() => expect(screen.getByText(/Fixture requests: 2\./)).toBeTruthy())
    // The parent's pin propagation turns the tile onto the file: the real
    // pendingReturn bookkeeping then offers the Back control.
    await event.click(canvas.getByTestId('story-repin'))
    await waitFor(() => expect(screen.getByText('report line one')).toBeTruthy())
    const back = canvasElement.querySelector('.pfp-back-btn') as HTMLButtonElement
    expect(back?.getAttribute('aria-label')).toBe('Back to storybook-drafts')
    // Back rewrites the chat's pin to the folder the file came from.
    await event.click(back)
    await waitFor(() => expect(panelStores(canvasElement).projects.pinnedFileFor(SB_CHAT_ID)).toBe(SB_FOLDER_PIN))
    expect(back.getAttribute('aria-label')).toBe('Back to storybook-drafts')
  },
}

export const Markdown: Story = {
  parameters: {
    fixture: { routes: [workspaceFileRoute(SB_MD_PLAIN_PATH, SB_MD_PLAIN_CONTENT), markdownPathsRoute([])] },
  },
  render: () => renderPinnedFilePanel('markdown note without frontmatter', { path: SB_MD_PLAIN_PATH }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('A synthetic paragraph with no frontmatter.')).toBeTruthy())
    expect(canvasElement.querySelector('.pfp-md h1')?.textContent).toBe('Storybook Note')
    // No frontmatter, no properties section.
    expect(screen.queryByRole('region', { name: 'Document properties' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Edit' }).hasAttribute('disabled')).toBe(false)
  },
}

export const DocumentProperties: Story = {
  parameters: {
    fixture: { routes: [workspaceFileRoute(SB_MD_PATH, SB_MD_CONTENT), markdownPathsRoute([])] },
  },
  render: () => renderPinnedFilePanel('markdown note with YAML frontmatter properties', { path: SB_MD_PATH }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByRole('region', { name: 'Document properties' })).toBeTruthy())
    const section = screen.getByRole('region', { name: 'Document properties' })
    expect(section.textContent).toContain('Note')
    expect(section.textContent).toContain('Draft')
    expect(section.textContent).toContain('A synthetic document for the workshop.')
    expect(section.textContent).toContain('#storybook')
    // The name repeats the document heading, so the properties header omits it.
    expect(section.querySelector('.pfp-meta-name')).toBeNull()
    // The markdown body still renders under the properties.
    expect(canvasElement.querySelector('.pfp-md h1')?.textContent).toBe('Storybook Alpha')
  },
}

export const Csv: Story = {
  parameters: { fixture: { routes: [workspaceFileRoute(SB_CSV_PATH, SB_CSV_HEADERED)] } },
  render: () => renderPinnedFilePanel('commentable CSV preview', { path: SB_CSV_PATH }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    // CsvViewer reaches the tile as a lazy chunk, so the table needs a beat.
    await waitFor(() => expect(screen.getByText('Click a cell to comment')).toBeTruthy())
    const body = canvasElement.querySelector('.pfp-body') as HTMLElement
    expect(body.classList.contains('pfp-body-csv')).toBe(true)
    expect(body.querySelectorAll('.csv-viewer tbody .csv-cell-wrap').length).toBeGreaterThan(2)
    expect(screen.getByText('2 rows · 2 cols')).toBeTruthy()
  },
}

export const Pdf: Story = {
  render: () => renderPinnedFilePanel('PDF preview iframe', { path: '/docs/storybook-report.pdf' }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    const frame = await waitFor(() => {
      const el = canvasElement.querySelector('.pfp-pdf-iframe') as HTMLIFrameElement
      expect(el).toBeTruthy()
      return el
    })
    expect(frame.getAttribute('src')?.startsWith(`/api/workspace-binary?path=${encodeURIComponent('/docs/storybook-report.pdf')}&t=`)).toBe(true)
    // A PDF offers Edit nowhere and the LibreOffice notice stays away for plain PDFs.
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(screen.queryByText('LibreOffice is required')).toBeNull()
  },
}

export const PptxLibreoffice: Story = {
  parameters: { fixture: { routes: [libreofficeStatusRoute(false)] } },
  render: () => renderPinnedFilePanel('PowerPoint preview before LibreOffice is installed', { path: SB_PPTX_PATH }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('LibreOffice is required to preview PowerPoint files.')).toBeTruthy())
    expect(canvasElement.querySelector('.hint--warn')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Install in Chat' })).toBeTruthy()
    expect(canvasElement.querySelector('.pfp-pdf-iframe')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
  },
}

export const HtmlPreview: Story = {
  render: () => renderPinnedFilePanel('HTML artifact in Preview view', { path: SB_HTML_PATH }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByRole('group', { name: 'Artifact view' })).toBeTruthy())
    const tabs = [...screen.getByRole('group', { name: 'Artifact view' }).querySelectorAll('button')]
    expect(tabs.map(tab => tab.textContent)).toEqual(['Preview', 'Code'])
    expect(tabs[0]?.getAttribute('aria-pressed')).toBe('true')
    expect(canvasElement.querySelector('.hav-frame')).toBeTruthy()
    // Preview never fetched the source, so the header offers no Edit yet.
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
  },
}

export const HtmlCode: Story = {
  parameters: { fixture: { routes: [workspaceFileRoute(SB_HTML_PATH, SB_HTML_SOURCE)] } },
  render: () => renderPinnedFilePanel('HTML artifact switched to Code view', { path: SB_HTML_PATH }),
  play: async ({ canvas, canvasElement, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByRole('group', { name: 'Artifact view' })).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Code' }))
    // The source read is declared and its lines render in the code block; the
    // raw HTML text carries its own tags, so assert the block's content.
    await waitFor(() => {
      const code = canvasElement.querySelector('.hav-code')
      expect(code?.textContent).toContain('Fixed inert storybook artifact.')
    })
    expect(canvasElement.querySelector('.hav-frame')).toBeNull()
    // Code view with a loaded source is where the header Edit appears for html kinds.
    expect(screen.getByRole('button', { name: 'Edit' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Code' }).getAttribute('aria-pressed')).toBe('true')
  },
}

export const HtmlCodeError: Story = {
  parameters: { fixture: { routes: [workspaceFileStatusRoute(SB_HTML_PATH, 500)] } },
  render: () => renderPinnedFilePanel('HTML artifact whose source read fails', { path: SB_HTML_PATH }),
  play: async ({ canvas, canvasElement, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByRole('group', { name: 'Artifact view' })).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Code' }))
    await waitFor(() => expect(screen.getByText('Failed to load source (HTTP 500).')).toBeTruthy())
    expect(canvasElement.querySelector('.hav-note-error')?.textContent).toBe('Failed to load source (HTTP 500).')
    // Without a loaded source the header Edit is withheld.
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
  },
}

export const CommentPopover: Story = {
  parameters: { fixture: { routes: [workspaceFileRoute(SB_NOTE_PATH, SB_NOTE_CONTENT)] } },
  render: () => renderPinnedFilePanel('a durable line comment read as the pinned popover', {
    path: SB_NOTE_PATH,
    seed: { comments: [{ path: SB_NOTE_PATH, id: SB_COMMENT_ID, comment: SB_COMMENT_NOTE, lineStart: SB_COMMENT_LINE }] },
  }),
  play: async ({ canvas, canvasElement, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('second note line')).toBeTruthy())
    const commented = canvasElement.querySelectorAll('.pre-line').item(SB_COMMENT_LINE - 1) as HTMLElement
    expect(commented.classList.contains('comment-highlight')).toBe(true)
    expect(commented.getAttribute('data-comment-id')).toBe(SB_COMMENT_ID)
    // A pin is explicit: clicking the highlighted line pins the read popover.
    await event.click(commented)
    const popover = await waitFor(() => screen.getByRole('dialog', { name: 'Comment' }))
    expect(popover.textContent).toContain('storybook-note.txt · line 2')
    expect(popover.textContent).toContain(SB_COMMENT_NOTE)
    expect(canvasElement.querySelector('.pfp-comment-backdrop')).toBeTruthy()
    // Pinning moves focus to the popover's first action, so keyboard users can act at once.
    await waitFor(() => expect(document.activeElement?.textContent).toBe('Edit'))
    // Escape closes through the popover's own handler and drops the backdrop.
    await event.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Comment' })).toBeNull())
    expect(canvasElement.querySelector('.pfp-comment-backdrop')).toBeNull()
  },
}

export const CommentDraft: Story = {
  parameters: { fixture: { routes: [workspaceFileRoute(SB_NOTE_PATH, SB_NOTE_CONTENT)] } },
  render: () => renderPinnedFilePanel('a text selection opened as the compose popover', { path: SB_NOTE_PATH }),
  play: async ({ canvas, canvasElement, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('first note line')).toBeTruthy())
    // Select the first line the way a pointer selection lands: on the pre's span.
    const line = canvasElement.querySelectorAll('.pre-line').item(0) as HTMLElement
    const range = document.createRange()
    range.selectNodeContents(line)
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)
    document.dispatchEvent(new Event('selectionchange'))
    const trigger = await waitFor(() => {
      const el = canvasElement.querySelector('.pfp-comment-trigger') as HTMLButtonElement | null
      expect(el).toBeTruthy()
      return el!
    })
    await event.click(trigger)
    // The compose popover opens at the selection, focused, with an empty draft blocked.
    const compose = await waitFor(() => screen.getByRole('dialog', { name: 'Add comment' }))
    const textarea = compose.querySelector('textarea') as HTMLTextAreaElement
    await waitFor(() => expect(document.activeElement).toBe(textarea))
    const add = screen.getByRole('button', { name: 'Add comment' })
    expect(add.hasAttribute('disabled')).toBe(true)
    await event.type(textarea, 'A synthetic note')
    expect(add.hasAttribute('disabled')).toBe(false)
    // Saving stages the note durably: the line gains its highlight and the popover closes.
    await event.click(add)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add comment' })).toBeNull())
    await waitFor(() => expect(canvasElement.querySelector('.pre-line[data-comment-id]')).toBeTruthy())
    expect(panelStores(canvasElement).projects.fileCommentsFor(SB_NOTE_PATH).some(c => c.comment === 'A synthetic note')).toBe(true)
  },
}

export const Refresh: Story = {
  parameters: { fixture: { routes: [workspaceFileThenRoute(SB_NOTE_PATH, SB_NOTE_CONTENT, SB_NOTE_REFRESHED)] } },
  render: () => renderPinnedFilePanel('the menu Refresh re-reading the pinned file', { path: SB_NOTE_PATH }),
  play: async ({ canvas, canvasElement, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('first note line')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'More file actions' }))
    // Menu items are menuitem, not buttons, so the click goes through their role.
    await event.click(screen.getByRole('menuitem', { name: 'Refresh' }))
    // The reload replaces the body with the read the boundary answered second.
    await waitFor(() => expect(screen.getByText('fresh note line')).toBeTruthy())
    expect(screen.queryByText('first note line')).toBeNull()
    expect(screen.getByText(/Fixture requests: 2\./)).toBeTruthy()
    expect(canvasElement.querySelector('.chat-title')?.textContent).toBe('storybook-note.txt')
  },
}

export const MemoryMapTile: Story = {
  parameters: {
    fixture: { routes: [workspaceFileRoute(SB_MD_PATH, SB_MD_CONTENT), markdownPathsRoute([])] },
  },
  render: () => renderPinnedFilePanel('the memory map note-tile props', {
    path: SB_MD_PATH,
    inMemoryMap: true,
    closeLabel: 'Close note',
  }),
  play: async ({ canvas, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('A synthetic document for the workshop.')).toBeTruthy())
    // The map's tile closes instead of unpins, and it is never desktop-only.
    expect(screen.getByRole('button', { name: 'Close note' }).classList.contains('desktop-only')).toBe(false)
    // Open in memory map would open the page the tile is already on, so the menu drops it.
    await event.click(screen.getByRole('button', { name: 'More file actions' }))
    const menu = screen.getByRole('menu')
    expect(menu.textContent).toContain('Show in folder')
    expect(menu.textContent).not.toContain('Open in memory map')
    await event.keyboard('{Escape}')
  },
}

export const CloseDisabled: Story = {
  parameters: { fixture: { routes: [workspaceFileRoute(SB_NOTE_PATH, SB_NOTE_CONTENT)] } },
  render: () => renderPinnedFilePanel('the tile while a pin write is in flight', {
    path: SB_NOTE_PATH,
    closeDisabled: true,
  }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('first note line')).toBeTruthy())
    const close = screen.getByRole('button', { name: 'Unpin file' })
    expect(close.hasAttribute('disabled')).toBe(true)
    expect(close.getAttribute('aria-disabled')).toBe('true')
    expect(canvasElement.querySelector('.pfp-back-btn')).toBeNull()
  },
}

export const Narrow: Story = {
  parameters: { fixture: { routes: [workspaceFileRoute(SB_NOTE_PATH, SB_NOTE_CONTENT)] } },
  render: () => renderPinnedFilePanel('the docked tile inside a 390px wrapper', { path: SB_NOTE_PATH, narrow: true }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByText('first note line')).toBeTruthy())
    const panel = canvasElement.querySelector('.pinned-file-panel') as HTMLElement
    expect(panel.getBoundingClientRect().width).toBeGreaterThan(300)
    expect(panel.getBoundingClientRect().width).toBeLessThanOrEqual(392)
    const body = canvasElement.querySelector('.pfp-body') as HTMLElement
    expect(body.scrollWidth).toBeLessThanOrEqual(body.clientWidth + 1)
    expect(canvasElement.querySelector('.chat-title')?.textContent).toBe('storybook-note.txt')
  },
}
