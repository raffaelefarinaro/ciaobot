import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import MemoryMapView from '../components/MemoryMapView.vue'
import { mapNoteIds, mapRoutes, mountMapFixture } from './memoryMapFixture'

/**
 * The MemoryMapView composition over the fail-closed fixture boundary. Every
 * story mounts the production component (whose own imports own its children)
 * with the synthetic storybook vault; the review queues answer empty so the
 * To decide section renders its own chrome only. The queue bodies themselves
 * belong to their own component rows.
 */

/** Which seeded note the tile shows; the tile reads the note's own file. */
function selectedIdFor(state: MapState): string | undefined {
  switch (state) {
    case 'detail-plain': return mapNoteIds.reference
    case 'detail-stale': return mapNoteIds.project
    case 'detail-pending': return mapNoteIds.pending
    case 'detail-conflicted': return mapNoteIds.conflicted
    case 'detail-facts': return mapNoteIds.facts
    default: return undefined
  }
}

/** Reads the selected note and (for the plain detail) the neighbour the play swaps to. */
function noteFilesFor(state: MapState): string[] {
  switch (state) {
    case 'detail-plain': return [mapNoteIds.reference, 'storybook-vault/Storybook hub.md']
    case 'detail-stale': return [mapNoteIds.project]
    case 'detail-pending': return [mapNoteIds.pending]
    case 'detail-conflicted': return [mapNoteIds.conflicted]
    case 'detail-facts': return [mapNoteIds.facts]
    default: return []
  }
}

type MapState =
  | 'default' | 'loading' | 'error'
  | 'detail-plain' | 'detail-stale' | 'detail-pending' | 'detail-conflicted' | 'detail-facts'

const stateDescription: Record<MapState, string> = {
  default: 'the store default section (To decide): filter chips over the two queues',
  loading: 'a held vault graph read behind the graph skeleton',
  error: 'a failed vault graph read',
  'detail-plain': 'a selected note with no callout, neighbours swappable in play',
  'detail-stale': 'a selected stale note with the review callout',
  'detail-pending': 'a selected note whose verification filed a proposal',
  'detail-conflicted': 'a selected note whose verification no longer applies',
  'detail-facts': 'a selected note with facts nobody re-checked',
}

function mountState(host: HTMLElement, state: MapState) {
  // Every state except the default To decide section lives in the map section.
  return mountMapFixture(host, MemoryMapView, {
    view: state === 'default' ? undefined : 'graph',
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
        h('div', { ref: host }),
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

// The store's default section is To decide, so the composition's default render
// is the review chrome: the two-queue filter chips with counts and no zero chips.
export const Default: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded' }) } },
  render: () => renderState('default'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('button', { name: 'All 0' })).toBeTruthy())
    const all = canvas.getByRole('button', { name: 'All 0' })
    expect(all.getAttribute('aria-pressed')).toBe('true')
    expect(canvas.queryByRole('button', { name: /Suggested/ })).toBeNull()
    expect(canvas.queryByRole('button', { name: /To revisit/ })).toBeNull()
    expect(canvas.getByText('Memory · To decide')).toBeTruthy()
    expect(canvas.getByRole('complementary', { name: 'Always loaded' })).toBeTruthy()
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

// A stale note: the tile suppresses nothing else and offers the To decide link.
export const DetailStale: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded', notes: noteFilesFor('detail-stale') }) } },
  render: () => renderState('detail-stale'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/Unchecked for/)).toBeTruthy())
    expect(canvas.getByText(/Notes like this are due every 30 days\./)).toBeTruthy()
    const link = canvas.getByRole('link', { name: 'Review in To decide' })
    expect(link.getAttribute('href')).toBe('/memory/review?show=revisit')
    await assertSettled(canvas)
  },
}

// A note with a verification that filed a proposal: the tile answers why the
// note is not being asked again on top of the pending question.
export const DetailPending: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded', notes: noteFilesFor('detail-pending') }) } },
  render: () => renderState('detail-pending'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/Being checked\./)).toBeTruthy())
    const link = canvas.getByRole('link', { name: 'Open the proposal' })
    expect(link.getAttribute('href')).toBe('/memory/review?show=suggested')
    await assertSettled(canvas)
  },
}

// A note whose verification answers an older revision: the verdict is dead and
// the tile routes to the revisit queue.
export const DetailConflicted: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded', notes: noteFilesFor('detail-conflicted') }) } },
  render: () => renderState('detail-conflicted'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/A verification is out of date for this note\./)).toBeTruthy())
    const link = canvas.getByRole('link', { name: 'Review in To decide' })
    expect(link.getAttribute('href')).toBe('/memory/review?show=revisit')
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
