// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import NewScheduleForm from '../NewScheduleForm.vue'
import HomeIntake from '../HomeIntake.vue'
import SchedulePanel from '../SchedulePanel.vue'
import ModelSelector from '../ModelSelector.vue'
import { createMemoryHistory, createRouter, routeLocationKey, routerKey } from 'vue-router'
import { defineComponent, reactive } from 'vue'
import TaskWorkspaceSurface from '../TaskWorkspaceSurface.vue'
import { useTaskBoardStore } from '../../stores/taskBoard'

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
})

it('closes an open Home model picker and starts a scoped read on a workspace switch', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  vi.mocked(api.get).mockImplementation(() => new Promise(() => {}))
  const wrapper = mount(HomeIntake)
  await wrapper.get('.home-intake-model-trigger').trigger('click')
  expect(wrapper.get('.home-intake-model-trigger').attributes('aria-expanded')).toBe('true')
  projects.activeWorkspace = 'work'
  await flushPromises()
  expect(wrapper.get('.home-intake-model-trigger').attributes('aria-expanded')).toBe('false')
  expect(api.get).toHaveBeenCalledWith('/api/models?workspace=work')
  expect(wrapper.text()).toContain('Loading new chat models')
  wrapper.unmount()
})

it('rejects a delayed Home selection even after switching back to its original workspace', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  vi.mocked(api.get).mockResolvedValue({ models: ['old'], default: 'old', provider_models: { claude: ['old'] } })
  const wrapper = mount(HomeIntake)
  await wrapper.get('.home-intake-model-trigger').trigger('click')
  await flushPromises()
  const oldPicker = wrapper.getComponent(ModelSelector)
  const oldOption = wrapper.get<HTMLButtonElement>('[role="option"][data-model="old"]').element
  projects.activeWorkspace = 'work'
  projects.activeWorkspace = 'personal'
  oldOption.click()
  oldOption.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
  oldPicker.vm.$emit('select', 'old', 'claude')
  await flushPromises()
  expect(wrapper.get('.home-intake-model-trigger').text()).not.toContain('old')
  wrapper.unmount()
})

it('returns focus to Home’s model opener only when the removed picker held focus', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  vi.mocked(api.get).mockResolvedValue({ models: [], default: '', provider_models: {} })
  const wrapper = mount(HomeIntake, { attachTo: document.body })
  await wrapper.get('.home-intake-model-trigger').trigger('click')
  await flushPromises()
  const search = wrapper.get<HTMLInputElement>('.model-selector__search').element
  search.focus()
  expect(document.activeElement).toBe(search)
  projects.activeWorkspace = 'work'
  await flushPromises()
  expect(document.activeElement).toBe(wrapper.get('.home-intake-model-trigger').element)
  wrapper.unmount()
})

it('does not steal focus when Home’s picker closes after focus moved elsewhere', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  vi.mocked(api.get).mockResolvedValue({ models: [], default: '', provider_models: {} })
  const wrapper = mount(HomeIntake, { attachTo: document.body })
  await wrapper.get('.home-intake-model-trigger').trigger('click')
  await flushPromises()
  const other = wrapper.get<HTMLTextAreaElement>('#home-intake-prompt').element
  other.focus()
  wrapper.getComponent(ModelSelector).vm.$emit('close')
  await flushPromises()
  expect(document.activeElement).toBe(other)
  wrapper.unmount()
})

it.each([
  { name: 'Home', component: HomeIntake, trigger: '.home-intake-model-trigger', status: 'New chat models' },
  { name: 'new automation', component: NewScheduleForm, trigger: '.model-selector__trigger', status: 'Automation models' },
])('selects a model by keyboard and announces completion in $name', async ({ component, trigger, status }) => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  vi.mocked(api.get).mockResolvedValue({ models: ['keyboard-model'], default: '', provider_models: {} })
  const wrapper = mount(component, { attachTo: document.body })
  await flushPromises()
  await wrapper.get(trigger).trigger('click')
  await flushPromises()
  const option = wrapper.get<HTMLButtonElement>('[role="option"][data-model="keyboard-model"]')
  option.element.focus()
  await option.trigger('keydown', { key: 'Enter' })
  await flushPromises()
  expect(wrapper.get(trigger).text().toLowerCase()).toContain('keyboard')
  expect(wrapper.get(`[role="status"][aria-label="${status}"]`).attributes('aria-live')).toBe('polite')
  wrapper.unmount()
})

