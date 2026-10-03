<template>
  <div class="card">
    <div class="settings-card-header settings-card-header--split">
      <div>
        <p class="section-title">Memory backup</p>
        <p class="hint">{{ cadenceText }}</p>
      </div>
      <!-- The configured state's two controls. One primary (Back up now), one
           neutral: a routine, reversible switch never wears the accent. -->
      <div v-if="showConfigured" class="settings-card-header-actions">
        <button
          v-if="flagControl"
          type="button"
          class="btn-secondary btn-small"
          :disabled="flagPending"
          @click="toggleFlag"
        >{{ flagPending ? 'Working...' : flagControl.label }}</button>
        <button
          type="button"
          class="btn-primary btn-small"
          :disabled="runPending"
          @click="runNow"
        >{{ runPending ? 'Backing up...' : 'Back up now' }}</button>
      </div>
    </div>

    <!-- First load. A pending read is its own state, never an empty claim. -->
    <div v-if="!loaded" class="hint" role="status">Checking your backup&hellip;</div>

    <!-- First-load failure: an error and a retry, not an empty-state claim. The
         unconfigured setup actions live behind a successful read, because
         "not configured" and "could not ask" are different answers. -->
    <template v-else-if="loadError && !status">
      <p class="action-result action-result--error" role="alert">
        Could not read the backup status: {{ loadError }}
      </p>
      <div class="action-row settings-actions">
        <button type="button" class="btn-secondary btn-small" :disabled="loading" @click="load()">
          {{ loading ? 'Checking...' : 'Check again' }}
        </button>
      </div>
    </template>

    <template v-else>
      <!-- A failed refresh over rows that are already on screen keeps them, and
           says they are stale. Only the read failed; the last known answer is
           still the best one the user has. -->
      <p v-if="loadError" class="hint hint--warn" role="alert">
        Could not refresh: {{ loadError }}. Showing the last known status.
        <button type="button" class="set-link" :disabled="loading" @click="load()">Check again</button>
      </p>

      <!-- Not set up: say what is true now, and offer both ways forward. -->
      <div v-if="!showConfigured" class="backup-setup">
        <p class="backup-lede">
          Your memory is saved on this device. Connect a private GitHub repository to back it up
          automatically.
        </p>
        <p class="hint hint--compact">
          Ciaobot keeps this folder as a Git repository, and creates one if it is not already. Once
          it is connected to a private GitHub repository, it syncs on its own. Ciao can do the
          connection for you here, or you can copy the same instructions to any agent.
          <a
            class="set-link"
            href="https://www.raffaelefarinaro.com/ciaobot/memory.html#backup"
            target="_blank"
            rel="noopener noreferrer"
          >How backup works</a>
        </p>
        <div class="action-row settings-actions">
          <button
            type="button"
            class="btn-primary btn-small"
            :disabled="setupPending"
            @click="startSetup"
          >{{ setupPending ? 'Starting setup...' : 'Set up in Ciaobot' }}</button>
          <button
            type="button"
            class="btn-secondary btn-small"
            :disabled="copyPending"
            @click="copyPrompt"
          >{{ copyPending ? 'Copying...' : 'Copy setup prompt' }}</button>
        </div>
        <p v-if="setupChatId" class="action-result" role="status">
          Ciao is setting this up in a chat. This page updates on its own once the repository is
          connected.
          <button type="button" class="set-link" @click="openChat">Open the setup chat</button>
        </p>
        <p v-if="setupResult" class="action-result" role="status">{{ setupResult }}</p>
        <p v-if="error" class="action-result action-result--error" role="alert">{{ error }}</p>
      </div>

      <!-- The online copy has its own status and timestamp; local saves are not
           evidence that the online copy succeeded. -->
      <template v-else-if="status">
        <div class="backup-status">
          <p class="backup-state-line" role="status">
            <span class="backup-dot" :class="`backup-dot--${stateView.tone}`" aria-hidden="true" />
            {{ stateView.label }}
          </p>
          <p class="backup-detail">Last online backup: {{ lastSuccessText }}</p>
          <p v-if="status.state !== 'ready' && status.state !== 'paused'" class="backup-detail">
            {{ stateView.detail }}
          </p>
          <!-- A coverage gap is a permanent fact about a repository that also
               holds something else (application source, say), not something
               wrong with this run. It is a note under whatever the state says —
               and the state only turns red for a failure the owner can fix. -->
          <p v-if="scopeGap" class="backup-warning">
            {{ scopeGapText }}
          </p>
          <details class="backup-details">
            <summary>{{ status.state === 'needs_attention' ? 'Review details' : 'Backup details' }}</summary>
            <div class="backup-details-body">
              <p v-if="serverReason" class="backup-reason">{{ serverReason }}</p>
              <p><strong>Backed up:</strong> {{ status.scope }}</p>
              <p><strong>Repository:</strong>
                <a v-if="repoHref" :href="repoHref" target="_blank" rel="noopener noreferrer">{{ repoText }}</a>
                <code v-else>{{ repoText }}</code>
                <template v-if="status.branch"> &middot; {{ status.branch }}</template>
              </p>
              <button v-if="status.state === 'ready'" type="button" class="set-link set-link--quiet" :disabled="loading" @click="load()">
                {{ loading ? 'Checking...' : 'Check again' }}
              </button>
            </div>
          </details>
          <button v-if="status.state !== 'ready'" type="button" class="set-link set-link--quiet backup-recheck" :disabled="loading" @click="load()">
            {{ loading ? 'Checking...' : 'Check again' }}
          </button>
        </div>
        <p v-if="actionResult" class="action-result" role="status">{{ actionResult }}</p>
        <p v-if="error" class="action-result action-result--error" role="alert">{{ error }}</p>
      </template>
    </template>

  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../../lib/api'
