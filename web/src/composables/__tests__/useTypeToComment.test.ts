// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, nextTick, ref } from 'vue'
import { useTypeToComment } from '../useTypeToComment'

type Harness = {
  opened: string[]
  dictated: number
  images: File[][]
  active: ReturnType<typeof ref<boolean>>
}

let harness: Harness
let wrapper: ReturnType<typeof mount> | null = null

function mountHarness(): void {
  const active = ref(true)
  harness = { opened: [], dictated: 0, images: [], active }
  const Host = defineComponent({
    setup() {
      useTypeToComment({
        isActive: () => !!active.value,
        open: (text: string) => { harness.opened.push(text) },
        dictate: () => { harness.dictated += 1 },
        addImages: (files: File[]) => { harness.images.push(files) },
      })
      return () => null
    },
  })
  wrapper = mount(Host, { attachTo: document.body })
}

function press(key: string, init: KeyboardEventInit = {}, target?: EventTarget): boolean {
  const e = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init })
  ;(target ?? document.body).dispatchEvent(e)
  return e.defaultPrevented
}

function paste(text: string, files: File[] = [], target?: EventTarget): boolean {
  const e = new Event('paste', { bubbles: true, cancelable: true })
  Object.defineProperty(e, 'clipboardData', {
    value: { getData: () => text, files },
  })
  ;(target ?? document.body).dispatchEvent(e)
  return e.defaultPrevented
}

describe('useTypeToComment', () => {
  beforeEach(() => { mountHarness() })
  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    document.body.innerHTML = ''
  })

  it('opens the composer seeded with the typed character', () => {
    expect(press('h')).toBe(true)
    expect(harness.opened).toEqual(['h'])
  })

  it('ignores keys that are not a single printable character', () => {
    press('Enter')
    press('ArrowDown')
    press('Escape')
    press('Shift')
    expect(harness.opened).toEqual([])
  })

  it('leaves space alone so it still scrolls', () => {
    expect(press(' ')).toBe(false)
    expect(harness.opened).toEqual([])
  })

  it('ignores an IME composition keystroke', () => {
    press('a', { })
    harness.opened.length = 0
    const e = new KeyboardEvent('keydown', { key: 'a', bubbles: true, cancelable: true })
    Object.defineProperty(e, 'isComposing', { value: true })
    document.body.dispatchEvent(e)
    expect(harness.opened).toEqual([])
  })

  it('does not steal typing aimed at a field', () => {
    const input = document.createElement('textarea')
    document.body.appendChild(input)
    expect(press('h', {}, input)).toBe(false)
    expect(harness.opened).toEqual([])
  })

  it('does nothing when no selection is anchored', async () => {
    harness.active.value = false
    await nextTick()
    press('h')
    paste('hello')
    press('d', { metaKey: true })
    expect(harness.opened).toEqual([])
    expect(harness.dictated).toBe(0)
  })

  it('opens with the pasted text', () => {
    expect(paste('pasted note')).toBe(true)
    expect(harness.opened).toEqual(['pasted note'])
  })

  it('attaches pasted images to the new draft', () => {
    const png = new File(['x'], 'shot.png', { type: 'image/png' })
    const txt = new File(['x'], 'notes.txt', { type: 'text/plain' })
    paste('', [png, txt])
    expect(harness.opened).toEqual([''])
    expect(harness.images).toEqual([[png]])
  })

  it('ignores an empty paste', () => {
    expect(paste('   ')).toBe(false)
    expect(harness.opened).toEqual([])
  })

  it('opens and starts dictating on Cmd+D', () => {
    expect(press('d', { metaKey: true })).toBe(true)
    expect(harness.opened).toEqual([''])
    expect(harness.dictated).toBe(1)
  })

  it('leaves other shortcuts to the browser', () => {
    expect(press('c', { metaKey: true })).toBe(false)
    expect(press('a', { ctrlKey: true })).toBe(false)
    expect(harness.opened).toEqual([])
  })

  it('stops listening once the component unmounts', () => {
    wrapper?.unmount()
    wrapper = null
    press('h')
    expect(harness.opened).toEqual([])
  })
})
