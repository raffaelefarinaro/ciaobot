<template>
  <div class="schedule-panel">
    <PaneHeader
      v-if="!schedule && !showNew"
      title="Schedules"
      @open-sidebar="emit('open-sidebar')"
    />
    <PaneHeader v-else @open-sidebar="emit('open-sidebar')">
      <template #title>
        <div class="header-left">
          <button class="close-btn desktop-only" @click="closeSchedule" title="Close">&times;</button>
          <span v-if="schedule" class="pane-title">{{ schedule.title || promptTitle(schedule.prompt) }}</span>
          <span v-else-if="showNew" class="pane-title">New schedule</span>
        </div>
      </template>
      <template #actions>
        <button
          v-if="schedule && !editing"
          class="btn-small"
          :class="{ 'btn-running': showRunning }"
          :disabled="isStarting && !runningChatId"
          @click="onRunButtonClick"
        >
          {{ showRunning ? 'Running...' : 'Run now' }}
        </button>
        <button v-if="schedule && !editing" class="btn-small" @click="onToggleEnabled">
          {{ schedule.enabled ? 'Disable' : 'Enable' }}
        </button>
        <button v-if="schedule && !editing && schedule.scope !== 'system'" class="btn-small" @click="startEdit">Edit</button>
        <button v-if="schedule && !editing && schedule.scope !== 'system'" class="btn-small btn-danger" @click="onDelete">Delete</button>
      </template>
    </PaneHeader>

    <!-- New schedule form -->
    <div v-if="showNew" class="scroll-body">
      <NewScheduleForm @created="onCreated" />
    </div>

    <!-- Detail -->
    <div v-else-if="schedule" class="scroll-body">
      <div v-if="!schedule.enabled" class="disabled-banner">
        Disabled — won't run automatically. "Run now" still works.
      </div>
      <div class="meta-grid">
        <div v-if="schedule.frequency !== 'manual'">
          <strong>Time</strong><br />{{ schedule.daily_time_utc }} ({{ schedule.timezone_name }})
        </div>
        <div><strong>Frequency</strong><br />{{ frequencyLabel(schedule) }}</div>
        <div><strong>Context</strong><br />{{ contextLabel(schedule) }}</div>
        <div v-if="schedule.frequency !== 'manual'">
          <strong>Next run</strong><br />{{ nextRunLabel(schedule) }}
        </div>
        <div><strong>Last triggered</strong><br />{{ schedule.last_triggered_on || 'never' }}</div>
        <div><strong>Model</strong><br />{{ modelLabel(schedule) }}</div>
        <div><strong>Provider</strong><br />{{ schedule.provider || 'inherit target' }}</div>
        <div><strong>Archive</strong><br />{{ archiveLabel(schedule.archive_policy) }}</div>
      </div>

      <div v-if="editing" class="edit-form">
        <div class="form-grid">
          <div v-if="editData.frequency !== 'manual'" class="form-group">
            <label>Time</label>
            <input v-model="editData.time" type="time" />
          </div>
          <div v-if="editData.frequency !== 'manual'" class="form-group">
            <label>Timezone</label>
            <select v-model="editData.timezone">
              <option value="Europe/Zurich">Europe/Zurich</option>
              <option value="Europe/Rome">Europe/Rome</option>
              <option value="UTC">UTC</option>
              <option value="America/New_York">US East</option>
              <option value="America/Los_Angeles">US West</option>
              <option value="Asia/Tokyo">Tokyo</option>
            </select>
          </div>
          <div class="form-group">
            <label>Deliver to</label>
            <select v-model="editData.contextKey">
              <optgroup v-for="group in contextGroups" :key="group.label" :label="group.label">
                <option v-for="ctx in group.items" :key="ctx.key" :value="ctx.key">
                  {{ ctx.label || ctx.key }}
                </option>
              </optgroup>
            </select>
          </div>
          <div class="form-group">
            <label>Model</label>
            <ModelSelector
              v-model="editData.model"
              :sections="scheduleModelSections"
              placeholder="Default ({{ store.models?.default || '—' }})"
              empty-placeholder="Default ({{ store.models?.default || '—' }})"
            />
          </div>
        </div>
        <div class="form-group">
          <label>Frequency</label>
          <select v-model="editData.frequency">
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
            <option value="manual">Manual (run on click only)</option>
          </select>
        </div>
        <div v-if="editData.frequency === 'weekly'" class="form-group">
          <label>Days</label>
          <div class="days-row">
            <label v-for="d in allDays" :key="d" class="checkbox-pill" :class="{ active: editData.days_of_week.includes(d) }">
              <input type="checkbox" :value="d" v-model="editData.days_of_week" hidden />
              {{ d }}
            </label>
          </div>
        </div>
        <div v-if="editData.frequency === 'monthly'" class="form-group">
          <label>Day of month</label>
          <input v-model.number="editData.day_of_month" type="number" min="1" max="31" placeholder="1-31" />
        </div>
        <div class="form-group">
          <label>Archive behavior</label>
          <select v-model="editData.archive_policy">
            <option value="manual">Manual, keep as normal chat</option>
            <option value="auto">Auto, archive if boring</option>
          </select>
        </div>
        <p class="hint">Auto runs a post-run classifier. If it finds proposals, decisions, warnings, or anything useful for the user to judge, the chat stays visible.</p>
        <div class="form-group">
          <label>Prompt</label>
          <textarea v-model="editData.prompt" rows="10"></textarea>
        </div>
        <div class="form-actions">
          <button class="btn-primary" @click="saveEdit">Save</button>
          <button class="btn-chip" @click="editing = false">Cancel</button>
        </div>
      </div>

      <div v-else class="prompt-display">
        <label class="prompt-label">Prompt</label>
        <pre class="full-prompt">{{ schedule.prompt }}</pre>
      </div>
    </div>

    <!-- Overview homepage: shown when no schedule is selected but some exist -->
    <div v-else-if="store.schedules.length" class="scroll-body overview-body">
      <div v-if="missedSchedules.length" class="ov-card ov-card--alert">
        <div class="ov-head">
          <span class="ov-dot ov-dot--alert"></span>
          Missed <span class="ov-count">{{ missedSchedules.length }}</span>
          <span class="ov-hint">expected to run, didn't</span>
        </div>
        <router-link
          v-for="s in missedSchedules"
          :key="s.schedule_id"
          :to="`/schedules/${s.schedule_id}`"
          class="ov-item"
        >
          <span class="ov-when ov-when--alert">{{ formatWhen(s.last_expected_run) }}</span>
          <span class="ov-title">{{ s.title || promptTitle(s.prompt) }}</span>
        </router-link>
      </div>
      <div class="ov-card">
        <div class="ov-head">
          <span class="ov-dot"></span>
          Next up
          <span class="ov-hint">soonest first</span>
        </div>
        <router-link
          v-for="s in upcomingSchedules"
          :key="s.schedule_id"
          :to="`/schedules/${s.schedule_id}`"
          class="ov-item"
        >
          <span class="ov-when">{{ formatWhen(s.next_run) }}</span>
          <span class="ov-title">{{ s.title || promptTitle(s.prompt) }}</span>
        </router-link>
        <p v-if="!upcomingSchedules.length" class="ov-empty">
          No upcoming runs. Only manual or paused schedules.
        </p>
      </div>
    </div>

    <div v-else class="empty-state">
      <div class="empty-mark"><span class="wordmark wordmark--md">schedules</span></div>
      <p class="empty-hint">// pick one on the left, or tap <strong>+ New</strong>.</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTaskStore } from '../stores/tasks'
