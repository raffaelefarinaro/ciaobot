<template>
  <div class="schedule-panel">
    <PaneHeader
      v-if="!schedule && !loop && !showNew"
      title="Automations"
      @open-sidebar="emit('open-sidebar')"
    />
    <PaneHeader v-else @open-sidebar="emit('open-sidebar')">
      <template #title>
        <div class="header-left">
          <button class="close-btn desktop-only" @click="closeSchedule" title="Close">&times;</button>
          <span v-if="schedule" class="pane-title">{{ schedule.title || promptTitle(schedule.prompt) }}</span>
          <span v-else-if="loop" class="pane-title">{{ loop.title || promptTitle(loop.prompt) }}</span>
          <span v-else-if="showNew" class="pane-title">New automation</span>
        </div>
      </template>
      <template #actions>
        <template v-if="schedule && !editing">
          <button
            class="btn-small desktop-only"
            :class="{ 'btn-running': showRunning }"
            :disabled="isStarting && !runningChatId"
            @click="onRunButtonClick"
          >{{ showRunning ? 'Running...' : 'Run now' }}</button>
          <button class="btn-small desktop-only" @click="onToggleEnabled">
            {{ schedule.enabled ? 'Disable' : 'Enable' }}
          </button>
          <button v-if="schedule.scope !== 'system'" class="btn-small desktop-only" @click="startEdit">Edit</button>
          <button v-if="schedule.scope !== 'system'" class="btn-small btn-danger desktop-only" @click="onDelete">Delete</button>

          <button
            class="btn-small mobile-primary"
            :class="{ 'btn-running': showRunning }"
            :disabled="isStarting && !runningChatId"
            @click="onRunButtonClick"
          >{{ showRunning ? 'Running...' : 'Run now' }}</button>
          <div class="mobile-overflow" @keydown.escape.stop="actionsOpen = false">
            <button
              type="button"
              class="btn-icon overflow-trigger"
              aria-label="Automation actions"
              :aria-expanded="actionsOpen"
              @click="actionsOpen = !actionsOpen"
            >•••</button>
            <div v-if="actionsOpen" class="header-menu" role="menu">
              <button role="menuitem" @click="runHeaderAction(onToggleEnabled)">
                {{ schedule.enabled ? 'Disable' : 'Enable' }}
              </button>
              <button v-if="schedule.scope !== 'system'" role="menuitem" @click="runHeaderAction(startEdit)">Edit</button>
              <button v-if="schedule.scope !== 'system'" class="danger" role="menuitem" @click="runHeaderAction(onDelete)">Delete</button>
            </div>
          </div>
        </template>

        <template v-if="loop && !loopEditing">
          <button
            class="btn-small desktop-only"
            :class="{ 'btn-running': loop.running }"
            @click="onToggleLoopRunning"
          >{{ loop.running ? 'Stop' : 'Start' }}</button>
          <button class="btn-small desktop-only" @click="onRunLoopNow">Run now</button>
          <button class="btn-small desktop-only" @click="startLoopEdit">Edit</button>
          <button class="btn-small btn-danger desktop-only" @click="onDeleteLoop">Delete</button>

          <button
            class="btn-small mobile-primary"
            :class="{ 'btn-running': loop.running }"
            @click="onToggleLoopRunning"
          >{{ loop.running ? 'Stop' : 'Start' }}</button>
          <div class="mobile-overflow" @keydown.escape.stop="actionsOpen = false">
            <button
              type="button"
              class="btn-icon overflow-trigger"
              aria-label="Automation actions"
              :aria-expanded="actionsOpen"
              @click="actionsOpen = !actionsOpen"
            >•••</button>
            <div v-if="actionsOpen" class="header-menu" role="menu">
              <button role="menuitem" @click="runHeaderAction(onRunLoopNow)">Run now</button>
              <button role="menuitem" @click="runHeaderAction(startLoopEdit)">Edit</button>
              <button class="danger" role="menuitem" @click="runHeaderAction(onDeleteLoop)">Delete</button>
            </div>
          </div>
        </template>
      </template>
    </PaneHeader>

    <!-- New automation form (schedule or loop) -->
    <div v-if="showNew" class="scroll-body">
      <div class="type-toggle">
        <button class="btn-chip" :class="{ 'type-active': newType === 'schedule' }" @click="newType = 'schedule'">Schedule</button>
        <button class="btn-chip" :class="{ 'type-active': newType === 'loop' }" @click="newType = 'loop'">Loop</button>
      </div>
      <p class="hint type-hint">
        {{ newType === 'schedule'
          ? 'Fires at a time of day (daily / weekly / monthly / once), usually in a new chat per run.'
          : 'Re-sends a prompt into one existing chat every N minutes, keeping the conversation going.' }}
      </p>
      <NewScheduleForm v-if="newType === 'schedule'" @created="onCreated" />
      <NewLoopForm v-else @created="onCreated" />
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
        <div>
          <strong>Context</strong><br />
          <span :class="{ 'context-unavailable': contextUnavailable(schedule) }">{{ contextLabel(schedule) }}</span>
          <span v-if="contextUnavailable(schedule)" class="context-help">Edit this automation to choose an available target.</span>
        </div>
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
            <option value="auto">Automatically archive routine results</option>
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
        <div class="prompt-heading">
          <span class="prompt-label">Prompt</span>
          <div class="prompt-actions">
            <button
              v-if="isPromptLong(schedule.prompt)"
              type="button"
              class="btn-small"
              :aria-expanded="promptExpanded"
              @click="promptExpanded = !promptExpanded"
            >{{ promptExpanded ? 'Collapse' : 'Expand' }}</button>
            <button type="button" class="btn-small" @click="copyPrompt(schedule.prompt, schedule.schedule_id)">
              {{ promptCopyLabel(schedule.schedule_id) }}
            </button>
          </div>
        </div>
        <pre class="full-prompt" :class="{ 'full-prompt--collapsed': isPromptLong(schedule.prompt) && !promptExpanded }">{{ schedule.prompt }}</pre>
      </div>
    </div>

    <!-- Loop detail -->
    <div v-else-if="loop" class="scroll-body">
      <div v-if="!loop.running" class="disabled-banner">
        Stopped — won't fire automatically. "Run now" still works.
      </div>
      <div class="meta-grid">
        <div><strong>Every</strong><br />{{ loop.interval_minutes }} min</div>
        <div>
          <strong>Chat</strong><br />
          <span :class="{ 'context-unavailable': loopContextUnavailable(loop) }">{{ loopChatLabel(loop) }}</span>
          <span v-if="loopContextUnavailable(loop)" class="context-help">Edit this loop to choose an available chat.</span>
        </div>
        <div><strong>Status</strong><br />{{ loopStatusLabel(loop) }}</div>
        <div><strong>Last run</strong><br />{{ loop.last_run_at ? formatWhen(loop.last_run_at) : 'never' }}</div>
        <div><strong>Next run</strong><br />{{ loop.running ? (loop.next_run ? formatWhen(loop.next_run) : 'soon') : 'stopped' }}</div>
        <div><strong>On server start</strong><br />{{ loop.autostart ? 'starts automatically' : 'stays stopped' }}</div>
      </div>

      <div v-if="loopEditing" class="edit-form">
        <div class="form-grid">
          <div class="form-group">
            <label>Every (minutes)</label>
            <input v-model.number="loopEditData.interval_minutes" type="number" min="1" />
          </div>
          <div class="form-group">
            <label>Chat</label>
            <select v-model="loopEditData.web_chat_id">
              <optgroup v-for="group in loopChatGroups" :key="group.label" :label="group.label">
                <option v-for="c in group.items" :key="c.key" :value="c.key">{{ c.label }}</option>
              </optgroup>
            </select>
          </div>
        </div>
        <div class="form-group">
          <label>Title</label>
          <input v-model="loopEditData.title" type="text" />
        </div>
        <label class="checkbox-line">
          <input v-model="loopEditData.autostart" type="checkbox" />
          Start with the server
        </label>
        <div class="form-group">
          <label>Prompt</label>
          <textarea v-model="loopEditData.prompt" rows="10"></textarea>
        </div>
        <div class="form-actions">
          <button class="btn-primary" @click="saveLoopEdit">Save</button>
          <button class="btn-chip" @click="loopEditing = false">Cancel</button>
        </div>
      </div>

      <div v-else class="prompt-display">
        <div class="prompt-heading">
          <span class="prompt-label">Prompt</span>
          <div class="prompt-actions">
            <button
              v-if="isPromptLong(loop.prompt)"
              type="button"
              class="btn-small"
              :aria-expanded="promptExpanded"
              @click="promptExpanded = !promptExpanded"
            >{{ promptExpanded ? 'Collapse' : 'Expand' }}</button>
            <button type="button" class="btn-small" @click="copyPrompt(loop.prompt, loop.loop_id)">
              {{ promptCopyLabel(loop.loop_id) }}
            </button>
          </div>
        </div>
        <pre class="full-prompt" :class="{ 'full-prompt--collapsed': isPromptLong(loop.prompt) && !promptExpanded }">{{ loop.prompt }}</pre>
      </div>
    </div>

    <!-- Overview homepage: shown when nothing is selected but automations exist -->
    <div v-else-if="store.schedules.length || store.loops.length" class="scroll-body overview-body">
      <div class="ov-card">
        <div class="ov-head">
          <span class="ov-dot"></span>
          Schedules vs loops
        </div>
        <p class="ov-explain">
          <strong>Schedules</strong> fire at a time of day — daily, weekly, monthly, or once —
          and usually open a fresh chat per run. Use them for briefings, reports, and maintenance.
        </p>
        <p class="ov-explain">
          <strong>Loops</strong> live inside one existing chat and re-send the same prompt every
          N minutes (e.g. "check my PRs for changes every 10 minutes"), so the conversation keeps
          its context between iterations. A loop always runs with the chat's own model — change the
          chat's model to change the loop's. Loops set to <strong>start with the server</strong>
          resume on boot; the others stay stopped until started manually. If an iteration is still
          running when the next one is due, the loop skips it and retries shortly after.
        </p>
      </div>

      <div v-if="store.loops.length" class="ov-card">
        <div class="ov-head">
          <span class="ov-dot"></span>
          Loops
          <span class="ov-hint">{{ runningLoops.length }} running</span>
        </div>
        <router-link
          v-for="l in store.loops"
          :key="l.loop_id"
          :to="`/schedules/${l.loop_id}`"
          class="ov-item"
        >
          <span class="ov-when" :class="{ 'ov-when--muted': !l.running }">
            {{ l.running ? `every ${l.interval_minutes}m` : 'stopped' }}
          </span>
          <span class="ov-title">{{ l.title || promptTitle(l.prompt) }}</span>
        </router-link>
      </div>
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
      <div class="empty-mark"><span class="wordmark wordmark--md">automations</span></div>
      <p class="empty-hint">// pick one on the left, or tap <strong>+ New</strong>.</p>
      <p class="empty-hint">
        <strong>Schedules</strong> fire at a time of day, usually in a new chat per run.<br />
        <strong>Loops</strong> re-send a prompt into one existing chat every N minutes.
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTaskStore } from '../stores/tasks'
import { useProjectStore } from '../stores/projects'
import type { Loop, Schedule, ScheduleArchivePolicy } from '../lib/types'
import NewScheduleForm from './NewScheduleForm.vue'
import NewLoopForm from './NewLoopForm.vue'
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
const actionsOpen = ref(false)
const promptExpanded = ref(false)
const copiedPromptKey = ref('')
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