it.each([
  { name: 'Home', component: HomeIntake, trigger: '.home-intake-model-trigger' },
  { name: 'new automation', component: NewScheduleForm, trigger: '.model-selector__trigger' },
])('does not steal focus after navigation from $name during a transition', async ({ component, trigger }) => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  vi.mocked(api.get).mockResolvedValue({ models: ['model'], default: '', provider_models: {} })
  const wrapper = mount(component, { attachTo: document.body })
  await flushPromises()
  await wrapper.get(trigger).trigger('click')
  await flushPromises()
  wrapper.get<HTMLInputElement>('.model-selector__search').element.focus()
  const outside = document.createElement('button')
  document.body.appendChild(outside)
  projects.activeWorkspace = 'work'
  wrapper.unmount()
  outside.focus()
  await flushPromises()
  expect(document.activeElement).toBe(outside)
  outside.remove()
})

it('does not restore Home picker focus after a second transition or a deliberate focus move', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  vi.mocked(api.get).mockResolvedValue({ models: [], default: '', provider_models: {} })
  const wrapper = mount(HomeIntake, { attachTo: document.body })
  await wrapper.get('.home-intake-model-trigger').trigger('click')
  await flushPromises()
  wrapper.get<HTMLInputElement>('.model-selector__search').element.focus()
  projects.activeWorkspace = 'work'
  projects.activeWorkspace = 'personal'
  const prompt = wrapper.get<HTMLTextAreaElement>('#home-intake-prompt').element
  prompt.focus()
  await flushPromises()
  expect(document.activeElement).toBe(prompt)
  wrapper.unmount()
})

it('keeps the Home model draft and open picker when a guarded workspace switch is declined', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  projects.registerWorkspaceSwitchGuard(async () => false)
  vi.mocked(api.get).mockResolvedValue({ models: ['chosen-model'], default: '', provider_models: {} })
  const tasks = useTaskStore()
  const wrapper = mount(HomeIntake, { attachTo: document.body })
  await wrapper.get('.home-intake-model-trigger').trigger('click')
  await flushPromises()
  await wrapper.get('[role="option"][data-model="chosen-model"]').trigger('click')
  await wrapper.get('#home-intake-prompt').setValue('Keep this draft')
  await wrapper.get('.home-intake-model-trigger').trigger('click')
  const transition = tasks.modelTransition
  const reads = vi.mocked(api.get).mock.calls.filter(([url]) => url.startsWith('/api/models')).length
  expect(await projects.switchWorkspace('work', { transition: false })).toBe(false)
  await flushPromises()
  expect(projects.activeWorkspace).toBe('personal')
  expect(tasks.modelTransition).toBe(transition)
  expect(vi.mocked(api.get).mock.calls.filter(([url]) => url.startsWith('/api/models'))).toHaveLength(reads)
  expect(wrapper.get('.home-intake-model-trigger').text()).toContain('chosen-model')
  expect(wrapper.get<HTMLTextAreaElement>('#home-intake-prompt').element.value).toBe('Keep this draft')
  expect(wrapper.find('.model-selector__popover').exists()).toBe(true)
  wrapper.unmount()
})

it('does not submit a selected personal model when starting a chat in work', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  projects.projects = [
    { project_id: 'p', workspace: 'personal', name: 'Personal' },
    { project_id: 'w', workspace: 'work', name: 'Work' },
  ] as typeof projects.projects
  vi.mocked(api.get).mockImplementation(async url => ({
    models: [url.includes('workspace=work') ? 'work-model' : 'personal-model'],
    default: '', provider_models: {},
  }))
  const create = vi.spyOn(projects, 'newChatInProject').mockResolvedValue({ chat_id: 'new' } as never)
  vi.spyOn(projects, 'sendMessage').mockReturnValue(true)
  const wrapper = mount(HomeIntake, { attachTo: document.body })
  await wrapper.get('.home-intake-model-trigger').trigger('click')
  await flushPromises()
  await wrapper.get('[role="option"][data-model="personal-model"]').trigger('click')
  projects.activeWorkspace = 'work'
  await flushPromises()
  await wrapper.get('#home-intake-prompt').setValue('Work request')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(create).toHaveBeenCalledWith('w')
  wrapper.unmount()
})

it('announces Home model failures outside the closed picker and retries', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  let attempts = 0
  vi.mocked(api.get).mockImplementation(async (url) => {
    if (url.startsWith('/api/models') && attempts++ === 0) throw new Error('offline')
    return { models: [], default: '', provider_models: {} }
  })
  const wrapper = mount(HomeIntake)
  await wrapper.get('.home-intake-model-trigger').trigger('click')
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('offline')
  const retry = wrapper.get('[aria-label="Retry new chat models"]')
  await retry.trigger('click')
  await flushPromises()
  expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  expect(wrapper.get('[role="status"]').text()).toContain('No models listed; availability is unknown')
  wrapper.unmount()
})

