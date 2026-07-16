<template>
  <div class="page">
    <header class="page-header">
      <h2>schedules</h2>
      <div class="header-actions">
        <router-link to="/" class="btn-small">Back to Chat</router-link>
        <button class="btn-small" @click="refresh" :disabled="store.loading">Refresh</button>
        <button class="btn-small" @click="showNewSchedule = !showNewSchedule">+ New</button>
      </div>
    </header>

    <NewScheduleForm
      v-if="showNewSchedule"
      @created="showNewSchedule = false; refresh()"
    />

    <div v-if="store.schedules.length === 0" class="empty-state">
      <span class="empty-prompt">$</span> ls schedules/ <span class="empty-comment">// nothing scheduled. tap + to add one.</span>
    </div>

    <!-- One-offs (delete after run) -->
    <section v-if="oneOffSchedules.length" class="schedule-section">
      <h3 class="section-heading">One-offs <span class="section-hint">delete after run</span></h3>
      <div v-for="schedule in oneOffSchedules" :key="schedule.schedule_id" class="schedule-card">
        <div class="schedule-summary" @click="toggle(schedule.schedule_id)">
          <div class="summary-row-1">
            <span class="schedule-time">{{ schedule.run_at_date }} {{ schedule.daily_time_utc }}</span>
            <span class="schedule-title">{{ schedule.title || promptTitle(schedule.prompt) }}</span>
            <span class="expand-icon">{{ expanded[schedule.schedule_id] ? '\u25BC' : '\u25B6' }}</span>
          </div>
          <div class="summary-row-2">
            <span class="schedule-days-label">fires once \u00B7 {{ schedule.timezone_name }}</span>
            <span class="badge badge--muted">{{ archiveLabel(schedule.archive_policy) }}</span>
            <span class="badge context-badge" :class="badgeVariant(schedule)">{{ contextLabel(schedule) }}</span>
          </div>
        </div>
        <div v-if="expanded[schedule.schedule_id]" class="schedule-detail">
          <div v-if="editing[schedule.schedule_id]" class="edit-form">
            <div class="form-grid">
              <div class="form-group">
                <label>Date</label>
                <input v-model="editData[schedule.schedule_id].run_at_date" type="date" />
              </div>
              <div class="form-group">
                <label>Time</label>
                <input v-model="editData[schedule.schedule_id].time" type="time" />
              </div>
              <div class="form-group">
                <label>Timezone</label>
                <select v-model="editData[schedule.schedule_id].timezone">
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
                <select v-model="editData[schedule.schedule_id].contextKey">
                  <optgroup v-for="group in contextGroups" :key="group.label" :label="group.label">
                    <option v-for="ctx in group.items" :key="ctx.key" :value="ctx.key">
                      {{ ctx.label || ctx.key }}
                    </option>
                  </optgroup>
                </select>
              </div>
            </div>
            <div class="form-group">
              <label>Archive behavior</label>
              <select v-model="editData[schedule.schedule_id].archive_policy">
                <option value="manual">Manual, keep as normal chat</option>
                <option value="auto">Auto, archive if boring</option>
              </select>
            </div>
            <p class="hint">Auto runs a post-run classifier. If it finds proposals, decisions, warnings, or anything useful for the user to judge, the chat stays visible.</p>
            <div class="form-group">
              <label>Prompt</label>
              <textarea v-model="editData[schedule.schedule_id].prompt" rows="4"></textarea>
            </div>
            <div class="form-actions">
              <button class="btn-primary" @click="saveEdit(schedule.schedule_id)">Save</button>
              <button class="btn-chip" @click="editing[schedule.schedule_id] = false">Cancel</button>
            </div>
          </div>
          <div v-else class="detail-body">
            <p class="full-prompt">{{ schedule.prompt }}</p>
            <div class="detail-meta">
              <span>Created: {{ schedule.created_at?.slice(0, 10) || '\u2014' }}</span>
              <span>ID: {{ schedule.schedule_id }}</span>
            </div>
            <div class="detail-actions">
              <button class="btn-primary" @click="runNow(schedule.schedule_id)">Run now (and consume)</button>
              <button class="btn-chip" @click="startEdit(schedule)">Edit</button>
              <button class="btn-chip btn-danger" @click="confirmDelete(schedule.schedule_id)">Delete</button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- User routines -->
    <section v-if="userRoutines.length" class="schedule-section">
      <h3 class="section-heading">Routines <span class="section-hint">recurring</span></h3>
      <div v-for="schedule in userRoutines" :key="schedule.schedule_id" class="schedule-card">
        <!-- Summary (two rows) -->
        <div class="schedule-summary" @click="toggle(schedule.schedule_id)">
          <div class="summary-row-1">
            <span class="schedule-time">{{ schedule.frequency === 'manual' ? 'Manual' : schedule.daily_time_utc }}</span>
            <span v-if="schedule.enabled === false" class="badge badge--warning" style="font-size:var(--text-xs);padding:1px 6px;font-weight:500;">paused</span>
            <span class="schedule-title">{{ schedule.title || promptTitle(schedule.prompt) }}</span>
            <span class="expand-icon">{{ expanded[schedule.schedule_id] ? '\u25BC' : '\u25B6' }}</span>
          </div>
          <div class="summary-row-2">
            <span v-if="schedule.frequency === 'manual'" class="schedule-days-label">run on click only</span>
            <span v-else-if="schedule.frequency === 'monthly'" class="schedule-days-label">day {{ schedule.day_of_month }} of month</span>
            <span v-else-if="schedule.frequency === 'weekly' && schedule.days_of_week?.length" class="schedule-days">
              <span v-for="d in allDays" :key="d" class="badge badge--dot" :class="{ active: schedule.days_of_week.includes(d) }">{{ d }}</span>
            </span>
            <span v-else class="schedule-days-label">every day</span>
            <span class="badge badge--muted">{{ archiveLabel(schedule.archive_policy) }}</span>
            <span class="badge context-badge" :class="badgeVariant(schedule)">{{ contextLabel(schedule) }}</span>
          </div>
        </div>

        <!-- Detail panel -->
        <div v-if="expanded[schedule.schedule_id]" class="schedule-detail">
          <!-- Edit form -->
          <div v-if="editing[schedule.schedule_id]" class="edit-form">
            <div class="form-grid">
              <div v-if="editData[schedule.schedule_id].frequency !== 'manual'" class="form-group">
                <label>Time</label>
                <input v-model="editData[schedule.schedule_id].time" type="time" />
              </div>
              <div v-if="editData[schedule.schedule_id].frequency !== 'manual'" class="form-group">
                <label>Timezone</label>
                <select v-model="editData[schedule.schedule_id].timezone">
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
                <select v-model="editData[schedule.schedule_id].contextKey">
                  <optgroup v-for="group in contextGroups" :key="group.label" :label="group.label">
                    <option v-for="ctx in group.items" :key="ctx.key" :value="ctx.key">
                      {{ ctx.label || ctx.key }}
                    </option>
                  </optgroup>
                </select>
              </div>
            </div>
            <div class="form-group">
              <label>Frequency</label>
              <select v-model="editData[schedule.schedule_id].frequency">
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
                <option value="manual">Manual (run on click only)</option>
              </select>
            </div>
            <div v-if="editData[schedule.schedule_id].frequency === 'weekly'" class="form-group">
              <label>Days</label>
              <div class="days-row">
                <label v-for="d in allDays" :key="d" class="checkbox-pill" :class="{ active: editData[schedule.schedule_id].days_of_week.includes(d) }">
                  <input type="checkbox" :value="d" v-model="editData[schedule.schedule_id].days_of_week" hidden />
                  {{ d }}
                </label>
              </div>
            </div>
            <div v-if="editData[schedule.schedule_id].frequency === 'monthly'" class="form-group">
              <label>Day of month</label>
              <input v-model.number="editData[schedule.schedule_id].day_of_month" type="number" min="1" max="31" placeholder="1-31" />
            </div>
            <div class="form-group">
              <label>Archive behavior</label>
              <select v-model="editData[schedule.schedule_id].archive_policy">
                <option value="manual">Manual, keep as normal chat</option>
                <option value="auto">Auto, archive if boring</option>
              </select>
            </div>
            <p class="hint">Auto runs a post-run classifier. If it finds proposals, decisions, warnings, or anything useful for the user to judge, the chat stays visible.</p>
            <div class="form-group">
              <label>Prompt</label>
              <textarea v-model="editData[schedule.schedule_id].prompt" rows="4"></textarea>
            </div>
            <div class="form-actions">
              <button class="btn-primary" @click="saveEdit(schedule.schedule_id)">Save</button>
              <button class="btn-chip" @click="editing[schedule.schedule_id] = false">Cancel</button>
            </div>
          </div>

          <!-- Read-only detail -->
          <div v-else class="detail-body">
            <p class="full-prompt">{{ schedule.prompt }}</p>
            <div class="detail-meta">
              <span>Last triggered: {{ schedule.last_triggered_on || 'never' }}</span>
              <span>ID: {{ schedule.schedule_id }}</span>
            </div>
            <div class="detail-actions">
              <button class="btn-primary" @click="runNow(schedule.schedule_id)">Run now</button>
              <button class="btn-chip" @click="togglePause(schedule)">
                {{ schedule.enabled === false ? 'Resume' : 'Pause' }}
              </button>
              <button class="btn-chip" @click="startEdit(schedule)">Edit</button>
              <button class="btn-chip btn-danger" @click="confirmDelete(schedule.schedule_id)">Delete</button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- System automations -->
    <section v-if="systemAutomations.length" class="schedule-section">
      <h3 class="section-heading">System Automations <span class="section-hint">built-in</span></h3>
      <div v-for="schedule in systemAutomations" :key="schedule.schedule_id" class="schedule-card">
        <!-- Summary (two rows) -->
        <div class="schedule-summary" @click="toggle(schedule.schedule_id)">
          <div class="summary-row-1">
            <span class="schedule-time">{{ schedule.frequency === 'manual' ? 'Manual' : schedule.daily_time_utc }}</span>
            <span v-if="schedule.enabled === false" class="badge badge--warning" style="font-size:var(--text-xs);padding:1px 6px;font-weight:500;">paused</span>
            <span class="schedule-title">{{ schedule.title || promptTitle(schedule.prompt) }}</span>
            <span class="expand-icon">{{ expanded[schedule.schedule_id] ? '\u25BC' : '\u25B6' }}</span>
          </div>
          <div class="summary-row-2">
            <span v-if="schedule.frequency === 'manual'" class="schedule-days-label">run on click only</span>
            <span v-else-if="schedule.frequency === 'monthly'" class="schedule-days-label">day {{ schedule.day_of_month }} of month</span>
            <span v-else-if="schedule.frequency === 'weekly' && schedule.days_of_week?.length" class="schedule-days">
              <span v-for="d in allDays" :key="d" class="badge badge--dot" :class="{ active: schedule.days_of_week.includes(d) }">{{ d }}</span>
            </span>
            <span v-else class="schedule-days-label">every day</span>
            <span class="badge badge--muted">{{ archiveLabel(schedule.archive_policy) }}</span>
            <span class="badge context-badge" :class="badgeVariant(schedule)">{{ contextLabel(schedule) }}</span>
          </div>
        </div>

        <!-- Detail panel -->
        <div v-if="expanded[schedule.schedule_id]" class="schedule-detail">
          <!-- Read-only detail only (no edit form) -->
          <div class="detail-body">
            <p class="full-prompt">{{ schedule.prompt }}</p>
            <div class="detail-meta">
              <span>Last triggered: {{ schedule.last_triggered_on || 'never' }}</span>
              <span>ID: {{ schedule.schedule_id }}</span>
            </div>
            <div class="detail-actions">
              <button class="btn-primary" @click="runNow(schedule.schedule_id)">Run now</button>
              <button class="btn-chip" @click="togglePause(schedule)">
                {{ schedule.enabled === false ? 'Resume' : 'Pause' }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, reactive, computed } from 'vue'
