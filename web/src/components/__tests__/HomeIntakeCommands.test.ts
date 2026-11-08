// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import HomeIntake from '../HomeIntake.vue'
import { applyKeyboardSettings } from '../../composables/useKeyboardSettings'
import { useProjectStore } from '../../stores/projects'
import { api } from '../../lib/api'
import type { ChatInfo, SlashCommand } from '../../lib/types'

const openPicker = vi.hoisted(() => vi.fn())

vi.mock('../../lib/newChat', async () => ({
  homeNewChatProjectId: (await import('vue')).ref(''),
  openNewChatPicker: openPicker,
}))

function command(name: string, argument_hint = ''): SlashCommand {
  return {
    name,
    description: `run ${name}`,
    argument_hint,
    source: name === 'linkedin-writing' ? 'skill' : 'user',
    path: `${name}.md`,
  }
}

describe('HomeIntake slash commands', () => {
  beforeEach(() => {
    applyKeyboardSettings({ keyboard_shortcuts: {}, keyboard_send_mode: 'enter' })
    setActivePinia(createPinia())
    openPicker.mockReset()
    window.history.replaceState({}, '', '/')
    vi.restoreAllMocks()
  })

  it('typing a slash opens the command picker and Enter inserts instead of sending', async () => {
    const store = useProjectStore()
    store.activeWorkspace = 'personal'
    store.workspaces = [
      { name: 'personal', vault_root: '', default_provider: 'claude', gws_profile: '' },
    ] as unknown as typeof store.workspaces
    store.projects = [
      { project_id: 'general', name: 'General', workspace: 'personal', order: 0 },
    ] as unknown as typeof store.projects
    const create = vi.spyOn(store, 'newChatInProject').mockResolvedValue({ chat_id: 'x' } as ChatInfo)
    vi.spyOn(store, 'sendMessage').mockReturnValue(true)
    vi.spyOn(api, 'get').mockImplementation((path: string) => {
      if (path.startsWith('/api/commands')) {
        return Promise.resolve({
          commands: [command('review')],
          skills: [command('linkedin-writing', '<file>')],
        }) as never
      }
      return Promise.resolve([]) as never
    })

    const wrapper = mount(HomeIntake)
    await flushPromises()
    const input = wrapper.get<HTMLTextAreaElement>('#home-intake-prompt')

    await input.setValue('/lin')
    await nextTick()
    const rows = wrapper.findAll('.commands-picker-row')
    expect(rows).toHaveLength(1)
    expect(rows[0].text()).toContain('/linkedin-writing')
    expect(rows[0].text()).toContain('skill')

    await input.trigger('keydown', { key: 'ArrowDown' })
    await input.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(input.element.value).toBe('/linkedin-writing ')
    expect(create).not.toHaveBeenCalled()

    await input.setValue('/rev')
    await nextTick()
    expect(wrapper.find('.commands-picker').exists()).toBe(true)
    await input.trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(wrapper.find('.commands-picker').exists()).toBe(false)
    expect(input.element.value).toBe('/rev')

    await input.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(create).toHaveBeenCalledWith('general')

    // The list is the workspace default provider's: losing that wiring would
    // still pass every assertion above.
    expect(api.get).toHaveBeenCalledWith('/api/commands?provider=claude&workspace=personal')

    await input.setValue('/rev')
    await nextTick()
    expect(wrapper.find('.commands-picker').exists()).toBe(true)
    await input.trigger('keydown', { key: 'Enter', isComposing: true })
    await nextTick()
    // An IME confirmation key is never the picker's: the draft stays and
    // nothing is created.
    expect(input.element.value).toBe('/rev')
    expect(wrapper.find('.commands-picker').exists()).toBe(true)
    wrapper.unmount()
  })
})
