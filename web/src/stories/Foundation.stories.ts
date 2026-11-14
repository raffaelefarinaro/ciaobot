import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { ref } from 'vue'
import type { ConcreteComponent } from 'vue'
import TabBar from '../components/TabBar.vue'
import type { TabSpec } from '../components/TabBar.vue'

type TabBarArgs = { modelValue: string; tabs: TabSpec<string>[]; label: string; idPrefix: string }

const meta = {
  id: 'foundation-tab-bar',
  title: 'Foundation/TabBar',
  component: TabBar as unknown as ConcreteComponent<TabBarArgs>,
  tags: ['autodocs'],
  argTypes: {
    modelValue: { control: false, description: 'Selection changes locally by clicking or using the keyboard.' },
  },
  render: (args) => ({
    components: { TabBar },
    setup() {
      const selected = ref<string>(args.modelValue)
      return { args, selected }
    },
    template: `<div :style="args.idPrefix === 'narrow-pane' ? 'width: 390px; max-width: 100%' : ''">
      <TabBar v-model="selected" :tabs="args.tabs" :label="args.label" :id-prefix="args.idPrefix" />
      <div v-for="tab in args.tabs" :key="tab.key" v-show="selected === tab.key"
        role="tabpanel" :id="args.idPrefix + '-panel-' + tab.key"
        :aria-labelledby="args.idPrefix + '-tab-' + tab.key">{{ tab.label }} panel</div>
    </div>`,
  }),
} satisfies Meta<TabBarArgs>
export default meta
type Story = StoryObj<typeof meta>

const tabs: TabSpec<string>[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'activity', label: 'Activity' },
  { key: 'settings', label: 'Settings' },
]

export const Default: Story = { args: { label: 'Sections', idPrefix: 'default', tabs, modelValue: 'overview' } }
export const Counts: Story = { args: { label: 'Queues', idPrefix: 'counts', modelValue: 'overview', tabs: [
  { key: 'overview', label: 'Empty', count: 0 },
  { key: 'activity', label: 'Pending', count: 12 },
  { key: 'settings', label: 'All' },
] } }
export const LongLabels: Story = { args: { label: 'Sections', idPrefix: 'long-labels', modelValue: 'overview', tabs: [
  { key: 'overview', label: 'Workspace overview and details' },
  { key: 'activity', label: 'Recent activity and history' },
  { key: 'settings', label: 'Settings and preferences' },
] } }
export const NarrowPane: Story = { args: { label: 'Sections', idPrefix: 'narrow-pane', modelValue: 'preferences', tabs: [
  { key: 'overview', label: 'Workspace overview' },
  { key: 'activity', label: 'Recent activity' },
  { key: 'settings', label: 'Workspace settings' },
  { key: 'preferences', label: 'Display preferences' },
] } }