import { useProjectStore } from '../stores/projects'
import type { Schedule, ScheduleArchivePolicy } from '../lib/types'
import NewScheduleForm from './NewScheduleForm.vue'
import PaneHeader from './PaneHeader.vue'
import ModelSelector from './ModelSelector.vue'
import { sectionsFromModelsResponse } from '../lib/modelSections'

const props = defineProps<{ showNew?: boolean }>()
const emit = defineEmits<{ (e: 'created'): void; (e: 'open-sidebar'): void; (e: 'close'): void }>()

const route = useRoute()
const router = useRouter()
const store = useTaskStore()
const projectStore = useProjectStore()

const allDays = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
const editing = ref(false)
const startingBySchedule = ref<Set<string>>(new Set())
// schedule_id -> chat_id while the linked chat is still streaming
const runningBySchedule = ref<Record<string, string>>({})
const editData = ref({
  time: '',
  prompt: '',
  timezone: 'Europe/Zurich',
  frequency: 'daily',
  days_of_week: [] as string[],
  day_of_month: null as number | null,
  contextKey: '',
  model: '',
  archive_policy: 'manual' as ScheduleArchivePolicy,
})

onMounted(() => {
  if (!store.models) store.fetchModels()
})

const scheduleId = computed(() => (route.params.scheduleId as string) || '')
const schedule = computed(() =>
  store.schedules.find(s => s.schedule_id === scheduleId.value) || null,
)

