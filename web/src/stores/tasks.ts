import { defineStore } from 'pinia'
import { computed, onScopeDispose, ref, watch } from 'vue'
import { api } from '../lib/api'
import { sectionsFromModelsResponse } from '../lib/modelSections'
import { useProjectStore } from './projects'
import type {
  RuntimeProvider,
  Schedule,
  StatusResponse,
  ModelsResponse,
  ScheduleArchivePolicy,
} from '../lib/types'

/**
 * Fields accepted by `PATCH /api/schedules/{id}`.
 *
 * Was an inline parameter type that omitted `title` and `description`, so both
 * edit forms sent them through an `as any` cast. They are real Schedule fields.
 */
export interface ScheduleUpdate {
  // The `revision` the edit was made from; filled in from the cached row.
  revision?: string
  time?: string
  prompt?: string
  timezone?: string
  days_of_week?: string[] | null
  chat_id?: number
  thread_id?: number | null
  frequency?: string
  interval_minutes?: number
  day_of_month?: number | null
  run_at_date?: string | null
  web_chat_id?: string | null
  web_project_id?: string | null
  workspace?: string
  model?: string
  provider?: RuntimeProvider | ''
  enabled?: boolean
  archive_policy?: ScheduleArchivePolicy
  title?: string
  description?: string
  // Optional shell command run before the prompt; "" clears it.
  command?: string
}

export type ModelFetchResult =
  | { status: 'success'; workspace: string; generation: number }
  | { status: 'error'; workspace: string; generation: number; error: string }
  | { status: 'superseded'; workspace: string; generation: number | null }

