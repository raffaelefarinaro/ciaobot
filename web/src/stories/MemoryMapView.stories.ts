import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import MemoryMapView from '../components/MemoryMapView.vue'
import { mapNoteIds, mapRoutes, mountMapFixture } from './memoryMapFixture'

/**
 * The MemoryMapView composition over the fail-closed fixture boundary. Every
 * story mounts the production component (whose own imports own its children)
 * with the synthetic storybook vault. The Unsure section reads its list from
 * the declared `/api/memory/unsure` route (thirteen synthetic lines).
 */

/** Which seeded note the tile shows; the tile reads the note's own file. */
function selectedIdFor(state: MapState): string | undefined {
  switch (state) {
    case 'detail-plain': return mapNoteIds.reference
    case 'detail-stale': return mapNoteIds.project
    case 'detail-facts': return mapNoteIds.facts
    default: return undefined
  }
}

/** Reads the selected note and (for the plain detail) the neighbour the play swaps to. */
function noteFilesFor(state: MapState): string[] {
  switch (state) {
    case 'detail-plain': return [mapNoteIds.reference, 'storybook-vault/Storybook hub.md']
    case 'detail-stale': return [mapNoteIds.project]
    case 'detail-facts': return [mapNoteIds.facts]
    default: return []
  }
}

type MapState =
  | 'default' | 'unsure' | 'unsure-phone' | 'unsure-empty' | 'unsure-error' | 'unsure-many' | 'loading' | 'error'
  | 'detail-plain' | 'detail-stale' | 'detail-facts'

const stateDescription: Record<MapState, string> = {
  default: 'the store default section (the map): toolbar over the drawing',
  unsure: 'the Unsure queue: thirteen lines, one approved, the first open in full, with the rail windows',
  'unsure-phone': 'the Unsure queue in a 390px pane: one card at a time with the buttons under it',
  'unsure-empty': 'the Unsure section with nothing waiting',
  'unsure-error': 'the Unsure section when the list cannot be read',
  'unsure-many': 'the Unsure section with sixty lines',
  loading: 'a held vault graph read behind the graph skeleton',
  error: 'a failed vault graph read',
  'detail-plain': 'a selected note with no callout, neighbours swappable in play',
  'detail-stale': 'a selected stale note with the age callout',
  'detail-facts': 'a selected note with facts nobody re-checked',
}

function mountState(host: HTMLElement, state: MapState) {
  return mountMapFixture(host, MemoryMapView, {
    section: state.startsWith('unsure') ? 'unsure' : undefined,
    view: state === 'default' || state.startsWith('unsure') ? undefined : 'graph',
    selectedId: selectedIdFor(state),
  })
}

function renderState(state: MapState) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountState>>()
      const error = ref('')
      onMounted(() => {
        try { fixture.value = mountState(host.value!, state) }
        catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', {}, [
        h('p', `Synthetic ${state} fixture: production MemoryMapView with ${stateDescription[state]}.`),
        // The phone story's host is the pane the app gives the page: 390px and the
        // `chat-pane` container its grid answers to, so the rail drops below.
        h('div', state === 'unsure-phone' ? { ref: host, style: 'max-width: 390px; container: chat-pane / inline-size' } : { ref: host }),
        h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map((message) => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

const meta = {
  id: 'memory-map-view',
  title: 'Memory map/MemoryMapView',
  component: MemoryMapView,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof MemoryMapView>
export default meta
type Story = StoryObj<typeof meta>

// Under the browser runner the fixture fence sees every transport the owner
// makes; these plays let late reporting arrive and then assert the ledger is
// empty and the story's own state is on screen.
async function assertSettled(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas']) {
  await waitFor(() => expect(canvas.getByText(/Fixture requests: [1-9]/)).toBeTruthy())
  await new Promise((resolve) => setTimeout(resolve, 500))
  expect(canvas.getByText(/Violations: 0\./)).toBeTruthy()
  expect(canvas.queryByText(/Story network violation/)).toBeNull()
}

// The store's default section is the map: toolbar over the drawing.
export const Default: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded' }) } },
  render: () => renderState('default'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('group', { name: 'View' })).toBeTruthy())
    expect(canvas.getByText('Memory · Map')).toBeTruthy()
    expect(canvas.queryByRole('complementary', { name: 'Always loaded' })).toBeNull()
    await assertSettled(canvas)
  },
}

const NORTHWIND = 'Is the live Northwind deployment unaffected by the December 11 model retirement?'

// The Unsure queue: the list beside the first item in full (with the line it
// saves and where), the rail's two docked windows, S skipping without a write,
// and Approve writing the tick through the one route and moving to the next.
export const Unsure: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded' }) } },
  render: () => renderState('unsure'),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('heading', { name: 'Waiting for you' })).toBeTruthy())
    expect(canvas.getByText('Memory · Unsure')).toBeTruthy()
    expect(canvas.getByRole('heading', { name: NORTHWIND, level: 3 })).toBeTruthy()
    expect(canvas.getByText('The live Northwind deployment uses no model that retires on December 11.')).toBeTruthy()
    // The destination note opens from the box.
    expect(canvas.getByText('projects/active/Northwind/Northwind.md').tagName).toBe('BUTTON')
    expect(canvas.getByRole('heading', { name: 'How this works' })).toBeTruthy()
    expect(canvas.getByRole('heading', { name: 'Always loaded' })).toBeTruthy()
    expect(canvasElement.querySelector('[data-unsure-count]')?.textContent).toBe('1 of 12')
    await userEvent.keyboard('s')
    await waitFor(() => expect(canvasElement.querySelector('[data-unsure-count]')?.textContent).toBe('2 of 12'))
    await userEvent.click(canvasElement.querySelector<HTMLElement>('[data-unsure-row]')!)
    await waitFor(() => expect(canvas.getByRole('heading', { name: NORTHWIND, level: 3 })).toBeTruthy())
    await userEvent.click(canvas.getByRole('button', { name: `Approve: ${NORTHWIND}` }))
    await waitFor(() => expect(canvas.getByText(`Approved “${NORTHWIND}”. The next Workspace care run saves it.`)).toBeTruthy())
    expect(canvas.getByRole('button', { name: 'Undo' })).toBeTruthy()
    expect(canvas.getByRole('heading', { name: /^Does the November 27 summary/, level: 3 })).toBeTruthy()
    await assertSettled(canvas)
  },
}

