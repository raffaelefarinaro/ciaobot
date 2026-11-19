import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import type { SyntheticListing } from './fileViewerFixture'
import { defineComponent, h, ref } from 'vue'
import FolderBrowser from '../components/FolderBrowser.vue'
import { SB_FOLDER_PATH, SB_SUB_PATH, folderListing, held, listRow } from './pinsFixture'

/**
 * The read-only folder browser (#3303, slice B18): the production
 * FolderBrowser.vue props-only mount, fenced by the preview boundary from the
 * one route each story declares. On mount it reads `GET /api/workspace-dir`
 * (path only, no chat here) and hands VueFinder a driver over that listing —
 * so every entry below is fixed synthetic data and navigation/opening are real
 * component+VueFinder flows against declared reads.
 */

const SB_CHATLESS = ''

const SB_ENTRIES = [
  { name: 'storybook-report.txt', path: `${SB_FOLDER_PATH}/storybook-report.txt`, type: 'file' as const, size: 38, mtime: 1700000000, ext: 'txt' },
  { name: 'storybook-sub', path: `${SB_FOLDER_PATH}/storybook-sub`, type: 'dir' as const, size: null, mtime: 1700000000, ext: '' },
]

const SB_SUB_LISTING: SyntheticListing = folderListing(SB_SUB_PATH, [
  { name: 'storybook-sub-note.txt', path: `${SB_SUB_PATH}/storybook-sub-note.txt`, type: 'file' as const, size: 21, mtime: 1700000001, ext: 'txt' },
])

function dirRoute(path: string, listing: SyntheticListing) {
  return { method: 'GET', path: `/api/workspace-dir?path=${encodeURIComponent(path)}`, respond: () => new Response(JSON.stringify(listing), { status: 200, headers: { 'Content-Type': 'application/json' } }) }
}

const dirHeld = (path: string) => ({ method: 'GET', path: `/api/workspace-dir?path=${encodeURIComponent(path)}`, respond: () => held() })

const dirError = (path: string, status: number, error: string) => ({
  method: 'GET',
  path: `/api/workspace-dir?path=${encodeURIComponent(path)}`,
  respond: () => new Response(JSON.stringify({ error }), { status, headers: { 'Content-Type': 'application/json' } }),
})

/**
 * The host for the interactive stories: the production component with its two
 * events recorded in a visible log, so navigate/open-file emissions are
 * asserted without the story owning any state itself.
 */
function renderBrowserHost(initialPath: string) {
  return defineComponent({
    name: 'FolderBrowserHost',
    setup() {
      const log = ref<string[]>([])
      return () => [
        h(FolderBrowser, {
          'initialPath': initialPath,
          'chatId': SB_CHATLESS,
          'onNavigate': (path: string) => { log.value = [...log.value, `navigate:${path}`] },
          'onOpenFile': (path: string) => { log.value = [...log.value, `open-file:${path}`] },
        }),
        h('p', { 'data-testid': 'story-events' }, `Events: ${log.value.at(-1) ?? 'none'}`),
      ]
    },
  })
}

/** The props-only host the non-interactive stories render through: the component alone, args fixed. */
function renderBrowser(initialPath: string) {
  return defineComponent({
    name: 'FolderBrowserPlain',
    setup() {
      return () => [h(FolderBrowser, { initialPath, chatId: SB_CHATLESS }), h('p', 'Synthetic folder listing fixture.')]
    },
  })
}

function eventsLine(): string {
  return document.querySelector('[data-testid="story-events"]')?.textContent ?? 'missing events line'
}