import { writeClipboard } from '../../lib/codeCopy'
import { apiErrorMessage, errorMessage, errorPayload } from '../../lib/errorMessage'
import { formatRelative, formatTime } from '../../lib/time'

/** One row of `GET /api/local/backup` — the #689 status contract, unchanged. */
interface BackupStatus {
  state: string
  scope: string
  branch: string
  remote: string
  last_remote: string
  enabled: boolean
  interval_s: number
  last_attempt_at: string
  last_success_at: string
  last_success_commit: string
  pending_changes: number
  pending_commits: number
  /** Tracked paths the backup scope refuses to commit (#733). A count, so the
   *  note below never depends on the wording of `reason`. */
  coverage_gap: number
  reason: string
}

const emit = defineEmits<{ 'open-chat': [chatId: string] }>()

/**
 * While there is nothing to back up to, a guided setup runs in a chat and
 * takes minutes. A bounded re-read is what makes the section turn itself over
 * when that chat finishes, so the user does not have to reload a page they
 * were told would update. Bounded on purpose: it stops at the cap, the moment
 * the install is configured, and on unmount.
 */
const UNCONFIGURED_REFRESH_MS = 10_000
const UNCONFIGURED_REFRESH_LIMIT = 36

type Tone = 'ok' | 'warn' | 'error' | 'off' | 'busy'

/** The plain-language sentence under each state, kept free of Git vocabulary. */
const STATE_DETAIL: Record<string, { label: string; tone: Tone; detail: string }> = {
  ready: {
    label: 'Backed up',
    tone: 'ok',
    detail: 'Everything waiting to be backed up is on the online copy.',
  },
  running: {
    label: 'Backing up now',
    tone: 'busy',
    detail: 'Ciaobot is copying your memory to the repository right now.',
  },
  offline: {
    label: 'Offline',
    tone: 'warn',
    detail: 'Your memory is safe on this computer. The online copy catches up when this computer can reach the repository again.',
  },
  needs_attention: {
    label: 'Needs attention',
    tone: 'error',
    detail: 'Your memory is safe on this computer, but something is in the way of the online copy. The line below says what.',
  },
  paused: {
    label: 'Paused',
    tone: 'off',
    detail: 'No automatic backup is running. The copy on this computer is unaffected.',
  },
}

