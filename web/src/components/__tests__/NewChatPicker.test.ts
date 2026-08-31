// @vitest-environment jsdom

import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import NewChatPicker from '../NewChatPicker.vue'
import { openNewChatPicker, pendingNewChat } from '../../lib/newChat'
import { useProjectStore } from '../../stores/projects'

describe('NewChatPicker', () => {
  let store: ReturnType<typeof useProjectStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    store = useProjectStore()
    store.projects = [
      { project_id: 'p-home', name: 'General', workspace: 'home', is_auto: true, context: '', created_at: '', order: 0, vault_folder: '' },
      { project_id: 'p-client', name: 'General', workspace: 'client', is_auto: true, context: '', created_at: '', order: 0, vault_folder: '' },
    ]
  })

  afterEach(() => {
    pendingNewChat.value?.resolve(null)
  })

  function mountPicker() {
    return mount(NewChatPicker, { attachTo: document.body })
  }

  it('lists each workspace and marks General', async () => {
    const wrapper = mountPicker()
    const answer = openNewChatPicker()
    await nextTick()
    await nextTick()

    const labels = wrapper.findAll('.newchat-name').map(el => el.text())
    expect(labels).toEqual(['Home', 'Client'])
    expect(wrapper.findAll('.newchat-badge')).toHaveLength(2)
    wrapper.unmount()
    await expect(answer).resolves.toBeNull()
  })

  it('Enter creates the chat in the first (default) workspace', async () => {
    const wrapper = mountPicker()
    const answer = openNewChatPicker()
    await nextTick()
    await nextTick()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true }))
    await expect(answer).resolves.toBe('home')
    wrapper.unmount()
  })

  it('arrows move the selection and Enter uses it', async () => {
    const wrapper = mountPicker()
    const answer = openNewChatPicker()
    await nextTick()
    await nextTick()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', cancelable: true }))
    await nextTick()
    expect(wrapper.get('.newchat-option--active').text()).toContain('Client')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true }))
    await expect(answer).resolves.toBe('client')
    wrapper.unmount()
  })

  it('a number key picks that workspace directly', async () => {
    const wrapper = mountPicker()
    const answer = openNewChatPicker()
    await nextTick()
    await nextTick()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: '2', cancelable: true }))
    await expect(answer).resolves.toBe('client')
    wrapper.unmount()
  })

  it('Escape cancels the picker', async () => {
    const wrapper = mountPicker()
    const answer = openNewChatPicker()
    await nextTick()
    await nextTick()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true }))
    await expect(answer).resolves.toBeNull()
    wrapper.unmount()
  })
})
