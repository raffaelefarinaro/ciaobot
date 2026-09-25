// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import HomeIntake from '../HomeIntake.vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
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

  it('starts the chat on a picked model instead of the workspace default', async () => {
    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '', default_provider: 'opencode', gws_profile: '' },
    ] as unknown as typeof store.workspaces
    store.workspaceProviderOptions = [
      { value: 'claude', label: 'Claude' },
      { value: 'opencode', label: 'opencode' },
    ] as unknown as typeof store.workspaceProviderOptions
    store.projects = [
      { project_id: 'general', name: 'General', workspace: 'personal', order: 0 },
    ] as unknown as typeof store.projects
    store.activeWorkspace = 'personal'
    const tasks = useTaskStore()
    tasks.models = {
      models: ['opus', 'sonnet'], default: 'opus',
      provider_models: { claude: ['opus', 'sonnet'], opencode: ['openai/gpt-5.2'] },
      provider_defaults: { claude: 'opus', opencode: 'openai/gpt-5.2' },
      opencode_models: ['openai/gpt-5.2'],
    }
    openPicker.mockResolvedValue('general')
    const create = vi.spyOn(store, 'newChatInProject').mockResolvedValue({ chat_id: 'x' } as ChatInfo)
    vi.spyOn(store, 'sendMessage').mockReturnValue(true)

    const wrapper = mount(HomeIntake, { attachTo: document.body })
    const trigger = wrapper.get('.home-intake-model-trigger')
    expect(trigger.text()).toContain('opencode default')
    expect(wrapper.get('button[type="submit"]').attributes('aria-label')).toBe('New')

    await trigger.trigger('click')
    await flushPromises()
    const sonnet = Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
      .find(option => option.textContent?.includes('sonnet'))
    expect(sonnet).toBeTruthy()
    sonnet!.click()
    await flushPromises()
    expect(trigger.text()).toContain('sonnet')

    await wrapper.get<HTMLTextAreaElement>('#home-intake-prompt').setValue('Plan the week')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(create).toHaveBeenCalledWith('general', 'Plan the week', 'Plan the week', { model: 'sonnet', provider: 'claude' })

    // A workspace switch drops the override: the other workspace has its
    // own default.
    store.activeWorkspace = 'work'
    await nextTick()
    expect(wrapper.get('.home-intake-model-trigger').text()).not.toContain('sonnet')
    wrapper.unmount()
  })

  it('stages dropped files and sends them with the first message of the new chat', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'general', name: 'General', workspace: 'personal', order: 0, vault_folder: 'Projects/General' },
    ] as unknown as typeof store.projects
    store.activeWorkspace = 'personal'
    openPicker.mockResolvedValue('general')
    vi.spyOn(store, 'newChatInProject').mockResolvedValue({ chat_id: 'fresh' } as ChatInfo)
    const uploadImages = vi.spyOn(store, 'uploadImages').mockResolvedValue(['img_1'])
    const send = vi.spyOn(store, 'sendMessage').mockReturnValue(true)
    const fetchMock = vi.fn(async (..._args: unknown[]) => new Response(JSON.stringify({ file_refs: [{ ref: 'drop_' + 'a'.repeat(32) }] }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(HomeIntake, { global: { stubs: { VoiceRecorder: true } } })
    const doc = new File(['x'], 'brief.pdf', { type: 'application/pdf' })
    const shot = new File(['y'], 'shot.png', { type: 'image/png' })
    await wrapper.get('form').trigger('drop', { dataTransfer: { items: [], files: [doc, shot] } })
    const chips = wrapper.findAll('.home-intake-attachment-name').map(chip => chip.text())
    expect(chips).toEqual(['brief.pdf', 'shot.png'])
    // Nothing is uploaded before the chat exists.
    expect(fetchMock).not.toHaveBeenCalled()

    await wrapper.get<HTMLTextAreaElement>('#home-intake-prompt').setValue('Summarise these')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(uploadImages).toHaveBeenCalledWith('fresh', [shot])
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/chats/fresh/attachments')
    expect(send).toHaveBeenCalledWith('fresh', `Summarise these\n\n\`ciao-drop:drop_${'a'.repeat(32)}\``)
    expect(wrapper.findAll('.home-intake-attachment')).toHaveLength(0)
    vi.unstubAllGlobals()
    wrapper.unmount()
  })

  it('dictates into the prompt without a chat', async () => {
    const store = useProjectStore()
    store.projects = [{ project_id: 'general', name: 'General', workspace: 'personal', order: 0 }] as unknown as typeof store.projects
    store.activeWorkspace = 'personal'
    const transcribe = vi.spyOn(store, 'transcribeVoice').mockResolvedValue('draft the brief')
    const wrapper = mount(HomeIntake)
    const recorder = wrapper.findComponent({ name: 'VoiceRecorder' })
    expect(recorder.exists()).toBe(true)
    recorder.vm.$emit('recorded', new Blob(['a'], { type: 'audio/webm' }))
    await flushPromises()
    expect(transcribe).toHaveBeenCalledWith(null, expect.any(Blob))
    expect(wrapper.get<HTMLTextAreaElement>('#home-intake-prompt').element.value).toBe('draft the brief')
    wrapper.unmount()
  })
})