import { useTaskStore } from '../stores/tasks'
import { useProjectStore } from '../stores/projects'
import type { Schedule } from '../lib/types'
import NewScheduleForm from './NewScheduleForm.vue'

interface EditState {
  time: string
  prompt: string
  timezone: string
  frequency: string
  days_of_week: string[]
  day_of_month: number | null
  run_at_date: string | null
  contextKey: string
  archive_policy: Schedule['archive_policy']
}

const store = useTaskStore()
const projectStore = useProjectStore()
const showNewSchedule = ref(false)
const expanded = reactive<Record<string, boolean>>({})
const editing = reactive<Record<string, boolean>>({})
const editData = reactive<Record<string, EditState>>({})
const allDays = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']

// Load projects for web chat targets
onMounted(async () => {
  if (!projectStore.projects.length) {
    await projectStore.fetchAll()
  }
})

// Split schedules into one-offs (delete after run) and recurring routines.
// Sort one-offs by their next fire datetime so the soonest is on top.
const oneOffSchedules = computed(() => {
  return store.schedules
    .filter(s => s.frequency === 'once')
    .slice()
    .sort((a, b) => {
      const ka = `${a.run_at_date || ''} ${a.daily_time_utc || ''}`
      const kb = `${b.run_at_date || ''} ${b.daily_time_utc || ''}`
      return ka.localeCompare(kb)
    })
})
const userRoutines = computed(() =>
  store.schedules.filter(s => s.frequency !== 'once' && s.scope !== 'system'),
)
const systemAutomations = computed(() =>
  store.schedules.filter(s => s.frequency !== 'once' && s.scope === 'system'),
)

