import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import TaskWorkspaceSurface from '../components/TaskWorkspaceSurface.vue'
import {
  assertSheetsSettled, openSheetEditor, sheetAnswers, sheetBoard,
  sheetBoardWithoutDraft, sheetCreateRoute, sheetDetail, sheetDetailRoute,
  sheetDraft, sheetFailure, sheetHeld, sheetJson, sheetList, sheetListRoute,
  sheetPatchRoute, sheetStoryView,
} from './taskSheetsFixture'

/**
 * The production surface's task-editor sheets (the `task-editor` inventory
 * row): the create sheet, the detail editor as a board page and as an overlay,
 * its description read, its refused closes, its missing-record draft alert and
 * the overlay's own task resolution. Each story mounts the production
 * component with declared fixture routes only; synthetic data everywhere.
 */
const meta = {
  id: 'task-editor',
  title: 'Task sheets/Task editor',
  component: TaskWorkspaceSurface,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof TaskWorkspaceSurface>
export default meta
type Story = StoryObj<typeof meta>

function view(label: string, props: { mode?: 'board' | 'overlay'; taskId?: string } = {}, maxWidth = 640) {
  return sheetStoryView({ label, props, maxWidth })
}

const listRoute = sheetListRoute(() => sheetJson(sheetList(sheetBoard)))
const detailProseRoute = sheetDetailRoute(sheetDraft.id, () => sheetJson(sheetDetail(sheetDraft, 'Prose the list never carried.')))
/** The create sheet's declared write: a refusal. */
const createRefused = () => sheetFailure('Synthetic vault refused the filing.', 500)

export const Default: Story = {
  parameters: { fixture: { routes: [listRoute] } },
  render: () => view('create sheet'),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await waitFor(() => canvas.getByRole('button', { name: 'New task' }))
    await assertSheetsSettled(canvasElement)
    await userEvent.click(canvas.getByRole('button', { name: 'New task' }))
    const dialog = await waitFor(() => canvas.getByRole('dialog', { name: 'New task' }))
    const title = dialog.querySelector<HTMLInputElement>('#task-create-name')!
    // Initial focus is the title field, as useModalFocus declares.
    expect(document.activeElement).toBe(title)
    const file = canvas.getByRole('button', { name: 'File task' }) as HTMLButtonElement
    expect(file.disabled).toBe(true)
    await userEvent.type(title, 'Sheet filed task')
    expect(file.disabled).toBe(false)
    expect(dialog.querySelector('.task-form')).toBeTruthy()
  },
}

export const CreateRefused: Story = {
  parameters: { fixture: { routes: [listRoute, sheetCreateRoute(createRefused)] } },
  render: () => view('create sheet, refused filing'),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await waitFor(() => canvas.getByRole('button', { name: 'New task' }))
    await userEvent.click(canvas.getByRole('button', { name: 'New task' }))
    const dialog = await waitFor(() => canvas.getByRole('dialog', { name: 'New task' }))
    const title = dialog.querySelector<HTMLInputElement>('#task-create-name')!
    await userEvent.type(title, 'Sheet filed task')
    await userEvent.click(canvas.getByRole('button', { name: 'File task' }))
    // The refusal keeps the form, what was typed and the server's sentence.
    const alert = await waitFor(() => canvas.getByRole('alert'))
    expect(alert.textContent).toContain('Synthetic vault refused the filing.')
    expect(dialog.querySelector<HTMLInputElement>('#task-create-name')!.value).toBe('Sheet filed task')
    await assertSheetsSettled(canvasElement)
  },
}

export const CreateBusy: Story = {
  parameters: { fixture: { routes: [listRoute, sheetCreateRoute(() => sheetHeld())] } },
  render: () => view('create sheet, write in flight'),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await waitFor(() => canvas.getByRole('button', { name: 'New task' }))
    await userEvent.click(canvas.getByRole('button', { name: 'New task' }))
    const dialog = await waitFor(() => canvas.getByRole('dialog', { name: 'New task' }))
    await userEvent.type(dialog.querySelector<HTMLInputElement>('#task-create-name')!, 'Sheet filed task')
    await userEvent.click(canvas.getByRole('button', { name: 'File task' }))
    // Busy is the block itself, not a style: the label and the disabled controls.
    const file = await waitFor(() => canvas.getByRole('button', { name: 'Filing…' }) as HTMLButtonElement)
    expect(file.disabled).toBe(true)
    expect(canvas.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).toHaveProperty('disabled', true)
    await assertSheetsSettled(canvasElement)
  },
}