const PENDING_DETAIL =
  'Your memory is safe on this computer. The online copy catches up on the next backup.'

const status = ref<BackupStatus | null>(null)
const loaded = ref(false)
const loading = ref(false)
// A failed read and a failed action are different things: a refresh must not
// erase an unread setup failure, and an action failure must not blank the rows.
const loadError = ref('')
const error = ref('')
const actionResult = ref('')
// The setup and copy results belong to the unconfigured half of the card and
// are never shown beside the configured rows: "Started a setup chat" under a
// "Needs attention" status is a claim about a different moment.
const setupResult = ref('')
const setupPending = ref(false)
const copyPending = ref(false)
const runPending = ref(false)
const flagPending = ref(false)
const setupChatId = ref('')

let loadSeq = 0
let refreshTimer: ReturnType<typeof setTimeout> | null = null
let refreshesLeft = UNCONFIGURED_REFRESH_LIMIT

/** A status is the only thing that counts as "set up". */
const showConfigured = computed(() => !!status.value && status.value.state !== 'not_configured')

const stateView = computed(() => {
  const current = status.value
  if (!current) return { label: 'Unknown', tone: 'off' as Tone, detail: '' }
  if (current.state === 'pending') {
    return {
      label: current.pending_commits > 0 ? 'Waiting to upload' : 'Saving locally',
      tone: 'warn' as Tone,
      detail: PENDING_DETAIL,
    }
  }
  return (
    STATE_DETAIL[current.state] ?? {
      label: current.state,
      tone: 'off' as Tone,
      detail: 'Ciaobot is reporting a state this page does not know about.',
    }
  )
})

/**
 * The service's own words, kept because they name the specific thing that is
 * wrong. "up to date" is dropped: it is the same claim as the label, and two
 * lines saying it reads as two problems.
 */
const serverReason = computed(() => {
  const reason = status.value?.reason?.trim() || ''
  return reason === 'up to date' ? '' : reason
})

/**
 * The scope gap, straight off the status.
 *
 * This used to be recovered by matching the count out of the service's own
 * sentence in `reason`, so rewording that sentence silently dropped the note.
 * The service reports the number itself and owns the wording here; a gap is a
 * coverage fact about the repository, not a state, and it is shown under every
 * state rather than replacing one (#733).
 */
const scopeGap = computed(() => status.value?.coverage_gap || 0)

const scopeGapText = computed(() =>
  scopeGap.value === 1
    ? '1 tracked file is outside the backup scope and will not be included online.'
    : `${scopeGap.value} tracked files are outside the backup scope and will not be included online.`,
)

/** The live origin, falling back to where the last run actually pushed. */
const repoText = computed(() => status.value?.remote || status.value?.last_remote || 'Not recorded yet')

const lastSuccessText = computed(() => {
  const at = status.value?.last_success_at
  if (!at) {
    // Two different "never" answers. With a repository there is a next
    // automatic run to wait for; without one there is a setup to do, and
    // saying "the first backup runs soon" would promise a run that is gated.
    return repoText.value === 'Not recorded yet'
      ? 'None yet. The first backup runs once a repository is connected.'
      : 'None yet. The first backup has not reached the repository yet.'
  }
  const when = formatRelative(at)
  const exact = formatTime(at)
  return when && exact ? `${when} (${exact})` : when || exact
})

/**
 * A link only for a real web URL. The service already strips credentials from
 * the remote it reports, but an href is still an instruction to the browser,
 * so a scheme that is not http(s) is rendered as text instead.
 */
const repoHref = computed(() => {
  const url = repoText.value
  return /^https?:\/\/\S+$/i.test(url) ? url : ''
})

