<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { api } from '../../lib/api'
import { apiErrorMessage } from '../../lib/errorMessage'
import type {
  OnboardingStageProps,
  OnboardingSummaryItem,
  StageState,
} from '../../lib/onboarding'
import OnboardingSummary from './OnboardingSummary.vue'
import OnboardingStack from './OnboardingStack.vue'
import OnboardingButton from './OnboardingButton.vue'

const props = defineProps<OnboardingStageProps>()
const emit = defineEmits<{ state: [state: StageState] }>()
const store = useProjectStore()

const items = ref<OnboardingSummaryItem[]>([])
// Only what needs a look is listed; a ready or deliberately deferred row is noise.
const attention = computed(() => items.value.filter(item => item.state === 'attention' || item.state === 'unknown'))
const loading = ref(true)
const readError = ref('')
const prompt = ref('')
const sending = ref(false)
const sendError = ref('')
const sent = ref(false)

const canContinue = computed(() => !loading.value && !readError.value && !sending.value)
watch([canContinue, loading, readError, sending, sent, prompt], () => {
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
const SETTINGS_HREF = '/settings/workspace'
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
    // The folder is only verified by the setup read; without it the state is
    // unknown even when the workspace registry answered.
    !setupRow
      ? row('location', 'Folder and workspace', 'Could not read the folder', 'unknown', SETTINGS_HREF)
      : row(
          'location',
          'Folder and workspace',
          `${props.context.workspace}${setupRow?.workspace_root ? ` · ${setupRow.workspace_root}` : ''}`,
          setupRow?.workspace_root ? 'ready' : 'deferred',
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

  // Without the workspace read, a missing value is unknown, not "not chosen".
  const overrides = wsRow?.provider_overrides ?? {}
  const chosenModel = Object.values(overrides).map(entry => entry?.model).find(Boolean)
  if (!wsRow) {
    rows.push(row('model', 'Model', 'Could not read the workspace settings', 'unknown', SETTINGS_HREF))
  } else {
    rows.push(
      chosenModel
        ? row('model', 'Model', `${chosenModel} selected; a successful reply is not yet verified`, 'attention', SETTINGS_HREF)
        : row('model', 'Model', 'No model chosen yet', 'deferred', SETTINGS_HREF),
    )
  }

  const scope = wsRow?.agent_fs_scope
  if (!wsRow) {
    rows.push(row('file_access', 'File access', 'Could not read the workspace settings', 'unknown', SETTINGS_HREF))
  } else {
    rows.push(
      scope === 'workspace' || scope === 'machine'
        ? row('file_access', 'File access', scope === 'machine' ? 'Whole machine' : 'Workspace only', 'ready', SETTINGS_HREF)
        : row('file_access', 'File access', 'Scope was not saved', 'deferred', SETTINGS_HREF),
    )
  }

  const explicitMode = Object.values(overrides).map(entry => entry?.permission_mode).find(Boolean)
  if (!wsRow) {
    rows.push(row('approvals', 'Approvals', 'Could not read the workspace settings', 'unknown', SETTINGS_HREF))
  } else {
    rows.push(
      explicitMode
        ? row('approvals', 'Approvals', `${explicitMode} (explicit)`, 'ready', SETTINGS_HREF)
        : row('approvals', 'Approvals', 'Engine default; no explicit mode', 'deferred', SETTINGS_HREF),
    )
  }

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
    // `sendMessage` reports a synchronous boolean and refuses without throwing
    // (offline, a blocked send, no bridge), so the text must survive a refusal.
    if (!store.sendMessage(chat.chat_id, text)) {
      throw new Error('The task could not be sent. Your text is still here.')
    }
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
  <OnboardingStack as="div" gap="2xl" class="finish-stage">
    <OnboardingStack as="section" aria-labelledby="ready-title">
      <h2 id="ready-title">You're set</h2>
      <p v-if="!loading && attention.length" class="hint">These could use a look before you rely on them.</p>
      <p v-else-if="!loading" class="hint">Everything checked looks ready.</p>
      <OnboardingSummary :items="attention" :loading="loading" />
    </OnboardingStack>

    <OnboardingStack as="section" aria-labelledby="first-task-title">
      <h2 id="first-task-title">Start with a real task</h2>
      <p class="hint">Ask for something you actually want. It opens a new chat and sends your text as the first message.</p>
      <form @submit.prevent="submit">
        <label for="first-task">What should Ciaobot help with?</label>
        <textarea id="first-task" v-model="prompt" rows="3" :disabled="sending" />
        <div><OnboardingButton type="submit" variant="primary" :disabled="sending || !prompt.trim()">{{ sending ? 'Starting…' : 'Start task' }}</OnboardingButton></div>
      </form>
      <p v-if="sendError" class="error" role="alert">{{ sendError }}</p>
      <p v-if="sent" role="status">Task sent in a new chat.</p>
    </OnboardingStack>

    <details class="tips">
      <summary>Tips</summary>
      <OnboardingStack gap="xl" class="tips-body">
        <OnboardingStack as="section" aria-labelledby="tools-title">
          <h3 id="tools-title">Tools to pair with Ciaobot</h3>
          <p class="hint">
            Optional, independent tools — nothing is installed or changed here.
            <a href="https://www.raffaelefarinaro.com/ciaobot/companions.html" target="_blank" rel="noopener noreferrer">See the guide</a>.
          </p>
          <ul>
            <li><strong>Dictation</strong> — FluidVoice, on-device speech to text (macOS).</li>
            <li><strong>Keeping the host awake</strong> — KeepingYouAwake (macOS); use the system power settings on Windows and Linux.</li>
            <li><strong>An agent browser</strong> — ego lite, for pages without an API.</li>
            <li><strong>Usage left</strong> — OpenUsage, to see a subscription limit coming.</li>
          </ul>
        </OnboardingStack>

        <OnboardingStack as="section" aria-labelledby="video-title">
          <h3 id="video-title">See chat, archive and memory</h3>
          <video
            controls
            preload="none"
            poster="https://www.raffaelefarinaro.com/ciaobot/assets/img/archive-poster.webp"
          >
            <source src="https://www.raffaelefarinaro.com/ciaobot/assets/img/archive.mp4" type="video/mp4">
            Your browser cannot play this video.
          </video>
        </OnboardingStack>
      </OnboardingStack>
    </details>
  </OnboardingStack>
</template>

<style scoped>
.hint { color: var(--fg2); line-height: 1.5; }
.error { color: var(--error); }
form { display: grid; gap: var(--space-2, 8px); }
label { font-weight: 600; }
textarea { font: inherit; padding: var(--space-3, 12px); color: var(--fg); background: var(--bg); border: 1px solid var(--border-strong); border-radius: var(--radius); }
textarea:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
summary { display: flex; align-items: center; gap: var(--space-2, 8px); min-height: var(--touch, 44px); font-weight: 600; cursor: pointer; list-style: none; }
summary::-webkit-details-marker { display: none; }
/* A chevron, so Tips reads as a disclosure and not a heading with nothing under it. */
summary::before { content: ''; width: 8px; height: 8px; border-right: 2px solid var(--fg2); border-bottom: 2px solid var(--fg2); transform: rotate(-45deg); transition: transform .15s; }
details[open] > summary::before { transform: rotate(45deg); }
.tips-body { padding-top: var(--space-3, 12px); }
h3 { font: 600 14px var(--font); }
video { width: 100%; max-width: 640px; border-radius: var(--radius); background: var(--bg); }
ul { margin: 0; padding-left: var(--space-5, 24px); display: grid; gap: var(--space-2, 8px); }
</style>
