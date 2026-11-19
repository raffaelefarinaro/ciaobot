// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { shouldRestoreFocus } from './focusRestore'

describe('shouldRestoreFocus', () => {
  it('restores focus only while the held element or the body has it', () => {
    const held = document.createElement('button')
    const inside = document.createElement('span')
    held.append(inside)

    expect(shouldRestoreFocus(held, held)).toBe(true)
    expect(shouldRestoreFocus(held, document.body)).toBe(true)
    expect(shouldRestoreFocus(null, document.body)).toBe(true)

    expect(shouldRestoreFocus(held, inside)).toBe(false)
    // The user moved focus into the draft: do not take it back.
    expect(shouldRestoreFocus(held, document.createElement('textarea'))).toBe(false)
    expect(shouldRestoreFocus(held, document.documentElement)).toBe(false)
    expect(shouldRestoreFocus(held, null)).toBe(false)
    // Nothing held and nothing focused at all: nothing in particular owns
    // focus, so the restore still runs — the shipped rule compares identity,
    // and null matches null.
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
    const held = document.createElement('button')
    const draft = document.createElement('textarea')
    draft.className = 'chat-input'
    bar.append(draft, held)
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
    for (const el of elsewhere) expect(shouldRestoreFocus(held, el)).toBe(false)
  })
})