// Loops change state server-side (running / last_status / next_run), so
// refresh them periodically while the panel is open.
let loopPollTimer: number | undefined
let copiedPromptTimer: number | undefined

onMounted(() => {
  if (!store.models) store.fetchModels()
  loopPollTimer = window.setInterval(() => {
    store.fetchLoops().catch(() => {})
  }, 30_000)
})

onUnmounted(() => {
  if (loopPollTimer !== undefined) window.clearInterval(loopPollTimer)
  if (copiedPromptTimer !== undefined) window.clearTimeout(copiedPromptTimer)
})

const scheduleId = computed(() => (route.params.scheduleId as string) || '')
const schedule = computed(() =>
  store.schedules.find(s => s.schedule_id === scheduleId.value) || null,
)
// Loops share the /schedules/:id route; their ids are "loop-…" so they never
// collide with "sched-…" schedule ids.
const loop = computed(() =>
  store.loops.find(l => l.loop_id === scheduleId.value) || null,
)

const newType = ref<'schedule' | 'loop'>('schedule')
const loopEditing = ref(false)
const loopEditData = ref({
  prompt: '',
  title: '',
  interval_minutes: 10,
  web_chat_id: '',
  autostart: false,
})

watch(scheduleId, () => {
  editing.value = false
  loopEditing.value = false
  actionsOpen.value = false
  promptExpanded.value = false
  copiedPromptKey.value = ''
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
const runningLoops = computed(() => store.loops.filter(l => l.running))

const loopChatGroups = computed(() => {
  const groups: { label: string; items: { key: string; label: string }[] }[] = []
  for (const p of projectStore.projects) {
    const items = projectStore.projectChats(p.project_id).map(c => ({
      key: c.chat_id,
      label: c.title,
    }))
    if (items.length) groups.push({ label: `${p.name} (${p.workspace})`, items })
  }
  return groups
})

function loopChatLabel(l: Loop): string {
  if (l.context_label) return l.context_label
  const chat = projectStore.chats.find(c => c.chat_id === l.web_chat_id)
  return chat?.title || 'Unavailable chat'
}

function loopContextUnavailable(l: Loop): boolean {
  return !l.context_label && !projectStore.chats.some(c => c.chat_id === l.web_chat_id)
}

function loopStatusLabel(l: Loop): string {
  if (l.last_status === 'missing-chat') return 'stopped — chat missing'
  if (l.last_status === 'busy') return 'waiting — chat busy'
  if (l.last_status === 'running') return 'iteration running…'
  if (l.last_status === 'error') return 'last run failed'
  if (l.last_status === 'ok') return 'ok'
  return l.running ? 'waiting for first run' : 'never ran'
}

function startLoopEdit() {
  if (!loop.value) return
  loopEditData.value = {
    prompt: loop.value.prompt,
    title: loop.value.title || '',
    interval_minutes: loop.value.interval_minutes,
    web_chat_id: loop.value.web_chat_id,
    autostart: loop.value.autostart,
  }
  loopEditing.value = true
}

async function saveLoopEdit() {
  if (!loop.value) return
  await store.updateLoop(loop.value.loop_id, { ...loopEditData.value })
  loopEditing.value = false
}

async function onToggleLoopRunning() {
  if (!loop.value) return
  await store.updateLoop(loop.value.loop_id, { running: !loop.value.running })
}

async function onRunLoopNow() {
  if (!loop.value) return
  const l = loop.value
  try {
    await store.runLoopNow(l.loop_id)
    projectStore.pushToast({
      chat_id: l.web_chat_id,
      title: 'Loop iteration started',
      body: l.title || promptTitle(l.prompt),
    })
  } catch {
    projectStore.pushToast({
      chat_id: l.web_chat_id,
      title: 'Loop not started',
      body: 'The chat has a turn in flight — try again when it finishes.',
    })
  }
  await store.fetchLoops().catch(() => {})
}

async function onDeleteLoop() {
  if (!loop.value) return
  if (!confirm('Delete this loop?')) return
  const id = loop.value.loop_id
  await store.deleteLoop(id)
  router.push('/schedules')
}

function archiveLabel(policy: ScheduleArchivePolicy | undefined): string {
  return policy === 'auto' ? 'automatic (archive routine results)' : 'manual (keep chat)'
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
    return proj ? `${proj.name} (new chat per run)` : 'Unavailable project'
  }
  if (s.web_chat_id) {
    const chat = projectStore.chats.find(c => c.chat_id === s.web_chat_id)
    return chat?.title || 'Unavailable chat'
  }
  return s.context_label || 'General'
}

function contextUnavailable(s: Schedule): boolean {
  if (s.context_label) return false
  if (s.web_project_id) {
    return !projectStore.projects.some(p => p.project_id === s.web_project_id)
  }
  if (s.web_chat_id) {
    return !projectStore.chats.some(c => c.chat_id === s.web_chat_id)
  }
  return false
}

function isPromptLong(prompt: string): boolean {
  return prompt.length > 500 || prompt.split('\n').length > 12
}

async function copyPrompt(prompt: string, key: string) {
  try {
    await navigator.clipboard.writeText(prompt)
    copiedPromptKey.value = key
    if (copiedPromptTimer !== undefined) window.clearTimeout(copiedPromptTimer)
    copiedPromptTimer = window.setTimeout(() => {
      if (copiedPromptKey.value === key) copiedPromptKey.value = ''
    }, 1800)
  } catch {
    copiedPromptKey.value = `error:${key}`
    if (copiedPromptTimer !== undefined) window.clearTimeout(copiedPromptTimer)
    copiedPromptTimer = window.setTimeout(() => {
      if (copiedPromptKey.value === `error:${key}`) copiedPromptKey.value = ''
    }, 1800)
  }
}

function promptCopyLabel(key: string): string {
  if (copiedPromptKey.value === key) return 'Copied'
  if (copiedPromptKey.value === `error:${key}`) return 'Copy failed'
  return 'Copy'
}

function runHeaderAction(action: () => void | Promise<void>) {
  actionsOpen.value = false
  void action()
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
.context-unavailable { color: var(--warning); font-weight: 600; }
.context-help {
  display: block;
  margin-top: 4px;
  color: var(--fg3);
  font-size: var(--text-xs);
  line-height: 1.4;
}

.prompt-label {
  display: block;
  font-size: var(--text-xs);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 6px;
}
.prompt-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: 6px;
}
.prompt-heading .prompt-label { margin-bottom: 0; }
.prompt-actions { display: flex; gap: var(--space-2); }

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
.full-prompt--collapsed {
  max-height: 14rem;
  overflow: hidden;
  -webkit-mask-image: linear-gradient(to bottom, #000 72%, transparent 100%);
  mask-image: linear-gradient(to bottom, #000 72%, transparent 100%);
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

/* ── New-automation type toggle ────────────────────────────────── */
.type-toggle { display: flex; gap: 8px; margin-bottom: 8px; }
.type-active {
  border-color: var(--accent);
  color: var(--accent);
  font-weight: 600;
}
.type-hint { margin-bottom: 12px; }

.checkbox-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--text-sm);
  color: var(--fg2);
  cursor: pointer;
}
.checkbox-line input { flex-shrink: 0; }

.ov-explain {
  margin: 0 0 8px;
  font-size: var(--text-sm);
  color: var(--fg2);
  line-height: 1.55;
}
.ov-explain:last-child { margin-bottom: 0; }
.ov-explain strong { color: var(--fg); }
.ov-when--muted { color: var(--fg3); }

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
  padding: 8px 0;
  min-height: var(--touch);
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
.mobile-primary,
.mobile-overflow { display: none; }

.mobile-overflow { position: relative; }
.overflow-trigger {
  font-size: 12px;
  letter-spacing: 1px;
}
.header-menu {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 100;
  min-width: 160px;
  padding: 4px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.4);
}
.header-menu button {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: var(--touch);
  padding: 8px 12px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg);
  text-align: left;
  cursor: pointer;
}
.header-menu button:hover { background: var(--bg3); }
.header-menu button.danger { color: var(--error); }

@media (max-width: 768px) {
  .desktop-only,
  .close-btn.desktop-only { display: none; }
  .mobile-primary,
  .mobile-overflow { display: inline-flex; }
  .prompt-heading { align-items: flex-start; }
}

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