const cadenceText = computed(() => {
  const seconds = status.value?.interval_s || 0
  const minutes = Math.round(seconds / 60)
  if (!minutes) return 'Automatic backups run while Ciaobot is running on this computer.'
  return `Automatic backup every ${minutes} minute${minutes === 1 ? '' : 's'} while Ciaobot is running.`
})

const flagControl = computed(() => {
  const current = status.value
  if (!current) return null
  if (!current.enabled) return { label: 'Turn on', body: { enabled: true } }
  if (current.state === 'paused') return { label: 'Resume', body: { paused: false } }
  return { label: 'Pause', body: { paused: true } }
})

/** The body of a failed action, when it is a status object in its own right. */
function statusFromError(err: unknown): BackupStatus | null {
  const payload = errorPayload(err)
  return payload && typeof payload.state === 'string' ? (payload as unknown as BackupStatus) : null
}

async function load(): Promise<void> {
  const seq = ++loadSeq
  loadError.value = ''
  loading.value = true
  try {
    const next = await api.get<BackupStatus>('/api/local/backup')
    if (seq !== loadSeq) return
    status.value = next
    scheduleUnconfiguredRefresh()
  } catch (err) {
    if (seq !== loadSeq) return
    // The previous rows stay: a failed refresh is not a cleared list.
    loadError.value = errorMessage(err, 'the request failed')
  } finally {
    if (seq === loadSeq) {
      loaded.value = true
      loading.value = false
    }
  }
}

/**
 * Re-read only while there is nothing to back up to, and only a bounded number
 * of times. A guided setup lands in a chat, so without this the section would
 * keep claiming "not set up" long after the user connected a repository.
 */
function scheduleUnconfiguredRefresh(): void {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = null
  if (showConfigured.value || refreshesLeft <= 0) return
  refreshTimer = setTimeout(() => {
    refreshesLeft -= 1
    void load()
  }, UNCONFIGURED_REFRESH_MS)
}

function openChat(): void {
  if (setupChatId.value) emit('open-chat', setupChatId.value)
}

async function startSetup(): Promise<void> {
  if (setupPending.value) return
  setupPending.value = true
  error.value = ''
  setupResult.value = ''
  try {
    const res = await api.post<{ ok: boolean; chat_id: string; reused: boolean }>(
      '/api/local/backup/setup-chat',
    )
    setupChatId.value = res?.chat_id || ''
    setupResult.value = res?.reused
      ? 'Reopened the setup chat that was already running.'
      : 'Started a setup chat.'
    // The answer to "is there a backup now" is the status read, never the
    // dispatch, so read it rather than assuming.
    await load()
  } catch (err) {
    error.value = apiErrorMessage(err, 'Could not start the setup chat.')
  } finally {
    setupPending.value = false
  }
}

async function copyPrompt(): Promise<void> {
  if (copyPending.value) return
  copyPending.value = true
  error.value = ''
  setupResult.value = ''
  try {
    const res = await api.get<{ prompt: string }>('/api/local/backup/setup-prompt')
    const prompt = res?.prompt || ''
    if (!prompt) throw new Error('the setup prompt came back empty')
    if (!(await writeClipboard(prompt))) throw new Error('the browser blocked the copy')
    setupResult.value = 'Setup prompt copied. Paste it into any agent to have it set up.'
  } catch (err) {
    error.value = errorMessage(err, 'Could not copy the setup prompt.')
  } finally {
    copyPending.value = false
  }
}

async function runNow(): Promise<void> {
  if (runPending.value) return
  runPending.value = true
  error.value = ''
  actionResult.value = ''
  try {
    const res = await api.post<BackupStatus>('/api/local/backup/run')
    status.value = res
    actionResult.value = res?.state === 'ready'
      ? 'Backup complete. The copy is on the repository.'
      : 'Backup finished. The status below says what is still outstanding.'
  } catch (err) {
    // A run that could not do its job answers 400 with the same status object,
    // so the state and its reason are the response rather than a status-line
    // guess — and the one thing worth saying out loud is that the local copy
    // is untouched, which is what "offline" and "needs attention" both mean.
    const failed = statusFromError(err)
    if (failed) status.value = failed
    error.value = failed
      ? `The backup could not run. Your memory is still safe on this computer. ${failed.reason || stateView.value.detail}`
      : apiErrorMessage(err, 'The backup could not run. Your memory is still safe on this computer.')
  } finally {
    runPending.value = false
  }
}

