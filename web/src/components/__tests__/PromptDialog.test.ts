// @vitest-environment jsdom

import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import PromptDialog from '../PromptDialog.vue'
import { askPrompt, pendingPrompt } from '../../lib/prompt'

describe('PromptDialog', () => {
  afterEach(() => {
    pendingPrompt.value?.resolve(null)
  })

  it('focuses the field and resolves the trimmed text on submit', async () => {
    const wrapper = mount(PromptDialog, { attachTo: document.body })
    const answer = askPrompt('Project name', { title: 'New project' })
    await nextTick()
    await nextTick()

    const input = wrapper.get('.prompt-input')
    expect(document.activeElement).toBe(input.element)
    expect(wrapper.get('.prompt-title').text()).toBe('New project')
    expect(wrapper.get('.prompt-label').text()).toBe('Project name')

    await input.setValue('  Kitchen remodel  ')
    await wrapper.get('.prompt-card').trigger('submit')
    await expect(answer).resolves.toBe('Kitchen remodel')
    wrapper.unmount()
  })

  it('resolves null when cancelled', async () => {
    const wrapper = mount(PromptDialog)
    const answer = askPrompt('Project name')
    await nextTick()

    await wrapper.get('.prompt-action--cancel').trigger('click')
    await expect(answer).resolves.toBe(null)
    expect(pendingPrompt.value).toBe(null)
    wrapper.unmount()
  })

  it('resolves null when Escape is pressed in the field', async () => {
    const wrapper = mount(PromptDialog)
    const answer = askPrompt('Project name')
    await nextTick()

    await wrapper.get('.prompt-input').trigger('keydown', { key: 'Escape' })
    await expect(answer).resolves.toBe(null)
    wrapper.unmount()
  })

  // An empty submit must not resolve as "confirmed with an empty name", or the
  // caller creates a nameless project.
  it('does not submit an empty or whitespace-only field', async () => {
    const wrapper = mount(PromptDialog)
    let settled = false
    void askPrompt('Project name').then(() => {
      settled = true
    })
    await nextTick()

    expect(wrapper.get<HTMLButtonElement>('.prompt-action--primary').element.disabled).toBe(true)
    await wrapper.get('.prompt-input').setValue('   ')
    await wrapper.get('.prompt-card').trigger('submit')
    await nextTick()
    expect(settled).toBe(false)
    expect(pendingPrompt.value).not.toBe(null)
    wrapper.unmount()
  })

  // A second question would otherwise orphan the first caller's promise.
  it('cancels an outstanding request when a new one arrives', async () => {
    const wrapper = mount(PromptDialog)
    const first = askPrompt('First')
    const second = askPrompt('Second')
    await nextTick()

    await expect(first).resolves.toBe(null)
    expect(pendingPrompt.value?.message).toBe('Second')

    await wrapper.get('.prompt-action--cancel').trigger('click')
    await expect(second).resolves.toBe(null)
    wrapper.unmount()
  })

  // Unmounting mid-question must not leave the caller awaiting forever.
  it('cancels on unmount', async () => {
    const wrapper = mount(PromptDialog)
    const answer = askPrompt('Project name')
    await nextTick()

    wrapper.unmount()
    await expect(answer).resolves.toBe(null)
  })

  it('seeds and selects an initial value', async () => {
    const wrapper = mount(PromptDialog, { attachTo: document.body })
    const answer = askPrompt('Project name', { value: 'Draft' })
    await nextTick()
    await nextTick()

    const input = wrapper.get<HTMLInputElement>('.prompt-input')
    expect(input.element.value).toBe('Draft')

    await wrapper.get('.prompt-card').trigger('submit')
    await expect(answer).resolves.toBe('Draft')
    wrapper.unmount()
  })
})
