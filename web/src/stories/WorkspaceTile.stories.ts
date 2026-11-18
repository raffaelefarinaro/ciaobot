import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { createApp, defineComponent, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { createPinia } from 'pinia'
import WorkspaceTile from '../components/WorkspaceTile.vue'
import { useProjectStore } from '../stores/projects'
import type { WorkspaceInfo } from '../lib/types'

// Synthetic registry only: `storybook-*` names, their own fake vault roots and
// the production accent presets in order. No real workspace name, path or user
// text reaches a story.
const presetColors = ['amber', 'pink', 'cyan', 'emerald', 'violet', 'blue', 'teal', 'lime', 'fuchsia', 'amber']
const registry: WorkspaceInfo[] = [
  'alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel', 'india', 'juliet',
].map((suffix, index) => ({
  name: `storybook-${suffix}`,
  vault_root: `storybook-${suffix}`,
  default_provider: 'claude',
  gws_profile: `storybook-${suffix}`,
  color: presetColors[index],
}))

type TileSeed = { workspaces?: WorkspaceInfo[]; active?: string }

// A separate Vue app with Pinia keeps the fixture out of Storybook's root app.
// The tile reads the project store only; it performs no fetches.
function renderTile(name: string, seed: TileSeed = {}) {
  return defineComponent({
    name: 'WorkspaceTileStory',
    setup() {
      const host = ref<HTMLElement>()
      let app: ReturnType<typeof createApp> | undefined
      onMounted(() => {
        const pinia = createPinia()
        const Root = defineComponent({
          setup() {
            const store = useProjectStore()
            store.workspaces = seed.workspaces ?? registry
            store.activeWorkspace = seed.active ?? 'storybook-alpha'
            return () => h('div', { style: 'display: flex; align-items: center; gap: var(--space-3)' }, [
              h(WorkspaceTile, { name }),
              h('code', `Synthetic workspace ${name}`),
            ])
          },
        })
        app = createApp(Root)
        app.use(pinia)
        app.mount(host.value!)
      })
      onBeforeUnmount(() => app?.unmount())
      return () => h('div', { ref: host })
    },
  })
}

const playTile = (text: string, accent: string): Story['play'] => async ({ canvasElement }) => {
  const tile = canvasElement.querySelector('.workspace-tile')
  expect(tile).not.toBeNull()
  expect(tile!.textContent).toBe(text)
  expect(tile!.getAttribute('data-workspace-color')).toBe(accent)
  expect(tile!.getAttribute('aria-hidden')).toBe('true')
  // The tile is the 22px identity square in every state.
  expect(Math.round(tile!.getBoundingClientRect().width)).toBe(22)
}

const meta = {
  id: 'workspace-tile',
  title: 'Selection remainder/WorkspaceTile',
  component: WorkspaceTile,
  args: { name: 'storybook-delta' },
  render: (args) => renderTile(args.name),
} satisfies Meta<typeof WorkspaceTile>
export default meta
type Story = StoryObj<typeof meta>

// `storybook-delta` is the fourth workspace option, so it shows the sidebar's
// 1-9 shortcut key and its emerald accent.
export const ShortcutKey: Story = {
  args: { name: 'storybook-delta' },
  play: playTile('4', 'emerald'),
}
// `storybook-juliet` is the tenth option: past the nine shortcuts, so the tile
// falls back to the label's initial (`Storybook` → S).
export const Initial: Story = {
  args: { name: 'storybook-juliet' },
  play: playTile('S', 'amber'),
}
// The second workspace keeps its key and wears its own pink accent rather than
// the active workspace's colour.
export const Accent: Story = {
  args: { name: 'storybook-bravo' },
  play: playTile('2', 'pink'),
}
// Before the registry arrives `workspaces` is empty: the tile shows its
// fallback initial and the neutral accent, never another workspace's colour.
export const NeutralAccent: Story = {
  render: () => renderTile('storybook-bravo', { workspaces: [] }),
  play: playTile('S', 'neutral'),
}