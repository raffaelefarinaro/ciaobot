// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { countTracks, setHomeRailBeside, trackHomeWorkbench, useHomeRail } from './useHomeRail'

function workbench(columns: string): HTMLElement {
  const el = document.createElement('div')
  // jsdom has no layout: say the element is rendered.
  el.getClientRects = () => [{} as DOMRect] as unknown as DOMRectList
  vi.spyOn(window, 'getComputedStyle').mockImplementation(() => ({ gridTemplateColumns: columns }) as CSSStyleDeclaration)
  document.body.append(el)
  return el
}

describe('useHomeRail', () => {
  afterEach(() => {
    trackHomeWorkbench(null)
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    document.body.innerHTML = ''
  })

  it('reads the layout: two grid tracks mean the rail is beside the column', () => {
    trackHomeWorkbench(workbench('700px 320px'))
    expect(useHomeRail().beside.value).toBe(true)
  })

  it('one track means the rail dropped below the column', () => {
    trackHomeWorkbench(workbench('640px'))
    expect(useHomeRail().beside.value).toBe(false)
  })

  it('re-reads on every size change, and stops when the element goes away', () => {
    let notify: () => void = () => {}
    vi.stubGlobal('ResizeObserver', class {
      constructor(cb: () => void) { notify = cb }
      observe() {}
      disconnect() {}
    })
    const el = workbench('640px')
    trackHomeWorkbench(el)
    expect(useHomeRail().beside.value).toBe(false)
    vi.spyOn(window, 'getComputedStyle').mockImplementation(() => ({ gridTemplateColumns: '600px 300px' }) as CSSStyleDeclaration)
    notify()
    expect(useHomeRail().beside.value).toBe(true)
    trackHomeWorkbench(null)
    expect(useHomeRail().beside.value).toBe(false)
  })

  it('counts top-level tracks, not the words inside minmax()', () => {
    expect(countTracks('minmax(0px, 1fr) 280px')).toBe(2)
    expect(countTracks('minmax(0px, 1fr)')).toBe(1)
    expect(countTracks('700px 320px')).toBe(2)
    expect(countTracks('')).toBe(0)
  })

  it('keeps the last answer while the element is display: none', () => {
    const el = workbench('700px 320px')
    trackHomeWorkbench(el)
    expect(useHomeRail().beside.value).toBe(true)
    el.getClientRects = () => [] as unknown as DOMRectList
    vi.spyOn(window, 'getComputedStyle').mockImplementation(() => ({ gridTemplateColumns: 'minmax(0px, 1fr)' }) as CSSStyleDeclaration)
    trackHomeWorkbench(null)
    trackHomeWorkbench(el)
    // Re-tracked while hidden: no layout to read, so it does not claim a drop either way.
    expect(useHomeRail().beside.value).toBe(false)
  })

  it('tracking the same element again is a no-op', () => {
    const el = workbench('700px 320px')
    trackHomeWorkbench(el)
    setHomeRailBeside(false)
    trackHomeWorkbench(el)
    expect(useHomeRail().beside.value).toBe(false)
  })
});
