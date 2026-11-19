import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import MemoryCategoriesPanel from '../components/MemoryCategoriesPanel.vue'
import { useProjectStore } from '../stores/projects'
import { categoriesRoutes, mapWorkspace, mountCategoriesFixture, secondWorkspace } from './memoryMapFixture'

/**
 * The MemoryCategoriesPanel component — the vault's category registry — over
 * the fail-closed fixture boundary. Every story mounts the production
 * component with the synthetic `storybook-*` registry and declares exactly
 * the reads and writes it needs.
 */
function mountState(host: HTMLElement) {
  return mountCategoriesFixture(host, MemoryCategoriesPanel)
}

function renderState(state: string) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountState>>()
      const error = ref('')
      onMounted(() => {
        try { fixture.value = mountState(host.value!) }
        catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', {}, [
        h('p', `Synthetic ${state} fixture: production MemoryCategoriesPanel with the storybook registry.`),
        h('label', [
          'Fixture workspace ',
          h('select', {
            'aria-label': 'Fixture workspace',
            onChange: (event: Event) => {
              if (fixture.value) useProjectStore(fixture.value.pinia).activeWorkspace = (event.target as HTMLSelectElement).value
            },
          }, [mapWorkspace, secondWorkspace].map((value) => h('option', { value }, value))),
        ]),
        h('div', { ref: host }),
        h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map((message) => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

const meta = {
  id: 'memory-categories-panel',
  title: 'Memory map/MemoryCategoriesPanel',
  component: MemoryCategoriesPanel,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof MemoryCategoriesPanel>
export default meta
type Story = StoryObj<typeof meta>

async function assertSettled(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas']) {
  await waitFor(() => expect(canvas.getByText(/Fixture requests: [1-9]/)).toBeTruthy())
  await new Promise((resolve) => setTimeout(resolve, 500))
  expect(canvas.getByText(/Violations: 0\./)).toBeTruthy()
  expect(canvas.queryByText(/Story network violation/)).toBeNull()
}

// The loaded registry: the core switch is genuinely blocked, a custom switch
// toggles the draft with an Unsaved flag, and Save round-trips the whole list.
export const Default: Story = {
  parameters: { fixture: { routes: categoriesRoutes({ mode: 'loaded', patch: 'echo' }) } },
  render: () => renderState('loaded'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText('3 of 3 categories on')).toBeTruthy())
    const core = canvas.getByRole('switch', { name: 'Storybook people is required and always on' })
    expect(core.getAttribute('disabled')).not.toBeNull()
    expect(core.getAttribute('aria-disabled')).toBe('true')
    expect(canvas.getByRole('button', { name: 'Saved' }).getAttribute('disabled')).not.toBeNull()
    await userEvent.click(canvas.getByRole('switch', { name: 'Disable Storybook custom type' }))
    expect(canvas.getByText('Unsaved')).toBeTruthy()
    expect(canvas.getByText('2 of 3 categories on')).toBeTruthy()
    const saveChanges = canvas.getByRole('button', { name: 'Save changes' })
    expect(saveChanges.getAttribute('disabled')).toBeNull()
    await userEvent.click(saveChanges)
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Saved' })).toBeTruthy())
    expect(canvas.queryByText('Unsaved')).toBeNull()
    expect(canvas.getByRole('switch', { name: 'Enable Storybook custom type' }).getAttribute('aria-checked')).toBe('false')
    expect(canvas.getByText('2 of 3 categories on')).toBeTruthy()
    await assertSettled(canvas)
  },
}

// A held first read: the rows-shaped skeleton, no table yet.
export const Loading: Story = {
  parameters: { fixture: { routes: categoriesRoutes({ mode: 'held' }) } },
  render: () => renderState('loading'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('status', { name: 'Loading categories' })).toBeTruthy())
    // Settle first: a read that wrongly answered would have drawn the table by now.
    await assertSettled(canvas)
    expect(canvas.getByRole('status', { name: 'Loading categories' })).toBeTruthy()
    expect(canvas.queryByRole('columnheader', { name: 'Category' })).toBeNull()
  },
}

// A failed first read: the alert names the failure and Retry asks again; the
// fixture refuses on every read, so the alert persists over two requests.
export const InitialError: Story = {
  parameters: { fixture: { routes: categoriesRoutes({ mode: 'error' }) } },
  render: () => renderState('initial-error'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText('Synthetic categories unavailable')).toBeTruthy())
    expect(canvas.getByRole('button', { name: 'Retry' })).toBeTruthy()
    await userEvent.click(canvas.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(canvas.getByText(/Fixture requests: 2\./)).toBeTruthy())
    expect(canvas.getByText('Synthetic categories unavailable')).toBeTruthy()
    await assertSettled(canvas)
  },
}

// A failed refresh over rows already on screen: the stale banner keeps the
// list, and Retry fails again without discarding what the user can still act on.
export const Stale: Story = {
  parameters: { fixture: { routes: categoriesRoutes({ mode: 'loaded', failingSecondWorkspace: true }) } },
  render: () => renderState('stale-fixture'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText('3 of 3 categories on')).toBeTruthy())
    await userEvent.selectOptions(canvas.getByRole('combobox', { name: 'Fixture workspace' }), secondWorkspace)
    await waitFor(() => expect(canvas.getByText(/These are the categories as they were last read\./)).toBeTruthy())
    expect(canvas.getByText(/Synthetic categories unavailable/)).toBeTruthy()
    expect(canvas.getAllByText('Storybook projects').length).toBeGreaterThan(0)
    await userEvent.click(canvas.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(canvas.getByText(/Fixture requests: 3\./)).toBeTruthy())
    expect(canvas.getByText(/These are the categories as they were last read\./)).toBeTruthy()
    await assertSettled(canvas)
  },
}