export const useTaskStore = defineStore('tasks', () => {
  const schedules = ref<Schedule[]>([])
  const status = ref<StatusResponse | null>(null)
  const models = ref<ModelsResponse | null>(null)
  const modelLoading = ref(false)
  const modelError = ref('')
  const modelResult = ref<'success' | 'error' | null>(null)
  const modelStale = ref(false)
  const modelEmpty = computed(() => !!models.value && !sectionsFromModelsResponse(models.value).some(section => section.models.length))
  const modelGeneration = ref(0)
  onScopeDispose(() => { modelGeneration.value++ })
  const modelTransition = ref(0)
  const projects = useProjectStore()
  const modelWorkspaceEligible = computed(() =>
    projects.workspaceRegistryStatus === 'ready'
    && !!projects.activeWorkspace
    && projects.workspaces.some(workspace => workspace.name === projects.activeWorkspace),
  )
  watch(
    () => [projects.activeWorkspace, projects.workspaceRegistryStatus, projects.workspaces.map(w => w.name).join('\0')],
    () => {
      modelGeneration.value++
      models.value = null
      modelLoading.value = false
      modelError.value = ''
      modelResult.value = null
      modelStale.value = false
      modelTransition.value++
    },
    { flush: 'sync' },
  )
  const loading = ref(false)
  const scheduleLoading = ref(false)
  const scheduleLoadError = ref('')
  const schedulesLoaded = ref(false)
  let scheduleRequestId = 0
  let scheduleUpdateVersion = 0
  let activeScheduleRead: { promise: Promise<void>; supersede: () => void } | null = null
  const scheduleUpdateOwners = new Map<string, symbol>()
  const updatingSchedules = ref<Set<string>>(new Set())

  function reserveScheduleUpdate(id: string): symbol | null {
    if (scheduleUpdateOwners.has(id)) return null
    const owner = Symbol(id)
    scheduleUpdateOwners.set(id, owner)
    updatingSchedules.value = new Set(scheduleUpdateOwners.keys())
    return owner
  }

  function releaseScheduleUpdate(id: string, owner: symbol): void {
    if (scheduleUpdateOwners.get(id) !== owner) return
    scheduleUpdateOwners.delete(id)
    updatingSchedules.value = new Set(scheduleUpdateOwners.keys())
  }

  // Automations of any cadence bound to one existing chat. Kept as one shared
  // lookup because every list that shows a chat must agree about both the
  // count and whether any of them is live.
  const schedulesByChat = computed(() => {
    const byChat = new Map<string, { count: number; running: boolean }>()
    for (const s of schedules.value) {
      if (!s.web_chat_id) continue
      const previous = byChat.get(s.web_chat_id)
      byChat.set(s.web_chat_id, {
        count: (previous?.count || 0) + 1,
        running: Boolean(previous?.running || s.enabled),
      })
    }
    return byChat
  })

  function fetchSchedules(): Promise<void> {
    const requestId = ++scheduleRequestId
    const previous = activeScheduleRead
    let supersede!: () => void
    const superseded = new Promise<void>(resolve => { supersede = resolve })
    scheduleLoading.value = true
    scheduleLoadError.value = ''
    const promise = (async () => {
      try {
        while (true) {
          const version = scheduleUpdateVersion
          const result = await Promise.race([
            api.get<Schedule[]>('/api/schedules').then(
              value => ({ kind: 'success' as const, value }),
              error => ({ kind: 'error' as const, error }),
            ),
            superseded.then(() => ({ kind: 'superseded' as const })),
          ])
          if (requestId !== scheduleRequestId) return await activeScheduleRead!.promise
          if (result.kind === 'superseded') return
          if (result.kind === 'error') throw result.error
          if (version !== scheduleUpdateVersion) continue
          schedules.value = result.value
          schedulesLoaded.value = true
          return
        }
      } catch (error: unknown) {
        if (requestId !== scheduleRequestId) return await activeScheduleRead!.promise
        scheduleLoadError.value = error instanceof Error
          ? error.message
          : 'Could not load automations.'
        throw error
      } finally {
        if (requestId === scheduleRequestId) scheduleLoading.value = false
      }
    })()
    activeScheduleRead = { promise, supersede }
    previous?.supersede()
    return promise
  }

  async function fetchStatus() {
    status.value = await api.get<StatusResponse>('/api/status')
  }

  async function fetchModels(workspace = projects.activeWorkspace): Promise<ModelFetchResult> {
    if (!modelWorkspaceEligible.value || workspace !== projects.activeWorkspace) {
      return { status: 'superseded', workspace, generation: null }
    }
    const generation = ++modelGeneration.value
    const current = () => generation === modelGeneration.value
      && modelWorkspaceEligible.value && workspace === projects.activeWorkspace
    modelLoading.value = true
    modelError.value = ''
    modelResult.value = null
    modelStale.value = models.value !== null
    try {
      const response = await api.get<ModelsResponse>(`/api/models?${new URLSearchParams({ workspace })}`)
      if (!current()) return { status: 'superseded', workspace, generation }
      models.value = response
      modelResult.value = 'success'
      modelStale.value = false
      return { status: 'success', workspace, generation }
    } catch {
      if (!current()) return { status: 'superseded', workspace, generation }
      modelError.value = 'Could not load models.'
      modelResult.value = 'error'
      return { status: 'error', workspace, generation, error: modelError.value }
    } finally {
      if (current()) modelLoading.value = false
    }
  }

  async function fetchAll() {
    loading.value = true
    try {
      await Promise.all([fetchSchedules(), fetchStatus(), fetchModels()])
    } finally {
      loading.value = false
    }
  }

  async function updateStatus(updates: { model?: string; mode?: string }) {
    status.value = await api.patch<StatusResponse>('/api/status', updates)
  }

  /**
   * Fields accepted by `POST /api/schedules`.
   *
   * Was a fourteen-argument positional signature; adding `interval_minutes` to
   * it would have meant threading another `undefined` through the middle of
   * every call. One object, named at the call site instead.
   */
  async function createSchedule(input: {
    prompt: string
    frequency: string
    time?: string
    timezone?: string
    daysOfWeek?: string[]
    dayOfMonth?: number | null
    runAtDate?: string | null
    // Required when frequency is 'interval'; ignored otherwise.
    intervalMinutes?: number
    webChatId?: string | null
    webProjectId?: string | null
    chatId?: number
    threadId?: number | null
    model?: string
    provider?: RuntimeProvider
    archivePolicy?: ScheduleArchivePolicy
    command?: string
  }) {
    const body: Record<string, unknown> = {
      time: input.time || '',
      prompt: input.prompt,
      timezone: input.timezone,
      days_of_week: input.daysOfWeek,
      frequency: input.frequency,
      day_of_month: input.dayOfMonth,
    }
    if (input.frequency === 'interval') body.interval_minutes = input.intervalMinutes
    if (input.archivePolicy) body.archive_policy = input.archivePolicy
    if (input.command) body.command = input.command
    if (input.runAtDate) body.run_at_date = input.runAtDate
    if (input.model) body.model = input.model
    if (input.provider) body.provider = input.provider
    if (input.webProjectId) {
      body.web_project_id = input.webProjectId
      body.chat_id = 0
    } else if (input.webChatId) {
      body.web_chat_id = input.webChatId
      body.chat_id = 0
    } else {
      if (input.chatId !== undefined) body.chat_id = input.chatId
      if (input.threadId !== undefined) body.thread_id = input.threadId
    }
    const s = await api.post<Schedule>('/api/schedules', body)
    schedules.value.push(s)
    return s
  }

  async function runScheduleNow(
    scheduleId: string,
  ): Promise<{ schedule_id: string; chat_id?: string; status?: string }> {
    return await api.post<{ schedule_id: string; chat_id?: string; status?: string }>(
      `/api/schedule-run/${scheduleId}`,
    )
  }

  async function updateSchedule(scheduleId: string, updates: ScheduleUpdate, reservation?: symbol) {
    const owner = reservation ?? reserveScheduleUpdate(scheduleId)
    if (!owner || scheduleUpdateOwners.get(scheduleId) !== owner) throw new Error('This automation is already being updated.')
    try {
      const known = schedules.value.find(x => x.schedule_id === scheduleId)?.revision
      const body = known && updates.revision === undefined ? { ...updates, revision: known } : updates
      let s: Schedule
      try {
        s = await api.patch<Schedule>(`/api/schedules/${scheduleId}`, body)
      } catch (error) {
        await refetchOnScheduleConflict(error)
        throw error
      }
      scheduleUpdateVersion++
      const idx = schedules.value.findIndex(x => x.schedule_id === scheduleId)
      if (idx >= 0) schedules.value[idx] = s
      return s
    } finally {
      if (!reservation) releaseScheduleUpdate(scheduleId, owner)
    }
  }

  // A 409 on an update or delete means the row changed since it was read:
  // load the current rows so the screen shows what to review, and let the
  // caller surface the error.
  async function refetchOnScheduleConflict(error: unknown) {
    if ((error as { status?: number } | null)?.status === 409) {
      await fetchSchedules().catch(() => undefined)
    }
  }

  async function deleteSchedule(scheduleId: string) {
    const known = schedules.value.find(x => x.schedule_id === scheduleId)?.revision
    try {
      await api.del(`/api/schedules/${scheduleId}${known ? `?revision=${encodeURIComponent(known)}` : ''}`)
    } catch (error) {
      await refetchOnScheduleConflict(error)
      throw error
    }
    schedules.value = schedules.value.filter(s => s.schedule_id !== scheduleId)
  }

  return {
    schedules, schedulesByChat, status, models, loading, modelLoading, modelError, modelResult, modelStale, modelEmpty, modelTransition, modelWorkspaceEligible, updatingSchedules,
    scheduleLoading, scheduleLoadError, schedulesLoaded,
    fetchSchedules, fetchStatus, fetchModels, fetchAll,
    createSchedule, runScheduleNow, updateSchedule, reserveScheduleUpdate, releaseScheduleUpdate, deleteSchedule, updateStatus,
  }
})