export const Page: Story = {
  parameters: {
    fixture: { routes: [listRoute, detailProseRoute] },
  },
  render: () => view('page editor, 390px', {}, 390),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelector('[data-task-id="sb-sheet-task"] .task-title')).toBeTruthy())
    await assertSheetsSettled(canvasElement)
    const region = await openSheetEditor(canvasElement, userEvent.click)
    await waitFor(() => expect(region.querySelector('.task-body')).toBeTruthy(), { timeout: 4000 })
    // The description opens rendered, one click from the textarea; the fields
    // that place the task sit in their side column.
    expect(region.querySelector('.task-body')!.textContent!.trim()).toBe('Prose the list never carried.')
    expect(region.querySelector('[data-status="in_progress"]')).toBeTruthy()
    expect(region.querySelector('.task-detail-side')).toBeTruthy()
    // A 390px wrapper is this state's narrow stand-in: the editor stays inside.
    const pane = canvasElement.querySelector('.task-detail-pane')!
    expect(pane.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    // An open editor's focus is its title.
    expect(document.activeElement).toBe(region.querySelector('#task-detail-name'))
  },
}

export const Overlay: Story = {
  parameters: {
    fixture: { routes: [listRoute, detailProseRoute] },
  },
  render: () => view('overlay editor', { mode: 'overlay', taskId: sheetDraft.id }),
  play: async ({ canvas, canvasElement }) => {
    // The overlay editor is a modal sheet: full-window, no board chrome.
    const dialog = await waitFor(() => canvas.getByRole('dialog', { name: 'Edit task' }))
    await waitFor(() => expect(dialog.querySelector('.task-body')).toBeTruthy(), { timeout: 4000 })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    await assertSheetsSettled(canvasElement)
    expect(canvasElement.querySelectorAll('.task-lane')).toHaveLength(0)
    expect(canvasElement.querySelectorAll('.task-filters')).toHaveLength(0)
    expect(document.activeElement).toBe(dialog.querySelector('#task-detail-name'))
  },
}

export const Editing: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute,
        detailProseRoute,
        sheetPatchRoute(sheetDraft.id, () => sheetJson(sheetDetail(sheetDraft, 'Prose the list never carried. Sheetbook revision.'))),
      ],
    },
  },
  render: () => view('page editor, description edit'),
  play: async ({ canvasElement, userEvent }) => {
    const region = await openSheetEditor(canvasElement, userEvent.click)
    await waitFor(() => expect(region.querySelector('.task-body')).toBeTruthy(), { timeout: 4000 })
    // Click on the rendered description is the edit, not a second field.
    await userEvent.click(region.querySelector('.task-body')!)
    const body = region.querySelector<HTMLTextAreaElement>('#task-detail-body')!
    expect(document.activeElement).toBe(body)
    await userEvent.type(body, ' Sheetbook revision.')
    // Typing pauses write: the pending line, then the write at the read revision.
    await waitFor(() => expect(canvasElement.textContent).toContain('Saving…'), { timeout: 3000 })
    await waitFor(() => expect(canvasElement.textContent).toContain('Saved'), { timeout: 4000 })
    expect(body.value.trim()).toBe('Prose the list never carried. Sheetbook revision.')
    await assertSheetsSettled(canvasElement)
  },
}

