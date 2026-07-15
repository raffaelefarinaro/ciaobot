import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import type {
  Loop,
  Schedule,
  StatusResponse,
  ModelsResponse,
  CliStats,
  ScheduleArchivePolicy,
} from '../lib/types'

export const useTaskStore = defineStore('tasks', () => {
  const schedules = ref<Schedule[]>([])
  const loops = ref<Loop[]>([])
  const status = ref<StatusResponse | null>(null)
  const models = ref<ModelsResponse | null>(null)
  const stats = ref<CliStats | null>(null)
  const loading = ref(false)

  async function fetchSchedules() {
    schedules.value = await api.get<Schedule[]>('/api/schedules')
  }

  async function fetchLoops() {
    loops.value = await api.get<Loop[]>('/api/loops')
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
    await Promise.all([fetchSchedules(), fetchLoops(), fetchStatus(), fetchModels(), fetchStats()])
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
    provider?: 'claude' | 'codex',
  ) {
    const body: Record<string, unknown> = { time, prompt, timezone, days_of_week: daysOfWeek, frequency, day_of_month: dayOfMonth }
    if (archivePolicy) body.archive_policy = archivePolicy
    if (runAtDate) body.run_at_date = runAtDate
    if (model) body.model = model
    if (provider) body.provider = provider
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

  async function updateSchedule(scheduleId: string, updates: { time?: string; prompt?: string; timezone?: string; days_of_week?: string[] | null; chat_id?: number; thread_id?: number | null; frequency?: string; day_of_month?: number | null; run_at_date?: string | null; web_chat_id?: string | null; web_project_id?: string | null; workspace?: string; model?: string; provider?: 'claude' | 'codex' | ''; enabled?: boolean; archive_policy?: ScheduleArchivePolicy }) {
    const s = await api.patch<Schedule>(`/api/schedules/${scheduleId}`, updates)
    const idx = schedules.value.findIndex(x => x.schedule_id === scheduleId)
    if (idx >= 0) schedules.value[idx] = s
    return s
  }

  async function deleteSchedule(scheduleId: string) {
    await api.del(`/api/schedules/${scheduleId}`)
    schedules.value = schedules.value.filter(s => s.schedule_id !== scheduleId)
  }

  async function createLoop(body: {
    prompt: string
    web_chat_id: string
    interval_minutes: number
    title?: string
    autostart?: boolean
    start?: boolean
  }) {
    const loop = await api.post<Loop>('/api/loops', body)
    loops.value.push(loop)
    return loop
  }

  async function updateLoop(loopId: string, updates: { prompt?: string; title?: string; interval_minutes?: number; web_chat_id?: string; autostart?: boolean; running?: boolean }) {
    const loop = await api.patch<Loop>(`/api/loops/${loopId}`, updates)
    const idx = loops.value.findIndex(x => x.loop_id === loopId)
    if (idx >= 0) loops.value[idx] = loop
    return loop
  }

  async function runLoopNow(loopId: string): Promise<{ loop_id: string; chat_id?: string; status: string }> {
    return await api.post<{ loop_id: string; chat_id?: string; status: string }>(`/api/loop-run/${loopId}`)
  }

  async function deleteLoop(loopId: string) {
    await api.del(`/api/loops/${loopId}`)
    loops.value = loops.value.filter(l => l.loop_id !== loopId)
  }

  return {
    schedules, loops, status, models, stats, loading,
    fetchSchedules, fetchLoops, fetchStatus, fetchModels, fetchStats, fetchAll,
    createSchedule, runScheduleNow, updateSchedule, deleteSchedule, updateStatus,
    createLoop, updateLoop, runLoopNow, deleteLoop,
  }
})
