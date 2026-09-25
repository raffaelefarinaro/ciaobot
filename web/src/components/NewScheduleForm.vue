<template>
  <!-- Same sections, in the same order, as the automation detail page
       (SchedulePanel): creating and reading an automation look alike. The
       parent supplies the .page-grid; this renders its main column and rail. -->
  <form class="new-form page-main" aria-label="New automation" @submit.prevent="submit">
    <section class="nsf-sec" aria-labelledby="nsf-prompt-h">
      <h2 id="nsf-prompt-h" class="nsf-h">Prompt</h2>
      <p class="nsf-hint">What Ciao does each time this runs.</p>
      <textarea
        id="nsf-prompt"
        v-model="prompt"
        class="nsf-prompt"
        aria-labelledby="nsf-prompt-h"
        placeholder="Review the latest project activity and write a concise briefing."
        rows="4"
        required
      ></textarea>
    </section>

    <section class="nsf-sec" aria-labelledby="nsf-when-h">
      <h2 id="nsf-when-h" class="nsf-h">When</h2>
      <div class="nsf-rows">
        <div class="nsf-row">
          <label class="nsf-k" for="nsf-frequency">Repeats</label>
          <div class="nsf-v">
            <select id="nsf-frequency" v-model="frequency">
              <option value="once">Once, then delete it</option>
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
              <option value="monthly">Monthly</option>
              <option value="interval">Every N minutes</option>
              <option value="manual">Only when I click Run</option>
            </select>
          </div>
        </div>
        <div v-if="isInterval" class="nsf-row nsf-row--top">
          <label class="nsf-k" for="nsf-interval">Every</label>
          <div class="nsf-v">
            <span class="nsf-inline">
              <input id="nsf-interval" v-model.number="intervalMinutes" class="nsf-num" type="number" min="1" required />
              <span class="nsf-unit">minutes</span>
            </span>
            <p class="nsf-note">
              Counted from the last run, not a time of day. Deliver to a chat to
              keep one conversation going, or to a project for a fresh chat each
              time. A run that comes due while the chat is busy is skipped and
              retried shortly after, never queued.
            </p>
          </div>
        </div>
        <div v-if="frequency === 'once'" class="nsf-row">
          <label class="nsf-k" for="nsf-date">On</label>
          <div class="nsf-v">
            <input id="nsf-date" v-model="runAtDate" type="date" :min="todayDate" required />
          </div>
        </div>
        <div v-if="frequency === 'weekly'" class="nsf-row nsf-row--top">
          <span id="nsf-days-k" class="nsf-k">On</span>
          <div class="nsf-v">
            <div class="nsf-days" role="group" aria-labelledby="nsf-days-k">
              <button
                v-for="d in days"
                :key="d"
                type="button"
                class="nsf-day"
                :aria-pressed="selectedDays.includes(d)"
                :aria-label="dayNames[d]"
                @click="toggleDay(d)"
              >{{ dayShort[d] }}</button>
            </div>
            <p v-if="!selectedDays.length" class="nsf-note">No days picked, so it runs every day.</p>
          </div>
        </div>
        <div v-if="frequency === 'monthly'" class="nsf-row">
          <label class="nsf-k" for="nsf-dom">Day of month</label>
          <div class="nsf-v">
            <input id="nsf-dom" v-model.number="dayOfMonth" class="nsf-num" type="number" min="1" max="31" placeholder="1–31" />
          </div>
        </div>
        <div v-if="needsTimeOfDay" class="nsf-row">
          <label class="nsf-k" for="nsf-time">At</label>
          <div class="nsf-v nsf-inline">
            <input id="nsf-time" v-model="time" class="nsf-time" type="time" required />
            <select v-model="timezone" aria-label="Timezone">
              <option value="Europe/Zurich">Europe/Zurich</option>
              <option value="Europe/Rome">Europe/Rome</option>
              <option value="UTC">UTC</option>
              <option value="America/New_York">US East</option>
              <option value="America/Los_Angeles">US West</option>
              <option value="Asia/Tokyo">Tokyo</option>
            </select>
          </div>
        </div>
      </div>
    </section>

    <section class="nsf-sec" aria-labelledby="nsf-where-h">
      <h2 id="nsf-where-h" class="nsf-h">Where it runs</h2>
      <div class="nsf-rows">
        <div class="nsf-row">
          <span class="nsf-k">Workspace</span>
          <div class="nsf-v">{{ workspaceLabel }}</div>
        </div>
        <div class="nsf-row">
          <label class="nsf-k" for="nsf-target">Deliver to</label>
          <div class="nsf-v">
            <select id="nsf-target" v-model="contextKey">
              <option value="" disabled>Select a target…</option>
              <optgroup v-for="group in contextGroups" :key="group.label" :label="group.label">
                <option v-for="ctx in group.items" :key="ctx.key" :value="ctx.key">
                  {{ ctx.label || ctx.key }}
                </option>
              </optgroup>
            </select>
          </div>
        </div>
        <div class="nsf-row nsf-row--top">
          <span class="nsf-k">Model</span>
          <div v-if="!inheritsChatModel" class="nsf-v">
            <div class="nsf-model">
            <ModelSelector
              :model-value="model"
              :sections="scheduleModelSections"
              :placeholder="inheritedModelLabel"
              :empty-placeholder="inheritedModelLabel"
              @select="selectScheduleModel"
            />
            </div>
            <p class="nsf-note">Uses {{ workspaceLabel }}'s default unless you pick another.</p>
          </div>
          <div v-else class="nsf-v">
            From the chat
            <p class="nsf-note">
              Every run uses the target chat's own model and mode. Change the
              chat's model to change this automation's.
            </p>
          </div>
        </div>
      </div>
    </section>

    <section class="nsf-sec" aria-labelledby="nsf-results-h">
      <h2 id="nsf-results-h" class="nsf-h">Results</h2>
      <div class="nsf-rows">
        <div class="nsf-row nsf-row--top">
          <span id="nsf-archive-k" class="nsf-k">Afterwards</span>
          <div v-if="supportsAutoArchive" class="nsf-v">
            <div class="nsf-seg" role="radiogroup" aria-labelledby="nsf-archive-k">
              <label :class="{ on: archivePolicy === 'manual' }">
                <input v-model="archivePolicy" type="radio" name="nsf-archive" value="manual" />
                Keep the chat
              </label>
              <label :class="{ on: archivePolicy === 'auto' }">
                <input v-model="archivePolicy" type="radio" name="nsf-archive" value="auto" />
                Archive if nothing to judge
              </label>
            </div>
            <p class="nsf-note">
              Archive runs a short check first. Chats with proposals, decisions or
              warnings stay visible.
            </p>
          </div>
          <div v-else class="nsf-v">
            Kept in the chat
            <p class="nsf-note">Runs land in one existing chat, so the conversation is kept and never auto-archived.</p>
          </div>
        </div>
      </div>
    </section>

    <div class="nsf-foot">
      <button type="button" class="nsf-cancel" @click="emit('cancel')">Cancel</button>
      <button type="submit" class="btn-primary" :disabled="!canSubmit">Create automation</button>
    </div>
  </form>

  <aside class="page-rail" aria-label="Preview">
    <section class="rail-section">
      <h2 class="rail-title">Preview</h2>
      <div class="rail-kvs">
        <div v-for="row in previewRows" :key="row.label" class="rail-kv">
          <span>{{ row.label }}</span><strong>{{ row.value }}</strong>
        </div>
      </div>
      <p class="rail-note">{{ previewNote }}</p>
    </section>
    <slot name="rail" />
  </aside>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useTaskStore } from '../stores/tasks'
