import { h } from 'vue'
import type { Pinia } from 'pinia'
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import ItemPeekHost from '../components/ItemPeekHost.vue'
import { useItemPeekStore } from '../stores/itemPeek'
import { peekHost, peekView, settlePeek } from './taskPeekFixture'
import {
  sheetAnswers, sheetBoard, sheetDetail, sheetDetailRoute, sheetDraft, sheetFailure,
  sheetHeld, sheetJson, sheetList, sheetListRoute, seedSheetStore,
} from './taskSheetsFixture'

/**
 * The item peek host (the `components-item-peek-host` inventory row, #3298):
 * where a task link opens the board's editor over the view it was clicked on.
 * The host draws nothing with no target, and the task surface in overlay mode
 * with one. Each story mounts the production host through `mountConnected`
 * with the routes its overlay reads; synthetic fixtures only.
 */
const meta = {
  id: 'item-peek-host',
  title: 'Task notes and peek/ItemPeekHost',
  component: ItemPeekHost,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof ItemPeekHost>
export default meta
type Story = StoryObj<typeof meta>

/** The synthetic workspace, with a peek open on the draft task or with none. */
function peekSeed(open: boolean) {
  return (pinia: Pinia): void => {
    seedSheetStore(pinia)
    if (open) useItemPeekStore(pinia).openTask(sheetDraft.id)
  }
}

function peek(label: string, open: boolean, maxWidth = 390) {
  return peekView({
    label,
    component: peekHost(() => h(ItemPeekHost)),
    seed: peekSeed(open),
    maxWidth,
  })
}

const listRoute = sheetListRoute(() => sheetJson(sheetList(sheetBoard)))
const detailProseRoute = sheetDetailRoute(sheetDraft.id, () => sheetJson(sheetDetail(sheetDraft, 'Prose the list never carried.')))

export const Closed: Story = {
  render: () => h(peek('no target', false)),
  play: async ({ canvasElement }) => {
    await settlePeek(canvasElement)
    // With no target the host mounts nothing: no overlay, no dialog.
    expect(canvasElement.querySelector('[role="dialog"]')).toBeNull()
  },
}

export const TaskOpen: Story = {
  parameters: { fixture: { routes: [listRoute, detailProseRoute] } },
  render: () => h(peek('task open over the view', true, 640)),
  play: async ({ canvasElement }) => {
    // The overlay's locate sheet is replaced by the editor, so the body is the
    // thing to wait for, and its dialog is read from it.
    const body = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('[role="dialog"] .task-body')
      expect(found).toBeTruthy()
      return found!
    }, { timeout: 4000 })
    // The overlay is the board's editor: modal, with its body drawn.
    const dialog = body.closest<HTMLElement>('[role="dialog"]')!
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.querySelector<HTMLInputElement>('#task-detail-name')!.value).toBe(sheetDraft.title)
    await settlePeek(canvasElement)
    // Closing the overlay's own close ends the target, and the host unmounts it.
    await userEvent.click(dialog.querySelector<HTMLButtonElement>('button[aria-label="Close"]')!)
    await waitFor(() => expect(canvasElement.querySelector('[role="dialog"]')).toBeNull())
  },
}

export const TaskOpenLoading: Story = {
  parameters: { fixture: { routes: [sheetListRoute(() => sheetHeld())] } },
  render: () => h(peek('task open, read held', true)),
  play: async ({ canvasElement }) => {
    // The overlay locates its task first: the held read shows the compact status sheet.
    const sheet = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('[role="dialog"]')
      expect(found).toBeTruthy()
      return found!
    })
    expect(sheet.textContent).toContain('Loading task…')
    expect(sheet.querySelector('[role="status"]')).toBeTruthy()
    expect(canvasElement.querySelectorAll('.task-detail-sheet')).toHaveLength(0)
    await settlePeek(canvasElement)
  },
}

export const TaskOpenFailed: Story = {
  parameters: {
    fixture: {
      routes: [
        sheetListRoute(sheetAnswers([
          sheetFailure('Synthetic board read failed.', 503),
          sheetJson(sheetList(sheetBoard)),
        ])),
        detailProseRoute,
      ],
    },
  },
  render: () => h(peek('task open, read failed then retried', true)),
  play: async ({ canvasElement }) => {
    // A failed locate is its own sheet, not an editor drawn over nothing.
    const sheet = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('[role="dialog"]')
      expect(found).toBeTruthy()
      expect(found!.textContent).toContain('Could not load task')
      return found!
    })
    expect(sheet.textContent).toContain('Synthetic board read failed.')
    await userEvent.click([...sheet.querySelectorAll<HTMLButtonElement>('button')].find((button) => button.textContent === 'Retry')!)
    await waitFor(() => expect(canvasElement.querySelector('.task-body')).toBeTruthy(), { timeout: 4000 })
    expect(canvasElement.querySelectorAll('[role="dialog"]')).toHaveLength(1)
    await settlePeek(canvasElement)
  },
}
