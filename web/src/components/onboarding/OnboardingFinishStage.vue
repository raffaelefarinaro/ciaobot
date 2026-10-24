<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { api } from '../../lib/api'
import { apiErrorMessage } from '../../lib/errorMessage'
import type {
  OnboardingStageEvents,
  OnboardingStageProps,
  OnboardingSummaryItem,
} from '../../lib/onboarding'
import OnboardingSummary from './OnboardingSummary.vue'

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<OnboardingStageEvents>()
const store = useProjectStore()

const items = ref<OnboardingSummaryItem[]>([])
const loading = ref(true)
const readError = ref('')
const prompt = ref('')
const sending = ref(false)
const sendError = ref('')
const sent = ref(false)

const canContinue = computed(() => !loading.value && !readError.value && !sending.value)
watch([canContinue, loading, readError, sending, sent], () => {
  emit('state', {
    dirty: !!prompt.value.trim(),
    pending: loading.value || sending.value,
    canContinue: canContinue.value,
    error: sendError.value || readError.value || null,
  })
}, { immediate: true })

function row(
  id: OnboardingSummaryItem['id'],
  label: string,
  value: string,
  state: OnboardingSummaryItem['state'],
  href?: string,
): OnboardingSummaryItem {
  return { id, label, value, state, href }
}

const BACKUP_HREF = '/settings'
const SETTINGS_HREF = '/settings/workspaces'
const AUTOMATIONS_HREF = '/automations'

async function loadSummary(): Promise<void> {
  loading.value = true
  readError.value = ''
  // Each read is independent: an unavailable one becomes `unknown`, not a
  // guessed value, and never a "ready".
  const [workspaces, setup, backup, providers, handover, schedules] = await Promise.allSettled([
    api.get<{ workspaces?: Array<{ name: string; agent_fs_scope?: string | null; provider_overrides?: Record<string, Record<string, string>> }> }>('/api/workspaces'),
    api.get<{ workspace_root?: string; ready?: boolean }>('/api/setup-status'),
    api.get<{ context?: Record<string, unknown> }>('/api/local/backup/setup-prompt'),
    api.get<{ connections?: Record<string, { ok?: boolean; auth?: string }> }>('/api/settings/providers'),
    api.get<{ handover?: string; acting_available?: boolean }>(`/api/proposals/handover?workspace=${encodeURIComponent(props.context.workspace)}`),
    api.get<Array<{ schedule_id?: string; scope?: string; enabled?: boolean; daily_time_utc?: string; timezone_name?: string }>>('/api/schedules'),
  ])
  const value = <T,>(result: PromiseSettledResult<T>): T | null =>
    result.status === 'fulfilled' ? result.value : null

  const wsRow = value(workspaces)?.workspaces?.find(item => item.name === props.context.workspace)
  const setupRow = value(setup)
  const backupContext = (value(backup)?.context ?? {}) as Record<string, unknown>
  const connections = value(providers)?.connections ?? null
  const handoverRow = value(handover)
  const scheduleRows = value(schedules) ?? null

  const rows: OnboardingSummaryItem[] = []

  rows.push(
    !wsRow && !setupRow
      ? row('location', 'Folder and workspace', 'Could not read the folder', 'unknown', SETTINGS_HREF)
      : row(
          'location',
          'Folder and workspace',
          `${props.context.workspace}${props.context.provisioned ? ' · provisioned' : ' · not provisioned yet'}`,
          props.context.provisioned ? 'ready' : 'deferred',
          SETTINGS_HREF,
        ),
  )

  if (!value(backup)) {
    rows.push(row('backup', 'Git and backup', 'Could not read Git or backup state', 'unknown', BACKUP_HREF))
  } else if (backupContext.has_remote) {
    rows.push(row('backup', 'Git and backup', 'A remote is configured; off-machine backup is not verified', 'attention', BACKUP_HREF))
  } else if (backupContext.has_commits) {
    rows.push(row('backup', 'Git and backup', 'Local history exists; no off-machine backup', 'attention', BACKUP_HREF))
  } else if (backupContext.has_repo) {
    rows.push(row('backup', 'Git and backup', 'Repository initialized without commits', 'attention', BACKUP_HREF))
  } else {
    rows.push(row('backup', 'Git and backup', 'No local history or backup yet', 'deferred', BACKUP_HREF))
  }

  const readyConnections = connections
    ? Object.entries(connections).filter(([, status]) => status?.ok)
    : []
  if (!connections) {
    rows.push(row('runtime', 'Provider', 'Could not read provider status', 'unknown', SETTINGS_HREF))
  } else if (readyConnections.length) {
    rows.push(row('runtime', 'Provider', `${readyConnections.map(([id]) => id).join(', ')} ready on this computer`, 'ready', SETTINGS_HREF))
  } else {
    rows.push(row('runtime', 'Provider', 'No provider is ready yet', 'deferred', SETTINGS_HREF))
  }

  const overrides = wsRow?.provider_overrides ?? {}
  const chosenModel = Object.values(overrides).map(entry => entry?.model).find(Boolean)
  rows.push(
    chosenModel
      ? row('model', 'Model', `${chosenModel} selected; a successful reply is not yet verified`, 'attention', SETTINGS_HREF)
      : row('model', 'Model', 'No model chosen yet', 'deferred', SETTINGS_HREF),
  )

  const scope = wsRow?.agent_fs_scope
  rows.push(
    scope === 'workspace' || scope === 'machine'
      ? row('file_access', 'File access', scope === 'machine' ? 'Whole machine' : 'Workspace only', 'ready', SETTINGS_HREF)
      : row('file_access', 'File access', 'Scope was not saved', 'deferred', SETTINGS_HREF),
  )

  const explicitMode = Object.values(overrides).map(entry => entry?.permission_mode).find(Boolean)
  rows.push(
    explicitMode
      ? row('approvals', 'Approvals', `${explicitMode} (explicit)`, 'ready', SETTINGS_HREF)
      : row('approvals', 'Approvals', 'Engine default; no explicit mode', 'deferred', SETTINGS_HREF),
  )

  if (!handoverRow) {
    rows.push(row('memory', 'Memory handover', 'Could not read memory handover', 'unknown', SETTINGS_HREF))
  } else if (handoverRow.handover && handoverRow.handover !== 'off') {
    const acting = handoverRow.acting_available === false ? '; acceptance is not acting on this install' : ''
    rows.push(row('memory', 'Memory handover', `${handoverRow.handover}${acting}`, handoverRow.acting_available === false ? 'attention' : 'ready', SETTINGS_HREF))
  } else {
    rows.push(row('memory', 'Memory handover', 'Off; suggestions are predicted only', 'deferred', SETTINGS_HREF))
  }

  const routine = scheduleRows?.find(
    entry => entry.scope === 'system' && entry.schedule_id === `system-memory-curation@${props.context.workspace}`,
  )
  if (!scheduleRows) {
    rows.push(row('care', 'Care timing', 'Could not read care timing', 'unknown', AUTOMATIONS_HREF))
  } else if (!routine) {
    rows.push(row('care', 'Care timing', 'No care routine for this workspace', 'deferred', AUTOMATIONS_HREF))
  } else {
    const timing = `${routine.daily_time_utc || 'unknown time'} ${routine.timezone_name || ''}`.trim()
    rows.push(row('care', 'Care timing', routine.enabled ? `${timing} (on)` : `${timing} (paused)`, 'ready', AUTOMATIONS_HREF))
  }

  items.value = rows
  loading.value = false
}

