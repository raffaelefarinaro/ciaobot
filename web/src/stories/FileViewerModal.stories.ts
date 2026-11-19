import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, userEvent, waitFor } from 'storybook/test'
import FileViewerModal from '../components/FileViewerModal.vue'
import {
  SB_CHAT_ID,
  SB_CSV_HEADERED,
  SB_CSV_PATH,
  SB_HTML_PATH,
  SB_HTML_SOURCE,
  SB_IMG_PATH,
  SB_IMAGE_DATA_URL,
  SB_LOGS_PATH,
  SB_MD_CONTENT,
  SB_MD_PATH,
  SB_PDF_PATH,
  SB_PPTX_PATH,
  SB_PROJECT_KEY,
  SB_TEXT_CONTENT,
  SB_TEXT_CONTENT_REFRESHED,
  SB_TEXT_PATH,
  chatFilePathRoute,
  continueChatRoute,
  patchPinRoute,
  playStores,
  renderFileViewerState,
  seedFileComment,
  syntheticListing,
  workspaceDirRoute,
  workspaceFileRoute,
  workspaceFileStatusRoute,
  workspaceImageRoute,
} from './fileViewerFixture'

/**
 * The production file-viewer dialog (#3302, slice B17): FileViewerModal.vue
 * renders inside `mountConnected` with a fresh Pinia and memory router, fenced
 * by the preview boundary from the routes each story declares. Synthetic
 * fixtures only. The dialog's fixed backdrop covers the page, so plays act on
 * the stores through `playStores` instead of clicking wrapper controls.
 */

const meta = {
  id: 'file-viewer-modal',
  title: 'File viewers/FileViewerModal',
  component: FileViewerModal,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof FileViewerModal>
export default meta
type Story = StoryObj<typeof meta>

/** The default loaded text open: prose modal, header actions, pin, Edit. */
const defaultSeed = {
  isOpen: true,
  path: SB_TEXT_PATH,
  content: SB_TEXT_CONTENT,
  chatId: SB_CHAT_ID,
  dockTarget: SB_PROJECT_KEY,
  loadToken: 1,
}

export const Default: Story = {
  render: () => renderFileViewerState('loaded text file in the viewer dialog', defaultSeed),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    const dialog = screen.getByRole('dialog', { name: /storybook-alpha\.txt/ })
    expect(dialog).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Edit' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Pin to sidebar' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Discuss' })).toBeTruthy()
    const lines = dialog.querySelectorAll('.pre-line')
    expect(lines.length).toBe(3)
    expect(lines[0]?.textContent).toBe('first line')
    // The dialog's open-auto-focus lands on the modal frame, not a control inside it.
    expect(document.activeElement?.classList.contains('fv-modal')).toBe(true)
    // Escape closes through requestClose; the dialog unmounts and the store records the close.
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(playStores(canvasElement).viewer.isOpen).toBe(false))
    expect(screen.queryByRole('dialog')).toBeNull()
  },
}

export const Markdown: Story = {
  render: () => renderFileViewerState('loaded markdown document', {
    isOpen: true,
    path: SB_MD_PATH,
    content: SB_MD_CONTENT,
  }),
  play: async () => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    const dialog = screen.getByRole('dialog', { name: /storybook-alpha\.md/ })
    // No chat context: the modal stays read-only (no Edit).
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(dialog.querySelector('.fv-md h1')?.textContent).toBe('Storybook Alpha')
    expect(dialog.querySelector('.fv-md')?.textContent).toContain('A synthetic paragraph.')
  },
}

export const DocumentProperties: Story = {
  render: () => renderFileViewerState('markdown document with YAML frontmatter properties', {
    isOpen: true,
    path: SB_MD_PATH,
    content: SB_MD_CONTENT,
  }),
  play: async () => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    const section = screen.getByRole('region', { name: 'Document properties' })
    expect(section.textContent).toContain('Note')
    expect(section.textContent).toContain('Draft')
    expect(section.textContent).toContain('A synthetic document for the workshop.')
    expect(section.textContent).toContain('#storybook')
    // The name equals the document heading, so the properties header omits it.
    expect(section.querySelector('.fv-meta-name')).toBeNull()
    // The markdown body still renders under the properties.
    expect(screen.getByRole('dialog', { name: /storybook-alpha\.md/ }).querySelector('.fv-md h1')?.textContent).toBe('Storybook Alpha')
  },
}