const meta = {
  id: 'folder-browser',
  title: 'Pins and comments/FolderBrowser',
  component: FolderBrowser,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof FolderBrowser>
export default meta
type Story = StoryObj

export const Default: Story = {
  parameters: { fixture: { routes: [dirRoute(SB_FOLDER_PATH, folderListing(SB_FOLDER_PATH, SB_ENTRIES))] } },
  render: () => renderBrowserHost(SB_FOLDER_PATH),
  play: async () => {
    // The real VueFinder mounts over the one declared listing; no error line.
    await waitFor(() => expect(screen.getAllByText('storybook-report.txt').length).toBeGreaterThan(0))
    await waitFor(() => expect(screen.getAllByText('storybook-sub').length).toBeGreaterThan(0))
    expect(document.querySelector('.fbr-error')).toBeNull()
    expect(document.querySelector('.fbr-loading')).toBeNull()
    // The explorer chrome (toolbar with the breadcrumb) mounted with the tree hidden until expanded.
    expect(document.querySelector('.vuefinder__toolbar')).toBeTruthy()
  },
}

export const Loading: Story = {
  parameters: { fixture: { routes: [dirHeld(SB_FOLDER_PATH)] } },
  render: () => renderBrowser(SB_FOLDER_PATH),
  play: async () => {
    // The held initial read keeps the Loading status line; the explorer is absent.
    await waitFor(() => expect(screen.getByText('Loading…')).toBeTruthy())
    const line = document.querySelector('.fbr-loading')
    expect(line?.getAttribute('role')).toBe('status')
    expect(document.querySelector('.vuefinder')).toBeNull()
  },
}

export const LoadError: Story = {
  parameters: { fixture: { routes: [dirError(SB_FOLDER_PATH, 404, 'not found')] } },
  render: () => renderBrowser(SB_FOLDER_PATH),
  play: async () => {
    // A failing listing names the folder in the alert line, never a half list.
    await waitFor(() => expect(screen.getByText('Folder not found.')).toBeTruthy())
    expect(document.querySelector('.fbr-error')?.getAttribute('role')).toBe('alert')
    expect(document.querySelector('.vuefinder')).toBeNull()
  },
}

export const Truncated: Story = {
  parameters: {
    fixture: {
      routes: [dirRoute(SB_FOLDER_PATH, folderListing(SB_FOLDER_PATH, SB_ENTRIES, true))],
    },
  },
  render: () => renderBrowser(SB_FOLDER_PATH),
  play: async () => {
    // The truncation note names how many entries the read answered; the
    // explorer's rows paint after the note, so both wait for their own frame.
    await waitFor(() => expect(screen.getByText('Showing the first 2 items.')).toBeTruthy())
    await waitFor(() => expect(screen.getAllByText('storybook-report.txt').length).toBeGreaterThan(0))
    expect(document.querySelector('.fbr-note')?.getAttribute('role')).toBe('status')
  },
}

export const Navigate: Story = {
  parameters: {
    fixture: { routes: [dirRoute(SB_FOLDER_PATH, folderListing(SB_FOLDER_PATH, SB_ENTRIES)), dirRoute(SB_SUB_PATH, SB_SUB_LISTING)] },
  },
  render: () => renderBrowserHost(SB_FOLDER_PATH),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getAllByText('storybook-sub').length).toBeGreaterThan(0))
    // Entering the folder re-reads the sub listing through the driver and re-emits navigate.
    await event.dblClick(listRow('storybook-sub'))
    await waitFor(() => expect(screen.getAllByText('storybook-sub-note.txt').length).toBeGreaterThan(0))
    expect(eventsLine()).toBe('Events: navigate:/notes/storybook-drafts/storybook-sub')
    expect(screen.queryByText('storybook-report.txt')).toBeNull()
  },
}

export const OpenFile: Story = {
  parameters: { fixture: { routes: [dirRoute(SB_FOLDER_PATH, folderListing(SB_FOLDER_PATH, SB_ENTRIES))] } },
  render: () => renderBrowserHost(SB_FOLDER_PATH),
  play: async ({ userEvent: event }) => {
    await waitFor(() => expect(screen.getAllByText('storybook-report.txt').length).toBeGreaterThan(0))
    // Double-clicking a file emits its absolute open path; no directory read fires.
    await event.dblClick(listRow('storybook-report.txt'))
    expect(eventsLine()).toBe('Events: open-file:/notes/storybook-drafts/storybook-report.txt')
    expect(screen.getAllByText('storybook-report.txt').length).toBeGreaterThan(0)
  },
}