async function submit(): Promise<void> {
  const text = prompt.value.trim()
  if (!text || sending.value) return
  sending.value = true
  sendError.value = ''
  try {
    const project = store.projects.find(item => item.workspace === props.context.workspace)
    if (!project) throw new Error('No project is available for a new chat.')
    const chat = await store.newChatInProject(project.project_id)
    if (!chat) throw new Error('Could not create a chat.')
    await store.sendMessage(chat.chat_id, text)
    prompt.value = ''
    sent.value = true
  } catch (reason) {
    sendError.value = apiErrorMessage(reason, 'Could not send the task.')
  } finally {
    sending.value = false
  }
}

onMounted(loadSummary)
defineExpose({ refresh: loadSummary })
</script>

<template>
  <div class="finish-stage">
    <OnboardingSummary :items="items" :loading="loading" />

    <section aria-labelledby="first-task-title">
      <h2 id="first-task-title">Start with a real task</h2>
      <p>Ask for something you actually want. It opens a new chat and sends your text as the first message.</p>
      <form @submit.prevent="submit">
        <label for="first-task">What should Ciaobot help with?</label>
        <textarea id="first-task" v-model="prompt" rows="3" :disabled="sending" />
        <button type="submit" :disabled="sending || !prompt.trim()">{{ sending ? 'Starting…' : 'Start task' }}</button>
      </form>
      <p v-if="sendError" role="alert">{{ sendError }}</p>
      <p v-if="sent" role="status">Task sent in a new chat.</p>
    </section>

    <section aria-labelledby="tools-title">
      <h2 id="tools-title">Tools to pair with Ciaobot</h2>
      <p>
        Optional, independent tools — nothing is installed or changed here.
        <a href="https://www.raffaelefarinaro.com/ciaobot/companions.html" target="_blank" rel="noopener noreferrer">See the guide</a>.
      </p>
      <ul>
        <li><strong>Dictation</strong> — FluidVoice, on-device speech to text (macOS).</li>
        <li><strong>Keeping the host awake</strong> — KeepingYouAwake (macOS); use the system power settings on Windows and Linux.</li>
        <li><strong>An agent browser</strong> — ego lite, for pages without an API.</li>
        <li><strong>Usage left</strong> — OpenUsage, to see a subscription limit coming.</li>
      </ul>
    </section>

    <section aria-labelledby="video-title">
      <h2 id="video-title">See chat, archive and memory</h2>
      <video
        controls
        preload="none"
        poster="https://www.raffaelefarinaro.com/ciaobot/assets/img/archive-poster.webp"
      >
        <source src="https://www.raffaelefarinaro.com/ciaobot/assets/img/archive.mp4" type="video/mp4">
        Your browser cannot play this video.
      </video>
      <p class="hint">Optional. The same flow is available from the first task above.</p>
    </section>

    <div class="actions">
      <button type="button" :disabled="props.busy || sending" @click="emit('back')">Back</button>
      <button type="button" class="primary" :disabled="!canContinue || props.busy" @click="emit('continue')">Finish setup</button>
    </div>
  </div>
</template>

<style scoped>
.finish-stage { display: grid; gap: 24px; }
h2 { font: 600 16px var(--font); margin: 0 0 8px; }
form { display: grid; gap: 8px; }
textarea { font: inherit; padding: 10px; color: var(--fg); background: var(--bg); border: 1px solid var(--border-strong); border-radius: var(--radius); }
button { min-height: 44px; padding: 8px 14px; color: var(--fg); background: transparent; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); font: inherit; cursor: pointer; }
button:disabled { opacity: .55; cursor: not-allowed; }
.primary { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.actions { display: flex; gap: 8px; justify-content: flex-end; }
video { width: 100%; max-width: 640px; border-radius: var(--radius); background: #000; }
ul { margin: 8px 0 0; padding-left: 18px; display: grid; gap: 4px; }
.hint { color: var(--fg2); }
</style>