it('labels Home’s last-good same-workspace models stale after a refresh failure', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  let reads = 0
  vi.mocked(api.get).mockImplementation(async url => {
    if (url.startsWith('/api/models')) {
      if (reads++ > 0) throw new Error('offline')
      return { models: ['saved-model'], default: '', provider_models: {} }
    }
    return []
  })
  const wrapper = mount(HomeIntake)
  await wrapper.get('.home-intake-model-trigger').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('Saved Model')
  await useTaskStore().fetchModels('personal')
  await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toContain('Previously loaded models may be out of date')
  expect(wrapper.text()).toContain('Saved Model')
  wrapper.unmount()
})

it('recovers an open Home picker when the registry returns without changing the active name', async () => {
  const projects = useProjectStore()
  const get = vi.mocked(api.get)
  get.mockResolvedValue({ models: ['ready-model'], default: '', provider_models: {} })
  const wrapper = mount(HomeIntake)
  await wrapper.get('.home-intake-model-trigger').trigger('click')
  await flushPromises()
  expect(get.mock.calls.filter(([url]) => url.startsWith('/api/models'))).toHaveLength(0)
  projects.workspaceRegistryStatus = 'error'
  await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toContain('Workspace list unavailable')
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  await flushPromises()
  expect(get).toHaveBeenCalledWith('/api/models?workspace=personal')
  await wrapper.get('.home-intake-model-trigger').trigger('click')
  expect(wrapper.text()).toContain('Ready Model')
  projects.workspaces = []
  await flushPromises()
  expect(wrapper.find('.model-selector__popover').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('Ready Model')
  wrapper.unmount()
})

it('loads a mounted schedule editor when the registry becomes ready without a name change', async () => {
  const projects = useProjectStore()
  const tasks = useTaskStore()
  const get = vi.mocked(api.get)
  get.mockResolvedValue({ models: [], default: '', provider_models: {} })
  const wrapper = mount(NewScheduleForm)
  await flushPromises()
  expect(get).not.toHaveBeenCalled()
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  await flushPromises()
  expect(get).toHaveBeenCalledWith('/api/models?workspace=personal')
  expect(tasks.models).not.toBeNull()
  wrapper.unmount()
})

it('loads models when a fixed-chat automation becomes project-bound after registry readiness', async () => {
  const projects = useProjectStore()
  projects.projects = [{ project_id: 'p', workspace: 'personal', name: 'Project' }] as typeof projects.projects
  projects.chats = [{ chat_id: 'c', project_id: 'p', title: 'Chat', created_at: '2026-01-01' }] as typeof projects.chats
  let resolve!: (value: unknown) => void
  vi.mocked(api.get).mockReturnValue(new Promise(done => { resolve = done }))
  const wrapper = mount(NewScheduleForm)
  await flushPromises()
  await wrapper.get('#nsf-frequency').setValue('interval')
  await wrapper.get('#nsf-target').setValue('web:c')
  expect(wrapper.text()).toContain('From the chat')
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  await flushPromises()
  expect(api.get).not.toHaveBeenCalled()
  await wrapper.get('#nsf-target').setValue('proj:p')
  expect(api.get).toHaveBeenCalledWith('/api/models?workspace=personal')
  await wrapper.get('#nsf-target').setValue('web:c')
  await wrapper.get('#nsf-target').setValue('proj:p')
  expect(api.get).toHaveBeenCalledTimes(1)
  resolve({ models: ['available-model'], default: '', provider_models: {} })
  await flushPromises()
  await wrapper.get('.model-selector__trigger').trigger('click')
  expect(wrapper.text()).toContain('Available Model')
  wrapper.unmount()
})

it('keeps a failed model read retryable instead of automatically repeating it on binding changes', async () => {
  const projects = useProjectStore()
  projects.projects = [{ project_id: 'p', workspace: 'personal', name: 'Project' }] as typeof projects.projects
  projects.chats = [{ chat_id: 'c', project_id: 'p', title: 'Chat', created_at: '2026-01-01' }] as typeof projects.chats
  vi.mocked(api.get).mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValue({ models: ['recovered-model'], default: '', provider_models: {} })
  const wrapper = mount(NewScheduleForm)
  await flushPromises()
  await wrapper.get('#nsf-frequency').setValue('interval')
  await wrapper.get('#nsf-target').setValue('web:c')
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  await flushPromises()
  await wrapper.get('#nsf-target').setValue('proj:p')
  await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toContain('offline')
  await wrapper.get('#nsf-target').setValue('web:c')
  await wrapper.get('#nsf-target').setValue('proj:p')
  await flushPromises()
  expect(api.get).toHaveBeenCalledTimes(1)
  await wrapper.get('[aria-label="Retry automation models"]').trigger('click')
  await flushPromises()
  expect(api.get).toHaveBeenCalledTimes(2)
  expect(wrapper.get('[role=status]').text()).toContain('Automation models loaded')
  wrapper.unmount()
})

it('does not submit the previous workspace model in a new automation', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  projects.projects = [
    { project_id: 'p', workspace: 'personal', name: 'Personal' },
    { project_id: 'w', workspace: 'work', name: 'Work' },
  ] as typeof projects.projects
  vi.mocked(api.get).mockResolvedValue({ models: ['old'], default: 'old', provider_models: { claude: ['old'] } })
  const tasks = useTaskStore()
  const create = vi.spyOn(tasks, 'createSchedule').mockResolvedValue({} as never)
  const wrapper = mount(NewScheduleForm)
  await flushPromises()
  await wrapper.get('.model-selector__trigger').trigger('click')
  await wrapper.get('[role="option"][data-model="old"]').trigger('click')
  await wrapper.get('#nsf-prompt').setValue('Keep this prompt')
  projects.activeWorkspace = 'work'
  await flushPromises()
  await wrapper.get('#nsf-frequency').setValue('manual')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(create).toHaveBeenCalledWith(expect.objectContaining({
    prompt: 'Keep this prompt', model: undefined, provider: undefined, webProjectId: 'w',
  }))
  wrapper.unmount()
})

