import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { createApp, defineComponent, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { createPinia } from 'pinia'
import ProviderInventory from '../components/settings/ProviderInventory.vue'
import type { ProviderInventoryData } from '../components/settings/ProviderInventory.vue'

// Synthetic provider names only: `storybook-*` ids and no real account data.
// The inventory reads the terminal store for its browser-terminal action, so
// each story mounts the owner in its own app with Pinia, as WorkspaceTile does.
type InventorySeed = { inventory?: ProviderInventoryData }

function renderInventory(seed: InventorySeed) {
  return defineComponent({
    name: 'ProviderInventoryStory',
    setup() {
      const host = ref<HTMLElement>()
      let app: ReturnType<typeof createApp> | undefined
      onMounted(() => {
        app = createApp({
          render: () => h(ProviderInventory, {
            workspace: 'storybook-alpha',
            command: 'storybook-alpha auth login',
            inventory: seed.inventory,
          }),
        })
        app.use(createPinia())
        app.mount(host.value!)
      })
      onBeforeUnmount(() => app?.unmount())
      return () => h('div', { ref: host })
    },
  })
}

const meta = {
  id: 'provider-inventory',
  title: 'Settings general/ProviderInventory',
  component: ProviderInventory,
  args: { workspace: 'storybook-alpha', command: 'storybook-alpha auth login' },
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof ProviderInventory>
export default meta
type Story = StoryObj<typeof meta>

// Each row says what was reported and that it is configured. It never claims a
// credential was verified: the one hint above the list says what OpenCode reports.
export const Configured: Story = {
  render: () => renderInventory({
    inventory: {
      status: 'complete',
      providers: [
        { id: 'storybook-one', name: 'Storybook One', auth_types: ['oauth', 'api_key'], configured: true, verified: null },
        { id: 'storybook-two', name: 'Storybook Two', auth_types: ['environment'], configured: true, verified: null },
      ],
    },
  }),
  play: async ({ canvasElement }) => {
    const text = canvasElement.textContent ?? ''
    expect(text).toContain('OpenCode reports which providers are configured. It does not check that their credentials work.')
    expect(text).toContain('OAuth, API key · Configured')
    expect(text).toContain('Environment · Configured')
    expect(text).not.toContain('verification unknown')
  },
}

export const NoneReported: Story = {
  render: () => renderInventory({ inventory: { status: 'complete', providers: [] } }),
  play: async ({ canvasElement }) => {
    expect(canvasElement.textContent).toContain('No configured providers reported')
  },
}

export const PartiallyReported: Story = {
  render: () => renderInventory({ inventory: { status: 'partial', providers: [] } }),
  play: async ({ canvasElement }) => {
    expect(canvasElement.textContent).toContain('Some providers may be missing from this inventory.')
  },
}

export const Unavailable: Story = {
  render: () => renderInventory({ inventory: { status: 'unknown', providers: [] } }),
  play: async ({ canvasElement }) => {
    expect(canvasElement.textContent).toContain('OpenCode could not report its configured providers.')
  },
}
