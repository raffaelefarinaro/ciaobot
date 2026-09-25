// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import FileViewerModal from '../FileViewerModal.vue'
import { pendingConfirm } from '../../lib/confirm'
import { useFileViewerStore } from '../../stores/fileViewer'
import { useProjectStore } from '../../stores/projects'

let wrapper: ReturnType<typeof mount> | null = null

async function settle() {
  await nextTick()
  await nextTick()
  await new Promise<void>(resolve => setTimeout(resolve, 0))
}

function openViewer(options: { dirty?: boolean } = {}) {
  const store = useFileViewerStore()
  store.kind = 'text'
  store.path = 'notes/today.md'
  store.line = null
  store.content = 'Hello'
  store.loading = false
  store.error = ''
  store.chatId = 'chat-1'
  if (options.dirty) {
    store.editing = true
    store.editBuffer = 'Unsaved'
  }
  wrapper = mount(FileViewerModal, {
    attachTo: document.body,
    global: {
      stubs: {
        VoiceRecorder: true,
      },
    },
  })
  store.isOpen = true
  store.loadToken++
  return store
}

function press(target: Element, key: string, init: KeyboardEventInit = {}) {
  target.dispatchEvent(new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
    ...init,
  }))
}

describe('FileViewerModal', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    })
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
  })

  afterEach(() => {
    pendingConfirm.value?.resolve(false)
    wrapper?.unmount()
    wrapper = null
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('renders a labelled modal and focuses the viewer on open', async () => {
    openViewer()
    await settle()

    const dialog = wrapper!.get<HTMLElement>('.fv-modal')
    const title = wrapper!.get<HTMLElement>('.fv-title')
    const description = wrapper!.get<HTMLElement>('.fv-subtitle')

    expect(dialog.attributes('role')).toBe('dialog')
    expect(dialog.attributes('aria-modal')).toBe('true')
    expect(dialog.attributes('aria-labelledby')).toBe(title.attributes('id'))
    expect(dialog.attributes('aria-describedby')).toBe(description.attributes('id'))
    expect(title.text()).toBe('today.md')
    expect(description.text()).toBe('notes/today.md')
    expect(document.activeElement).toBe(dialog.element)
  })

  it('keeps Tab focus inside the viewer in both directions', async () => {
    openViewer()
    await settle()

    const buttons = wrapper!.findAll<HTMLButtonElement>('.fv-actions button:not([disabled])')
    const first = buttons[0].element
    const last = buttons.at(-1)!.element

    last.focus()
    press(last, 'Tab')
    expect(document.activeElement).toBe(first)
    first.focus()
    press(first, 'Tab', { shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  it('closes once on Escape and returns focus to the opener', async () => {
    const trigger = document.createElement('button')
    trigger.type = 'button'
    document.body.appendChild(trigger)
    trigger.focus()

    const store = openViewer()
    const close = vi.spyOn(store, 'close')
    await settle()
    press(wrapper!.get<HTMLElement>('.fv-modal').element, 'Escape')
    await flushPromises()
    await settle()

    expect(close).toHaveBeenCalledTimes(1)
    expect(store.isOpen).toBe(false)
    expect(document.activeElement).toBe(trigger)
    trigger.remove()
  })

  it('closes from an outside pointer and returns focus to the opener', async () => {
    const trigger = document.createElement('button')
    trigger.type = 'button'
    document.body.appendChild(trigger)
    trigger.focus()

    const store = openViewer()
    const close = vi.spyOn(store, 'close')
    await settle()
    wrapper!.get<HTMLElement>('.fv-backdrop').element.dispatchEvent(new MouseEvent('pointerdown', {
      bubbles: true,
      cancelable: true,
      button: 0,
    }))
    await flushPromises()
    await settle()

    expect(close).toHaveBeenCalledTimes(1)
    expect(store.isOpen).toBe(false)
    expect(document.activeElement).toBe(trigger)
    trigger.remove()
  })

  it('keeps the viewer open for a secondary or ctrl-click backdrop press', async () => {
    const store = openViewer()
    const close = vi.spyOn(store, 'close')
    await settle()
    const backdrop = wrapper!.get<HTMLElement>('.fv-backdrop').element

    backdrop.dispatchEvent(new MouseEvent('pointerdown', {
      bubbles: true,
      cancelable: true,
      button: 2,
    }))
    backdrop.dispatchEvent(new MouseEvent('pointerdown', {
      bubbles: true,
      cancelable: true,
      button: 0,
      ctrlKey: true,
    }))
    await settle()

    expect(close).not.toHaveBeenCalled()
    expect(store.isOpen).toBe(true)
  })

  it('keeps a dirty viewer open when discard is declined', async () => {
    const confirmModule = await import('../../lib/confirm')
    const confirm = vi.fn()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true)
    vi.spyOn(confirmModule, 'askConfirm').mockImplementation(confirm as never)

    const store = openViewer({ dirty: true })
    await settle()
    press(wrapper!.get<HTMLElement>('.fv-modal').element, 'Escape')
    await flushPromises()
    await settle()

    expect(confirm).toHaveBeenCalledWith(
      'You have unsaved file changes. Are you sure you want to close?',
      expect.objectContaining({ title: 'Discard unsaved changes?' }),
    )
    expect(store.isOpen).toBe(true)
    expect(wrapper!.find('.fv-modal').exists()).toBe(true)

    await wrapper!.get<HTMLButtonElement>('.fv-actions button[aria-label="Close"]').trigger('click')
    await flushPromises()
    await settle()
    expect(store.isOpen).toBe(false)
    expect(confirm).toHaveBeenCalledTimes(2)
  })

  it('keeps the nested comment composer focusable and Escape-local', async () => {
    const store = openViewer()
    await settle()
    const text = wrapper!.get<HTMLElement>('.fv-md p').element.firstChild
    const body = wrapper!.get<HTMLElement>('.fv-body').element
    const modal = wrapper!.get<HTMLElement>('.fv-modal').element
    const viewportRect = new DOMRect(0, 0, 800, 600)
    vi.spyOn(body, 'getBoundingClientRect').mockReturnValue(viewportRect)
    vi.spyOn(modal, 'getBoundingClientRect').mockReturnValue(viewportRect)
    Object.defineProperty(body, 'clientWidth', { configurable: true, value: 800 })
    expect(text).toBeInstanceOf(Text)

    const range = document.createRange()
    range.setStart(text!, 0)
    range.setEnd(text!, 5)
    const selection = window.getSelection()!
    selection.removeAllRanges()
    selection.addRange(range)
    const selectedRange = selection.getRangeAt(0)
    const rangeRect = new DOMRect(0, 1, 40, 16)
    Object.defineProperties(selectedRange, {
      getBoundingClientRect: { value: () => rangeRect },
      getClientRects: { value: () => [rangeRect] },
    })
    expect(selection.isCollapsed).toBe(false)
    document.dispatchEvent(new Event('selectionchange'))
    await nextTick()

    await wrapper!.get<HTMLButtonElement>('.fv-comment-trigger').trigger('click')
    await settle()

    const composer = document.body.querySelector<HTMLElement>('.compose')
    const input = document.body.querySelector<HTMLTextAreaElement>('.compose-input')
    expect(composer?.getAttribute('role')).toBe('dialog')
    expect(document.activeElement).toBe(input)

    press(input!, 'Escape')
    await settle()
    expect(document.body.querySelector('.compose')).toBeNull()
    expect(store.isOpen).toBe(true)
  })

  it('gives the internal read-comment popup a nested focus scope and local Escape', async () => {
    const projects = useProjectStore()
    projects.fileComments = {
      'notes/today.md': [{
        id: 'comment-1', path: 'notes/today.md', selection: 'Hello', comment: 'A note about this line', lineStart: 1, lineEnd: 1, createdAt: '2026-01-01T00:00:00Z',
      }],
    }
    const store = openViewer()
    await settle()
    const highlight = wrapper!.get<HTMLElement>('.comment-highlight')
    highlight.element.tabIndex = 0
    highlight.element.focus()
    highlight.element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await settle()

    const popup = document.body.querySelector<HTMLElement>('.fv-comment-pop')
    expect(popup).not.toBeNull()
    expect(popup?.getAttribute('role')).toBe('dialog')
    expect(popup?.getAttribute('aria-label')).toBe('Comment')
    expect(document.activeElement).toBe(popup?.querySelector('button'))

    popup!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await settle()
    expect(document.body.querySelector('.fv-comment-pop')).toBeNull()
    expect(store.isOpen).toBe(true)
    expect(document.activeElement).toBe(highlight.element)
  })

  it('clamps the read popup inside the viewer pane near the mobile bottom edge', async () => {
    const projects = useProjectStore()
    projects.fileComments = {
      'notes/today.md': [{
        id: 'comment-2', path: 'notes/today.md', selection: 'Hello', comment: 'Keep me visible', lineStart: 1, lineEnd: 1, createdAt: '2026-01-01T00:00:00Z',
      }],
    }
    openViewer()
    await settle()
    const main = wrapper!.get<HTMLElement>('.fv-main')
    Object.defineProperty(main.element, 'clientHeight', { configurable: true, value: 200 })
    vi.spyOn(main.element, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 390, 200))
    const highlight = wrapper!.get<HTMLElement>('.comment-highlight')
    vi.spyOn(highlight.element, 'getBoundingClientRect').mockReturnValue(new DOMRect(20, 180, 80, 16))
    highlight.element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await settle()

    const popup = document.body.querySelector<HTMLElement>('.fv-comment-pop')
    expect(popup).not.toBeNull()
    // 180 + 6 + the 96px fallback is below the 200px pane; the popup is
    // pulled up to its bottom edge instead of becoming unreachable.
    expect(parseFloat(popup!.style.top)).toBe(104)
  })

  it('focuses the textarea when edit mode opens', async () => {
    const store = openViewer()
    await settle()
    store.startEditing()
    store.editBuffer = 'Changed'
    await settle()

    expect(document.activeElement).toBe(wrapper!.get<HTMLTextAreaElement>('.fv-edit-textarea').element)
  })

  it('removes its dismissal listeners on unmount', async () => {
    const store = openViewer()
    const close = vi.spyOn(store, 'close')
    await settle()
    wrapper?.unmount()
    wrapper = null

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0 }))
    await flushPromises()

    expect(close).not.toHaveBeenCalled()
  })

  it('keeps two text actions visible and puts the file utilities in one menu', async () => {
    openViewer()
    await settle()

    const header = document.querySelector<HTMLElement>('.fv-header')!
    const labels = Array.from(header.querySelectorAll('button')).map(b => b.getAttribute('aria-label') || b.textContent?.trim())
    expect(labels).toContain('Discuss')
    expect(labels).toContain('More file actions')
    // The utilities are no longer a row of header icons.
    expect(header.querySelector('[aria-label="Download"]')).toBeNull()
    expect(header.querySelector('[aria-label="Copy path"]')).toBeNull()
  })

  it('renders the comment compose popover in place when asked to', async () => {
    const { default: CommentComposePopover } = await import('../CommentComposePopover.vue')
    const host = document.createElement('div')
    document.body.appendChild(host)
    const inline = mount(CommentComposePopover, {
      attachTo: host,
      props: { anchor: { top: 10, left: 10 } as never, modelValue: 'note', inline: true },
      global: { stubs: { VoiceRecorder: true } },
    })
    await settle()
    // Rendered in place, not teleported to <body>: reka disables pointer
    // events outside a modal dialog and closes it on an outside click, so a
    // teleported popover inside the file viewer could not be used.
    expect(host.querySelector('.compose')).not.toBeNull()
    inline.unmount()
    host.remove()
  })

  it('keeps the file viewer comment popovers inside the dialog', async () => {
    openViewer()
    await settle()
    const popovers = wrapper!.findAllComponents({ name: 'CommentComposePopover' })
    expect(popovers.length).toBeGreaterThan(0)
    for (const popover of popovers) expect(popover.props('inline')).toBe(true)
  })
})

