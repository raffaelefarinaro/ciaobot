import { describe, expect, it, vi } from 'vitest'

import { clampAnchorLeft, clampAnchorTop } from './popoverAnchor'

function viewport(width: number, height: number): void {
  vi.stubGlobal('window', { innerWidth: width, innerHeight: height })
}

/** Window height unchanged, visible area shrunk by the on-screen keyboard. */
function viewportWithKeyboard(width: number, height: number, visibleH: number): void {
  vi.stubGlobal('window', {
    innerWidth: width,
    innerHeight: height,
    visualViewport: { width, height: visibleH },
  })
}

describe('clampAnchorTop', () => {
  it('leaves an anchor that already fits alone', () => {
    viewport(1200, 900)
    expect(clampAnchorTop(300, 208)).toBe(300)
  })

  it('pulls a popover up so its bottom edge stays on screen', () => {
    viewport(1200, 900)
    // 850 + 208 would run 158px past the fold.
    expect(clampAnchorTop(850, 208)).toBe(692)
  })

  it('never returns less than the pad, even when the popover cannot fit', () => {
    viewport(1200, 150)
    expect(clampAnchorTop(140, 208)).toBe(8)
  })

  it('clamps against the visible area, not the window, when the keyboard is up', () => {
    // iPhone portrait: 932pt tall, ~596pt visible with the keyboard open.
    viewportWithKeyboard(430, 932, 596)
    expect(clampAnchorTop(700, 208)).toBe(388)
  })

  it('accepts an explicit available height so callers can re-clamp reactively', () => {
    viewport(430, 932)
    expect(clampAnchorTop(700, 208, 596)).toBe(388)
  })
})

describe('clampAnchorLeft', () => {
  it('leaves an anchor that already fits alone', () => {
    viewport(1200, 900)
    expect(clampAnchorLeft(400, 280)).toBe(400)
  })

  it('pulls a popover left so its right edge stays on screen', () => {
    viewport(1200, 900)
    expect(clampAnchorLeft(1100, 280)).toBe(912)
  })

  it('never returns less than the pad on a narrow viewport', () => {
    viewport(200, 900)
    expect(clampAnchorLeft(150, 280)).toBe(8)
  })
})