const contextGroups = computed(() => {
  const groups: { label: string; items: { key: string; label: string }[] }[] = []
  // Projects (new chat per run)
  const projItems = projectStore.projects.map(p => ({
    key: `proj:${p.project_id}`,
    label: `${p.name} (${p.workspace})`,
  }))
  if (projItems.length) groups.push({ label: 'Projects (new chat per run)', items: projItems })
  // Fixed web chats
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

function contextKeyFor(schedule: Schedule): string {
  if (schedule.web_project_id) return `proj:${schedule.web_project_id}`
  if (schedule.web_chat_id) return `web:${schedule.web_chat_id}`
  if (schedule.thread_id) return `${schedule.chat_id}:${schedule.thread_id}`
  return `${schedule.chat_id}`
}

function contextLabel(schedule: Schedule): string {
  if (schedule.web_project_id) {
    const proj = projectStore.projects.find(p => p.project_id === schedule.web_project_id)
    if (proj) return `${proj.name} (new chat)`
  }
  if (schedule.web_chat_id) {
    const chat = projectStore.chats.find(c => c.chat_id === schedule.web_chat_id)
    if (chat) return chat.title || 'Untitled chat'
  }
  if (schedule.context_label) return schedule.context_label
  return 'General'
}

function badgeVariant(schedule: Schedule): string {
  if (schedule.web_project_id || schedule.web_chat_id) return 'badge--accent2'
  return 'badge--muted'
}

function promptTitle(prompt: string): string {
  const first = prompt.split('\n')[0].trim()
  return first.length > 60 ? first.slice(0, 57) + '...' : first
}

function archiveLabel(policy: Schedule['archive_policy'] | undefined): string {
  if (policy === 'auto') return 'auto archive'
  return 'manual'
}

async function refresh() { await store.fetchAll() }

function toggle(id: string) { expanded[id] = !expanded[id] }

function startEdit(s: Schedule) {
  editData[s.schedule_id] = {
    time: s.daily_time_utc,
    prompt: s.prompt,
    timezone: s.timezone_name,
    frequency: s.frequency || (s.days_of_week?.length ? 'weekly' : 'daily'),
    days_of_week: s.days_of_week ? [...s.days_of_week] : [],
    day_of_month: s.day_of_month ?? null,
    run_at_date: s.run_at_date ?? null,
    contextKey: contextKeyFor(s),
    archive_policy: s.archive_policy || 'manual',
  }
  editing[s.schedule_id] = true
}

async function saveEdit(id: string) {
  const d = editData[id]
  const updates: Record<string, unknown> = {
    time: d.frequency === 'manual' ? '' : d.time,
    prompt: d.prompt,
    timezone: d.timezone,
    frequency: d.frequency,
    days_of_week: d.frequency === 'weekly' && d.days_of_week.length > 0 ? d.days_of_week : null,
    day_of_month: d.frequency === 'monthly' ? d.day_of_month : null,
    run_at_date: d.frequency === 'once' ? d.run_at_date : null,
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

  await store.updateSchedule(id, updates as any)
  editing[id] = false
}

async function runNow(id: string) {
  await store.runScheduleNow(id)
  await refresh()
}

function confirmDelete(id: string) {
  if (confirm('Delete this schedule?')) store.deleteSchedule(id)
}

async function togglePause(schedule: Schedule) {
  await store.updateSchedule(schedule.schedule_id, { enabled: !schedule.enabled })
}

onMounted(refresh)
</script>

<style scoped>
.header-actions { display: flex; gap: 8px; }
.empty-state {
  color: var(--fg2);
  padding: 40px 0;
  font-size: var(--text-sm);
  text-align: left;
  padding-left: 8px;
}
.empty-prompt { color: var(--accent); font-weight: 700; margin-right: 6px; }
.empty-comment { color: var(--fg3); margin-left: 6px; }

.schedule-section { display: flex; flex-direction: column; gap: 8px; margin-bottom: 24px; }
.schedule-section .schedule-card { margin-bottom: 0; }
.section-heading {
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--fg);
  margin: 0 0 4px;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.section-hint {
  font-size: var(--text-xs);
  color: var(--fg2);
  font-weight: 400;
}

/* ── Card ──────────────────────────────────────────────────────── */
.schedule-card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}

/* ── Summary (two-row clickable header) ─────────────────────── */
.schedule-summary {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 16px;
  cursor: pointer;
}
.schedule-summary:hover { background: var(--bg3); }

.summary-row-1 {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.summary-row-2 {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.schedule-time {
  font-weight: 700;
  font-size: var(--text-lg);
  flex-shrink: 0;
}
.schedule-title {
  flex: 1;
  font-size: var(--text-base);
  color: var(--fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.expand-icon {
  font-size: var(--text-xs);
  color: var(--fg2);
  flex-shrink: 0;
  margin-left: auto;
}

.schedule-days {
  display: inline-flex;
  gap: 2px;
  flex-shrink: 0;
}
.schedule-days-label {
  font-size: var(--text-xs);
  color: var(--fg2);
  opacity: 0.7;
}

/* Constrain the context badge width without overriding shared styles */
.context-badge {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  display: inline-block;
}

/* ── Detail panel ──────────────────────────────────────────────── */
.schedule-detail {
  padding: 0 16px 16px;
  border-top: 1px solid var(--border);
}

.detail-body { padding-top: 12px; }

.full-prompt {
  font-size: var(--text-base);
  color: var(--fg);
  line-height: 1.6;
  white-space: pre-wrap;
  margin-bottom: 12px;
}
.detail-meta {
  display: flex;
  gap: 16px;
  font-size: var(--text-xs);
  color: var(--fg2);
  margin-bottom: 12px;
}
.detail-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

/* ── Edit form ─────────────────────────────────────────────────── */
.edit-form {
  padding-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.days-row {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

textarea {
  resize: vertical;
  min-height: 80px;
}
</style>
