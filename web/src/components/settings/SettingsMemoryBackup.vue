<template>
  <div class="card">
    <div class="settings-card-header settings-card-header--split">
      <div>
        <p class="section-title">Memory backup</p>
        <p class="hint">
          A private copy of your memory, kept somewhere other than this computer. It runs by
          itself &mdash; you do not have to remember to do it.
        </p>
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
          Ciao can do the setup for you here, or you can copy the same instructions to any agent.
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
        <p class="hint hint--compact">
          <button type="button" class="set-link" @click="openGuide">How memory backup works</button>
        </p>
        <p v-if="setupChatId" class="action-result" role="status">
          Ciao is setting this up in a chat. This page updates on its own once the repository is
          connected.
          <button type="button" class="set-link" @click="openChat">Open the setup chat</button>
        </p>
        <p v-if="setupResult" class="action-result" role="status">{{ setupResult }}</p>
        <p v-if="error" class="action-result action-result--error" role="alert">{{ error }}</p>
      </div>

      <!-- Set up: what is true about the two copies, which are not the same
           thing, and what to do about the state the backup is in. -->
      <template v-else-if="status">
        <div class="set-list">
          <div class="set-subrow">
            <span class="set-subrow-label">Online backup</span>
            <div class="set-subrow-control backup-state">
              <span class="backup-state-line">
                <span class="backup-dot" :class="`backup-dot--${stateView.tone}`" aria-hidden="true" />
                {{ stateView.label }}
              </span>
              <span class="backup-detail">{{ stateView.detail }}</span>
              <span v-if="serverReason" class="backup-reason">{{ serverReason }}</span>
              <!-- A status is only as old as the read behind it, and a state
                   like Offline can clear on its own. Re-reading is a quiet
                   text action, never a second button in the header. -->
              <button
                type="button"
                class="set-link set-link--quiet backup-recheck"
                :disabled="loading"
                @click="load()"
              >{{ loading ? 'Checking...' : 'Check again' }}</button>
            </div>
          </div>
          <div class="set-subrow">
            <span class="set-subrow-label">On this computer</span>
            <div class="set-subrow-control">
              <span class="backup-detail">
                Saved here every time, independently of the online copy.
              </span>
            </div>
          </div>
          <div class="set-subrow">
            <span class="set-subrow-label">Last online backup</span>
            <div class="set-subrow-control">
              <span class="backup-detail">{{ lastSuccessText }}</span>
            </div>
          </div>
          <div class="set-subrow">
            <span class="set-subrow-label">What is backed up</span>
            <div class="set-subrow-control">
              <span class="backup-detail">{{ status.scope }}</span>
            </div>
          </div>
          <div class="set-subrow">
            <span class="set-subrow-label">Repository</span>
            <div class="set-subrow-control">
              <span class="backup-detail">
                <a
                  v-if="repoHref"
                  class="backup-repo-link"
                  :href="repoHref"
                  target="_blank"
                  rel="noopener noreferrer"
                >{{ repoText }}</a>
                <code v-else>{{ repoText }}</code>
                <template v-if="status.branch"> &middot; {{ status.branch }}</template>
              </span>
            </div>
          </div>
          <div class="set-subrow">
            <span class="set-subrow-label">Automatic</span>
            <div class="set-subrow-control">
              <span class="backup-detail">{{ cadenceText }}</span>
            </div>
          </div>
        </div>
        <p v-if="actionResult" class="action-result" role="status">{{ actionResult }}</p>
        <p v-if="error" class="action-result action-result--error" role="alert">{{ error }}</p>
      </template>
    </template>

    <!-- The guide. A native disclosure, so it is keyboard-operable and
         announced without a line of script; the link above opens it and moves
         focus with it. -->
    <details ref="guideEl" class="backup-guide">
      <summary>How memory backup works</summary>
      <div ref="guideBodyEl" class="backup-guide-body" tabindex="-1">
        <h3>What gets backed up</h3>
        <p>
          Your memory vault &mdash; the notes Ciaobot remembers, the proposals waiting for you and
          the receipts of what it did &mdash; plus the skills, subagents and commands you wrote, the
          workspace guide, and workspaces you archived. Never your passwords, keys or other
          secrets, and never the Ciaobot program itself.
        </p>

        <h3>Two ways to set it up</h3>
        <p>
          <strong>Set up in Ciaobot</strong> opens a chat here and starts the work for you. It is
          the same prompt either way, so a chat you already started is re-entered rather than
          duplicated.
        </p>
        <p>
          <strong>Copy setup prompt</strong> puts those instructions on your clipboard, ready to
          paste into any agent. Use it when the agent you trust runs on another machine.
        </p>

        <h3>Saved here, and backed up there</h3>
        <p>
          Two different things, on purpose. Ciaobot always saves to this computer first, and that
          is what it reads from &mdash; nothing about your memory changes when you connect a
          repository. The online copy is the second copy: it is what you have if this computer is
          lost, replaced or reinstalled, and it is the one that can fall behind. When the two
          disagree, the copy on this computer is the one Ciaobot uses.
        </p>

        <h3>It needs the computer Ciaobot runs on</h3>
        <p>
          Backups are pushed by the machine Ciaobot is running on. Open Ciaobot on your phone or
          another computer and you will see the same status, but the backup happens on the host.
        </p>

        <h3>Pause it, or back up now</h3>
        <p>
          Pause stops the automatic backups and survives a restart; nothing is lost, the copy on
          this computer is untouched. <strong>Back up now</strong> does one backup immediately
          instead of waiting for the next automatic one. Never back up while a chat is mid-run:
          wait for it to finish, or use pause.
        </p>

        <h3>Finding the repository and its history</h3>
        <p>
          The <strong>Repository</strong> row above is where copies are sent &mdash; open it to see
          every backup as it lands, and the history of them. <strong>Last online backup</strong>
          records the copy that is known to be there; a newer local change is not a failure, it
          simply has not been uploaded yet.
        </p>
      </div>
    </details>
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
const guideEl = ref<HTMLDetailsElement | null>(null)
const guideBodyEl = ref<HTMLElement | null>(null)

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