// A registry with no categories: the hint says so and Save stays blocked.
export const Empty: Story = {
  parameters: { fixture: { routes: categoriesRoutes({ mode: 'empty' }) } },
  render: () => renderState('empty'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('This vault has no categories yet.')).toBeTruthy())
    expect(canvas.getByText('0 of 0 categories on')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Add category' })).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Saved' }).getAttribute('disabled')).not.toBeNull()
    await assertSettled(canvas)
  },
}

// A held write: Saving… blocks the save rather than doubling it; the draft
// keeps whatever the user changed while the request is in flight.
export const Saving: Story = {
  parameters: { fixture: { routes: categoriesRoutes({ mode: 'loaded', patch: 'held' }) } },
  render: () => renderState('saving-fixture'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText('3 of 3 categories on')).toBeTruthy())
    await userEvent.click(canvas.getByRole('switch', { name: 'Disable Storybook custom type' }))
    const saveChanges = canvas.getByRole('button', { name: 'Save changes' })
    await userEvent.click(saveChanges)
    const saving = canvas.getByRole('button', { name: 'Saving…' })
    expect(saving.getAttribute('disabled')).not.toBeNull()
    await assertSettled(canvas)
  },
}

// A refused write: the alert names the failure, the draft stays as the user
// built it and Save stays enabled for a corrected retry.
export const SaveError: Story = {
  parameters: { fixture: { routes: categoriesRoutes({ mode: 'loaded', patch: 'error' }) } },
  render: () => renderState('save-error-fixture'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText('3 of 3 categories on')).toBeTruthy())
    await userEvent.click(canvas.getByRole('switch', { name: 'Disable Storybook custom type' }))
    await userEvent.click(canvas.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(canvas.getByText('Synthetic categories write refused')).toBeTruthy())
    expect(canvas.getByText('Unsaved')).toBeTruthy()
    await userEvent.click(canvas.getByRole('switch', { name: 'Enable Storybook custom type' }))
    expect(canvas.getByRole('button', { name: 'Saved' })).toBeTruthy()
    expect(canvas.getAllByRole('alert')).toHaveLength(1)
    await assertSettled(canvas)
  },
}

// The edit drawer: focus lands on the Label while the id is read-only, the
// built-in note replaces Delete for a built-in row — here a custom row keeps
// Delete — and Escape claims close.
export const DrawerEdit: Story = {
  parameters: { fixture: { routes: categoriesRoutes({ mode: 'loaded', patch: 'echo' }) } },
  render: () => renderState('drawer-edit-fixture'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText('3 of 3 categories on')).toBeTruthy())
    await userEvent.click(canvas.getByRole('button', { name: 'Edit Storybook custom type' }))
    const dialog = canvas.getByRole('dialog', { name: 'Storybook custom type' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(document.activeElement).toBe(canvas.getByLabelText('Label'))
    const idField = canvas.getByLabelText('ID')
    expect(idField.getAttribute('readonly')).not.toBeNull()
    expect(canvas.getByRole('button', { name: 'Delete category' })).toBeTruthy()
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(canvas.queryByRole('dialog')).toBeNull())
    await assertSettled(canvas)
  },
}

// The add drawer: focus lands on the id, presets are offered, a bad id shows
// the inline alert and blocks Save, and a valid one unblocks it.
export const DrawerAdd: Story = {
  parameters: { fixture: { routes: categoriesRoutes({ mode: 'loaded' }) } },
  render: () => renderState('drawer-add-fixture'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText('3 of 3 categories on')).toBeTruthy())
    await userEvent.click(canvas.getByRole('button', { name: 'Add category' }))
    expect(canvas.getByRole('dialog', { name: 'Add category' })).toBeTruthy()
    expect(canvas.getByText('Start from')).toBeTruthy()
    const idField = canvas.getByLabelText('ID')
    expect(document.activeElement).toBe(idField)
    await userEvent.type(idField, 'Storybook')
    expect(canvas.getByText('A lower-case word, hyphen-separated — customer, not Customer.')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Save' }).getAttribute('disabled')).not.toBeNull()
    await userEvent.clear(idField)
    await userEvent.type(idField, 'storybook-fixture')
    await userEvent.type(canvas.getByLabelText('Label'), 'Storybook fixture')
    expect(canvas.queryByText('A lower-case word, hyphen-separated — customer, not Customer.')).toBeNull()
    expect(canvas.getByRole('button', { name: 'Save' }).getAttribute('disabled')).toBeNull()
    await assertSettled(canvas)
  },
}

// At 390px the table keeps all six columns and scrolls sideways in its own
// region; the switches stay reachable.
export const Narrow: Story = {
  parameters: { fixture: { routes: categoriesRoutes({ mode: 'loaded' }) } },
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  render: () => renderState('narrow-fixture'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('region', { name: 'Categories' })).toBeTruthy())
    expect(canvas.getByText('3 of 3 categories on')).toBeTruthy()
    expect(canvas.getByRole('switch', { name: 'Storybook people is required and always on' })).toBeTruthy()
    expect(canvas.getByRole('switch', { name: 'Disable Storybook custom type' })).toBeTruthy()
    await assertSettled(canvas)
  },
}
