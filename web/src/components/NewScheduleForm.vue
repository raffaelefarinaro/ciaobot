<template>
  <form class="new-form" @submit.prevent="submit">
    <div class="form-grid">
      <div class="form-group">
        <label>Workspace</label>
        <div class="inherited-value">{{ workspaceLabel }}</div>
      </div>
      <div v-if="frequency !== 'manual'" class="form-group">
        <label>Time</label>
        <input v-model="time" type="time" required />
      </div>
      <div v-if="frequency !== 'manual'" class="form-group">
        <label>Timezone</label>
        <select v-model="timezone">
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
        <select v-model="contextKey">
          <option value="" disabled>Select a target…</option>
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
          :model-value="model"
          :sections="scheduleModelSections"
          :placeholder="inheritedModelLabel"
          :empty-placeholder="inheritedModelLabel"
          @select="selectScheduleModel"
        />
        <p class="hint">Provider and model inherit from {{ workspaceLabel }} unless you choose an override.</p>
      </div>
    </div>
    <div class="form-group">
      <label>Frequency</label>
      <select v-model="frequency">
        <option value="once">Once (delete after run)</option>
        <option value="daily">Daily</option>
        <option value="weekly">Weekly</option>
        <option value="monthly">Monthly</option>
        <option value="manual">Manual (run on click only)</option>
      </select>
    </div>
    <div class="form-group">
      <label>Archive behavior</label>
      <select v-model="archivePolicy">
        <option value="manual">Manual, keep as normal chat</option>
        <option value="auto">Auto, archive if boring</option>
      </select>
    </div>
    <p class="hint">Auto runs a post-run classifier. If it finds proposals, decisions, warnings, or anything useful for the user to judge, the chat stays visible.</p>
    <div v-if="frequency === 'once'" class="form-group">
      <label>Date</label>
      <input v-model="runAtDate" type="date" :min="todayDate" required />
    </div>
    <div v-if="frequency === 'weekly'" class="form-group">
      <label>Days</label>
      <div class="days-row">
        <label v-for="d in days" :key="d" class="checkbox-pill" :class="{ active: selectedDays.includes(d) }">
          <input type="checkbox" :value="d" v-model="selectedDays" hidden />
          {{ d }}
        </label>
      </div>
    </div>
    <div v-if="frequency === 'monthly'" class="form-group">
      <label>Day of month</label>
      <input v-model.number="dayOfMonth" type="number" min="1" max="31" placeholder="1-31" />
    </div>
    <div class="form-group">
      <label>Prompt</label>
      <textarea v-model="prompt" placeholder="Schedule prompt" rows="2" required></textarea>
    </div>
    <button class="btn-primary" :disabled="!prompt || !contextKey || (frequency !== 'manual' && !time) || (frequency === 'once' && !runAtDate)">Create Schedule</button>
  </form>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useTaskStore } from '../stores/tasks'
import { useProjectStore } from '../stores/projects'
import type { ScheduleArchivePolicy } from '../lib/types'
import ModelSelector from '../components/ModelSelector.vue'
import { sectionsFromModelsResponse } from '../lib/modelSections'
const props = defineProps<{}>()
const emit = defineEmits<{ created: [] }>()
const store = useTaskStore()
const projectStore = useProjectStore()

const time = ref('')
const prompt = ref('')
const timezone = ref('Europe/Zurich')
const contextKey = ref('')
const frequency = ref('weekly')
const days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
const selectedDays = ref<string[]>([])
const dayOfMonth = ref<number | null>(null)
const runAtDate = ref('')
const model = ref('')
const archivePolicy = ref<ScheduleArchivePolicy>('manual')

const todayDate = computed(() => new Date().toISOString().split('T')[0])

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
  const model = workspace?.default_model
    || (workspace?.default_provider === 'codex' ? '' : projectStore.workspaceAppDefaultModel)
    || store.models?.default
    || ''
  return model ? `Inherit ${provider} / ${model}` : `Inherit ${provider} default`
})

const selectedProvider = ref<'claude' | 'codex' | undefined>(undefined)

function selectScheduleModel(value: string | string[], sectionKey: string) {
  model.value = Array.isArray(value) ? value[0] || '' : value
  selectedProvider.value = sectionKey === 'codex' ? 'codex' : 'claude'
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

  await store.createSchedule(
    frequency.value === 'manual' ? '' : time.value,
    prompt.value,
    timezone.value,
    frequency.value === 'weekly' && selectedDays.value.length > 0 ? selectedDays.value : undefined,
    chatId,
    threadId,
    frequency.value,
    frequency.value === 'monthly' ? dayOfMonth.value : undefined,
    webChatId,
    webProjectId,
    model.value || undefined,
    frequency.value === 'once' ? runAtDate.value : null,
    archivePolicy.value,
    selectedProvider.value,
  )
  time.value = ''
  prompt.value = ''
  frequency.value = 'weekly'
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
.new-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 12px;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
}

.days-row { display: flex; gap: 4px; flex-wrap: wrap; }

textarea { resize: vertical; min-height: 50px; }

.inherited-value {
  display: flex;
  align-items: center;
  min-height: 44px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg);
}
</style>
