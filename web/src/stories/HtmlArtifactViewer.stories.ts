import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { defineComponent, h, ref } from 'vue'
import HtmlArtifactViewer from '../components/HtmlArtifactViewer.vue'

/**
 * The production artifact viewer (#3302, slice B17): props-only owner whose
 * `view` prop is controlled, so stories render it with real props and a
 * wrapper that owns the view state the way its parents do. The preview frame
 * reaches /api/workspace-html by plain iframe navigation, which the fixture
 * boundary does not fence; every story asserts the sandbox/iframe attributes
 * the artifact contract depends on. Fixed inert HTML only — no live sources.
 */

const SB_PATH = '/notes/storybook-alpha.html'
const SB_SOURCE = [
  '<!doctype html>',
  '<html><body>',
  '<p>Fixed inert storybook artifact.</p>',
  '</body></html>',
  '',
].join('\n')

function renderArtifact(props: {
  filePath?: string
  reloadToken?: number | string
  view?: 'preview' | 'code'
  source?: string
  sourceLoading?: boolean
  sourceError?: string
}) {
  return defineComponent({
    setup() {
      // The parents own the controlled `view`; the wrapper mirrors that here.
      const view = ref(props.view ?? 'preview')
      return () => h(HtmlArtifactViewer, {
        filePath: props.filePath ?? SB_PATH,
        reloadToken: props.reloadToken ?? 0,
        source: props.source ?? '',
        sourceLoading: props.sourceLoading ?? false,
        sourceError: props.sourceError ?? '',
        view: view.value,
        'onUpdate:view': (value) => { view.value = value },
      })
    },
  })
}

const artifactRoot = (scope: unknown): HTMLElement => {
  const found = (scope as { querySelector?: (selector: string) => Element | null }).querySelector?.('.hav')
  if (!found) throw new Error('Artifact root not rendered')
  return found as HTMLElement
}

const meta = {
  id: 'html-artifact-viewer',
  title: 'File viewers/HtmlArtifactViewer',
  component: HtmlArtifactViewer,
  args: {
    filePath: SB_PATH,
    reloadToken: 0,
    view: 'preview',
    source: '',
  },
} satisfies Meta<typeof HtmlArtifactViewer>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  render: () => renderArtifact({ view: 'preview' }),
  play: async ({ canvasElement }) => {
    const toolbar = screen.getByRole('group', { name: 'Artifact view' })
    const preview = toolbar.querySelector('.hav-tab.active')
    expect(preview?.getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: 'Code' }).getAttribute('aria-pressed')).toBe('false')
    const root = artifactRoot(canvasElement)
    const frame = root.querySelector('.hav-frame') as HTMLIFrameElement
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
    expect(frame.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(frame.getAttribute('title')).toBe(`Artifact preview: ${SB_PATH}`)
    expect(frame.getAttribute('loading')).toBe('lazy')
    expect(frame.getAttribute('src')).toBe(`/api/workspace-html?path=${encodeURIComponent(SB_PATH)}&t=0`)
  },
}

export const Code: Story = {
  render: () => renderArtifact({ view: 'code', source: SB_SOURCE }),
  play: async ({ canvasElement, userEvent: event }) => {
    const toolbar = screen.getByRole('group', { name: 'Artifact view' })
    expect(screen.getByRole('button', { name: 'Code' }).getAttribute('aria-pressed')).toBe('true')
    expect(toolbar.querySelector('.hav-tab.active')?.textContent).toBe('Code')
    // The source renders one line per element, whitespace kept.
    const root = artifactRoot(canvasElement)
    const lines = root.querySelectorAll('.hav-code-line')
    expect(lines.length).toBe(5)
    expect(lines[2]?.textContent).toBe('<p>Fixed inert storybook artifact.</p>')
    expect(root.querySelector('.hav-frame')).toBeNull()
    // Toggling back to Preview unmounts the source and remounts the sandboxed frame.
    await event.click(screen.getByRole('button', { name: 'Preview' }))
    await waitFor(() => {
      const frame = root.querySelector('.hav-frame') as HTMLIFrameElement | null
      expect(frame?.getAttribute('sandbox')).toBe('allow-scripts')
    })
  },
}

export const CodeLoading: Story = {
  render: () => renderArtifact({ view: 'code', sourceLoading: true }),
  play: async ({ canvasElement }) => {
    expect(screen.getByText('Loading source…')).toBeTruthy()
    const root = artifactRoot(canvasElement)
    expect(root.querySelector('.hav-frame')).toBeNull()
    expect(root.querySelector('.hav-code')).toBeNull()
  },
}

export const CodeError: Story = {
  render: () => renderArtifact({ view: 'code', sourceError: 'Failed to load source (HTTP 404).' }),
  play: async () => {
    const note = screen.getByText('Failed to load source (HTTP 404).')
    expect(note.classList.contains('hav-note-error')).toBe(true)
    // The error note replaces the code, never the frame: Preview stays intact.
    expect(screen.getByRole('group', { name: 'Artifact view' }).querySelector('.hav-tab')).toBeTruthy()
  },
}

export const Narrow: Story = {
  render: () => renderArtifact({ view: 'preview' }),
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  play: async ({ canvasElement }) => {
    const toolbar = screen.getByRole('group', { name: 'Artifact view' })
    expect(toolbar.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    const frame = artifactRoot(canvasElement).querySelector('.hav-frame') as HTMLIFrameElement
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
  },
}
