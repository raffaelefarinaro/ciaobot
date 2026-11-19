import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import CsvViewer from '../components/CsvViewer.vue'

/**
 * The production CSV table viewer (#3302, slice B17): a props-only owner, so
 * each story passes real prop values and the play asserts the distinguishing
 * DOM of the template branch it renders. Synthetic CSV fixtures only; the
 * viewer performs no network work of its own.
 */

const meta = {
  id: 'csv-viewer',
  title: 'File viewers/CsvViewer',
  component: CsvViewer,
} satisfies Meta<typeof CsvViewer>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  args: { content: '1,2,3\n4,5,6\n' },
  play: async ({ canvas }) => {
    // A numeric first row synthesizes its headers, so nothing is editable and no row-number column renders.
    expect(canvas.getByText('2 rows · 3 cols')).toBeTruthy()
    for (const header of ['Column 1', 'Column 2', 'Column 3']) {
      expect(canvas.getByText(header)).toBeTruthy()
    }
    expect(canvas.queryByRole('button')).toBeNull()
    expect(canvas.queryByRole('button', { name: '+ Row' })).toBeNull()
    expect(document.querySelector('.csv-viewer')?.querySelectorAll('input')).toHaveLength(0)
  },
}

export const Header: Story = {
  args: { content: 'fruit,count\napples,3\npears,4\n' },
  play: async ({ canvas }) => {
    // The first row reads as a header: it renders as text, not inputs.
    expect(canvas.getByText('2 rows · 2 cols')).toBeTruthy()
    const headerRow = document.querySelector('.csv-viewer thead tr')
    expect(headerRow?.querySelector('.csv-input')).toBeNull()
    expect(headerRow?.textContent).toContain('fruit')
    expect(headerRow?.textContent).toContain('count')
    expect(canvas.getByText('apples')).toBeTruthy()
    expect(canvas.getByText('pears')).toBeTruthy()
  },
}

export const Commentable: Story = {
  args: {
    content: 'fruit,count\napples,3\npears,4\n',
    commentable: true,
    cellComments: [{ id: 'sb-csv-c1', row: 1, colIndex: 0 }],
  },
  play: async ({ canvas, userEvent: event }) => {
    // Commentable previews gain the row-number column and show durable cell comments.
    expect(canvas.getByText('Click a cell to comment')).toBeTruthy()
    const commented = document.querySelector('.csv-viewer [data-comment-id="sb-csv-c1"]')
    expect(commented?.classList.contains('comment-highlight')).toBe(true)
    const cell = [...document.querySelectorAll('.csv-viewer tbody .csv-cell-wrap')]
      .find(el => !el.getAttribute('data-comment-id')) as HTMLElement
    await event.click(cell)
    await waitFor(() => expect(cell.classList.contains('is-selected')).toBe(true))
    // The production parent turns `cell-select` into its Comment trigger; the viewer's own DOM marks the pick.
    expect(commented?.getAttribute('data-comment-id')).toBe('sb-csv-c1')
  },
}

export const Editable: Story = {
  args: { content: 'fruit,count\napples,3\npears,4\n', readOnly: false },
  play: async ({ canvas, userEvent: event }) => {
    // The editor renders the toolbar, editable header and cell inputs, and one delete per row.
    expect(canvas.getByRole('button', { name: '+ Row' })).toBeTruthy()
    expect(canvas.getByRole('button', { name: '+ Column' })).toBeTruthy()
    expect(canvas.getByText('2 rows · 2 cols')).toBeTruthy()
    expect(canvas.queryByText('Click a cell to comment')).toBeNull()
    expect(canvas.getByLabelText('Column 1 header').getAttribute('value')).toBe('fruit')
    const cell = canvas.getByLabelText('Row 2, count') as HTMLInputElement
    expect(cell.getAttribute('value')).toBe('4')
    await event.type(cell, '2')
    expect(cell.value).toBe('42')
    await event.click(canvas.getByRole('button', { name: '+ Row' }))
    await waitFor(() => expect(canvas.getByText('3 rows · 2 cols')).toBeTruthy())
    expect(canvas.getByRole('button', { name: 'Delete row 3' })).toBeTruthy()
    await event.click(canvas.getByRole('button', { name: 'Delete row 2' }))
    await waitFor(() => expect(canvas.getByText('2 rows · 2 cols')).toBeTruthy())
  },
}

export const Empty: Story = {
  args: { content: '\n\n' },
  play: async ({ canvas }) => {
    expect(canvas.getByText('Empty CSV.')).toBeTruthy()
    expect(document.querySelector('.csv-viewer .csv-table')).toBeNull()
  },
}

export const Narrow: Story = {
  args: { content: 'fruit,count\napples,3\n' },
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  play: async ({ canvas }) => {
    // The table keeps its own scroll container inside the 390px pane.
    const scroll = document.querySelector('.csv-viewer .csv-scroll')
    expect(scroll).toBeTruthy()
    expect((scroll as HTMLElement).getBoundingClientRect().width).toBeLessThanOrEqual(390)
    expect(canvas.getByText('1 rows · 2 cols')).toBeTruthy()
  },
}