export const DescriptionLoading: Story = {
  parameters: {
    fixture: {
      routes: [listRoute, sheetDetailRoute(sheetDraft.id, () => sheetHeld())],
    },
  },
  render: () => view('page editor, description read in flight'),
  play: async ({ canvasElement }) => {
    const region = await openSheetEditor(canvasElement, userEvent.click)
    // While the read is in flight the field is absent, not an empty box whose
    // Save would erase prose nobody was shown.
    // A status region's name is not its text, so the match is on the text.
    const loading = await waitFor(() => {
      const node = [...canvasElement.querySelectorAll<HTMLElement>('[role="status"]')]
        .find((candidate) => candidate.textContent?.includes('Loading description…'))
      expect(node).toBeTruthy()
      return node!
    })
    expect(loading.textContent).toContain('Loading description…')
    expect(region.querySelectorAll('#task-detail-body, .task-body')).toHaveLength(0)
    await assertSheetsSettled(canvasElement)
  },
}

export const DescriptionFailed: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute,
        sheetDetailRoute(sheetDraft.id, sheetAnswers([
          sheetFailure('Synthetic task read refused.', 500),
          sheetJson(sheetDetail(sheetDraft, 'Prose the list never carried.')),
        ])),
      ],
    },
  },
  render: () => view('page editor, description read refused'),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await openSheetEditor(canvasElement, userEvent.click)
    await waitFor(() => expect(canvasElement.textContent).toContain("This board could not read this task's description"))
    await userEvent.click(canvas.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(canvasElement.querySelector('.task-body')).toBeTruthy(), { timeout: 4000 })
    expect(canvasElement.querySelector('.task-body')!.textContent!.trim()).toBe('Prose the list never carried.')
    await assertSheetsSettled(canvasElement)
  },
}

export const TitleRequired: Story = {
  parameters: {
    fixture: {
      routes: [listRoute, sheetDetailRoute(sheetDraft.id, () => sheetJson(sheetDetail(sheetDraft, '')))],
    },
  },
  render: () => view('overlay editor, blank title close', { mode: 'overlay', taskId: sheetDraft.id }),
  play: async ({ canvas, userEvent }) => {
    const dialog = await waitFor(() => canvas.getByRole('dialog', { name: 'Edit task' }))
    const title = dialog.querySelector<HTMLTextAreaElement>('#task-detail-name')!
    title.value = '   '
    title.dispatchEvent(new Event('input', { bubbles: true }))
    await waitFor(() => expect(dialog.textContent).toContain('Add a title to save.'))
    // A close the host awaits cannot drop a title it cannot write: the refusal
    // names itself and nothing is written.
    await userEvent.click(dialog.querySelector('.task-sheet-head .btn-icon')!)
    await waitFor(() => expect(dialog.textContent).toContain('Add a title before closing.'))
    expect(canvas.getByRole('dialog', { name: 'Edit task' })).toBeTruthy()
  },
}

export const WriteRefused: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute,
        sheetDetailRoute(sheetDraft.id, () => sheetJson(sheetDetail(sheetDraft, ''))),
        sheetPatchRoute(sheetDraft.id, () => sheetFailure('Synthetic patch refused: that task changed on disk.', 409)),
      ],
    },
  },
  render: () => view('overlay editor, refused save', { mode: 'overlay', taskId: sheetDraft.id }),
  play: async ({ canvas, canvasElement, userEvent }) => {
    const dialog = await waitFor(() => canvas.getByRole('dialog', { name: 'Edit task' }))
    const title = dialog.querySelector<HTMLTextAreaElement>('#task-detail-name')!
    title.value = 'Sheet draft task, renamed'
    title.dispatchEvent(new Event('input', { bubbles: true }))
    await userEvent.click(dialog.querySelector('.task-sheet-head .btn-icon')!)
    // The close commits first; the refused write keeps the editor open with
    // the server's sentence, and the rows are re-read rather than lost.
    await waitFor(() => expect(canvasElement.textContent).toContain('Synthetic patch refused: that task changed on disk.'), { timeout: 4000 })
    expect(canvas.getByRole('dialog', { name: 'Edit task' })).toBeTruthy()
    expect(title.value).toBe('Sheet draft task, renamed')
    await assertSheetsSettled(canvasElement)
  },
}

