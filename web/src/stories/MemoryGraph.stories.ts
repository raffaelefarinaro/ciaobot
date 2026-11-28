import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import MemoryMapView from '../components/MemoryMapView.vue'
import { useMemoryMapStore } from '../stores/memoryMap'
import { mapNoteIds, mapRoutes, mountMapFixture } from './memoryMapFixture'

/**
 * The `.mm-canvas-wrap` family — the vault drawing with its toolbar, zoom
 * controls and on-canvas hint. The stories mount the production MemoryMapView
 * in its map section; the graph is the fail-closed synthetic fixture.
 */

function renderState(state: 'graph-dense' | 'graph-selection') {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountMapFixture>>()
      const error = ref('')
      onMounted(() => {
        try { fixture.value = mountMapFixture(host.value!, MemoryMapView, { view: 'graph' }) }
        catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      const dense = state === 'graph-dense'
      return () => {
        return h('div', {}, [
        h('p', dense
          ? 'Dense synthetic vault (44 notes): fit scale is past the label gate, so titles come from hover or zoom.'
          : 'Small synthetic vault: the fixture control drives the production sidebar path into a note.'),
        // The sidebar and the sidebar's lists focus notes through the store's
        // production requestFocus; the fixture stands in for that caller.
        !dense
          ? h('button', {
            type: 'button',
            'aria-label': 'Fixture: show Storybook reference on the map',
            onClick: () => {
              if (fixture.value) useMemoryMapStore(fixture.value.pinia).requestFocus(mapNoteIds.reference)
            },
          }, 'Fixture: show a note on the map')
          : null,
        h('div', { ref: host, style: 'container: chat-pane / inline-size' }),
        h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map((message) => h('p', { role: 'alert' }, message)),
        ])
      }
    },
  })
}

const meta = {
  id: 'memory-graph',
  title: 'Memory map/Memory graph',
  component: MemoryMapView,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof MemoryMapView>
export default meta
type Story = StoryObj<typeof meta>

async function assertSettled(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas']) {
  await waitFor(() => expect(canvas.getByText(/Fixture requests: [1-9]/)).toBeTruthy())
  await new Promise((resolve) => setTimeout(resolve, 500))
  expect(canvas.getByText(/Violations: 0\./)).toBeTruthy()
  expect(canvas.queryByText(/Story network violation/)).toBeNull()
}

// Fit scale over a vault past the label gate: titles hidden, the hover hint on,
// stale notes counted in the toolbar. Zooming in three steps opens the label
// gate and flips the hint to the click-to-open wording.
export const Graph: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded', dense: true }) } },
  render: () => renderState('graph-dense'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('region', { name: 'Vault graph' })).toBeTruthy())
    await waitFor(() => expect(canvas.getByText('44', { selector: '.mm-toolbar-stats strong' })).toBeTruthy())
    expect(canvas.getByText(/Hover or tap a note to name it/)).toBeTruthy()
    // The camera tweens for 160 ms per step, so each click waits for the last.
    const zoomIn = canvas.getByRole('button', { name: 'Zoom in' })
    for (let step = 0; step < 4; step += 1) {
      await userEvent.click(zoomIn)
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    await waitFor(() => expect(canvas.getByText(/Click a note to open it and light up its neighbours/)).toBeTruthy())
    const graphButton = canvas.getByRole('button', { name: 'Graph' })
    expect(graphButton.getAttribute('aria-pressed')).toBe('true')
    await assertSettled(canvas)
  },
}

// A note named from outside the canvas: the fixture button walks the
// production focus path, the camera flies in and the note's tile opens.
export const Selection: Story = {
  parameters: {
    fixture: {
      routes: mapRoutes({ graph: 'loaded', notes: [mapNoteIds.reference, 'storybook-vault/Storybook hub.md'] }),
    },
  },
  render: () => renderState('graph-selection'),
  play: async ({ canvas, userEvent }) => {
    const drive = canvas.getByRole('button', { name: 'Fixture: show Storybook reference on the map' })
    await userEvent.click(drive)
    await waitFor(() => expect(canvas.getByRole('dialog', { name: 'Details for Storybook reference' })).toBeTruthy())
    expect(canvas.getAllByRole('button', { name: 'Show Storybook hub on the map' }).length).toBe(1)
    await assertSettled(canvas)
  },
}
