// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { canOfferMoveToBackground, nextBackgroundableCalls, shouldRestoreFocus } from './moveToBackground'

describe('move to background helpers', () => {
  it('adds a backgroundable call once and keeps its first-seen time', () => {
    const first = nextBackgroundableCalls(undefined, { type: 'tool_use', tool_use_id: 'c1', backgroundable: true }, 10)
    expect(nextBackgroundableCalls(first, { type: 'tool_use', tool_use_id: 'c1', backgroundable: true }, 20)).toEqual({ c1: 10 })
  })

  it('removes a call when its tool_result frame arrives', () => {
    expect(nextBackgroundableCalls({ c1: 10 }, { type: 'tool_result', tool_use_id: 'c1' }, 20)).toEqual({})
  })

  it('ignores unmarked, id-less and subagent-internal frames', () => {
    const calls = { c1: 10 }
    expect(nextBackgroundableCalls(calls, { type: 'tool_use', tool_use_id: 'c2' }, 20)).toBe(calls)
    expect(nextBackgroundableCalls(calls, { type: 'tool_use', backgroundable: true }, 20)).toBe(calls)
    expect(nextBackgroundableCalls(calls, { type: 'tool_use', tool_use_id: 'c2', backgroundable: true, parent_tool_use_id: 'p1' }, 20)).toBe(calls)
  })

  it('offers the action only after the delay', () => {
    expect(canOfferMoveToBackground({ a: 0 }, 2999)).toBe(false)
    expect(canOfferMoveToBackground({ a: 0 }, 3000)).toBe(true)
    expect(canOfferMoveToBackground({}, 1e9)).toBe(false)
  })

  it('restores focus only while the chip or the body has it', () => {
    const chip = document.createElement('button')
    const inside = document.createElement('span')
    chip.append(inside)

    expect(shouldRestoreFocus(chip, chip)).toBe(true)
    expect(shouldRestoreFocus(chip, document.body)).toBe(true)
    expect(shouldRestoreFocus(null, document.body)).toBe(true)

    expect(shouldRestoreFocus(chip, inside)).toBe(false)
    expect(shouldRestoreFocus(chip, document.createElement('textarea'))).toBe(false)
    expect(shouldRestoreFocus(chip, document.documentElement)).toBe(false)
    expect(shouldRestoreFocus(chip, null)).toBe(false)
    // No chip and nothing focused at all: nothing in particular owns focus, so
    // the restore still runs — the shipped rule compares identity, and null
    // matches null.
    expect(shouldRestoreFocus(null, null)).toBe(true)
  })

  it('reads ownership from identity, never from where an element sits', () => {
    // "Inside the composer, or the chat pane, or the app is ours" is the
    // container-shaped widening every review round of the browser spec found,
    // and it is what let a restore take focus the user had put on a toast or a
    // comment box. Here the answer is a function of the two elements it is
    // given, so a deeply nested, familiar-looking element is still not ours.
    const chip = document.createElement('button')
    const layout = document.createElement('div')
    layout.id = 'ciao-app'
    const surface = document.createElement('div')
    surface.className = 'composer-surface'
    const bar = document.createElement('div')
    bar.className = 'composer-bar'
    const draft = document.createElement('textarea')
    draft.className = 'chat-input'
    bar.append(draft)
    surface.append(bar)
    layout.append(surface, chip)

    for (const el of [draft, bar, surface, layout]) expect(shouldRestoreFocus(chip, el)).toBe(false)
  })
})
