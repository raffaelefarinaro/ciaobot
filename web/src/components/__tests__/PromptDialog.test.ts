// @vitest-environment jsdom

import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import PromptDialog from '../PromptDialog.vue'
import { askPrompt, pendingPrompt } from '../../lib/prompt'

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

function openDialog(message = 'Project name', options: Parameters<typeof askPrompt>[1] = {}) {
  wrapper = mount(PromptDialog, { attachTo: document.body })
  return askPrompt(message, options)
}

function press(target: Element, key: string, init: KeyboardEventInit = {}) {
  target.dispatchEvent(new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
    ...init,
  }))
}

describe('PromptDialog', () => {
  afterEach(() => {
    pendingPrompt.value?.resolve(null)
    wrapper?.unmount()
    wrapper = null
  })

  it('renders a labelled modal, focuses the field, and resolves trimmed text', async () => {
    const answer = openDialog('Project name', { title: 'New project' })
    await settle()

    const dialog = find<HTMLElement>('[role="dialog"]')
    const title = find<HTMLElement>('.prompt-title')
    const description = find<HTMLLabelElement>('.prompt-label')
    const input = find<HTMLInputElement>('.prompt-input')

    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.getAttribute('aria-labelledby')).toBe(title.id)
    expect(dialog.getAttribute('aria-describedby')).toBe(description.id)
    expect(description.htmlFor).toBe(input.id)
    expect(title.textContent).toBe('New project')
    expect(description.textContent).toBe('Project name')
    expect(document.activeElement).toBe(input)

    input.value = '  Kitchen remodel  '
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    find<HTMLFormElement>('.prompt-card').dispatchEvent(new Event('submit', {
      bubbles: true,
      cancelable: true,
    }))
    await expect(answer).resolves.toBe('Kitchen remodel')
  })

  it('resolves null when cancelled', async () => {
    const answer = openDialog()
    await settle()

    await find<HTMLButtonElement>('.prompt-action--cancel').click()
    await expect(answer).resolves.toBe(null)
    expect(pendingPrompt.value).toBe(null)
  })

  it('cancels on Escape from an action and returns focus to the opener', async () => {
    const trigger = document.createElement('button')
    trigger.type = 'button'
    document.body.appendChild(trigger)
    trigger.focus()

    const answer = openDialog()
    await settle()
    press(find<HTMLButtonElement>('.prompt-action--primary'), 'Escape')

    await expect(answer).resolves.toBe(null)
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(document.activeElement).toBe(trigger)
    trigger.remove()
  })

  it('keeps Tab focus inside the dialog in both directions', async () => {
    openDialog()
    await settle()

    const input = find<HTMLInputElement>('.prompt-input')
    const cancel = find<HTMLButtonElement>('.prompt-action--cancel')
    const primary = find<HTMLButtonElement>('.prompt-action--primary')
    input.value = 'Draft'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    primary.focus()
    press(primary, 'Tab')
    expect(document.activeElement).toBe(input)
    input.focus()
    press(input, 'Tab', { shiftKey: true })
    expect(document.activeElement).toBe(primary)
    expect(cancel).not.toBeNull()
  })

  it('cancels when the backdrop is pressed', async () => {
    const answer = openDialog()
    await settle()

    find<HTMLElement>('.prompt-backdrop').dispatchEvent(new MouseEvent('pointerdown', {
      bubbles: true,
      cancelable: true,
      button: 0,
    }))
    await new Promise(resolve => setTimeout(resolve, 0))
    await expect(answer).resolves.toBe(null)
  })

  it('does not submit an empty or whitespace-only field', async () => {
    const answer = openDialog()
    await settle()
    let settled = false
    void answer.then(() => {
      settled = true
    })

    const input = find<HTMLInputElement>('.prompt-input')
    const primary = find<HTMLButtonElement>('.prompt-action--primary')
    expect(primary.disabled).toBe(true)
    input.value = '   '
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    expect(primary.disabled).toBe(true)
    find<HTMLFormElement>('.prompt-card').dispatchEvent(new Event('submit', {
      bubbles: true,
      cancelable: true,
    }))
    await nextTick()
    expect(settled).toBe(false)
    expect(pendingPrompt.value).not.toBe(null)
  })

  it('cancels an outstanding request when a new one arrives', async () => {
    wrapper = mount(PromptDialog, { attachTo: document.body })
    const first = askPrompt('First')
    const second = askPrompt('Second')
    await settle()

    await expect(first).resolves.toBe(null)
    expect(pendingPrompt.value?.message).toBe('Second')
    await find<HTMLButtonElement>('.prompt-action--cancel').click()
    await expect(second).resolves.toBe(null)
  })

  it('cancels on unmount', async () => {
    const answer = openDialog()
    await settle()
    wrapper?.unmount()
    wrapper = null
    await expect(answer).resolves.toBe(null)
  })

  it('seeds and selects an initial value', async () => {
    const answer = openDialog('Project name', { value: 'Draft' })
    await settle()

    const input = find<HTMLInputElement>('.prompt-input')
    expect(input.value).toBe('Draft')
    expect(input.selectionStart).toBe(0)
    expect(input.selectionEnd).toBe('Draft'.length)

    find<HTMLFormElement>('.prompt-card').dispatchEvent(new Event('submit', {
      bubbles: true,
      cancelable: true,
    }))
    await expect(answer).resolves.toBe('Draft')
  })
})
