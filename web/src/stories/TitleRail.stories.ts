import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { ref } from 'vue'
import { RAIL_SLIDE_MS, type RailItem } from '../lib/titleRail'
import TitleRail from '../components/TitleRail.vue'

// The shared fixture: three open items over two synthetic projects. Labels are
// what a host passes (shape `title, project[, state]`), so the rail's names
// stay honest without any real title, path or user text.
const items: RailItem[] = [
  { id: 'rail-one', title: 'Storybook first', subtitle: 'Project one', mark: null, titlePending: false, label: 'Storybook first, Project one' },
  { id: 'rail-two', title: 'Storybook second', subtitle: 'Project one', mark: null, titlePending: false, label: 'Storybook second, Project one' },
  { id: 'rail-three', title: 'Storybook third', subtitle: 'Project two', mark: null, titlePending: false, label: 'Storybook third, Project two' },
]

// Hosts own the current id: `select` moves it, so the play can watch
// aria-current follow, and `rename` counts on the host element (the rail has
// no DOM of its own to show either event).
const meta = {
  id: 'title-rail',
  title: 'Rows and titles/TitleRail',
  component: TitleRail,
  args: { items, currentId: 'rail-two', groupLabel: 'Storybook chats' },
  render: (args) => ({
    components: { TitleRail },
    setup() {
      const current = ref(args.currentId)
      const renames = ref(0)
      return { args, current, renames }
    },
    template: `
      <div
        data-testid="rail-host"
        :data-current="current"
        :data-renames="renames"
        style="display: flex; align-items: center; min-height: 61px; width: 100%;"
      >
        <TitleRail
          :items="args.items"
          :current-id="current"
          :group-label="args.groupLabel"
          :busy="args.busy"
          @select="current = $event"
          @rename="renames += 1"
        />
      </div>
    `,
  }),
} satisfies Meta<typeof TitleRail>
export default meta
type Story = StoryObj<typeof meta>

// The settled rail: a named group, the current item with aria-current and
// tabindex 0 over neighbours at -1, title over project, and the segmented
// track under it. Swipe, wheel and pull gestures are not claimed by stories.
export const Default: Story = {
  play: async ({ canvasElement }) => {
    const root = canvasElement.querySelector('.title-rail') as HTMLElement
    expect(root.getAttribute('role')).toBe('group')
    expect(root.getAttribute('aria-label')).toBe('Storybook chats, 2 of 3')
    await waitFor(() => expect(root.className).toContain('title-rail--settled'))
    const railItems = root.querySelectorAll('.title-rail-item')
    expect(railItems.length).toBe(3)
    const current = railItems[1] as HTMLElement
    expect(current.getAttribute('aria-current')).toBe('page')
    expect(current.getAttribute('tabindex')).toBe('0')
    expect((railItems[0] as HTMLElement).getAttribute('tabindex')).toBe('-1')
    expect((railItems[2] as HTMLElement).getAttribute('tabindex')).toBe('-1')
    expect(current.querySelector('.title-rail-title')!.textContent).toBe('Storybook second')
    expect(current.querySelector('.title-rail-project')!.textContent).toBe('Project one')
    expect(root.querySelectorAll('.title-rail-seg').length).toBe(3)
    expect(root.querySelector('.title-rail-seg-current')).not.toBeNull()
    expect(root.querySelector('.title-rail-track')!.getAttribute('aria-hidden')).toBe('true')
  },
}

// The titlePending branch: the naming shimmer stands in for the title, and
// there is no title text on the pending item while neighbours keep theirs.
const pendingItems: RailItem[] = [
  { ...items[0], titlePending: true, label: 'Naming conversation, Project one' },
  items[1],
  items[2],
]
export const PendingTitle: Story = {
  args: { items: pendingItems, currentId: 'rail-one' },
  play: async ({ canvasElement }) => {
    const current = canvasElement.querySelector('.title-rail-item[aria-current="page"]') as HTMLElement
    expect(current.getAttribute('aria-label')).toBe('Naming conversation, Project one')
    expect(current.querySelector('.chat-title-shimmer')).not.toBeNull()
    expect(current.querySelector('.title-rail-title')).toBeNull()
    const neighbour = canvasElement.querySelectorAll('.title-rail-item')[1] as HTMLElement
    expect(neighbour.querySelector('.title-rail-title')!.textContent).toBe('Storybook second')
  },
}

