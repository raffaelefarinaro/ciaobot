// @vitest-environment jsdom

/**
 * The shared window shell for overlays that open over a view: a modal dialog
 * with focus moved in and handed back, Escape and a plain backdrop click
 * dismissing it, and a × that does the same.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h, nextTick, ref } from 'vue'
import OverlaySheet from '../OverlaySheet.vue'

const mounted: VueWrapper[] = []

function renderSheet(open = true, onClose = vi.fn()) {
  const host = defineComponent({
    setup() {
      const isOpen = ref(open)
      return () =>
        h(OverlaySheet, {
          open: isOpen.value,
          title: 'Ship the board',
          subtitle: 'In progress · Work',
          onClose,
        }, {
          default: () => h('p', { class: 'probe-body' }, 'Body text'),
          actions: () => h('button', { type: 'button', class: 'probe-action' }, 'Open task page'),
        })
    },
  })
  const wrapper = mount(host, { attachTo: document.body })
  mounted.push(wrapper)
  return { wrapper, onClose }
}

function sheet(): HTMLElement {
  const el = document.querySelector<HTMLElement>('[role="dialog"]')
  if (!el) throw new Error('no dialog rendered')
  return el
}

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('OverlaySheet', () => {
  it('is a modal dialog named by its title', async () => {
    renderSheet()
    await nextTick()
    const dialog = sheet()
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    const labelId = dialog.getAttribute('aria-labelledby')
    expect(labelId).toBeTruthy()
    expect(document.getElementById(labelId!)?.textContent?.trim()).toBe('Ship the board')
    expect(dialog.textContent).toContain('Body text')
    expect(dialog.querySelector('.probe-action')).not.toBeNull()
  })

  it('moves focus into the sheet when it opens', async () => {
    renderSheet()
    await vi.waitFor(() => expect(sheet().contains(document.activeElement)).toBe(true))
  })

  it('emits close on Escape', async () => {
    const { onClose } = renderSheet()
    await vi.waitFor(() => expect(sheet().contains(document.activeElement)).toBe(true))
    document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(onClose).toHaveBeenCalled()
  })

  it('emits close on a left click on the backdrop, not on a modified or right click', async () => {
    const { onClose } = renderSheet()
    await vi.waitFor(() => expect(sheet().contains(document.activeElement)).toBe(true))
    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 2 }))
    expect(onClose).not.toHaveBeenCalled()
    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0, ctrlKey: true }))
    expect(onClose).not.toHaveBeenCalled()
    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0 }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('emits close from the × button', async () => {
    const { onClose } = renderSheet()
    await nextTick()
    sheet().querySelector<HTMLButtonElement>('button[aria-label="Close"]')!.click()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('returns focus to the element that opened it', async () => {
    const opener = document.createElement('button')
    opener.textContent = 'Task link'
    document.body.appendChild(opener)
    opener.focus()
    const isOpen = ref(false)
    const host = defineComponent({
      setup() {
        return () => h(OverlaySheet, { open: isOpen.value, title: 'T', onClose: () => { isOpen.value = false } })
      },
    })
    const wrapper = mount(host, { attachTo: document.body })
    mounted.push(wrapper)
    opener.focus()
    isOpen.value = true
    await vi.waitFor(() => expect(sheet().contains(document.activeElement)).toBe(true))
    isOpen.value = false
    await vi.waitFor(() => expect(document.activeElement).toBe(opener))
  })
})
