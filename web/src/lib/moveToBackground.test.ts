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
    // "Inside the composer, or the chat layout, or a toast, or the app is ours"
    // is the container-shaped widening every review round of the browser spec
    // found, and it is what let a restore take focus the user had put on a toast
    // or on the comment box. The answer here is a function of the two elements it
    // is given, so every element of every one of those containers is still not
    // ours. The fixture carries one tree per container the rounds named — the app
    // root, the chat layout inside it, the composer surface and bar, the toast
    // stack beside the layout, and the comment popover teleported out to <body> —
    // because a widening naming a container the fixture does not carry passes
    // every test in the repository unnoticed.
    const app = document.createElement('div')
    app.id = 'ciao-app'
    const chatLayout = document.createElement('div')
    chatLayout.className = 'chat-layout'
    const surface = document.createElement('div')
    surface.className = 'composer-surface'
    const bar = document.createElement('div')
    bar.className = 'composer-bar'
    const chip = document.createElement('button')
    const draft = document.createElement('textarea')
    draft.className = 'chat-input'
    bar.append(draft, chip)
    surface.append(bar)
    chatLayout.append(surface)
    const toastStack = document.createElement('div')
    toastStack.className = 'toast-stack'
    const toast = document.createElement('div')
    toast.className = 'toast-error'
    const dismiss = document.createElement('button')
    dismiss.className = 'toast-close'
    toast.append(dismiss)
    toastStack.append(toast)
    app.append(chatLayout, toastStack)

    // Rendered through a teleport, so it sits outside the app root entirely.
    const compose = document.createElement('div')
    compose.className = 'compose'
    const composeInput = document.createElement('textarea')
    composeInput.className = 'compose-input'
    compose.append(composeInput)

    const elsewhere = [
      draft, bar, surface, chatLayout, app, toastStack, toast, dismiss, compose, composeInput,
    ]
    for (const el of elsewhere) expect(shouldRestoreFocus(chip, el)).toBe(false)
  })
})
