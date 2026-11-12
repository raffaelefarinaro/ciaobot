// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { listAnchor, roamFocus } from './listRoaming'

function rows(n: number, prefix = 'r'): HTMLElement[] {
  return Array.from({ length: n }, (_, i) => {
    const el = document.createElement('button')
    el.textContent = `${prefix}${i}`
    document.body.appendChild(el)
    return el
  })
}

afterEach(() => { document.body.innerHTML = '' })

describe('roamFocus', () => {
  it('ignores keys that are not arrows, and an empty list', () => {
    const list = rows(2)
    expect(roamFocus([list], 'Enter', 'list', () => ({ group: 0, item: -1 }))).toBe(false)
    expect(roamFocus([[]], 'ArrowDown', 'list', () => ({ group: 0, item: -1 }))).toBe(false)
  })

  it('starts on the first row when focus is outside the list', () => {
    const list = rows(3)
    expect(roamFocus([list], 'ArrowDown', 'list', () => listAnchor(list, null))).toBe(true)
    expect(document.activeElement).toBe(list[0])
  })

  it('moves from the open item when focus is outside the list', () => {
    const list = rows(3)
    expect(roamFocus([list], 'ArrowDown', 'list', () => listAnchor(list, list[1]))).toBe(true)
    expect(document.activeElement).toBe(list[2])
  })

  it('walks a flat list with every arrow and stops at the ends', () => {
    const list = rows(3)
    list[0].focus()
    const anchor = () => listAnchor(list, null)
    roamFocus([list], 'ArrowRight', 'list', anchor)
    expect(document.activeElement).toBe(list[1])
    roamFocus([list], 'ArrowDown', 'list', anchor)
    expect(document.activeElement).toBe(list[2])
    // At the end the key is still consumed, and focus stays put: no wrap-around.
    expect(roamFocus([list], 'ArrowDown', 'list', anchor)).toBe(true)
    expect(document.activeElement).toBe(list[2])
    roamFocus([list], 'ArrowUp', 'list', anchor)
    roamFocus([list], 'ArrowLeft', 'list', anchor)
    expect(document.activeElement).toBe(list[0])
  })

  it('moves between stacked groups with Up/Down and within one with Left/Right', () => {
    const a = rows(2, 'a')
    const empty: HTMLElement[] = []
    const b = rows(3, 'b')
    a[1].focus()
    const anchor = () => ({ group: 0, item: -1 })
    roamFocus([a, empty, b], 'ArrowDown', 'rows', anchor)
    // Skips the empty group and keeps the row index.
    expect(document.activeElement).toBe(b[1])
    roamFocus([a, empty, b], 'ArrowRight', 'rows', anchor)
    expect(document.activeElement).toBe(b[2])
    roamFocus([a, empty, b], 'ArrowUp', 'rows', anchor)
    // Clamped to the shorter group.
    expect(document.activeElement).toBe(a[1])
    expect(roamFocus([a, empty, b], 'ArrowUp', 'rows', anchor)).toBe(true)
    expect(document.activeElement).toBe(a[1])
  })

  it('moves between side-by-side groups with Left/Right', () => {
    const a = rows(2, 'a')
    const b = rows(2, 'b')
    a[0].focus()
    roamFocus([a, b], 'ArrowRight', 'columns', () => ({ group: 0, item: -1 }))
    expect(document.activeElement).toBe(b[0])
    roamFocus([a, b], 'ArrowDown', 'columns', () => ({ group: 0, item: -1 }))
    expect(document.activeElement).toBe(b[1])
  })

  it('starts in the anchored group, not the first one', () => {
    const a = rows(2, 'a')
    const b = rows(2, 'b')
    roamFocus([a, b], 'ArrowDown', 'rows', () => ({ group: 1, item: -1 }))
    expect(document.activeElement).toBe(b[0])
  })
})