it('remounts the new automation picker on switch, fences delayed selection and retries a failed read', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  let failWork!: (reason: Error) => void
  let workReads = 0
  vi.mocked(api.get).mockImplementation(async url => {
    if (url.includes('workspace=work') && workReads++ === 0) {
      return new Promise((_, reject) => { failWork = reject })
    }
    return { models: [url.includes('workspace=work') ? 'work-model' : 'personal-model'], default: '', provider_models: {} }
  })
  const wrapper = mount(NewScheduleForm, { attachTo: document.body })
  await flushPromises()
  await wrapper.get('.model-selector__trigger').trigger('click')
  await flushPromises()
  const old = wrapper.getComponent(ModelSelector)
  wrapper.get<HTMLInputElement>('.model-selector__search').element.focus()
  projects.activeWorkspace = 'work'
  old.vm.$emit('select', 'personal-model', 'anthropic')
  await flushPromises()
  expect(wrapper.find('.model-selector__popover').exists()).toBe(false)
  expect(wrapper.get('[role=status]').text()).toContain('Loading automation models')
  expect(document.activeElement).toBe(wrapper.get('.model-selector__trigger').element)
  failWork(new Error('offline'))
  await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toContain('offline')
  await wrapper.get('[aria-label="Retry automation models"]').trigger('click')
  await flushPromises()
  await wrapper.get('.model-selector__trigger').trigger('click')
  expect(wrapper.text()).toContain('Work Model')
  expect(wrapper.text()).not.toContain('Personal Model')
  wrapper.unmount()
})

it('ignores delayed automation picker input after A to B to A and never steals newly moved focus', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  vi.mocked(api.get).mockResolvedValue({ models: ['personal-model'], default: '', provider_models: {} })
  const wrapper = mount(NewScheduleForm, { attachTo: document.body })
  await flushPromises()
  await wrapper.get('.model-selector__trigger').trigger('click')
  await flushPromises()
  const old = wrapper.getComponent(ModelSelector)
  wrapper.get<HTMLInputElement>('.model-selector__search').element.focus()
  projects.activeWorkspace = 'work'
  projects.activeWorkspace = 'personal'
  old.vm.$emit('select', 'personal-model', 'anthropic')
  const prompt = wrapper.get<HTMLTextAreaElement>('#nsf-prompt').element
  prompt.focus()
  await flushPromises()
  expect(document.activeElement).toBe(prompt)
  expect(wrapper.getComponent(ModelSelector).props('modelValue')).toBe('')
  wrapper.unmount()
})

it('reports registry failure and recovers the mounted automation editor without a name change', async () => {
  const projects = useProjectStore()
  vi.mocked(api.get).mockResolvedValue({ models: ['recovered-model'], default: '', provider_models: {} })
  const wrapper = mount(NewScheduleForm)
  await flushPromises()
  expect(vi.mocked(api.get).mock.calls.filter(([url]) => url.startsWith('/api/models'))).toHaveLength(0)
  projects.workspaceRegistryStatus = 'error'
  await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toContain('Workspace list unavailable')
  expect(wrapper.find('[aria-label="Retry workspace list for automation models"]').exists()).toBe(true)
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  await flushPromises()
  expect(api.get).toHaveBeenCalledWith('/api/models?workspace=personal')
  expect(wrapper.get('[role=status]').attributes('aria-live')).toBe('polite')
  await wrapper.get('.model-selector__trigger').trigger('click')
  expect(wrapper.text()).toContain('Recovered Model')
  projects.workspaces = []
  await flushPromises()
  expect(wrapper.text()).not.toContain('Recovered Model')
  wrapper.unmount()
})