watch(scheduleId, () => {
  editing.value = false
  purgeFinishedRuns()
})

const isStarting = computed(() =>
  scheduleId.value ? startingBySchedule.value.has(scheduleId.value) : false,
)

const runningChatId = computed(() =>
  scheduleId.value ? runningBySchedule.value[scheduleId.value] : undefined,
)

const showRunning = computed(() => {
  if (isStarting.value) return true
  const chatId = runningChatId.value
  return chatId ? projectStore.isChatStreaming(chatId) : false
})

function purgeFinishedRuns() {
  const next: Record<string, string> = {}
  for (const [sid, chatId] of Object.entries(runningBySchedule.value)) {
    if (projectStore.isChatStreaming(chatId)) next[sid] = chatId
  }
  runningBySchedule.value = next
}

const lastStreamingBySchedule = ref<Record<string, boolean>>({})

watch(
  () => Object.entries(runningBySchedule.value).map(([sid, chatId]) => ({
    sid,
    streaming: projectStore.isChatStreaming(chatId),
  })),
  (entries) => {
    const next = { ...runningBySchedule.value }
    let changed = false
    for (const { sid, streaming } of entries) {
      const wasStreaming = lastStreamingBySchedule.value[sid] ?? false
      lastStreamingBySchedule.value[sid] = streaming
      if (wasStreaming && !streaming && next[sid]) {
        delete next[sid]
        changed = true
      }
    }
    if (changed) runningBySchedule.value = next
  },
  { deep: true },
)

// Overview (homepage): soonest upcoming runs and missed runs (expected to
// fire, no trigger recorded — flagged server-side via the `missed` field).
const upcomingSchedules = computed(() =>
  store.schedules
    .filter(s => s.next_run)
    .sort((a, b) => (a.next_run! < b.next_run! ? -1 : 1))
    .slice(0, 5),
)
const missedSchedules = computed(() => store.schedules.filter(s => s.missed))

function archiveLabel(policy: ScheduleArchivePolicy | undefined): string {
  return policy === 'auto' ? 'auto (archive if boring)' : 'manual (keep chat)'
}

