import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { createApp, defineComponent, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { createMemoryHistory, createRouter, type RouteLocationRaw } from 'vue-router'
import SidebarRow from '../components/SidebarRow.vue'

const meta = {
  id: 'foundation-sidebar-row',
  title: 'Foundation/Sidebar row',
  component: SidebarRow,
  args: { text: 'Weekly review', meta: '09:00' },
} satisfies Meta<typeof SidebarRow>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {}
export const Active: Story = { args: { active: true } }
export const Unread: Story = { args: { unread: true } }
export const Muted: Story = { args: { muted: true } }
export const Pending: Story = { args: { pending: true } }
export const LongContent: Story = {
  args: { text: 'Weekly review of projects, outstanding decisions and next steps' },
  render: (args) => ({
    components: { SidebarRow },
    setup: () => ({ args }),
    template: '<div style="width: 240px"><SidebarRow v-bind="args" /></div>',
  }),
}

// The v-if `to` branch, mounted in a separate app with a fresh memory router:
// RouterLink resolves in memory (no fetches, no store, no engine).
const CHAT_ROUTE = '/chats/storybook-alpha'

type LinkArgs = { to?: RouteLocationRaw; text?: string; meta?: string }

function routerRow(args: LinkArgs, initialRoute: string) {
  return defineComponent({
    name: 'SidebarRowRouterStory',
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
          name: 'SidebarRowLinkStory',
          // `text` is a required prop: the fixture guarantees it for the h() call.
          setup: () => () => h(SidebarRow, { ...args, text: args.text ?? 'Storybook chat' }),
        }))
        app.use(router)
        app.mount(host.value!)
      })
      onBeforeUnmount(() => app?.unmount())
      return () => h('div', { ref: host })
    },
  })
}

// The link branch off its route: an anchor with the row's href and the same
// title and meta marks, and a click that lands on the linked route, where
// RouterLink's active-class marks the row (every production consumer passes
// `to`; the sidebar's task and settings rows add `active` and aria-current on
// top).
export const Link: Story = {
  args: { to: CHAT_ROUTE, text: 'Storybook chat', meta: '09:00' },
  render: (args) => routerRow(args, '/'),
  play: async ({ canvas, canvasElement, userEvent }) => {
    const row = await waitFor(() => canvas.getByRole('link', { name: /Storybook chat/ }))
    expect(row.tagName).toBe('A')
    expect(row.getAttribute('href')).toBe(CHAT_ROUTE)
    expect(canvasElement.querySelector('.sidebar-row-meta')!.textContent).toBe('09:00')
    expect(row.className).not.toContain('router-link-exact-active')
    await userEvent.click(row)
    await waitFor(() => expect(row.className).toContain('router-link-exact-active'))
  },
}

// The link branch on the linked route itself: RouterLink's active-class marks
// the row the way the sidebar shows the open chat or task.
export const LinkActive: Story = {
  args: { to: CHAT_ROUTE, text: 'Storybook chat', meta: '09:00' },
  render: (args) => routerRow(args, CHAT_ROUTE),
  play: async ({ canvas }) => {
    const row = await waitFor(() => canvas.getByRole('link', { name: /Storybook chat/ }))
    expect(row.getAttribute('href')).toBe(CHAT_ROUTE)
    expect(row.className).toContain('router-link-exact-active')
    expect(row.className).toContain('active')
  },
}

// The v-else branch the host wires up: the host's role, tabindex,
// aria-pressed and click handler land on the plain root element. No current
// production consumer uses this branch (they all pass `to` or `placeholder`),
// so the story claims the branch's own contract.
export const HostControl: Story = {
  args: { text: 'Storybook plain row' },
  render: (args) => ({
    components: { SidebarRow },
    setup() {
      const pressed = ref(false)
      return { args, pressed }
    },
    template: `
      <SidebarRow
        v-bind="args"
        role="button"
        tabindex="0"
        :aria-pressed="pressed"
        @click="pressed = !pressed"
      />
    `,
  }),
  play: async ({ canvas, userEvent }) => {
    const row = canvas.getByRole('button', { name: /Storybook plain row/ })
    expect(row.tagName).toBe('DIV')
    expect(row.getAttribute('tabindex')).toBe('0')
    expect(row.getAttribute('aria-pressed')).toBe('false')
    await userEvent.click(row)
    expect(row.getAttribute('aria-pressed')).toBe('true')
  },
}

// Tab reaches a link row by itself, :focus-visible draws the row's 2px accent
// outline, and Enter opens the row's route.
export const KeyboardFocus: Story = {
  args: { to: CHAT_ROUTE, text: 'Storybook chat' },
  render: (args) => routerRow(args, '/'),
  play: async ({ canvas, userEvent }) => {
    const row = await waitFor(() => canvas.getByRole('link', { name: /Storybook chat/ }))
    expect(document.activeElement).not.toBe(row)
    await userEvent.tab()
    expect(document.activeElement).toBe(row)
    expect(row.matches(':focus-visible')).toBe(true)
    expect(getComputedStyle(row).outlineWidth).toBe('2px')
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(row.className).toContain('router-link-exact-active'))
  },
}