export const Loading: Story = {
  parameters: { fixture: { routes: [workspaceFileRoute(SB_TEXT_PATH, SB_TEXT_CONTENT)] } },
  render: () => renderFileViewerState('file open held in its initial load', {}),
  play: async ({ canvasElement }) => {
    expect(screen.queryByRole('dialog')).toBeNull()
    void playStores(canvasElement).viewer.open(SB_TEXT_PATH)
    await waitFor(() => expect(screen.getByText('Loading…')).toBeTruthy())
    await waitFor(() => {
      expect(screen.getByText(/Fixture requests: [1-9]/)).toBeTruthy()
      expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
    })
  },
}

export const Error: Story = {
  parameters: { fixture: { routes: [workspaceFileStatusRoute(SB_TEXT_PATH, 404)] } },
  render: () => renderFileViewerState('file open answered with 404', {}),
  play: async ({ canvasElement }) => {
    await playStores(canvasElement).viewer.open(SB_TEXT_PATH)
    await waitFor(() => expect(screen.getByText('File not found.')).toBeTruthy())
    expect(screen.queryByText('Loading…')).toBeNull()
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const Empty: Story = {
  render: () => renderFileViewerState('loaded file with empty content', {
    isOpen: true,
    path: SB_TEXT_PATH,
    content: '',
    chatId: SB_CHAT_ID,
  }),
  play: async () => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    const body = screen.getByRole('dialog', { name: /storybook-alpha\.txt/ }).querySelector('.fv-body')
    expect(body?.querySelector('.fv-pre')).toBeTruthy()
    expect(body?.querySelector('.fv-pre')?.textContent).toBe('')
  },
}

export const Csv: Story = {
  render: () => renderFileViewerState('commentable CSV preview with a durable cell comment', {
    isOpen: true,
    path: SB_CSV_PATH,
    content: SB_CSV_HEADERED,
    chatId: SB_CHAT_ID,
    dockTarget: SB_PROJECT_KEY,
    loadToken: 1,
  }, ({ projects }) => {
    seedFileComment(projects, SB_CSV_PATH, { id: 'sb-c-1', comment: 'check totals', colIndex: 0, lineStart: 1 })
  }),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    // CsvViewer reaches the modal as a lazy chunk, so the table needs a beat.
    await waitFor(() => expect(screen.getByText('Click a cell to comment')).toBeTruthy())
    const body = screen.getByRole('dialog', { name: /storybook-alpha\.csv/ }).querySelector('.fv-body')
    expect(body?.querySelector('.csv-meta-bar')?.textContent).toContain('Click a cell to comment')
    const cells = [...(body?.querySelectorAll('.csv-viewer tbody .csv-cell-wrap') ?? [])]
    expect(cells.length).toBeGreaterThan(2)
    // The seeded comment highlights its cell through the CSV viewer's props.
    const commented = body?.querySelector('[data-comment-id="sb-c-1"]')
    expect(commented?.classList.contains('comment-highlight')).toBe(true)
    // Selecting a clean cell marks it (the cell-select event the parent turns into a Comment trigger).
    const cell = cells.find(el => !el.getAttribute('data-comment-id'))!
    await event.click(cell)
    await waitFor(() => expect(cell.classList.contains('is-selected')).toBe(true))
    // The Comment trigger is not asserted: a click fires the document selectionchange
    // handler, which clears the CSV cell anchor (useFileComments onSelectionChange).
  },
}