function formatWhen(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const diffMs = d.getTime() - Date.now()
  const past = diffMs < 0
  const absMin = Math.round(Math.abs(diffMs) / 60000)
  let rel: string
  if (absMin < 1) rel = 'now'
  else if (absMin < 60) rel = `${absMin}m`
  else if (absMin < 60 * 24) rel = `${Math.round(absMin / 60)}h`
  else rel = `${Math.round(absMin / 1440)}d`
  const clock = d.toLocaleString(undefined, {
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
  if (rel === 'now') return clock
  return past ? `${clock} (${rel} ago)` : `${clock} (in ${rel})`
}

const scheduleModelSections = computed(() => sectionsFromModelsResponse(store.models))

const contextGroups = computed(() => {
  const groups: { label: string; items: { key: string; label: string }[] }[] = []
  const projItems = projectStore.projects.map(p => ({
    key: `proj:${p.project_id}`,
    label: `${p.name} (${p.workspace})`,
  }))
  if (projItems.length) groups.push({ label: 'Projects (new chat per run)', items: projItems })
  const webItems: { key: string; label: string }[] = []
  for (const p of projectStore.projects) {
    const pChats = projectStore.projectChats(p.project_id)
    for (const c of pChats) {
      webItems.push({ key: `web:${c.chat_id}`, label: `${p.name} / ${c.title}` })
    }
  }
  if (webItems.length) groups.push({ label: 'Fixed Web Chat', items: webItems })
  return groups
})

function promptTitle(prompt: string): string {
  const first = prompt.split('\n')[0].trim()
  return first.length > 60 ? first.slice(0, 57) + '...' : first
}

function nextRunLabel(s: Schedule): string {
  if (!s.enabled) return 'Disabled'
  if (!s.next_run) return '—'
  try {
    const d = new Date(s.next_run)
    const fmt = new Intl.DateTimeFormat('en-CA', {
      timeZone: s.timezone_name,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    })
    const parts = Object.fromEntries(
      fmt.formatToParts(d).filter(p => p.type !== 'literal').map(p => [p.type, p.value]),
    )
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute} (${s.timezone_name})`
  } catch {
    return s.next_run
  }
}

function modelLabel(s: Schedule): string {
  if (s.model) return s.model
  const def = store.models?.default
  return def ? `Default (${def})` : 'Default'
}

function frequencyLabel(s: Schedule): string {
  if (s.frequency === 'manual') return 'Manual (run on click only)'
  if (s.frequency === 'monthly') return `Monthly, day ${s.day_of_month}`
  if (s.frequency === 'weekly') {
    if (s.days_of_week?.length) return `Weekly (${s.days_of_week.join(', ')})`
    return 'Weekly'
  }
  return 'Daily'
}

function contextLabel(s: Schedule): string {
  if (s.context_label) return s.context_label
  if (s.web_project_id) {
    const proj = projectStore.projects.find(p => p.project_id === s.web_project_id)
    return proj ? `${proj.name} (new chat per run)` : s.web_project_id
  }
  if (s.web_chat_id) {
    const chat = projectStore.chats.find(c => c.chat_id === s.web_chat_id)
    return chat?.title || s.web_chat_id
  }
  return s.context_label || 'General'
}

function contextKeyFor(s: Schedule): string {
  if (s.web_project_id) return `proj:${s.web_project_id}`
  if (s.web_chat_id) return `web:${s.web_chat_id}`
  if (s.thread_id) return `${s.chat_id}:${s.thread_id}`
  return `${s.chat_id}`
}

function startEdit() {
  if (!schedule.value) return
  editData.value = {
    time: schedule.value.daily_time_utc,
    prompt: schedule.value.prompt,
    timezone: schedule.value.timezone_name,
    frequency: schedule.value.frequency || (schedule.value.days_of_week?.length ? 'weekly' : 'daily'),
    days_of_week: schedule.value.days_of_week ? [...schedule.value.days_of_week] : [],
    day_of_month: schedule.value.day_of_month ?? null,
    contextKey: contextKeyFor(schedule.value),
    model: schedule.value.model || '',
    archive_policy: schedule.value.archive_policy || 'manual',
  }
  editing.value = true
}

async function saveEdit() {
  if (!schedule.value) return
  const d = editData.value
  const updates: Record<string, unknown> = {
    time: d.frequency === 'manual' ? '' : d.time,
    prompt: d.prompt,
    timezone: d.timezone,
    frequency: d.frequency,
    days_of_week: d.frequency === 'weekly' && d.days_of_week.length > 0 ? d.days_of_week : null,
    day_of_month: d.frequency === 'monthly' ? d.day_of_month : null,
    model: d.model,
    provider: d.model && (store.models?.codex_models || []).includes(d.model)
      ? 'codex'
      : (d.model ? 'claude' : ''),
    archive_policy: d.archive_policy,
  }
  if (d.contextKey.startsWith('proj:')) {
    updates.web_project_id = d.contextKey.replace('proj:', '')
    updates.web_chat_id = null
    updates.chat_id = 0
    updates.thread_id = null
  } else if (d.contextKey.startsWith('web:')) {
    updates.web_chat_id = d.contextKey.replace('web:', '')
    updates.web_project_id = null
    updates.chat_id = 0
    updates.thread_id = null
  } else {
    updates.web_chat_id = null
    updates.web_project_id = null
    const parts = d.contextKey.split(':')
    updates.chat_id = parseInt(parts[0], 10)
    updates.thread_id = parts.length > 1 ? parseInt(parts[1], 10) : null
  }
  await store.updateSchedule(schedule.value.schedule_id, updates as any)
  editing.value = false
}

function openRunningChat() {
  const chatId = runningChatId.value
  if (!chatId) return
  router.push(`/chat/${chatId}`)
}

function onRunButtonClick() {
  if (showRunning.value && runningChatId.value) {
    openRunningChat()
    return
  }
  void runNow()
}

function stopStarting(scheduleKey: string) {
  if (!startingBySchedule.value.has(scheduleKey)) return
  const next = new Set(startingBySchedule.value)
  next.delete(scheduleKey)
  startingBySchedule.value = next
}

async function runNow() {
  if (!schedule.value) return
  const scheduleKey = schedule.value.schedule_id
  if (startingBySchedule.value.has(scheduleKey)) return
  startingBySchedule.value = new Set([...startingBySchedule.value, scheduleKey])
  try {
    const result = await store.runScheduleNow(scheduleKey)
    await store.fetchSchedules()
    if (result.chat_id) {
      runningBySchedule.value = { ...runningBySchedule.value, [scheduleKey]: result.chat_id }
      // Refresh chats so the new chat is available for navigation
      await projectStore.fetchAll()
      projectStore.pushToast({
        chat_id: result.chat_id,
        title: 'Schedule started',
        body: schedule.value.title || promptTitle(schedule.value.prompt),
      })
      // Keep "Running..." through the API→stream handoff.
      for (let i = 0; i < 50 && !projectStore.isChatStreaming(result.chat_id); i++) {
        await new Promise(resolve => window.setTimeout(resolve, 100))
      }
    }
  } finally {
    stopStarting(scheduleKey)
  }
}

async function onToggleEnabled() {
  if (!schedule.value) return
  await store.updateSchedule(schedule.value.schedule_id, { enabled: !schedule.value.enabled })
}

async function onDelete() {
  if (!schedule.value) return
  if (!confirm('Delete this schedule?')) return
  const id = schedule.value.schedule_id
  await store.deleteSchedule(id)
  router.push('/schedules')
}

function onCreated() {
  emit('created')
}

function closeSchedule() {
  if (props.showNew) {
    emit('close')
  } else {
    router.push('/schedules')
  }
}
</script>

<style scoped>
.schedule-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
}


.scroll-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-4);
}

.disabled-banner {
  font-size: var(--text-sm);
  color: var(--fg2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: var(--space-2) var(--space-3);
  margin-bottom: var(--space-4);
}
.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--space-3);
  padding-bottom: var(--space-4);
  border-bottom: 1px solid var(--border);
  margin-bottom: var(--space-4);
  font-size: var(--text-sm);
  color: var(--fg2);
}
.meta-grid strong { color: var(--fg); font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.5px; }

.prompt-label {
  display: block;
  font-size: var(--text-xs);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 6px;
}

.full-prompt {
  font-size: var(--text-base);
  color: var(--fg);
  line-height: 1.55;
  white-space: pre-wrap;
  word-wrap: break-word;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--space-3);
  margin: 0;
}

.edit-form { display: flex; flex-direction: column; gap: var(--space-3); }
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--space-3);
}
.form-group { display: flex; flex-direction: column; gap: 4px; }
.form-group label { font-size: var(--text-xs); color: var(--fg2); }
.form-group input, .form-group select, .form-group textarea {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--fg);
  font-size: var(--text-base);
}
.form-group textarea { resize: vertical; min-height: 160px; font-family: ui-monospace, monospace; }

.days-row { display: flex; flex-wrap: wrap; gap: 4px; }

.form-actions { display: flex; gap: 8px; margin-top: 4px; }

.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  color: var(--fg2);
  text-align: center;
  padding: var(--space-4);
}
.empty-state .empty-mark { opacity: 0.85; }
.empty-state .empty-hint { color: var(--fg3); font-size: var(--text-sm); }

.hint { font-size: var(--text-xs); color: var(--fg2); margin: 0; }

/* ── Overview (next up + missed) ───────────────────────────────── */
.overview-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.ov-card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 16px;
}
.ov-card--alert {
  border-color: var(--warning);
  box-shadow: inset 3px 0 0 var(--warning);
}
.ov-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--fg);
}
.ov-count {
  font-size: var(--text-xs);
  background: var(--warning);
  color: var(--bg);
  border-radius: 999px;
  padding: 0 7px;
  font-weight: 700;
}
.ov-hint { font-size: var(--text-xs); color: var(--fg2); font-weight: 400; }
.ov-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); flex-shrink: 0; }
.ov-dot--alert { background: var(--warning); }
.ov-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 0;
  min-width: 0;
  border-top: 1px solid var(--border);
  text-decoration: none;
  color: inherit;
}
.ov-item:hover .ov-title { color: var(--accent); }
.ov-when {
  font-size: var(--text-xs);
  font-weight: 700;
  color: var(--fg);
  white-space: nowrap;
  flex-shrink: 0;
  min-width: 132px;
}
.ov-when--alert { color: var(--warning); }
.ov-title {
  flex: 1;
  font-size: var(--text-sm);
  color: var(--fg2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.ov-empty { margin: 0; font-size: var(--text-xs); color: var(--fg2); }

/* Close button */
.desktop-only { display: inline-flex; }
@media (max-width: 768px) { .desktop-only { display: none; } }

.close-btn {
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: 20px;
  line-height: 1;
  font-family: var(--font);
  min-width: 30px;
  min-height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.close-btn:hover { color: var(--fg); }

.btn-running:not(:disabled) {
  cursor: pointer;
}
.btn-running:not(:disabled):hover {
  filter: brightness(1.08);
}
</style>