// The track's per-item marks in miniature: needs is the alert triangle,
// working the chase, unread the dot.
const markedItems: RailItem[] = [
  { ...items[0], mark: 'needs', label: 'Storybook first, Project one, needs you' },
  { ...items[1], mark: 'working', label: 'Storybook second, Project one, working' },
  { ...items[2], mark: 'unread', label: 'Storybook third, Project two, unread' },
]
export const SegmentMarks: Story = {
  args: { items: markedItems },
  play: async ({ canvasElement }) => {
    const track = canvasElement.querySelector('.title-rail-track') as HTMLElement
    expect(track.querySelector('.title-rail-seg--needs')).not.toBeNull()
    expect(track.querySelector('.title-rail-seg--unread')).not.toBeNull()
    const working = track.querySelector('.title-rail-seg--working') as HTMLElement
    expect(working).not.toBeNull()
    // The built workshop hashes the keyframe name; the chase is still the chase.
    expect(getComputedStyle(working).animationName).toContain('title-rail-chase')
  },
}

// With busy the swipe, wheel and arrow-key steps are all refused: no select
// can land through a full slide window, so the switch never happens.
export const Busy: Story = {
  args: { busy: true },
  play: async ({ canvasElement, userEvent }) => {
    const host = canvasElement.querySelector('[data-testid="rail-host"]') as HTMLElement
    const current = canvasElement.querySelector('.title-rail-item[aria-current="page"]') as HTMLElement
    await userEvent.tab()
    expect(document.activeElement).toBe(current)
    await userEvent.keyboard('{ArrowRight}')
    await waitFor(
      () => expect(host.getAttribute('data-current')).toBe('rail-two'),
      { interval: 60, timeout: RAIL_SLIDE_MS + 150 },
    )
    expect(current.getAttribute('aria-current')).toBe('page')
  },
}

// ArrowRight on the focused current item slides to the next item and selects
// it; the host's select handler moves the current id and the rail re-centres.
export const KeyboardStep: Story = {
  play: async ({ canvasElement, userEvent }) => {
    const railItems = canvasElement.querySelectorAll('.title-rail-item')
    const current = railItems[1] as HTMLElement
    await userEvent.tab()
    expect(document.activeElement).toBe(current)
    await userEvent.keyboard('{ArrowRight}')
    await waitFor(() => expect(
      (canvasElement.querySelector('[data-testid="rail-host"]') as HTMLElement).getAttribute('data-current'),
    ).toBe('rail-three'))
    const next = canvasElement.querySelector('.title-rail-item[aria-current="page"]') as HTMLElement
    expect(next.getAttribute('aria-label')).toBe('Storybook third, Project two')
    expect(next.querySelector('.title-rail-title')!.textContent).toBe('Storybook third')
  },
}

// A double click on the current item is the rename gesture (the hosts open
// the rename field).
export const RenameGesture: Story = {
  play: async ({ canvasElement, userEvent }) => {
    const host = canvasElement.querySelector('[data-testid="rail-host"]') as HTMLElement
    const current = canvasElement.querySelector('.title-rail-item[aria-current="page"]') as HTMLElement
    expect(host.getAttribute('data-renames')).toBe('0')
    await userEvent.dblClick(current)
    await waitFor(() => expect(host.getAttribute('data-renames')).toBe('1'))
  },
}

// Tab reaches the current item only (its neighbours are not in the tab order)
// and :focus-visible draws the rail's 2px outline on it.
export const KeyboardFocus: Story = {
  play: async ({ canvasElement, userEvent }) => {
    const railItems = canvasElement.querySelectorAll('.title-rail-item')
    await userEvent.tab()
    const focused = document.activeElement as HTMLElement | null
    expect(focused).toBe(railItems[1])
    expect(focused!.matches(':focus-visible')).toBe(true)
    expect(getComputedStyle(focused as HTMLElement).outlineWidth).toBe('2px')
    expect((railItems[0] as HTMLElement).matches(':focus-visible')).toBe(false)
    expect((railItems[2] as HTMLElement).matches(':focus-visible')).toBe(false)
  },
}
