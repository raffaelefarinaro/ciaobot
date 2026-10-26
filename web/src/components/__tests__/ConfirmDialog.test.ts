// @vitest-environment jsdom

import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import ConfirmDialog from '../ConfirmDialog.vue'
import { askConfirm, askConfirmReason, pendingConfirm } from '../../lib/confirm'

type ConfirmOptions = Parameters<typeof askConfirm>[1]

let wrapper: ReturnType<typeof mount> | null = null

function find<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector)
  if (!element) throw new Error(`Missing dialog element: ${selector}`)
  return element
}

async function settle() {
  await nextTick()
  await nextTick()
  await new Promise<void>(resolve => setTimeout(resolve, 0))
}

function openDialog(message = 'Archive this?', options: ConfirmOptions = {}) {
  wrapper = mount(ConfirmDialog, { attachTo: document.body })
  return askConfirm(message, options)
}

function press(target: Element, key: string, init: KeyboardEventInit = {}) {
  target.dispatchEvent(new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
    ...init,
  }))
}

describe('ConfirmDialog', () => {
  afterEach(() => {
    pendingConfirm.value?.resolve(false)
    wrapper?.unmount()
    wrapper = null
  })

  it('renders a labelled modal and focuses the safe action first', async () => {
    const answer = openDialog('Archive this chat?', {
      title: 'Archive chat',
      confirmLabel: 'Archive',
    })
    await settle()

    const dialog = find<HTMLElement>('[role="dialog"]')
    const title = find<HTMLElement>('.confirm-title')
    const message = find<HTMLElement>('.confirm-message')
    const cancel = find<HTMLButtonElement>('.confirm-action--cancel')
    const confirm = find<HTMLButtonElement>('.confirm-action--primary')

    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.getAttribute('aria-labelledby')).toBe(title.id)
    expect(dialog.getAttribute('aria-describedby')).toBe(message.id)
    expect(title.textContent).toBe('Archive chat')
    expect(message.textContent).toBe('Archive this chat?')
    expect(cancel.textContent).toBe('Cancel')
    expect(confirm.textContent).toBe('Archive')
    expect(document.activeElement).toBe(cancel)

    await cancel.click()
    await expect(answer).resolves.toBe(false)
  })

  it('uses the danger treatment for destructive confirmation', async () => {
    const answer = openDialog('Delete this?', {
      confirmLabel: 'Delete',
      destructive: true,
    })
    await settle()

    expect(find<HTMLElement>('.confirm-action--danger')).not.toBeNull()
    expect(document.querySelector('.confirm-action--primary')).toBeNull()

    await find<HTMLButtonElement>('.confirm-action--danger').click()
    await expect(answer).resolves.toBe(true)
  })

  it('keeps Tab focus inside the dialog in both directions', async () => {
    openDialog()
    await settle()

    const cancel = find<HTMLButtonElement>('.confirm-action--cancel')
    const confirm = find<HTMLButtonElement>('.confirm-action--primary')
    expect(document.activeElement).toBe(cancel)

    confirm.focus()
    press(confirm, 'Tab')
    expect(document.activeElement).toBe(cancel)
    cancel.focus()
    press(cancel, 'Tab', { shiftKey: true })
    expect(document.activeElement).toBe(confirm)
  })

  it('confirms on Enter without activating the cancel action', async () => {
    const answer = openDialog()
    await settle()
    const cancel = find<HTMLButtonElement>('.confirm-action--cancel')

    press(cancel, 'Enter')
    await expect(answer).resolves.toBe(true)
  })

  it('cancels on Escape and returns focus to the opening control', async () => {
    const trigger = document.createElement('button')
    trigger.type = 'button'
    document.body.appendChild(trigger)
    trigger.focus()

    const answer = openDialog()
    await settle()
    const cancel = find<HTMLButtonElement>('.confirm-action--cancel')
    expect(document.activeElement).toBe(cancel)

    press(cancel, 'Escape')
    await expect(answer).resolves.toBe(false)
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(document.activeElement).toBe(trigger)
    trigger.remove()
  })

  it('cancels when the backdrop is pressed', async () => {
    const answer = openDialog()
    await settle()
    const backdrop = find<HTMLElement>('.confirm-backdrop')

    backdrop.dispatchEvent(new MouseEvent('pointerdown', {
      bubbles: true,
      cancelable: true,
      button: 0,
    }))
    await new Promise(resolve => setTimeout(resolve, 0))
    await expect(answer).resolves.toBe(false)
  })

  it('cancels the pending promise when unmounted', async () => {
    const answer = openDialog()
    await settle()
    wrapper?.unmount()
    wrapper = null
    await expect(answer).resolves.toBe(false)
  })

  it('cancels an outstanding request when a new one arrives', async () => {
    wrapper = mount(ConfirmDialog, { attachTo: document.body })
    const first = askConfirm('First?')
    const second = askConfirm('Second?')
    await settle()

    await expect(first).resolves.toBe(false)
    expect(pendingConfirm.value?.message).toBe('Second?')
    await find<HTMLButtonElement>('.confirm-action--cancel').click()
    await expect(second).resolves.toBe(false)
  })

  describe('with a reason field', () => {
    function openReason() {
      wrapper = mount(ConfirmDialog, { attachTo: document.body })
      return askConfirmReason('Delete it?', { confirmLabel: 'Delete', destructive: true })
    }

    it('shows the reason field only for a withReason request', async () => {
      const plain = openDialog('Plain?')
      await settle()
      expect(document.querySelector('.decision-reason')).toBeNull()
      pendingConfirm.value?.resolve(false)
      await plain
      wrapper?.unmount()
      wrapper = null

      const answer = openReason()
      await settle()
      expect(document.querySelector('.decision-reason')).not.toBeNull()
      expect(document.activeElement).toBe(find('.confirm-action--cancel'))
      pendingConfirm.value?.resolve(false)
      await answer
    })

    it('does not confirm on Enter inside the textarea', async () => {
      const answer = openReason()
      await settle()
      const field = find<HTMLTextAreaElement>('.decision-reason textarea')
      field.focus()
      press(field, 'Enter')
      await settle()
      expect(pendingConfirm.value).not.toBeNull()
      pendingConfirm.value?.resolve(false)
      await answer
    })

    it('does not confirm on Enter on a chip', async () => {
      const answer = openReason()
      await settle()
      const chip = find<HTMLButtonElement>('.decision-reason .task-chip')
      chip.focus()
      press(chip, 'Enter')
      await settle()
      expect(pendingConfirm.value).not.toBeNull()
      pendingConfirm.value?.resolve(false)
      await answer
    })

    it('still confirms on Enter elsewhere and copies the typed reason out', async () => {
      const answer = openReason()
      await settle()
      const field = find<HTMLTextAreaElement>('.decision-reason textarea')
      field.value = 'wrong person'
      field.dispatchEvent(new Event('input', { bubbles: true }))
      press(find('.confirm-action--cancel'), 'Enter')
      await expect(answer).resolves.toEqual({ confirmed: true, reason: 'wrong person' })
    })

    it('Escape cancels with no reason', async () => {
      const answer = openReason()
      await settle()
      const field = find<HTMLTextAreaElement>('.decision-reason textarea')
      field.value = 'typed'
      field.dispatchEvent(new Event('input', { bubbles: true }))
      press(field, 'Escape')
      await expect(answer).resolves.toEqual({ confirmed: false, reason: '' })
    })
  })
})
