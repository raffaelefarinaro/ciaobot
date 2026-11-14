import type { Meta, StoryObj } from '@storybook/vue3-vite'
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
export const Default: Story = { render: () => renderState('default') }
export const Loading: Story = { render: () => renderState('loading') }
export const Failed: Story = { render: () => renderState('failed') }
export const Partial: Story = { render: () => renderState('partial') }