import { useProjectStore } from '../stores/projects'
import type { RuntimeProvider, ScheduleArchivePolicy } from '../lib/types'
import ModelSelector from '../components/ModelSelector.vue'
import { providerForModelSection, sectionsFromModelsResponse } from '../lib/modelSections'
import { contextBindsFixedChat } from '../lib/scheduleBinding'
const emit = defineEmits<{ created: []; cancel: [] }>()
const store = useTaskStore()
const projectStore = useProjectStore()

const time = ref('')
const prompt = ref('')
const timezone = ref('Europe/Zurich')
const contextKey = ref('')
const frequency = ref('weekly')
const intervalMinutes = ref(10)
const days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
const dayShort: Record<string, string> = { mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun' }
const dayNames: Record<string, string> = {
  mon: 'Monday', tue: 'Tuesday', wed: 'Wednesday', thu: 'Thursday', fri: 'Friday', sat: 'Saturday', sun: 'Sunday',
}
const selectedDays = ref<string[]>([])
function toggleDay(d: string) {
  selectedDays.value = selectedDays.value.includes(d)
    ? selectedDays.value.filter(x => x !== d)
    : days.filter(x => x === d || selectedDays.value.includes(x))
}
const dayOfMonth = ref<number | null>(null)
const runAtDate = ref('')
const model = ref('')
const archivePolicy = ref<ScheduleArchivePolicy>('manual')

const todayDate = computed(() => new Date().toISOString().split('T')[0])

const isInterval = computed(() => frequency.value === 'interval')
// Interval cadence is relative and manual never auto-fires, so neither takes a
// time of day.
const needsTimeOfDay = computed(
  () => frequency.value !== 'manual' && !isInterval.value,
)
// One binding, two consequences — see lib/scheduleBinding. The backend ignores
// a model set here (the chat's own wins), and the dispatcher refuses to
// auto-archive the chat the runs land in, so neither control is offered.
const inheritsChatModel = computed(
  () => contextBindsFixedChat(frequency.value, contextKey.value),
)
const supportsAutoArchive = computed(() => !inheritsChatModel.value)
watch(supportsAutoArchive, (supported) => {
  if (!supported) archivePolicy.value = 'manual'
})

const canSubmit = computed(() => {
  if (!prompt.value || !contextKey.value) return false
  if (needsTimeOfDay.value && !time.value) return false
  if (isInterval.value && (!intervalMinutes.value || intervalMinutes.value < 1)) return false
  if (frequency.value === 'once' && !runAtDate.value) return false
  return true
})

onMounted(() => {
  if (!store.models) store.fetchModels()
})

const scheduleModelSections = computed(() => sectionsFromModelsResponse(store.models))

const activeWorkspaceConfig = computed(() =>
  projectStore.workspaceOptions.find(w => w.name === projectStore.activeWorkspace),
)
const workspaceLabel = computed(() => {
  const name = projectStore.activeWorkspace
  return name ? name.charAt(0).toUpperCase() + name.slice(1) : 'Workspace'
})
const inheritedModelLabel = computed(() => {
  const workspace = activeWorkspaceConfig.value
  const provider = projectStore.workspaceProviderOptions.find(
    option => option.value === workspace?.default_provider,
  )?.label || workspace?.default_provider || 'provider'
  // Workspaces no longer pin a model: the effective default is the
  // provider's own default from the Models tab.
  const model = store.models?.provider_defaults?.[workspace?.default_provider || '']
    || store.models?.default
    || ''
  return model ? `Inherit ${provider} / ${model}` : `Inherit ${provider} default`
})

const selectedProvider = ref<RuntimeProvider | undefined>(undefined)

function selectScheduleModel(value: string | string[], sectionKey: string) {
  model.value = Array.isArray(value) ? value[0] || '' : value
  selectedProvider.value = providerForModelSection(sectionKey)
}

const contextGroups = computed(() => {
  const groups: { label: string; items: { key: string; label: string }[] }[] = []
  // Projects (new chat per run)
  const projects = projectStore.projects.filter(
    p => p.workspace === projectStore.activeWorkspace,
  )
  const projItems = projects.map(p => ({
    key: `proj:${p.project_id}`,
    label: p.name,
  }))
  if (projItems.length) groups.push({ label: 'Projects (new chat per run)', items: projItems })
  // Fixed web chats
  const webItems: { key: string; label: string }[] = []
  for (const p of projects) {
    for (const c of projectStore.projectChats(p.project_id)) {
      webItems.push({ key: `web:${c.chat_id}`, label: `${p.name} / ${c.title}` })
    }
  }
  if (webItems.length) groups.push({ label: 'Fixed Web Chat', items: webItems })
  return groups
})

watch(
  [
    () => projectStore.activeWorkspace,
    () => projectStore.projects.length,
    () => projectStore.chats.length,
  ],
  () => {
    const projects = projectStore.projects.filter(
      p => p.workspace === projectStore.activeWorkspace,
    )
    const currentProjectId = contextKey.value.startsWith('proj:')
      ? contextKey.value.slice(5)
      : contextKey.value.startsWith('web:')
        ? projectStore.chats.find(c => c.chat_id === contextKey.value.slice(4))?.project_id
        : ''
    if (currentProjectId && projects.some(p => p.project_id === currentProjectId)) return
    const general = projects.find(p => p.is_auto && p.name === 'General') || projects[0]
    contextKey.value = general ? `proj:${general.project_id}` : ''
    model.value = ''
    selectedProvider.value = undefined
  },
  { immediate: true },
)

// ── Preview ─────────────────────────────────────────────────────────────
// The next two runs, computed from the choices, in the chosen timezone's wall
// clock. It mirrors the dispatcher's rules (weekly with no days runs daily; a
// monthly day past the month's end is skipped) closely enough to confirm the
// schedule reads the way the user meant; the server stays the authority.
const nowTick = ref(Date.now())
onMounted(() => { nowTick.value = Date.now() })
function wallNow(tz: string): { date: string; hm: string } {
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(new Date(nowTick.value))
    const get = (t: string) => parts.find(p => p.type === t)?.value || ''
    return { date: `${get('year')}-${get('month')}-${get('day')}`, hm: `${get('hour')}:${get('minute')}` }
  } catch {
    const d = new Date(nowTick.value)
    return { date: d.toISOString().slice(0, 10), hm: d.toISOString().slice(11, 16) }
  }
}
const WEEKDAY_KEYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']
function nextRuns(count: number): Date[] {
  if (!needsTimeOfDay.value || !time.value) return []
  const now = wallNow(timezone.value)
  const [y, m, d] = now.date.split('-').map(Number)
  const out: Date[] = []
  if (frequency.value === 'once') {
    if (!runAtDate.value) return []
    const [ry, rm, rd] = runAtDate.value.split('-').map(Number)
    return [new Date(Date.UTC(ry, rm - 1, rd))]
  }
  for (let i = 0; i < 400 && out.length < count; i += 1) {
    const day = new Date(Date.UTC(y, m - 1, d + i))
    if (i === 0 && time.value <= now.hm) continue
    const weekday = WEEKDAY_KEYS[day.getUTCDay()]
    if (frequency.value === 'weekly' && selectedDays.value.length && !selectedDays.value.includes(weekday)) continue
    if (frequency.value === 'monthly' && day.getUTCDate() !== (dayOfMonth.value || 1)) continue
    out.push(day)
  }
  return out
}
function runLabel(day: Date): string {
  const wd = dayShort[WEEKDAY_KEYS[day.getUTCDay()]]
  const [y, m, d] = wallNow(timezone.value).date.split('-').map(Number)
  const within = (day.getTime() - Date.UTC(y, m - 1, d)) / 86400000
  const date = within >= 0 && within < 7
    ? wd
    : `${wd} ${day.getUTCDate()} ${day.toLocaleString('en-GB', { month: 'short', timeZone: 'UTC' })}`
  return `${date} ${time.value}`
}
const targetLabel = computed(() => {
  for (const group of contextGroups.value) {
    const hit = group.items.find(item => item.key === contextKey.value)
    if (hit) return { label: hit.label, fixedChat: hit.key.startsWith('web:') }
  }
  return null
})
const previewRows = computed(() => {
  if (frequency.value === 'manual') return [{ label: 'Runs', value: 'When you click Run' }]
  if (isInterval.value) {
    const n = intervalMinutes.value
    return [{ label: 'Runs', value: n && n > 0 ? `Every ${n} min` : 'Set the interval' }]
  }
  if (!time.value) return [{ label: 'Next run', value: 'Pick a time' }]
  if (frequency.value === 'once' && !runAtDate.value) return [{ label: 'Runs', value: 'Pick a date' }]
  const runs = nextRuns(2)
  if (!runs.length) return [{ label: 'Next run', value: '—' }]
  const rows = [{ label: frequency.value === 'once' ? 'Runs' : 'Next run', value: runLabel(runs[0]) }]
  if (runs[1]) rows.push({ label: 'Then', value: runLabel(runs[1]) })
  return rows
})
const previewNote = computed(() => {
  const target = targetLabel.value
  const where = !target
    ? 'Pick where it delivers.'
    : target.fixedChat
      ? `Each run continues ${target.label}.`
      : `Each run opens a new chat in ${target.label}.`
  return needsTimeOfDay.value ? `Times are ${timezone.value}. ${where}` : where
})