async function toggleFlag(): Promise<void> {
  const control = flagControl.value
  if (!control || flagPending.value) return
  flagPending.value = true
  error.value = ''
  actionResult.value = ''
  try {
    const res = await api.patch<BackupStatus>('/api/local/backup', control.body)
    status.value = res
    // Only pausing sets `paused`; "Turn on" sends { enabled: true } and "Resume"
    // sends { paused: false }, so anything other than an explicit pause leaves
    // the backups running.
    actionResult.value = control.body.paused === true
      ? 'Automatic backups are off. The copy on this computer is unaffected.'
      : 'Automatic backups are running again.'
  } catch (err) {
    error.value = apiErrorMessage(err, 'Could not change the backup setting.')
  } finally {
    flagPending.value = false
  }
}

onMounted(load)
onUnmounted(() => {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = null
})
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
/* The error variant of the shared .action-result lives in SettingsView's own
   scoped block, which cannot reach a child's markup, so it is restated here. */
.action-result--error {
  color: var(--error);
}
.backup-lede {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-base, 14px);
  line-height: 1.6;
  max-width: 68ch;
}
.backup-setup {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}
.backup-status {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}
.backup-status p { margin: 0; }
.backup-state-line {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--fg);
  font-weight: 600;
}
/* A state is words first; the dot is the second signal, never the only one. */
.backup-dot {
  width: 8px;
  height: 8px;
  flex: none;
  border-radius: 50%;
  background: var(--fg3);
}
.backup-dot--ok { background: var(--success); }
.backup-dot--warn { background: var(--warning); }
.backup-dot--error { background: var(--error); }
.backup-dot--busy { background: var(--accent2); }
.backup-detail {
  color: var(--fg3);
  font-size: var(--text-sm);
  line-height: 1.5;
  max-width: 68ch;
}
.backup-reason {
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.45;
  overflow-wrap: anywhere;
}
/* A coverage gap is a note, not an alarm: the backup itself did its work, so
   this stays in the ordinary foreground and the state line above keeps whatever
   tone that state earns. Only a real `needs_attention` is ever red. */
.backup-warning { color: var(--fg); font-size: var(--text-sm); line-height: 1.5; }
/* A flex child stretches to the column's width, and a button centres its own
   label, so the re-read would float in the middle of the row. */
.backup-recheck {
  align-self: flex-start;
  text-align: left;
}
/* An inline link inside a row of prose is a text-sized target. The repository
   link is the one control on this card that opens something off-app, so it
   gets the same 44px as every other tap target on a coarse pointer. */
.backup-details a {
  display: inline-block;
  min-height: var(--touch);
  overflow-wrap: anywhere;
}
@media (pointer: fine) {
  /* Nothing to hit on a mouse; keep the row's line box as it was. */
  .backup-details a { min-height: 0; }
}
.backup-details > summary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: var(--touch);
  color: var(--fg2);
  font-size: var(--text-sm);
  cursor: pointer;
  list-style: none;
}
.backup-details > summary::-webkit-details-marker { display: none; }
.backup-details > summary::before {
  content: '';
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  margin: 0 2px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  transform: rotate(-45deg);
  transition: transform 120ms var(--ease);
}
.backup-details[open] > summary::before { transform: rotate(45deg); }
.backup-details-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-3);
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.6;
  max-width: 72ch;
}
.backup-details-body p { margin: 0; overflow-wrap: anywhere; }
@media (pointer: coarse), (max-width: 600px) {
  .settings-card-header-actions button { min-height: var(--touch); }
}
</style>