it('does not show the active workspace catalog in another workspace’s automation editor', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  projects.activeWorkspace = 'work'
  const tasks = useTaskStore()
  tasks.schedules = [{
    schedule_id: 's1', workspace: 'personal', title: 'Test', prompt: 'Keep me',
    daily_time_utc: '08:00', timezone_name: 'UTC', frequency: 'daily', chat_id: 0,
    enabled: true, model: 'personal-model', provider: 'claude',
  }] as typeof tasks.schedules
  tasks.schedulesLoaded = true
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/schedules/:scheduleId?', name: 'schedules', component: defineComponent({}) },
  ] })
  await router.push('/schedules/s1')
  await router.isReady()
  const wrapper = mount(SchedulePanel, { attachTo: document.body, global: { plugins: [router] } })
  await flushPromises()
  projects.activeWorkspace = 'work'
  await wrapper.get('[aria-label="Edit engine"]').trigger('click')
  expect(wrapper.text()).toContain('Switch to personal to choose automation models')
  expect(wrapper.find('.model-selector').exists()).toBe(false)
  wrapper.unmount()
})

it('keeps an unsaved engine edit bound to its schedule when the active workspace changes', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const tasks = useTaskStore()
  tasks.schedules = [{
    schedule_id: 's1', workspace: 'personal', title: 'Test', prompt: 'Keep me',
    daily_time_utc: '08:00', timezone_name: 'UTC', frequency: 'daily', chat_id: 0,
    enabled: true, model: '', provider: '', web_project_id: 'p',
  }] as typeof tasks.schedules
  tasks.schedulesLoaded = true
  vi.mocked(api.get).mockResolvedValue({ models: ['personal-model'], default: '', provider_models: {} })
  const update = vi.spyOn(tasks, 'updateSchedule').mockResolvedValue(tasks.schedules[0])
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/schedules/:scheduleId?', name: 'schedules', component: defineComponent({}) },
  ] })
  await router.push('/schedules/s1')
  await router.isReady()
  const wrapper = mount(SchedulePanel, { attachTo: document.body, global: { plugins: [router] } })
  await flushPromises()
  await wrapper.get('[aria-label="Edit engine"]').trigger('click')
  await wrapper.get('.model-selector__trigger').trigger('click')
  await flushPromises()
  const old = wrapper.getComponent(ModelSelector)
  await wrapper.get('[role="option"][data-model="personal-model"]').trigger('click')
  await wrapper.get('.model-selector__trigger').trigger('click')
  await flushPromises()
  wrapper.get<HTMLInputElement>('.model-selector__search').element.focus()
  const outside = document.createElement('button')
  document.body.appendChild(outside)
  projects.activeWorkspace = 'work'
  outside.focus()
  old.vm.$emit('select', 'stale-model', 'anthropic')
  await flushPromises()
  expect(document.activeElement).toBe(outside)
  expect(wrapper.text()).toContain('Switch to personal to choose automation models')
  expect(wrapper.get('.card-editing button.btn-primary').attributes('disabled')).toBeDefined()
  expect(update).not.toHaveBeenCalled()
  outside.remove()
  projects.activeWorkspace = 'personal'
  await flushPromises()
  expect(wrapper.getComponent(ModelSelector).props('modelValue')).toBe('personal-model')
  await wrapper.get('.card-editing button.btn-primary').trigger('click')
  await flushPromises()
  expect(update).toHaveBeenCalledWith('s1', expect.objectContaining({
    workspace: 'personal', model: 'personal-model', provider: 'claude', prompt: 'Keep me',
  }))
  wrapper.unmount()
})

it('keeps automation model errors and retry available in the engine editor', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  const tasks = useTaskStore()
  tasks.schedules = [{
    schedule_id: 's1', workspace: 'personal', title: 'Test', prompt: 'Keep me',
    daily_time_utc: '08:00', timezone_name: 'UTC', frequency: 'daily', chat_id: 0,
    enabled: true, model: '', provider: '',
  }] as typeof tasks.schedules
  tasks.schedulesLoaded = true
  let reads = 0
  vi.mocked(api.get).mockImplementation(async url => {
    if (url.startsWith('/api/models') && reads++ === 0) throw new Error('offline')
    return { models: ['personal-model'], default: '', provider_models: {} }
  })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/schedules/:scheduleId?', name: 'schedules', component: defineComponent({}) },
  ] })
  await router.push('/schedules/s1')
  await router.isReady()
  const wrapper = mount(SchedulePanel, { attachTo: document.body, global: { plugins: [router] } })
  await flushPromises()
  await wrapper.get('[aria-label="Edit engine"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toContain('offline')
  await wrapper.get('[aria-label="Retry automation models"]').trigger('click')
  await flushPromises()
  await wrapper.get('.model-selector__trigger').trigger('click')
  expect(wrapper.text()).toContain('Personal Model')
  const option = wrapper.get<HTMLButtonElement>('[role="option"][data-model="personal-model"]')
  option.element.focus()
  await option.trigger('keydown', { key: 'Enter' })
  await flushPromises()
  expect(wrapper.getComponent(ModelSelector).props('modelValue')).toBe('personal-model')
  wrapper.unmount()
})