function openGuide(): void {
  const el = guideEl.value
  if (!el) return
  el.open = true
  // Focus follows the reveal, or a keyboard user is left on a control whose
  // effect happened somewhere below it.
  guideBodyEl.value?.focus({ preventScroll: false })
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
.backup-state {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
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
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs);
  line-height: 1.45;
  overflow-wrap: anywhere;
}
/* A flex child stretches to the column's width, and a button centres its own
   label, so the re-read would float in the middle of the row. */
.backup-recheck {
  align-self: flex-start;
  text-align: left;
}
/* An inline link inside a row of prose is a text-sized target. The repository
   link is the one control on this card that opens something off-app, so it
   gets the same 44px as every other tap target on a coarse pointer. */
.backup-repo-link {
  display: inline-block;
  min-height: var(--touch);
  overflow-wrap: anywhere;
}
@media (pointer: fine) {
  /* Nothing to hit on a mouse; keep the row's line box as it was. */
  .backup-repo-link { min-height: 0; }
}
.backup-guide {
  border-top: 1px solid var(--border);
  padding-top: var(--space-2);
}
.backup-guide > summary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 32px;
  color: var(--accent);
  font-size: var(--text-sm);
  cursor: pointer;
  list-style: none;
}
.backup-guide > summary::-webkit-details-marker { display: none; }
.backup-guide > summary::before {
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
.backup-guide[open] > summary::before { transform: rotate(45deg); }
.backup-guide-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-3);
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.6;
  max-width: 72ch;
}
.backup-guide-body:focus { outline: none; }
.backup-guide-body h3 {
  margin: var(--space-2) 0 0;
  color: var(--fg);
  font-size: var(--text-sm);
  font-weight: 600;
}
.backup-guide-body h3:first-child { margin-top: 0; }
.backup-guide-body p { margin: 0; }
@media (pointer: coarse) {
  .backup-guide > summary { min-height: var(--touch); }
}
</style>
