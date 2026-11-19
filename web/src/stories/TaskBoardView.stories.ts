import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { defineComponent, h, onBeforeUnmount, onMounted, provide, reactive, ref, shallowRef } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import { expect, waitFor } from 'storybook/test'
import TaskBoardView from '../components/TaskBoardView.vue'
import { mountConnected, type ConnectedFixture } from './mountConnected'
import {
  assertSheetsSettled, sheetBoard, sheetJson, sheetList, sheetListRoute, seedSheetStore,
} from './taskSheetsFixture'

/**
 * The `/tasks` route entry point (the `components-task-board-view` inventory
 * row, #3296): the wrapper that renders the production TaskWorkspaceSurface
 * as the board, with the pane's events forwarded. The board's own states live
 * in the TaskWorkspaceSurface and task-board-cards stories; this story proves
 * the route wrapper mounts that board. Mounted through the #3267 boundary with
 * declared routes only, over an inert address (the workshop has no `/tasks`
 * route to hand back).
 */
const meta = {
  id: 'task-board-view',
  title: 'Tasks board/TaskBoardView',
  component: TaskBoardView,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof TaskBoardView>
export default meta
type Story = StoryObj<typeof meta>

/** The route entry point over an inert address, as the surface's own seam does. */
const routeHost = defineComponent({
  name: 'TaskBoardViewStoryHost',
  setup() {
    provide(routerKey, { replace: () => Promise.resolve(), afterEach: () => () => {} } as never)
    provide(routeLocationKey, reactive({ fullPath: '/tasks', params: {} }) as never)
    return () => h(TaskBoardView)
  },
})

const boardView = defineComponent({
  name: 'TaskBoardViewStoryView',
  setup() {
    const host = ref<HTMLElement>()
    const fixture = shallowRef<ConnectedFixture>()
    const error = ref('')
    onMounted(() => {
      try { fixture.value = mountConnected(host.value!, { component: routeHost, seed: seedSheetStore }) } catch (cause) { error.value = String(cause) }
    })
    onBeforeUnmount(() => fixture.value?.dispose())
    return () => h('div', { style: 'max-width: 640px' }, [
      h('p', 'Synthetic route entry fixture for the production TaskBoardView.'),
      h('div', { ref: host }),
      h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
      error.value ? h('p', { role: 'alert' }, error.value) : null,
    ])
  },
})

export const Default: Story = {
  parameters: {
    fixture: {
      routes: [sheetListRoute(() => sheetJson(sheetList(sheetBoard)))],
    },
  },
  render: () => h(boardView),
  play: async ({ canvasElement }) => {
    // The wrapper draws the production board: its New task action and both cards.
    await waitFor(() => expect(canvasElement.querySelector('[data-task-id="sb-sheet-task"]')).toBeTruthy())
    expect(canvasElement.querySelector('[data-task-id="sb-sheet-second"]')).toBeTruthy()
    const create = [...canvasElement.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'New task')
    expect(create).toBeTruthy()
    await assertSheetsSettled(canvasElement)
  },
}