it('recovers an open engine editor after registry readiness changes without a workspace change', async () => {
  const projects = useProjectStore()
  const tasks = useTaskStore()
  tasks.schedules = [{
    schedule_id: 's1', workspace: 'personal', title: 'Test', prompt: 'Keep me',
    daily_time_utc: '08:00', timezone_name: 'UTC', frequency: 'daily', chat_id: 0,
    enabled: true, model: '', provider: '',
  }] as typeof tasks.schedules
  tasks.schedulesLoaded = true
  vi.mocked(api.get).mockResolvedValue({ models: ['recovered-model'], default: '', provider_models: {} })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/schedules/:scheduleId?', name: 'schedules', component: defineComponent({}) },
  ] })
  await router.push('/schedules/s1')
  await router.isReady()
  const wrapper = mount(SchedulePanel, { global: { plugins: [router] } })
  await flushPromises()
  await wrapper.get('[aria-label="Edit engine"]').trigger('click')
  await flushPromises()
  expect(vi.mocked(api.get).mock.calls.filter(([url]) => url.startsWith('/api/models'))).toHaveLength(0)
  projects.workspaceRegistryStatus = 'error'
  await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toContain('Workspace list unavailable')
  projects.workspaces = [{ name: 'personal' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  await flushPromises()
  expect(api.get).toHaveBeenCalledWith('/api/models?workspace=personal')
  await wrapper.get('.model-selector__trigger').trigger('click')
  expect(wrapper.text()).toContain('Recovered Model')
  wrapper.unmount()
})

it('requires explicit default selection before moving an automation with a model override', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  projects.projects = [
    { project_id: 'p', workspace: 'personal', name: 'Personal' },
    { project_id: 'w', workspace: 'work', name: 'Work' },
  ] as typeof projects.projects
  const tasks = useTaskStore()
  tasks.schedules = [{
    schedule_id: 's1', workspace: 'personal', title: 'Test', prompt: 'Keep me',
    daily_time_utc: '08:00', timezone_name: 'UTC', frequency: 'daily', chat_id: 0,
    enabled: true, model: 'personal-model', provider: 'claude', web_project_id: 'p',
  }] as typeof tasks.schedules
  tasks.schedulesLoaded = true
  const update = vi.spyOn(tasks, 'updateSchedule').mockResolvedValue(tasks.schedules[0])
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/schedules/:scheduleId?', name: 'schedules', component: defineComponent({}) },
  ] })
  await router.push('/schedules/s1')
  await router.isReady()
  const wrapper = mount(SchedulePanel, { global: { plugins: [router] } })
  await flushPromises()
  await wrapper.get('[aria-label="Edit delivery"]').trigger('click')
  await wrapper.get('.card-editing select').setValue('work')
  expect(wrapper.get('.card-editing button.btn-primary').attributes('disabled')).toBeDefined()
  await wrapper.get('input[aria-label="Use destination workspace default model"]').setValue(true)
  await wrapper.get('.card-editing button.btn-primary').trigger('click')
  await flushPromises()
  expect(update).toHaveBeenCalledWith('s1', expect.objectContaining({
    workspace: 'work', model: '', provider: '', prompt: 'Keep me',
  }))
  wrapper.unmount()
})

