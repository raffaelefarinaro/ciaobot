// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import HomeIntake from '../HomeIntake.vue'
import { useProjectStore } from '../../stores/projects'
import type { ChatInfo } from '../../lib/types'

const openPicker = vi.hoisted(() => vi.fn())

vi.mock('../../lib/newChat', () => ({
  openNewChatPicker: openPicker,
}))

describe('HomeIntake', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    openPicker.mockReset()
    vi.restoreAllMocks()
  })

  it('opens the shared project picker and starts work in the selected project', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'general', name: 'General', workspace: 'personal', order: 0 },
      { project_id: 'launch', name: 'Launch', workspace: 'personal', order: 1 },
    ] as unknown as typeof store.projects
    store.activeWorkspace = 'personal'
    openPicker.mockResolvedValue('launch')

    const chat = { chat_id: 'new-chat' } as ChatInfo
    const create = vi.spyOn(store, 'newChatInProject').mockResolvedValue(chat)
    const send = vi.spyOn(store, 'sendMessage').mockReturnValue(true)

    const wrapper = mount(HomeIntake)
    const input = wrapper.get<HTMLInputElement>('#home-intake-prompt')
    await input.setValue('Turn the research notes into a decision brief')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(openPicker).toHaveBeenCalledWith({ workspace: 'personal', projectId: 'general' })
    expect(create).toHaveBeenCalledWith(
      'launch',
      'Turn the research notes into a decision brief',
      'Turn the research notes into a decision brief',
    )
    expect(send).toHaveBeenCalledWith('new-chat', 'Turn the research notes into a decision brief')
    expect(wrapper.get<HTMLTextAreaElement>('#home-intake-prompt').element.value).toBe('')
    wrapper.unmount()
  })

  it('keeps the project chip in sync with the workspace default', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'personal-general', name: 'General', workspace: 'personal', order: 0 },
      { project_id: 'work-launch', name: 'Launch', workspace: 'work', order: 0 },
    ] as unknown as typeof store.projects
    store.activeWorkspace = 'personal'

    const wrapper = mount(HomeIntake)
    expect(wrapper.get('.home-intake-project').text()).toContain('General')

    store.activeWorkspace = 'work'
    await nextTick()
    expect(wrapper.get('.home-intake-project').text()).toContain('Launch')
    wrapper.unmount()
  })

  it('remembers a picked project without creating a chat or clearing the draft', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'general', name: 'General', workspace: 'personal', order: 0 },
      { project_id: 'launch', name: 'Launch', workspace: 'personal', order: 1 },
    ] as unknown as typeof store.projects
    store.activeWorkspace = 'personal'
    openPicker.mockResolvedValue('launch')
    const create = vi.spyOn(store, 'newChatInProject').mockResolvedValue({ chat_id: 'x' } as ChatInfo)

    const wrapper = mount(HomeIntake)
    await wrapper.get<HTMLTextAreaElement>('#home-intake-prompt').setValue('Keep this draft')
    await wrapper.get('.home-intake-project').trigger('click')
    await flushPromises()

    expect(store.newChatInProject).not.toHaveBeenCalled()
    expect(create).not.toHaveBeenCalled()
    expect(wrapper.get('.home-intake-project').text()).toContain('Launch')
    expect(wrapper.get<HTMLTextAreaElement>('#home-intake-prompt').element.value).toBe('Keep this draft')
    wrapper.unmount()
  })

  it('uses the remembered project for the next request', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'general', name: 'General', workspace: 'personal', order: 0 },
      { project_id: 'launch', name: 'Launch', workspace: 'personal', order: 1 },
    ] as unknown as typeof store.projects
    store.activeWorkspace = 'personal'
    openPicker.mockResolvedValue('launch')
    const create = vi.spyOn(store, 'newChatInProject').mockResolvedValue({ chat_id: 'x' } as ChatInfo)
    vi.spyOn(store, 'sendMessage').mockReturnValue(true)

    const wrapper = mount(HomeIntake)
    await wrapper.get('.home-intake-project').trigger('click')
    await flushPromises()
    await wrapper.get<HTMLTextAreaElement>('#home-intake-prompt').setValue('Ship it')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(create).toHaveBeenCalledWith('launch', 'Ship it', 'Ship it')
    wrapper.unmount()
  })

  it('keeps drafts scoped to their workspace and never chooses a project inline', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'personal-launch', name: 'Launch', workspace: 'personal', order: 0 },
      { project_id: 'work-general', name: 'General', workspace: 'work', order: 0 },
    ] as unknown as typeof store.projects
    store.activeWorkspace = 'personal'
    openPicker.mockResolvedValue('work-general')
    vi.spyOn(store, 'newChatInProject').mockResolvedValue({ chat_id: 'work-chat' } as ChatInfo)
    vi.spyOn(store, 'sendMessage').mockReturnValue(true)

    const wrapper = mount(HomeIntake)
    const input = wrapper.get<HTMLTextAreaElement>('#home-intake-prompt')
    await input.setValue('Personal launch brief')

    store.activeWorkspace = 'work'
    await nextTick()
    expect(input.element.value).toBe('')

    await input.setValue('Work planning brief')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(openPicker).toHaveBeenCalledWith({ workspace: 'work', projectId: 'work-general' })
    expect(store.newChatInProject).toHaveBeenCalledWith(
      'work-general',
      'Work planning brief',
      'Work planning brief',
    )

    store.activeWorkspace = 'personal'
    await nextTick()
    expect(input.element.value).toBe('Personal launch brief')
    wrapper.unmount()
  })

  it('opens an empty chat when New is used without a prompt', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'general', name: 'General', workspace: 'personal', order: 0 },
    ] as unknown as typeof store.projects
    store.activeWorkspace = 'personal'
    openPicker.mockResolvedValue('general')
    const create = vi.spyOn(store, 'newChatInProject').mockResolvedValue({ chat_id: 'empty' } as ChatInfo)

    const wrapper = mount(HomeIntake)
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(create).toHaveBeenCalledWith('general')
    wrapper.unmount()
  })

  it('sends with Cmd/Ctrl+Enter and keeps bare Enter as a newline', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'general', name: 'General', workspace: 'personal', order: 0 },
    ] as unknown as typeof store.projects
    store.activeWorkspace = 'personal'
    openPicker.mockResolvedValue('general')
    vi.spyOn(store, 'newChatInProject').mockResolvedValue({ chat_id: 'x' } as ChatInfo)
    vi.spyOn(store, 'sendMessage').mockReturnValue(true)

    const wrapper = mount(HomeIntake)
    const input = wrapper.get<HTMLTextAreaElement>('#home-intake-prompt')
    await input.setValue('Draft the plan')
    await input.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(openPicker).not.toHaveBeenCalled()

    await input.trigger('keydown', { key: 'Enter', ctrlKey: true })
    await flushPromises()
    expect(openPicker).toHaveBeenCalledTimes(1)
    expect(store.newChatInProject).toHaveBeenCalledWith('general', 'Draft the plan', 'Draft the plan')
    wrapper.unmount()
  })

  it('names the workspace default provider and keeps New as the send control name', async () => {
    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '', default_provider: 'opencode', gws_profile: '' },
    ] as unknown as typeof store.workspaces
    store.workspaceProviderOptions = [
      { value: 'claude', label: 'Claude' },
      { value: 'opencode', label: 'OpenCode' },
    ] as unknown as typeof store.workspaceProviderOptions
    store.projects = [
      { project_id: 'general', name: 'General', workspace: 'personal', order: 0 },
    ] as unknown as typeof store.projects
    store.activeWorkspace = 'personal'

    const wrapper = mount(HomeIntake)
    expect(wrapper.get('.home-intake-provider').text()).toContain('OpenCode')
    // Read-only: it states a fact, it is not a picker.
    expect(wrapper.find('.home-intake-provider').element.tagName).toBe('SPAN')
    expect(wrapper.get('button[type="submit"]').attributes('aria-label')).toBe('New')
    wrapper.unmount()
  })
})
