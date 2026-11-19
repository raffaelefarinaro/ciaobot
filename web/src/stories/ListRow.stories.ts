import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { createApp, defineComponent, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { createMemoryHistory, createRouter, type RouteLocationRaw } from 'vue-router'
import ListRow from '../components/ListRow.vue'

// The production link rows (SchedulePanel's Automations overview) open routes
// through vue-router. The link story mounts the row in a separate app with a
// fresh memory router: RouterLink resolves in memory, with no fetches, no
// store and no engine.
const CHAT_ROUTE = '/schedules/storybook-alpha'

type RowArgs = { to?: RouteLocationRaw; text?: string }

function routerRow(args: RowArgs, initialRoute: string) {
  return defineComponent({
    name: 'ListRowRouterStory',
    setup() {
      const host = ref<HTMLElement>()
      let app: ReturnType<typeof createApp> | undefined
      onMounted(async () => {
        const router = createRouter({
          history: createMemoryHistory(),
          routes: [
            { path: '/', component: { render: () => null } },
            { path: CHAT_ROUTE, component: { render: () => null } },
          ],
        })
        await router.push(initialRoute)
        await router.isReady()
        app = createApp(defineComponent({
          name: 'ListRowLinkStory',
          setup: () => () => h(ListRow, args),
        }))
        app.use(router)
        app.mount(host.value!)
      })
      onBeforeUnmount(() => app?.unmount())
      return () => h('div', { ref: host })
    },
  })
}

const meta = {
  id: 'list-row',
  title: 'Rows and titles/ListRow',
  component: ListRow,
  args: { text: 'Storybook row' },
} satisfies Meta<typeof ListRow>
export default meta
type Story = StoryObj<typeof meta>

// The v-else branch: a button row the host wires up. HomeRecentChats passes
// @click, `title` and `:disabled` to it; the play proves a host click reaches
// the root and that the plain `text` title renders.
export const Default: Story = {
  play: async ({ canvas, userEvent }) => {
    const row = canvas.getByRole('button', { name: 'Storybook row' })
    expect(row.tagName).toBe('BUTTON')
    expect((row as HTMLButtonElement).type).toBe('button')
    expect(canvas.getByText('Storybook row')).toBeTruthy()
    let clicks = 0
    row.addEventListener('click', () => { clicks += 1 })
    await userEvent.click(row)
    expect(clicks).toBe(1)
  },
}

// The v-if `to` branch in a fresh memory router: an anchor with the row's
// href whose click opens the route, after which RouterLink marks it active.
export const LinkRow: Story = {
  args: { text: 'Storybook schedule', to: CHAT_ROUTE },
  render: (args) => routerRow(args, '/'),
  play: async ({ canvas, userEvent }) => {
    const row = await waitFor(() => canvas.getByRole('link', { name: 'Storybook schedule' }))
    expect(row.tagName).toBe('A')
    expect(row.getAttribute('href')).toBe(CHAT_ROUTE)
    expect(row.className).not.toContain('router-link-exact-active')
    await userEvent.click(row)
    await waitFor(() => expect(row.className).toContain('router-link-exact-active'))
  },
}

// The heading slot replaces the plain title; a host title span must carry
// `list-row-title` to share the title style (SchedulePanel's `ov-title`).
export const HeadingSlot: Story = {
  render: () => ({
    components: { ListRow },
    template: `
      <ListRow>
        <template #heading><span class="list-row-title">Named by the host</span></template>
      </ListRow>
    `,
  }),
  play: async ({ canvas, canvasElement }) => {
    const title = canvasElement.querySelector('.list-row-title') as HTMLElement
    expect(title.textContent).toBe('Named by the host')
    expect(title.closest('.list-row-heading')).not.toBeNull()
    expect(canvas.queryByText('Storybook row')).toBeNull()
  },
}

// The v-if `$slots.sub` branch: one muted sub-line under the title.
export const SubSlot: Story = {
  render: () => ({
    components: { ListRow },
    template: `
      <ListRow text="Storybook row">
        <template #sub>Muted sub-line for storybook-alpha</template>
      </ListRow>
    `,
  }),
  play: async ({ canvas, canvasElement }) => {
    const sub = canvasElement.querySelector('.list-row-sub') as HTMLElement
    expect(sub.textContent).toContain('Muted sub-line')
    expect(canvas.getByText('Storybook row')).toBeTruthy()
  },
}

// The detail slot: clamped lines under the sub-line (a pending question, a
// reply snippet): the slot renders raw, with no wrapper of the row's own.
export const DetailSlot: Story = {
  render: () => ({
    components: { ListRow },
    template: `
      <ListRow text="Storybook row">
        <template #sub>Muted sub-line</template>
        <template #detail>A clamped detail line the host clamps itself</template>
      </ListRow>
    `,
  }),
  play: async ({ canvasElement }) => {
    const row = canvasElement.querySelector('.list-row') as HTMLElement
    expect(row.textContent).toContain('A clamped detail line')
  },
}

// The v-if `$slots.end` branch: the right-hand end column, whose time or
// state span carries `list-row-when` for the muted mono (:slotted rule).
export const EndSlot: Story = {
  render: () => ({
    components: { ListRow },
    template: `
      <ListRow text="Storybook row">
        <template #end><span class="list-row-when">09:00</span></template>
      </ListRow>
    `,
  }),
  play: async ({ canvasElement }) => {
    const end = canvasElement.querySelector('.list-row-end') as HTMLElement
    expect(end).not.toBeNull()
    const when = end.querySelector('.list-row-when') as HTMLElement
    expect(when.textContent).toBe('09:00')
  },
}

// The host's `:disabled` lands on the root button (HomeRecentChats for chats
// that are not in this browser) and stays actually disabled, while a host
// `title` stays the root's tooltip beside the visible `text` label.
export const Disabled: Story = {
  render: () => ({
    components: { ListRow },
    template: '<ListRow text="Storybook row" disabled title="The chat is not in this browser" />',
  }),
  play: async ({ canvas }) => {
    const row = canvas.getByRole('button', { name: 'Storybook row' })
    expect(row).toBeDisabled()
    expect(row.getAttribute('title')).toBe('The chat is not in this browser')
  },
}

// Tab reaches the row and :focus-visible draws the global 2px accent outline.
export const KeyboardFocus: Story = {
  play: async ({ canvas, userEvent }) => {
    const row = canvas.getByRole('button', { name: 'Storybook row' })
    expect(document.activeElement).not.toBe(row)
    await userEvent.tab()
    expect(document.activeElement).toBe(row)
    expect(row.matches(':focus-visible')).toBe(true)
    expect(getComputedStyle(row).outlineWidth).toBe('2px')
  },
}