it('closes task delegation on switching and opens the new workspace task with its own models', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  projects.projects = [
    { project_id: 'p', name: 'General', workspace: 'personal', is_auto: true },
    { project_id: 'w', name: 'General', workspace: 'work', is_auto: true },
  ] as typeof projects.projects
  const row = (workspace: string) => ({
    id: 'ship', title: `Task in ${workspace}`, status: 'backlog', project_id: workspace === 'work' ? 'w' : 'p',
    due: '', assignee: 'user', chat_id: '', attempt_id: '', linked_chats: [],
    created_at: '2026-03-01T09:00:00Z', updated_at: '2026-03-01T09:00:00Z',
    revision: 'a'.repeat(64), relative_path: 'Tasks/ship.md', body: 'Keep this description',
    attempt_state: '', attempt_outcome: '', attempt_summary: '', attempt_detail: '',
    live_attempt_id: '', completed_at: null, has_resolution: false,
  })
  let workModelReads = 0
  vi.mocked(api.get).mockImplementation(async url => {
    const workspace = url.includes('workspace=work') ? 'work' : 'personal'
    if (url.startsWith('/api/models')) {
      if (workspace === 'work' && workModelReads++ === 0) throw new Error('work offline')
      return { models: [`${workspace}-model`], default: '', provider_models: {} }
    }
    if (url.startsWith('/api/tasks?')) return { workspace, tasks: [row(workspace)] }
    if (url.startsWith('/api/tasks/')) return { workspace, task: row(workspace) }
    return []
  })
  const route = reactive({ fullPath: '/chat/abc', params: {} })
  const options = {
    attachTo: document.body,
    props: { mode: 'overlay' as const, taskId: 'ship' },
    global: { provide: { [routeLocationKey as symbol]: route, [routerKey as symbol]: { replace: vi.fn(), afterEach: vi.fn() } } },
  }
  const first = mount(TaskWorkspaceSurface, options)
  await flushPromises()
  await first.get('.task-delegate-go').trigger('click')
  await flushPromises()
  await first.get('.task-delegate-model').trigger('click')
  await flushPromises()
  first.get<HTMLInputElement>('.model-selector__search').element.focus()
  projects.workspaceRegistryStatus = 'error'
  await flushPromises()
  expect(first.get('[role=alert]').text()).toContain('Workspace list unavailable')
  expect(document.activeElement).toBe(first.get('.task-delegate-model').element)
  projects.workspaceRegistryStatus = 'ready'
  await flushPromises()
  await first.get('.task-delegate-model').trigger('click')
  await flushPromises()
  expect(first.text()).toContain('Personal Model')
  expect(first.text()).toContain('Your workspace default')
  await first.get('[role="option"][data-model="personal-model"]').trigger('click')
  expect(first.get('.task-delegate-model').text()).toContain('personal-model')
  await first.get('.task-delegate-model').trigger('click')
  await flushPromises()
  const old = first.getComponent(ModelSelector)
  first.get<HTMLInputElement>('.model-selector__search').element.focus()
  const outside = document.createElement('button')
  document.body.appendChild(outside)
  projects.activeWorkspace = 'work'
  outside.focus()
  old.vm.$emit('select', 'personal-model', 'anthropic')
  await flushPromises()
  expect(document.activeElement).toBe(outside)
  expect(first.find('#task-delegate-title').exists()).toBe(false)
  expect(first.emitted('close')).toBeTruthy()
  expect(api.get).not.toHaveBeenCalledWith('/api/models?workspace=work')
  outside.remove()
  first.unmount()
  const second = mount(TaskWorkspaceSurface, options)
  await flushPromises()
  await second.get('.task-delegate-go').trigger('click')
  await flushPromises()
  await second.get('.task-delegate-model').trigger('click')
  await flushPromises()
  expect(second.get('[role=alert]').text()).toContain('work offline')
  expect(second.findAll('[role=alert]')).toHaveLength(1)
  await second.get('[aria-label="Retry delegation models"]').trigger('click')
  await flushPromises()
  expect(second.get('[role=status]').attributes('aria-live')).toBe('polite')
  expect(second.text()).toContain('Work Model')
  expect(second.text()).not.toContain('Personal Model')
  await second.get('[role="option"][data-model="__workspace-default__"]').trigger('click')
  await second.get('.task-delegate-model').trigger('click')
  await flushPromises()
  const workOption = second.get<HTMLButtonElement>('[role="option"][data-model="work-model"]')
  workOption.element.focus()
  await workOption.trigger('keydown', { key: 'Enter' })
  await second.get('#task-delegate-instructions').setValue('Keep the B instruction')
  const delegate = vi.spyOn(useTaskBoardStore(), 'delegate').mockResolvedValue(null)
  await second.get('.task-sheet .btn-primary').trigger('click')
  await flushPromises()
  expect(delegate).toHaveBeenCalledWith('work', 'ship', 'a'.repeat(64), undefined,
    'Keep the B instruction', { model: 'work-model', provider: 'claude' })
  second.unmount()
})

