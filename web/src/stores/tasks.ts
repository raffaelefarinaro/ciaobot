import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../lib/api'
import type {
  RuntimeProvider,
  Schedule,
  StatusResponse,
  ModelsResponse,
  CliStats,
  ScheduleArchivePolicy,
} from '../lib/types'

/**
 * Fields accepted by `PATCH /api/schedules/{id}`.
 *
 * Was an inline parameter type that omitted `title` and `description`, so both
 * edit forms sent them through an `as any` cast. They are real Schedule fields.
 */
export interface ScheduleUpdate {
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
}

export const useTaskStore = defineStore('tasks', () => {
  const schedules = ref<Schedule[]>([])
  const status = ref<StatusResponse | null>(null)
  const models = ref<ModelsResponse | null>(null)
  const stats = ref<CliStats | null>(null)
  const loading = ref(false)

  // Interval schedules bound to one existing chat -- the cadence that replaced
  // loops. Kept as one shared lookup because Home, the sidebar, and the chat
  // banner must agree about both the count and whether any of them is live.
  const intervalsByChat = computed(() => {
    const byChat = new Map<string, { count: number; running: boolean }>()
    for (const s of schedules.value) {
      if (s.frequency !== 'interval' || !s.web_chat_id) continue
      const previous = byChat.get(s.web_chat_id)
      byChat.set(s.web_chat_id, {
        count: (previous?.count || 0) + 1,
        running: Boolean(previous?.running || s.enabled),
      })
    }
    return byChat
  })

  async function fetchSchedules() {
    schedules.value = await api.get<Schedule[]>('/api/schedules')
  }

  async function fetchStatus() {
    status.value = await api.get<StatusResponse>('/api/status')
  }

  async function fetchModels() {
    models.value = await api.get<ModelsResponse>('/api/models')
  }

  async function fetchStats() {
    try {
      stats.value = await api.get<CliStats>('/api/stats')
    } catch {
      stats.value = null
    }
  }

  async function fetchAll() {
    loading.value = true
    await Promise.all([fetchSchedules(), fetchStatus(), fetchModels(), fetchStats()])
    loading.value = false
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

  async function updateSchedule(scheduleId: string, updates: ScheduleUpdate) {
    const s = await api.patch<Schedule>(`/api/schedules/${scheduleId}`, updates)
    const idx = schedules.value.findIndex(x => x.schedule_id === scheduleId)
    if (idx >= 0) schedules.value[idx] = s
    return s
  }

  async function deleteSchedule(scheduleId: string) {
    await api.del(`/api/schedules/${scheduleId}`)
    schedules.value = schedules.value.filter(s => s.schedule_id !== scheduleId)
  }

  return {
    schedules, intervalsByChat, status, models, stats, loading,
    fetchSchedules, fetchStatus, fetchModels, fetchStats, fetchAll,
    createSchedule, runScheduleNow, updateSchedule, deleteSchedule, updateStatus,
  }
})