async function submit() {
  let chatId: number | undefined
  let threadId: number | null | undefined
  let webChatId: string | null = null
  let webProjectId: string | null = null

  if (contextKey.value.startsWith('proj:')) {
    webProjectId = contextKey.value.replace('proj:', '')
  } else if (contextKey.value.startsWith('web:')) {
    webChatId = contextKey.value.replace('web:', '')
  } else if (contextKey.value) {
    const parts = contextKey.value.split(':')
    chatId = parseInt(parts[0], 10)
    threadId = parts.length > 1 ? parseInt(parts[1], 10) : null
  }

  await store.createSchedule({
    prompt: prompt.value,
    frequency: frequency.value,
    time: needsTimeOfDay.value ? time.value : '',
    timezone: timezone.value,
    daysOfWeek:
      frequency.value === 'weekly' && selectedDays.value.length > 0
        ? selectedDays.value
        : undefined,
    dayOfMonth: frequency.value === 'monthly' ? dayOfMonth.value : undefined,
    runAtDate: frequency.value === 'once' ? runAtDate.value : null,
    intervalMinutes: intervalMinutes.value,
    webChatId,
    webProjectId,
    chatId,
    threadId,
    // A chat-bound interval run inherits the chat, so never send an override.
    model: inheritsChatModel.value ? undefined : model.value || undefined,
    provider: inheritsChatModel.value ? undefined : selectedProvider.value,
    archivePolicy: archivePolicy.value,
  })
  time.value = ''
  prompt.value = ''
  frequency.value = 'weekly'
  intervalMinutes.value = 10
  selectedDays.value = []
  dayOfMonth.value = null
  runAtDate.value = ''
  contextKey.value = ''
  model.value = ''
  selectedProvider.value = undefined
  archivePolicy.value = 'manual'
  emit('created')
}
</script>