it('closes the task and invalidates a pending delegation preview on synchronous A to B to A', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  projects.projects = [{ project_id: 'p', name: 'General', workspace: 'personal', is_auto: true }] as typeof projects.projects
  const task = {
    id: 'ship', title: 'Task', status: 'backlog', project_id: 'p',
    due: '', assignee: 'user', chat_id: '', attempt_id: '', linked_chats: [],
    created_at: '2026-03-01T09:00:00Z', updated_at: '2026-03-01T09:00:00Z',
    revision: 'a'.repeat(64), relative_path: 'Tasks/ship.md', body: 'Old description',
    attempt_state: '', attempt_outcome: '', attempt_summary: '', attempt_detail: '',
    live_attempt_id: '', completed_at: null, has_resolution: false,
  }
  vi.mocked(api.get).mockImplementation(async url => {
    if (url.startsWith('/api/tasks?')) return { workspace: 'personal', tasks: [task] }
    if (url.startsWith('/api/tasks/')) return { workspace: 'personal', task }
    return { models: [], default: '', provider_models: {} }
  })
  const route = reactive({ fullPath: '/chat/abc', params: {} })
  const options = {
    attachTo: document.body,
    props: { mode: 'overlay' as const, taskId: 'ship' },
    global: { provide: { [routeLocationKey as symbol]: route, [routerKey as symbol]: { replace: vi.fn(), afterEach: vi.fn() } } },
  }
  const wrapper = mount(TaskWorkspaceSurface, options)
  await flushPromises()
  const board = useTaskBoardStore()
  let resolveRead!: (value: Awaited<ReturnType<typeof board.get>>) => void
  const pending = new Promise<Awaited<ReturnType<typeof board.get>>>(resolve => { resolveRead = resolve })
  vi.spyOn(board, 'get').mockReturnValueOnce(pending)
  await wrapper.get('.task-delegate-go').trigger('click')
  await flushPromises()
  await wrapper.get('#task-delegate-instructions').setValue('Old instructions')
  projects.activeWorkspace = 'work'
  projects.activeWorkspace = 'personal'
  expect(wrapper.emitted('close')).toBeTruthy()
  resolveRead({ ...task, status: 'backlog', assignee: 'user', attempt_state: '', attempt_outcome: '',
    resolution: '', completions: [], delegation_log: '', updates: [] })
  await flushPromises()
  expect(wrapper.find('#task-delegate-title').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('Old instructions')
  wrapper.unmount()
})

it('does not apply an old preview to a newly opened delegation after A to B to A', async () => {
  const projects = useProjectStore()
  projects.workspaces = [{ name: 'personal' }, { name: 'work' }] as typeof projects.workspaces
  projects.workspaceRegistryStatus = 'ready'
  projects.projects = [{ project_id: 'p', name: 'General', workspace: 'personal', is_auto: true }] as typeof projects.projects
  const task = {
    id: 'ship', title: 'Task', status: 'backlog' as const, project_id: 'p',
    due: '', assignee: 'user' as const, chat_id: '', attempt_id: '', linked_chats: [],
    created_at: '2026-03-01T09:00:00Z', updated_at: '2026-03-01T09:00:00Z',
    revision: 'a'.repeat(64), relative_path: 'Tasks/ship.md', body: 'Current description',
    attempt_state: '' as const, attempt_outcome: '' as const, attempt_summary: '', attempt_detail: '',
    live_attempt_id: '', completed_at: null, has_resolution: false,
    resolution: '', completions: [], delegation_log: '', updates: [],
  }
  vi.mocked(api.get).mockImplementation(async url => {
    if (url.startsWith('/api/tasks?')) return { workspace: 'personal', tasks: [task] }
    if (url.startsWith('/api/tasks/')) return { workspace: 'personal', task }
    return { models: [], default: '', provider_models: {} }
  })
  const wrapper = mount(TaskWorkspaceSurface, { attachTo: document.body })
  await flushPromises()
  await wrapper.get('.task-card .task-open').trigger('click')
  await flushPromises()
  const board = useTaskBoardStore()
  type Preview = Awaited<ReturnType<typeof board.get>>
  let resolveOld!: (value: Preview) => void
  let resolveNew!: (value: Preview) => void
  const oldRead = new Promise<Preview>(resolve => { resolveOld = resolve })
  const newRead = new Promise<Preview>(resolve => { resolveNew = resolve })
  const get = vi.spyOn(board, 'get').mockReturnValueOnce(oldRead)
  await wrapper.get('.task-delegate-go').trigger('click')
  await flushPromises()
  await wrapper.get('#task-delegate-instructions').setValue('Old instructions')
  projects.activeWorkspace = 'work'
  projects.activeWorkspace = 'personal'
  await flushPromises()
  expect(wrapper.find('#task-delegate-title').exists()).toBe(false)
  await wrapper.get('.task-card .task-open').trigger('click')
  await flushPromises()
  get.mockReturnValueOnce(newRead)
  await wrapper.get('.task-delegate-go').trigger('click')
  await flushPromises()
  expect(wrapper.get<HTMLTextAreaElement>('#task-delegate-instructions').element.value).toBe('')
  resolveOld({ ...task, body: 'Stale description', revision: 'b'.repeat(64) })
  await flushPromises()
  expect(wrapper.text()).not.toContain('Stale description')
  expect(wrapper.text()).toContain('Loading description')
  const delegate = vi.spyOn(board, 'delegate').mockResolvedValue(null)
  resolveNew(task)
  await flushPromises()
  expect(wrapper.text()).toContain('Current description')
  await wrapper.get('.task-delegate-sheet .btn-primary').trigger('click')
  await flushPromises()
  expect(delegate).toHaveBeenCalledWith('personal', 'ship', 'a'.repeat(64), undefined, '', undefined)
  wrapper.unmount()
})