export const RecordMissing: Story = {
  parameters: {
    fixture: {
      routes: [
        sheetListRoute(sheetAnswers([
          sheetJson(sheetList(sheetBoard)),
          sheetJson(sheetList(sheetBoardWithoutDraft)),
        ])),
        detailProseRoute,
        sheetPatchRoute(sheetDraft.id, () => sheetFailure('Synthetic patch refused: that task changed on disk.', 409)),
      ],
    },
  },
  render: () => view('page editor, record vanished under a draft'),
  play: async ({ canvasElement, userEvent }) => {
    const region = await openSheetEditor(canvasElement, userEvent.click)
    await waitFor(() => expect(region.querySelector('.task-body')).toBeTruthy(), { timeout: 4000 })
    // A refused status write re-reads the board; the fixture's second list
    // answer no longer carries the record, so the row vanishes under the
    // editor while the draft is still here.
    await userEvent.click(region.querySelector('[data-status="in_progress"]')!)
    await waitFor(() => expect(region.textContent).toContain('Synthetic patch refused'), { timeout: 4000 })
    const title = region.querySelector<HTMLTextAreaElement>('#task-detail-name')!
    title.value = 'Sheet draft task, drafted'
    title.dispatchEvent(new Event('input', { bubbles: true }))
    await waitFor(() => {
      const alert = [...canvasElement.querySelectorAll('[role="alert"]')]
        .find((node) => node.textContent?.includes('This task was removed from the board while you were editing.'))
      return expect(alert).toBeTruthy()
    }, { timeout: 4000 })
    expect(canvasElement.textContent).toContain('Copy draft')
    expect(canvasElement.textContent).toContain('Discard draft')
    await assertSheetsSettled(canvasElement)
  },
}

export const OverlayLoading: Story = {
  parameters: { fixture: { routes: [sheetListRoute(() => sheetHeld())] } },
  render: () => view('overlay resolution, read held', { mode: 'overlay', taskId: sheetDraft.id }),
  play: async ({ canvas, canvasElement }) => {
    // The overlay's own read, exactly the held-GET shape: a compact sheet.
    const sheet = await waitFor(() => canvas.getByRole('dialog', { name: 'Task' }))
    expect(sheet.textContent).toContain('Loading task…')
    expect(sheet.querySelector('[role="status"]')).toBeTruthy()
    expect(canvasElement.querySelectorAll('.task-detail-sheet')).toHaveLength(0)
    await assertSheetsSettled(canvasElement)
  },
}

export const OverlayFailed: Story = {
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
  render: () => view('overlay resolution, read failed then retried', { mode: 'overlay', taskId: sheetDraft.id }),
  play: async ({ canvas, canvasElement, userEvent }) => {
    // A failed read is its own state, not an editor drawn over stale rows.
    const sheet = await waitFor(() => canvas.getByRole('dialog', { name: 'Could not load task' }))
    expect(sheet.textContent).toContain('Synthetic board read failed.')
    expect(canvasElement.querySelectorAll('.task-detail-sheet')).toHaveLength(0)
    await userEvent.click([...sheet.querySelectorAll('button')].find((button) => button.textContent === 'Retry')!)
    await waitFor(() => canvas.getByRole('dialog', { name: 'Edit task' }), { timeout: 4000 })
    expect(canvasElement.querySelectorAll('[role="dialog"]')).toHaveLength(1)
    await assertSheetsSettled(canvasElement)
  },
}

export const OverlayMissing: Story = {
  parameters: {
    fixture: { routes: [sheetListRoute(() => sheetJson(sheetList(sheetBoardWithoutDraft)))] },
  },
  render: () => view('overlay resolution, task not found', { mode: 'overlay', taskId: 'sb-sheet-gone' }),
  play: async ({ canvas, canvasElement }) => {
    const sheet = await waitFor(() => canvas.getByRole('dialog', { name: 'Task not found' }))
    expect(sheet.textContent).toContain('This task is not on the storybook-sheet board.')
    expect(canvasElement.querySelectorAll('.task-detail-sheet')).toHaveLength(0)
    await assertSheetsSettled(canvasElement)
  },
}