// The same queue in a phone-wide pane: one card, its place, the swipe hints and
// the two 44px buttons under it; Approve sends the card off and shows the next.
export const UnsurePhone: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded' }) } },
  render: () => renderState('unsure-phone'),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await waitFor(() => expect(canvasElement.querySelector('[data-unsure-card]')).toBeTruthy())
    expect(canvasElement.querySelector('[data-unsure-count]')?.textContent).toBe('1 of 12')
    expect(canvas.getByText('← swipe left to dismiss')).toBeTruthy()
    const approve = canvas.getByRole('button', { name: `Approve: ${NORTHWIND}` })
    expect(approve.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    expect(canvas.getByRole('button', { name: `Dismiss: ${NORTHWIND}` }).getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    await userEvent.click(approve)
    await waitFor(() => expect(canvasElement.querySelector('[data-unsure-card]')?.textContent).toContain('Does the November 27 summary'))
    await assertSettled(canvas)
  },
}

export const UnsureEmpty: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded', unsure: 'empty' }) } },
  render: () => renderState('unsure-empty'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/Nothing is waiting\. Care adds a line here only when/)).toBeTruthy())
    expect(canvas.getByRole('button', { name: 'Open Unsure.md' }).getAttribute('disabled')).not.toBeNull()
    await assertSettled(canvas)
  },
}

export const UnsureError: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded', unsure: 'error' }) } },
  render: () => renderState('unsure-error'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('alert')).toBeTruthy())
    expect(canvas.getByText(/Could not read the list\./)).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Try again' })).toBeTruthy()
    await assertSettled(canvas)
  },
}

export const UnsureMany: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded', unsure: 'many' }) } },
  render: () => renderState('unsure-many'),
  play: async ({ canvas, canvasElement }) => {
    await waitFor(() => expect(canvas.getByRole('heading', { name: 'Waiting for you' })).toBeTruthy())
    expect(canvasElement.querySelectorAll('[data-unsure-row]')).toHaveLength(60)
    expect(canvasElement.querySelector('[data-unsure-count]')?.textContent).toBe('1 of 60')
    await assertSettled(canvas)
  },
}

// A held first vault read: the map body is the graph skeleton, no toolbar yet.
export const Loading: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'held' }) } },
  render: () => renderState('loading'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('status', { name: 'Loading vault graph' })).toBeTruthy())
    expect(canvas.queryByRole('group', { name: 'View' })).toBeNull()
    expect(canvas.queryByRole('region', { name: 'Vault graph' })).toBeNull()
    await assertSettled(canvas)
  },
}

// A failed first vault read: the error line, and neither skeleton nor drawing.
export const Error: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'error' }) } },
  render: () => renderState('error'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/Synthetic vault graph unavailable/)).toBeTruthy())
    expect(canvas.queryByRole('status', { name: 'Loading vault graph' })).toBeNull()
    expect(canvas.queryByRole('region', { name: 'Vault graph' })).toBeNull()
    expect(canvas.queryByRole('group', { name: 'View' })).toBeNull()
    await assertSettled(canvas)
  },
}

// A selected note without callouts: the tile is a dialog naming the note, and a
// linked note swaps the tile to that note (the production neighbour path).
export const SelectionDetail: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded', notes: noteFilesFor('detail-plain') }) } },
  render: () => renderState('detail-plain'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('dialog', { name: 'Details for Storybook reference' })).toBeTruthy())
    const neighbour = canvas.getByRole('button', { name: 'Show Storybook hub on the map' })
    await userEvent.click(neighbour)
    await waitFor(() => expect(canvas.getByRole('dialog', { name: 'Details for Storybook hub' })).toBeTruthy())
    expect(canvas.getByRole('heading', { name: /Linked notes/ })).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Close note' })).toBeTruthy()
    await assertSettled(canvas)
  },
}

// A stale note: the tile names the age and the rule, and links nowhere.
export const DetailStale: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded', notes: noteFilesFor('detail-stale') }) } },
  render: () => renderState('detail-stale'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/Unchecked for/)).toBeTruthy())
    expect(canvas.getByText(/Notes like this are due every 30 days\./)).toBeTruthy()
    expect(canvas.queryByRole('link', { name: 'Review in To decide' })).toBeNull()
    await assertSettled(canvas)
  },
}

// A note with per-fact coverage: the facts list names the overdue bullet.
export const DetailFacts: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded', notes: noteFilesFor('detail-facts') }) } },
  render: () => renderState('detail-facts'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/Facts inside this note/)).toBeTruthy())
    expect(canvas.getByText('Never checked')).toBeTruthy()
    expect(canvas.getByText('Storybook fact recorded in the vault.')).toBeTruthy()
    await assertSettled(canvas)
  },
}