<style scoped>
/* Sections mirror SchedulePanel's detail view: sentence-case heading, muted
   line, hairline key/value rows. No card, no uppercase field labels. */
.new-form { display: flex; flex-direction: column; gap: 28px; }
.nsf-h {
  margin: 0 0 6px;
  color: var(--fg);
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.02em;
}
.nsf-hint { margin: 0 0 var(--space-2); color: var(--fg3); font-size: var(--text-sm); }
.nsf-prompt { width: 100%; box-sizing: border-box; min-height: 96px; resize: vertical; line-height: 1.5; }
.nsf-rows { border-top: 1px solid var(--border); }
.nsf-row {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  min-height: 52px;
  padding: 8px 2px;
  box-sizing: border-box;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}
.nsf-row--top { align-items: flex-start; }
.nsf-row--top .nsf-k { padding-top: 9px; }
.nsf-k { flex: 0 0 140px; color: var(--fg2); font-size: var(--text-sm); }
.nsf-v { flex: 1; min-width: 0; color: var(--fg); }
.nsf-v > select, .nsf-v > input { max-width: 100%; }
.nsf-v > select, .nsf-model { width: min(100%, 360px); }
.nsf-inline { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
.nsf-v.nsf-inline > select { width: auto; max-width: 100%; }
.nsf-num { width: 96px; }
.nsf-time { width: 120px; }
.nsf-unit { color: var(--fg2); }
.nsf-note { margin: 6px 0 0; color: var(--fg3); font-size: var(--text-xs); line-height: 1.45; max-width: 60ch; }

.nsf-days { display: flex; flex-wrap: wrap; gap: 4px; }
.nsf-day {
  min-width: 44px;
  min-height: 36px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease), color 120ms var(--ease);
}
.nsf-day:hover { border-color: var(--border-strong); color: var(--fg); }
.nsf-day[aria-pressed="true"] {
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  color: var(--fg);
}

.nsf-seg {
  display: inline-flex;
  flex-wrap: wrap;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
}
.nsf-seg label {
  display: inline-flex;
  align-items: center;
  min-height: 32px;
  padding: 0 12px;
  border-radius: calc(var(--radius-sm) - 2px);
  color: var(--fg2);
  cursor: pointer;
}
.nsf-seg label.on { background: var(--bg3); color: var(--fg); font-weight: 600; }
.nsf-seg label:focus-within { outline: 2px solid var(--accent); outline-offset: 1px; }
.nsf-seg input { position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }

.nsf-foot { display: flex; justify-content: flex-end; align-items: center; gap: var(--space-4); }
.nsf-cancel {
  min-height: 36px;
  padding: 0 4px;
  border: 0;
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}
.nsf-cancel:hover { color: var(--fg); }

@media (pointer: coarse) {
  .nsf-day, .nsf-seg label, .nsf-cancel { min-height: var(--touch); }
}
@container chat-pane (max-width: 560px) {
  .nsf-row, .nsf-row--top { flex-direction: column; align-items: stretch; gap: 6px; }
  .nsf-k { flex: none; }
  .nsf-row--top .nsf-k { padding-top: 0; }
}
</style>