export const Image: Story = {
  render: () => renderFileViewerState('served chat attachment image', {
    isOpen: true,
    kind: 'image',
    path: 'storybook-figure.png',
    imageUrl: SB_IMAGE_DATA_URL,
    loadToken: 1,
  }),
  play: async () => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    const body = screen.getByRole('dialog', { name: /storybook-figure\.png/ }).querySelector('.fv-body')
    const img = body?.querySelector('.fv-img')
    expect(img?.getAttribute('src')?.startsWith('data:image/svg+xml')).toBe(true)
    expect(img?.getAttribute('alt')).toBe('storybook-figure.png')
    // A served image has no workspace path: no pin, Discuss or Edit.
    expect(screen.queryByRole('button', { name: 'Pin to sidebar' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Discuss' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
  },
}

export const ImageError: Story = {
  parameters: { fixture: { routes: [workspaceImageRoute(SB_IMG_PATH, 404)] } },
  render: () => renderFileViewerState('workspace image that fails to load', {
    isOpen: true,
    kind: 'image',
    path: SB_IMG_PATH,
  }),
  play: async () => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    // The <img> itself 404s (a plain resource load, not the boundary), then
    // onImageError probes the same URL through the declared route and the
    // error branch replaces it.
    await waitFor(() => expect(screen.getByText('File not found.')).toBeTruthy())
    expect(screen.getByRole('dialog').querySelector('.fv-img')).toBeNull()
    expect(screen.getByText(/Fixture requests: [1-9]/)).toBeTruthy()
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const Folder: Story = {
  parameters: {
    fixture: {
      routes: [workspaceDirRoute('/notes', syntheticListing('/notes', [
        { name: 'storybook-data.csv', path: '/notes/storybook-data.csv', type: 'file', size: 24, mtime: 1700000000, ext: 'csv' },
        { name: 'storybook-drafts', path: '/notes/storybook-drafts', type: 'dir', size: null, mtime: 1700000000, ext: '' },
      ]))],
    },
  },
  render: () => renderFileViewerState('folder listing in the viewer body', {
    isOpen: true,
    kind: 'folder',
    path: '/notes',
  }),
  play: async () => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    // The real FolderBrowser chunk (VueFinder) mounts over the one declared listing.
    await waitFor(() => {
      expect(screen.getAllByText('storybook-data.csv').length).toBeGreaterThan(0)
      expect(screen.getAllByText('storybook-drafts').length).toBeGreaterThan(0)
    })
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const Pdf: Story = {
  render: () => renderFileViewerState('PDF preview iframe', {
    isOpen: true,
    kind: 'pdf',
    path: SB_PDF_PATH,
    loadToken: 1,
  }),
  play: async () => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    const iframe = screen.getByRole('dialog').querySelector('.fv-pdf-iframe')
    expect(iframe?.getAttribute('src')).toBe(`/api/workspace-binary?path=${encodeURIComponent(SB_PDF_PATH)}&t=1`)
    expect(iframe?.getAttribute('title')).toBe('Preview of storybook-report.pdf')
  },
}

export const LibreofficeNotice: Story = {
  render: () => renderFileViewerState('PowerPoint preview without LibreOffice', {
    isOpen: true,
    kind: 'pdf',
    path: SB_PPTX_PATH,
    pptxNeedsLibreoffice: true,
    loadToken: 1,
  }),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    expect(screen.getByText('LibreOffice is required to preview PowerPoint files.')).toBeTruthy()
    const install = screen.getByRole('button', { name: 'Install in Chat' })
    await event.click(install)
    // No General project is seeded, so the fix-chat flow stops with an error toast and never reaches the network.
    await waitFor(() => expect(screen.getByText(/Toasts: 1\./)).toBeTruthy())
    expect(screen.getByText(/Fixture requests: 0\./)).toBeTruthy()
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const Html: Story = {
  render: () => renderFileViewerState('HTML artifact preview', {
    isOpen: true,
    kind: 'html',
    path: SB_HTML_PATH,
    loadToken: 1,
  }),
  play: async () => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    // HtmlArtifactViewer is a lazy chunk too.
    await waitFor(() => {
      const body = screen.getByRole('dialog', { name: /storybook-alpha\.html/ }).querySelector('.fv-body')
      expect(body?.querySelector('.hav-frame')).toBeTruthy()
    })
    const body = screen.getByRole('dialog', { name: /storybook-alpha\.html/ }).querySelector('.fv-body')
    const frame = body?.querySelector('.hav-frame')
    // The sandbox/iframe attributes the artifact contract depends on (the frame
    // itself reaches /api/workspace-html by plain navigation, not the fence).
    expect(frame?.getAttribute('sandbox')).toBe('allow-scripts')
    expect(frame?.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(frame?.getAttribute('title')).toBe(`Artifact preview: ${SB_HTML_PATH}`)
    const toolbar = body?.querySelector('.hav-toolbar')
    expect(toolbar?.getAttribute('aria-label')).toBe('Artifact view')
    expect(toolbar?.querySelector('.hav-tab')?.getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: 'Code' }).getAttribute('aria-pressed')).toBe('false')
  },
}

export const HtmlCode: Story = {
  parameters: { fixture: { routes: [workspaceFileRoute(SB_HTML_PATH, SB_HTML_SOURCE)] } },
  render: () => renderFileViewerState('HTML artifact switched to code view', {
    isOpen: true,
    kind: 'html',
    path: SB_HTML_PATH,
    chatId: SB_CHAT_ID,
    loadToken: 1,
  }),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    // Code view fetches the artifact source over the declared route.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Code' })).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Code' }))
    await waitFor(() => expect(screen.getByText('<p>Fixed inert storybook artifact.</p>')).toBeTruthy())
    // Source in hand: Edit becomes available on artifacts.
    expect(screen.getByRole('button', { name: 'Edit' })).toBeTruthy()
    expect(screen.getByText(/Fixture requests: [1-9]/)).toBeTruthy()
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const HtmlCodeLoading: Story = {
  render: () => renderFileViewerState('artifact code view holding its source load', {
    isOpen: true,
    kind: 'html',
    path: SB_HTML_PATH,
    htmlView: 'code',
    sourceLoading: true,
    loadToken: 1,
  }),
  play: async () => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    await waitFor(() => expect(screen.getByText('Loading source…')).toBeTruthy())
    // Without the source, Edit is refused.
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
  },
}

export const HtmlCodeError: Story = {
  render: () => renderFileViewerState('artifact code view that failed to load its source', {
    isOpen: true,
    kind: 'html',
    path: SB_HTML_PATH,
    htmlView: 'code',
    sourceError: 'Failed to load source (HTTP 404).',
    loadToken: 1,
  }),
  play: async () => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    await waitFor(() => expect(screen.getByText('Failed to load source (HTTP 404).')).toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
  },
}

export const Pinned: Story = {
  render: () => renderFileViewerState('file pinned beside its docked chat tile', defaultSeed, ({ projects }) => {
    void projects.pinFile(SB_PROJECT_KEY, SB_TEXT_PATH)
  }),
  play: async ({ canvasElement, userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    const pin = screen.getByRole('button', { name: 'Unpin from sidebar' })
    expect(pin.classList.contains('active')).toBe(true)
    await event.click(pin)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Pin to sidebar' })).toBeTruthy())
    // Unpinning keeps the modal open; only a successful pin closes it.
    expect(playStores(canvasElement).viewer.isOpen).toBe(true)
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const PinPending: Story = {
  parameters: { fixture: { routes: [patchPinRoute(SB_CHAT_ID)] } },
  render: () => renderFileViewerState('chat-scoped pin write in flight', {
    isOpen: true,
    path: SB_TEXT_PATH,
    content: SB_TEXT_CONTENT,
    chatId: SB_CHAT_ID,
    dockTarget: SB_CHAT_ID,
    loadToken: 1,
  }, ({ projects }) => {
    projects.chats = [{ chat_id: SB_CHAT_ID } as never]
  }),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    const pin = screen.getByRole('button', { name: 'Pin to sidebar' })
    await event.click(pin)
    // The PATCH is held, so the control is actually blocked, not merely styled.
    await waitFor(() => expect(pin.hasAttribute('disabled')).toBe(true))
    expect(screen.getByText(/Fixture requests: [1-9]/)).toBeTruthy()
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const ActionsMenu: Story = {
  render: () => renderFileViewerState('the More file-actions menu', defaultSeed),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'More file actions' }))
    const menu = screen.getByRole('menu')
    for (const item of ['Copy path', 'Show in folder', 'Download', 'Open in default app']) {
      expect(menu.textContent).toContain(item)
    }
    // A text path offers memory-map only for markdown, so the row is absent here.
    expect(menu.textContent).not.toContain('Open in memory map')
    const download = [...menu.querySelectorAll('button')].find(b => b.textContent === 'Download')!
    expect(download.hasAttribute('data-disabled')).toBe(false)
  },
}

export const ArchivedTranscript: Story = {
  parameters: { fixture: { routes: [continueChatRoute(SB_CHAT_ID, 500)] } },
  render: () => renderFileViewerState('archived chat transcript opened as a file', {
    isOpen: true,
    path: SB_LOGS_PATH,
    content: 'storybook archived transcript',
    chatId: SB_CHAT_ID,
  }),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    const button = screen.getByRole('button', { name: 'Continue this chat' })
    await event.click(button)
    // The declared continue route answers 500: the failure surfaces as an error toast, never a silent success.
    await waitFor(() => expect(screen.getByText(/Toasts: 1\./)).toBeTruthy())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Continue this chat' })).toBeTruthy())
    expect(screen.getByText(/Fixture requests: [1-9]/)).toBeTruthy()
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const Refresh: Story = {
  parameters: {
    fixture: {
      routes: [
        chatFilePathRoute(SB_CHAT_ID, SB_TEXT_PATH, SB_TEXT_PATH),
        workspaceFileRoute(SB_TEXT_PATH, SB_TEXT_CONTENT_REFRESHED),
      ],
    },
  },
  render: () => renderFileViewerState('file refreshed when its turn finishes streaming', {
    isOpen: true,
    path: SB_TEXT_PATH,
    content: SB_TEXT_CONTENT,
    chatId: SB_CHAT_ID,
  }, ({ projects }) => {
    projects.streaming = { [SB_CHAT_ID]: true }
  }),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    expect(screen.getByText('first line')).toBeTruthy()
    // The turn ends: the modal's streaming-end watcher re-opens the same file.
    playStores(canvasElement).projects.streaming = {}
    await waitFor(() => expect(screen.getByText('refreshed line one')).toBeTruthy())
    expect(screen.getByText(/Fixture requests: [1-9]/)).toBeTruthy()
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const RefreshFailure: Story = {
  parameters: {
    fixture: {
      routes: [
        chatFilePathRoute(SB_CHAT_ID, SB_TEXT_PATH, SB_TEXT_PATH),
        workspaceFileStatusRoute(SB_TEXT_PATH, 500),
      ],
    },
  },
  render: () => renderFileViewerState('refresh that fails after streaming ends', {
    isOpen: true,
    path: SB_TEXT_PATH,
    content: SB_TEXT_CONTENT,
    chatId: SB_CHAT_ID,
  }, ({ projects }) => {
    projects.streaming = { [SB_CHAT_ID]: true }
  }),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    expect(screen.getByText('first line')).toBeTruthy()
    playStores(canvasElement).projects.streaming = {}
    await waitFor(() => expect(screen.getByText('Failed to load file (HTTP 500).')).toBeTruthy())
    expect(screen.getByText(/Violations: 0\./)).toBeTruthy()
  },
}

export const CommentDraft: Story = {
  render: () => renderFileViewerState('selection comment draft on a text file', {
    isOpen: true,
    path: SB_TEXT_PATH,
    content: SB_TEXT_CONTENT,
  }),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    const line = screen.getByText('second line')
    const selection = document.getSelection()
    selection?.removeAllRanges()
    const range = document.createRange()
    range.selectNodeContents(line)
    selection?.addRange(range)
    document.dispatchEvent(new Event('selectionchange'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Comment' })).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Comment' }))
    const composer = await waitFor(() => screen.getByRole('dialog', { name: 'Add comment' }))
    const textarea = composer.querySelector('textarea')!
    await event.type(textarea, 'Fix this line')
    await event.click(screen.getByRole('button', { name: 'Add comment' }))
    // The comment is staged durably for the file, and the line carries its highlight.
    await waitFor(() => expect(screen.getByText(/File comments: 1\./)).toBeTruthy())
    const highlight = screen.getByRole('dialog').querySelector('.pre-line[data-comment-id]')
    expect(highlight?.classList.contains('comment-highlight')).toBe(true)
    expect(screen.queryByRole('button', { name: 'Comment' })).toBeNull()
  },
}

export const CommentPopover: Story = {
  render: () => renderFileViewerState('reading a durable line comment', {
    isOpen: true,
    path: SB_TEXT_PATH,
    content: SB_TEXT_CONTENT,
  }, ({ projects }) => {
    seedFileComment(projects, SB_TEXT_PATH, { id: 'sb-c-2', comment: 'Check this line', lineStart: 2 })
  }),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    const dialog = screen.getByRole('dialog')
    const line = dialog.querySelector('.pre-line[data-comment-id="sb-c-2"]')
    expect(line?.classList.contains('comment-highlight')).toBe(true)
    await event.click(line!)
    const popover = await waitFor(() => screen.getByRole('dialog', { name: 'Comment' }))
    expect(popover.textContent).toContain('Check this line')
    expect(popover.textContent).toContain('line 2')
    await event.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.getByText(/File comments: 0\./)).toBeTruthy())
  },
}

export const Narrow: Story = {
  // The dialog is a fixed overlay teleported to <body>, so a 390px wrapper cannot
  // constrain it: the narrow viewport is the story viewport instead.
  parameters: { viewport: { defaultViewport: 'narrow' } },
  render: () => renderFileViewerState('text file in a 390px viewport', defaultSeed),
  play: async () => {
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
    const modal = screen.getByRole('dialog')
    const width = modal.getBoundingClientRect().width
    // The modal fills the viewport it was given (its phone layout is the viewport media query).
    expect(width).toBeGreaterThan(0)
    expect(width).toBeLessThanOrEqual(window.innerWidth)
    expect(modal.textContent).toContain('storybook-alpha.txt')
  },
}
