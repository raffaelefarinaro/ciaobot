// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { computed, ref } from 'vue'
import { useFileComments } from '../useFileComments'

// A read-only CSV body: each cell is a td.csv-cell-wrap wrapping a span.csv-cell,
// the way CsvViewer renders it. A paragraph outside the table stands in for
// chrome the user clicks to dismiss the cell anchor.
let root: HTMLElement
let cellSpan: HTMLElement
let outside: HTMLElement

function selectCollapsedIn(node: Node): void {
  const range = document.createRange()
  range.setStart(node, 0)
  range.collapse(true)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
}

function cellRect(): DOMRect {
  return { top: 10, bottom: 30, left: 10, right: 60, width: 50, height: 20, x: 10, y: 10, toJSON: () => ({}) } as DOMRect
}

// jsdom lays nothing out: give the frame and body a real box so the cell is visible.
function frameRect(): DOMRect {
  return { top: 0, bottom: 1000, left: 0, right: 1000, width: 1000, height: 1000, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
}

function setup() {
  const container = document.createElement('div')
  const body = document.createElement('div')
  container.getBoundingClientRect = frameRect
  body.getBoundingClientRect = frameRect
  body.innerHTML = `
    <table><tbody><tr>
      <td class="csv-cell-wrap" data-row="1" data-col="0"><span class="csv-cell">alpha</span></td>
      <td class="csv-cell-wrap" data-row="1" data-col="1"><span class="csv-cell">beta</span></td>
    </tr></tbody></table>
    <p class="outside">Outside the table</p>
  `
  container.appendChild(body)
  root.appendChild(container)
  cellSpan = body.querySelector('td[data-col="0"] .csv-cell') as HTMLElement
  outside = body.querySelector('.outside') as HTMLElement

  const comments = useFileComments({
    path: () => 'data.csv',
    content: () => '',
    commentsForFile: computed(() => []),
    isCommentable: computed(() => true),
    containerEl: ref(container),
    bodyEl: ref(body),
    mdEl: ref(undefined),
    preEl: ref(undefined),
    preCodeEl: ref(undefined),
    scrollToHighlight: vi.fn(),
  })
  return { comments, body }
}

beforeEach(() => {
  setActivePinia(createPinia())
  root = document.createElement('div')
  document.body.appendChild(root)
})

afterEach(() => {
  window.getSelection()?.removeAllRanges()
  root.remove()
})

describe('useFileComments CSV cell anchor', () => {
  it('keeps the cell anchor when the click collapses the selection inside the same cell', () => {
    const { comments } = setup()
    comments.onCsvCellSelect({ row: 1, colIndex: 0, colHeader: 'name', value: 'alpha' }, cellRect())
    expect(comments.selectionAnchor.value).not.toBeNull()

    // A click on the cell collapses the document selection inside that cell.
    selectCollapsedIn(cellSpan.firstChild!)
    comments.onSelectionChange()

    expect(comments.selectionAnchor.value).not.toBeNull()
  })

  it('clears the cell anchor when a collapsed selection lands outside the table', () => {
    const { comments } = setup()
    comments.onCsvCellSelect({ row: 1, colIndex: 0, colHeader: 'name', value: 'alpha' }, cellRect())

    selectCollapsedIn(outside.firstChild!)
    comments.onSelectionChange()

    expect(comments.selectionAnchor.value).toBeNull()
  })
})
