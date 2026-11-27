// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { setHomeRailBeside, trackHomeWorkbench, useHomeRail } from './useHomeRail'

function workbench(columns: string): HTMLElement {
  const el = document.createElement('div')
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

  it('tracking the same element again is a no-op', () => {
    const el = workbench('700px 320px')
    trackHomeWorkbench(el)
    setHomeRailBeside(false)
    trackHomeWorkbench(el)
    expect(useHomeRail().beside.value).toBe(false)
  })
});
