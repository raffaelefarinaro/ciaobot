import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import type {
  Schedule,
  StatusResponse,
  ModelsResponse,
  CliStats,
  ScheduleArchivePolicy,
} from '../lib/types'

export const useTaskStore = defineStore('tasks', () => {
  const schedules = ref<Schedule[]>([])
  const status = ref<StatusResponse | null>(null)
  const models = ref<ModelsResponse | null>(null)
  const stats = ref<CliStats | null>(null)
  const loading = ref(false)

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

  async function createSchedule(
    time: string,
    prompt: string,
    timezone?: string,
    daysOfWeek?: string[],
    chatId?: number,
    threadId?: number | null,
    frequency?: string,
    dayOfMonth?: number | null,
    webChatId?: string | null,
    webProjectId?: string | null,
    model?: string,
    runAtDate?: string | null,
    archivePolicy?: ScheduleArchivePolicy,
  ) {
    const body: Record<string, unknown> = { time, prompt, timezone, days_of_week: daysOfWeek, frequency, day_of_month: dayOfMonth }
    if (archivePolicy) body.archive_policy = archivePolicy
    if (runAtDate) body.run_at_date = runAtDate
    if (model) body.model = model
    if (webProjectId) {
      body.web_project_id = webProjectId
      body.chat_id = 0
    } else if (webChatId) {
      body.web_chat_id = webChatId
      body.chat_id = 0
    } else {
      if (chatId !== undefined) body.chat_id = chatId
      if (threadId !== undefined) body.thread_id = threadId
    }
    const s = await api.post<Schedule>('/api/schedules', body)
    schedules.value.push(s)
  }

  async function runScheduleNow(scheduleId: string): Promise<{ schedule_id: string; chat_id?: string }> {
    return await api.post<{ schedule_id: string; chat_id?: string }>(`/api/schedule-run/${scheduleId}`)
  }

  async function updateSchedule(scheduleId: string, updates: { time?: string; prompt?: string; timezone?: string; days_of_week?: string[] | null; chat_id?: number; thread_id?: number | null; frequency?: string; day_of_month?: number | null; run_at_date?: string | null; web_chat_id?: string | null; web_project_id?: string | null; model?: string; enabled?: boolean; archive_policy?: ScheduleArchivePolicy }) {
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
    schedules, status, models, stats, loading,
    fetchSchedules, fetchStatus, fetchModels, fetchStats, fetchAll,
    createSchedule, runScheduleNow, updateSchedule, deleteSchedule, updateStatus,
  }
})
