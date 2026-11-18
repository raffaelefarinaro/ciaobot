import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import HomeTodayStats from '../components/HomeTodayStats.vue'
import { mountConnectedStats } from './connectedStats'
import { statsWorkspaces, type StatsState } from './statsBoundary'

function renderState(state: StatsState) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountConnectedStats>>()
      const error = ref('')
      onMounted(() => {
        try { fixture.value = mountConnectedStats(host.value!, state) }
        catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', { style: 'max-width: 390px' }, [
        h('p', `Synthetic ${state} fixture. Loading and failure both show dashes in the real owner.`),
        h('label', [
          'Fixture workspace ',
          h('select', {
            'aria-label': 'Fixture workspace',
            onChange: (event: Event) => {
              if (fixture.value) fixture.value.projects.activeWorkspace = (event.target as HTMLSelectElement).value
            },
          }, statsWorkspaces.map(value => h('option', { value }, value))),
        ]),
        h('div', { ref: host }),
        h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ... (fixture.value?.boundary.violations ?? []).map(message => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}
const meta = {
  id: 'connected-home-today-stats',
  title: 'Connected/Home Today stats',
  component: HomeTodayStats,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof HomeTodayStats>
export default meta
type Story = StoryObj<typeof meta>
// Under `npm run test-storybook` the vitest browser runner's own control socket
// (/__vitest_browser_api__) is live while these stories render and report. The
// fence sees that traffic: anything it does not recognize is rejected and
// recorded as a "Story network violation" in the visible ledger (and throws on
// the runner's socket). Each story therefore settles past the runner's
// reporting and asserts the ledger is empty.
const playSettled: Story['play'] = async ({ canvas }) => {
  await waitFor(() => expect(canvas.getByText(/Fixture requests: [1-9]/)).toBeTruthy())
  await new Promise(resolve => setTimeout(resolve, 500))
  expect(canvas.getByText(/Violations: 0\./)).toBeTruthy()
  expect(canvas.queryAllByRole('alert')).toHaveLength(0)
  expect(canvas.queryByText(/Story network violation/)).toBeNull()
}
export const Default: Story = { render: () => renderState('default'), play: playSettled }
export const Loading: Story = { render: () => renderState('loading'), play: playSettled }
export const Failed: Story = { render: () => renderState('failed'), play: playSettled }
export const Partial: Story = { render: () => renderState('partial'), play: playSettled }
