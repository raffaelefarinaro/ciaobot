import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import UsageView from '../components/UsageView.vue'
import { mountConnectedUsage } from './connectedUsage'
import { usageWorkspaces, type UsageFixtureState } from './usageBoundary'

function renderState(state: UsageFixtureState) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountConnectedUsage>>()
      const error = ref('')
      onMounted(() => {
        try { fixture.value = mountConnectedUsage(host.value!, state) }
        catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', { style: 'max-width: 390px' }, [
        h('p', `Synthetic ${state} fixture for the production Usage page.`),
        h('label', [
          'Fixture workspace ',
          h('select', {
            'aria-label': 'Fixture workspace',
            onChange: (event: Event) => {
              if (fixture.value) fixture.value.projects.activeWorkspace = (event.target as HTMLSelectElement).value
            },
          }, usageWorkspaces.map(value => h('option', { value }, value))),
        ]),
        h('div', { ref: host }),
        h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map(message => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

const narrow: Meta<typeof UsageView>['decorators'] = [
  () => ({ template: '<div style="max-width: 390px"><story /></div>' }),
]

const meta = {
  id: 'usage-view',
  title: 'Usage/UsageView',
  component: UsageView,
  tags: ['!autodocs'],
  decorators: narrow,
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof UsageView>
export default meta
type Story = StoryObj<typeof meta>

async function assertFenceQuiet(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas']) {
  await waitFor(() => expect(canvas.getByText(/Fixture requests: [1-9]/)).toBeTruthy())
  await new Promise(resolve => setTimeout(resolve, 500))
  expect(canvas.getByText(/Violations: 0\./)).toBeTruthy()
  expect(canvas.queryByText(/Story network violation/)).toBeNull()
}

export const InitialLoading: Story = {
  render: () => renderState('initial-loading'),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    expect(canvas.getByRole('status', { name: 'Loading usage' })).toBeTruthy()
    expect(canvas.queryByText('Chats opened')).toBeNull()
  },
}

export const Loaded: Story = {
  render: () => renderState('loaded'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Chats opened')).toBeTruthy())
    await assertFenceQuiet(canvas)
    expect(canvas.getByText('248')).toBeTruthy()
    expect(canvas.getByText('Activity')).toBeTruthy()
    expect(canvas.getByText('Memory pipeline')).toBeTruthy()
  },
}

export const LoadFailed: Story = {
  render: () => renderState('failed'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText(/Could not load usage/)).toBeTruthy())
    await assertFenceQuiet(canvas)
    const retry = canvas.getByRole('button', { name: 'Try again' })
    await userEvent.click(retry)
    await waitFor(() => expect(canvas.getByText(/Fixture requests: 2/)).toBeTruthy())
    expect(canvas.getByText(/Could not load usage/)).toBeTruthy()
  },
}

export const RefreshLoading: Story = {
  render: () => renderState('refresh-loading'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText('248')).toBeTruthy())
    const rangeGroup = canvas.getByRole('group', { name: 'Range' })
    const ninety = Array.from(rangeGroup.querySelectorAll('button')).find(btn => btn.textContent === '90 days')
    expect(ninety).toBeTruthy()
    await userEvent.click(ninety!)
    await waitFor(() => expect(canvas.getAllByText('—').length).toBeGreaterThan(0))
    expect(canvas.queryByRole('status', { name: 'Loading usage' })).toBeNull()
    expect(ninety!.getAttribute('aria-pressed')).toBe('true')
  },
}

export const RangeFocus: Story = {
  render: () => renderState('loaded'),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByText('30 days')).toBeTruthy())
    const seven = canvas.getByRole('button', { name: '7 days' })
    await userEvent.click(seven)
    expect(document.activeElement).toBe(seven)
    expect(seven.getAttribute('aria-pressed')).toBe('true')
  },
}