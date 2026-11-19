import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import MemoryMapView from '../components/MemoryMapView.vue'
import { mapNoteIds, mapRoutes, mountMapFixture } from './memoryMapFixture'

/**
 * The `.mm-list-wrap` family — the sortable vault list, row selection and the
 * orphans filter. The stories mount the production MemoryMapView seeded into
 * its list rendering; rows open the production tile.
 */

function renderState(state: 'list' | 'list-selection' | 'list-filter') {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountMapFixture>>()
      const error = ref('')
      onMounted(() => {
        try {
          fixture.value = mountMapFixture(host.value!, MemoryMapView, {
            view: 'list',
            selectedId: state === 'list-selection' ? mapNoteIds.reference : undefined,
          })
        } catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', {}, [
        h('p', `Synthetic ${state} fixture: production MemoryMapView list surface over the storybook vault.`),
        h('div', { ref: host }),
        h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map((message) => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

const meta = {
  id: 'memory-list-selection',
  title: 'Memory map/Memory list and selection',
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

function listRows(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas']): HTMLElement[] {
  return Array.from((canvas.getByRole('region', { name: 'Vault notes list' }).querySelectorAll('tbody tr')))
}

// The list renders the sortable table; the checked-age header opens oldest
// first (a descending aria-sort on the first click) and the undated note
// carries the dash placeholder and the stale row its review flag.
export const List: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded' }) } },
  render: () => renderState('list'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('region', { name: 'Vault notes list' })).toBeTruthy())
    const listButton = canvas.getByRole('button', { name: 'List' })
    expect(listButton.getAttribute('aria-pressed')).toBe('true')
    expect(listRows(canvas).length).toBe(11)
    expect(canvas.getByText('Needs review')).toBeTruthy()
    const checkedHeader = canvas.getByRole('button', { name: /Checked/ })
    await userEvent.click(checkedHeader)
    expect(checkedHeader.closest('th')?.getAttribute('aria-sort')).toBe('descending')
    const nameHeader = canvas.getByRole('button', { name: /Name/ })
    expect(nameHeader.closest('th')?.getAttribute('aria-sort')).toBe('none')
    expect(listRows(canvas)[10].textContent).toContain('Storybook meeting notes')
    await assertSettled(canvas)
  },
}

// A row's title button is the keyboard-reachable opener: pressing it selects
// the row, mirrors aria-pressed and opens the note's tile.
export const Selection: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded', notes: [mapNoteIds.reference] }) } },
  render: () => renderState('list-selection'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('region', { name: 'Vault notes list' })).toBeTruthy())
    const titleButton = canvas.getByRole('button', { name: 'Open Storybook reference' })
    await userEvent.click(titleButton)
    expect(titleButton.getAttribute('aria-pressed')).toBe('true')
    expect(titleButton.closest('tr')?.className).toContain('current')
    expect(document.activeElement).toBe(titleButton)
    await waitFor(() => expect(canvas.getByRole('dialog', { name: 'Details for Storybook reference' })).toBeTruthy())
    await assertSettled(canvas)
  },
}

// The orphans filter narrows the list to notes no other note links to.
export const Filter: Story = {
  parameters: { fixture: { routes: mapRoutes({ graph: 'loaded' }) } },
  render: () => renderState('list-filter'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('region', { name: 'Vault notes list' })).toBeTruthy())
    const filter = canvas.getByRole('combobox', { name: 'Which notes to show' })
    expect(filter.getAttribute('title')).toBe('1 notes have no links')
    await userEvent.selectOptions(filter, 'only')
    await waitFor(() => expect(listRows(canvas).length).toBe(1))
    expect(listRows(canvas)[0].textContent).toContain('Storybook orphan')
    expect(canvas.queryByText('Storybook person')).toBeNull()
    await assertSettled(canvas)
  },
}
