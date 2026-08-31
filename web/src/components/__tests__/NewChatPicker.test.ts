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
  let wrapper: ReturnType<typeof mount> | null = null

  beforeEach(() => {
    setActivePinia(createPinia())
    store = useProjectStore()
    store.workspaces = [
      { name: 'home', vault_root: '', default_provider: 'claude', gws_profile: '' },
      { name: 'client', vault_root: '', default_provider: 'claude', gws_profile: '' },
    ]
    store.projects = [
      { project_id: 'p-home', name: 'General', workspace: 'home', is_auto: true, context: '', created_at: '', order: 0, vault_folder: '' },
      { project_id: 'p-client', name: 'General', workspace: 'client', is_auto: true, context: '', created_at: '', order: 0, vault_folder: '' },
      { project_id: 'p-shipping', name: 'Shipping', workspace: 'client', context: '', created_at: '', order: 1, vault_folder: '' },
    ]
    store.activeWorkspace = 'home'
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    pendingNewChat.value?.resolve(null)
  })

  function labels() {
    return wrapper!.findAll('.newchat-name').map(el => el.text())
  }

  function press(key: string) {
    window.dispatchEvent(new KeyboardEvent('keydown', { key, cancelable: true }))
  }

  async function settle(answer: Promise<string | null>): Promise<string | null | 'TIMEOUT'> {
    return Promise.race([
      answer,
      new Promise<'TIMEOUT'>(resolve => setTimeout(() => resolve('TIMEOUT'), 100)),
    ])
  }

  it('opens directly on the active workspace\u2019s projects', async () => {
    wrapper = mount(NewChatPicker, { attachTo: document.body })
    const answer = openNewChatPicker()
    await nextTick()
    await nextTick()

    // Active workspace is "home", so only its projects are listed.
    expect(labels()).toEqual(['General'])

    press('Enter')
    expect(await settle(answer)).toBe('p-home')
  })

  it('a number key switches the workspace and swaps the project list', async () => {
    wrapper = mount(NewChatPicker, { attachTo: document.body })
    const answer = openNewChatPicker()
    await nextTick()
    await nextTick()

    expect(labels()).toEqual(['General'])

    // "2" selects the second workspace ("client") and shows its projects.
    press('2')
    await nextTick()
    expect(labels()).toEqual(['General', 'Shipping'])

    press('Enter')
    expect(await settle(answer)).toBe('p-client')
  })

  it('arrow keys move through the project list', async () => {
    store.activeWorkspace = 'client'
    wrapper = mount(NewChatPicker, { attachTo: document.body })
    const answer = openNewChatPicker()
    await nextTick()
    await nextTick()

    expect(labels()).toEqual(['General', 'Shipping'])
    press('ArrowDown')
    await nextTick()
    press('Enter')
    expect(await settle(answer)).toBe('p-shipping')
  })

  it('Escape cancels the picker', async () => {
    wrapper = mount(NewChatPicker, { attachTo: document.body })
    const answer = openNewChatPicker()
    await nextTick()
    await nextTick()

    press('Escape')
    expect(await settle(answer)).toBeNull()
  })

  it('browsing workspaces with 1-9 does not move the app', async () => {
    wrapper = mount(NewChatPicker, { attachTo: document.body })
    const answer = openNewChatPicker()
    await nextTick()
    await nextTick()
    expect(labels()).toEqual(['General'])

    press('2')
    await nextTick()

    // The list previews the other workspace...
    expect(labels()).toEqual(['General', 'Shipping'])
    // ...but the app has not moved. Assigning store.activeWorkspace here left
    // the user on the peeked workspace after Escape, with activeChatId still
    // pointing at a chat in the old one and nothing persisted.
    expect(store.activeWorkspace).toBe('home')

    press('Escape')
    expect(await settle(answer)).toBeNull()
    expect(store.activeWorkspace).toBe('home')
  })

  it('choosing a previewed project still resolves to that project', async () => {
    wrapper = mount(NewChatPicker, { attachTo: document.body })
    const answer = openNewChatPicker()
    await nextTick()
    await nextTick()

    press('2')
    await nextTick()
    press('ArrowDown')
    await nextTick()
    press('Enter')

    // newChatInProject performs the real workspace switch on the way in,
    // which is what disconnects the previous chat's WebSocket.
    expect(await settle(answer)).toBe('p-shipping')
    expect(store.activeWorkspace).toBe('home')
  })

  it('reopening starts from the workspace the user is actually in', async () => {
    wrapper = mount(NewChatPicker, { attachTo: document.body })
    const first = openNewChatPicker()
    await nextTick()
    await nextTick()
    press('2')
    await nextTick()
    press('Escape')
    await settle(first)

    const second = openNewChatPicker()
    await nextTick()
    await nextTick()
    expect(labels()).toEqual(['General'])
    press('Escape')
    expect(await settle(second)).toBeNull()
  })
})
