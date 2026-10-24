// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import OnboardingFinishStage from '../OnboardingFinishStage.vue'
import { useProjectStore } from '../../../stores/projects'
import { api } from '../../../lib/api'

vi.mock('../../../lib/api', () => ({ api: { get: vi.fn() } }))

const context = { target: { kind: 'workspace' as const, name: 'personal' }, workspace: 'personal', revision: 1, provisioned: true }

function payloads() {
  vi.mocked(api.get).mockImplementation((url: string) => {
    if (url === '/api/workspaces') return Promise.resolve({ workspaces: [{ name: 'personal', agent_fs_scope: 'workspace', provider_overrides: { claude: { model: 'opus' } } }] }) as never
    if (url === '/api/setup-status') return Promise.resolve({ workspace_root: '/srv/ciaobot', ready: true }) as never
    if (url === '/api/local/backup/setup-prompt') return Promise.resolve({ context: { has_repo: true, has_commits: true, has_remote: true } }) as never
    if (url === '/api/settings/providers') return Promise.resolve({ connections: { claude: { ok: true, auth: 'signed in' } } }) as never
    if (url.startsWith('/api/proposals/handover')) return Promise.resolve({ handover: 'all', acting_available: true }) as never
    if (url === '/api/schedules') return Promise.resolve([{ schedule_id: 'system-memory-curation@personal', scope: 'system', enabled: true, daily_time_utc: '08:00', timezone_name: 'Europe/Zurich' }]) as never
    return Promise.reject(new Error('unexpected')) as never
  })
}

beforeEach(() => { setActivePinia(createPinia()); vi.resetAllMocks() })

describe('OnboardingFinishStage', () => {
  it('reports an unverified remote as attention, not ready', async () => {
    payloads()
    const wrapper = mount(OnboardingFinishStage, { props: { context, busy: false } })
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('off-machine backup is not verified')
    expect(text).toContain('attention')
    // A chosen model is not proof that it can reply.
    expect(text).toContain('not yet verified')
  })

  it('reports readiness to the shell and never completes by itself', async () => {
    payloads()
    const wrapper = mount(OnboardingFinishStage, { props: { context, busy: false } })
    await flushPromises()
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({ canContinue: true })
    expect(wrapper.emitted('continue')).toBeUndefined()
  })

  it('creates and sends only on submit, keeping the text on failure', async () => {
    payloads()
    const store = useProjectStore()
    store.projects = [{ project_id: 'general', name: 'General', workspace: 'personal', order: 0 }] as typeof store.projects
    const create = vi.spyOn(store, 'newChatInProject').mockResolvedValue({ chat_id: 'c1' } as never)
    vi.spyOn(store, 'sendMessage').mockRejectedValue(new Error('offline'))
    const wrapper = mount(OnboardingFinishStage, { props: { context, busy: false } })
    await flushPromises()
    expect(create).not.toHaveBeenCalled()
    await wrapper.get('textarea').setValue('Do a real task')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('textarea').element as HTMLTextAreaElement).toHaveProperty('value', 'Do a real task')
    expect(wrapper.text()).toContain('offline')
  })

  it('offers the archive video without autoplay and the companion guide', async () => {
    payloads()
    const wrapper = mount(OnboardingFinishStage, { props: { context, busy: false } })
    await flushPromises()
    const video = wrapper.get('video')
    expect(video.attributes('autoplay')).toBeUndefined()
    expect(video.attributes('controls')).toBeDefined()
    expect(video.attributes('preload')).toBe('none')
    expect(wrapper.get('video source').attributes('src')).toContain('archive.mp4')
    expect(wrapper.html()).toContain('companions.html')
    expect(wrapper.text()).toContain('Dictation')
    expect(wrapper.text()).toContain('KeepingYouAwake')
  })
})
