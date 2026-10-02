<template>
  <div class="card">
    <div class="settings-card-header">
      <h2 class="section-title">Start at sign-in</h2>
      <!-- Three separate things, kept separate: what this control changes (the
           next sign-in), what it never touches (the engine running now), and
           the optional app window that SettingsAppInstall owns. -->
      <p class="hint">
        Whether the Ciaobot engine service on this host is registered to start by itself when you
        sign in to this computer. That is the <em>next</em> sign-in: changing it here never starts or
        stops the engine you are talking to now. Installing Ciaobot as an app is a separate,
        optional thing &mdash; a window onto this same address, explained in
        <strong>Use Ciaobot as an app</strong> below.
      </p>
    </div>

    <!-- First load. A pending read is its own state, never an empty claim. -->
    <p v-if="!loaded" class="hint" role="status">Checking this host&hellip;</p>

    <!-- First-load failure: the error and a retry. "Could not ask the machine"
         and "the machine says no" are different answers, so neither is ever
         derived from the other. -->
    <template v-else-if="loadError && !status">
      <p class="action-result action-result--error" role="alert">
        Could not read this host&rsquo;s sign-in state: {{ loadError }}
      </p>
      <div class="action-row settings-actions">
        <button type="button" class="btn-secondary btn-small" :disabled="loading" @click="load()">
          {{ loading ? 'Checking…' : 'Check again' }}
        </button>
      </div>
    </template>

    <template v-else-if="status">
      <!-- A failed refresh over a row that is already on screen keeps it, and
           says it is the last known one: only the read failed. -->
      <p v-if="loadError" class="hint hint--warn" role="alert">
        Could not refresh: {{ loadError }}. Showing the last known state.
        <button type="button" class="set-link" :disabled="loading" @click="load()">
          {{ loading ? 'Checking…' : 'Check again' }}
        </button>
      </p>

      <div class="login-rows">
        <div class="login-row">
          <span class="login-key">This host</span>
          <span class="login-value">
            <!-- A state is a dot plus words. The words carry the position and
                 the colour only repeats it, so the row is readable in either
                 theme and without colour vision. -->
            <span class="login-state">
              <span class="login-dot" :class="`login-dot--${stateView.tone}`" aria-hidden="true" />
              {{ stateView.label }}
            </span>
            <span v-if="stateView.detail" class="login-detail">{{ stateView.detail }}</span>
          </span>
          <!-- One control, and only where the machine both named a position and
               licensed the write. `can_change` is that licence; the known
               position is what makes the body honest, since the PATCH may only
               be `{"enabled": bool}`. Everywhere else the row carries the
               reason instead: a disabled switch would be a dead control. -->
          <span v-if="showAction" class="login-end">
            <button
              type="button"
              class="btn-small"
              :class="status.enabled ? 'btn-secondary' : 'btn-primary'"
              :disabled="pending"
              :aria-busy="pending ? 'true' : undefined"
              :aria-label="actionLabel"
              @click="toggle"
            >{{ pending ? 'Working…' : actionLabel }}</button>
          </span>
        </div>
      </div>

      <!-- A command to run by hand, offered as text and never run from here:
           this route does not create, register or repoint a service. -->
      <div v-if="setupCommand" class="login-setup">
        <p class="login-detail">{{ setupLead }}</p>
        <div class="login-command">
          <code class="login-command-text">{{ setupCommand }}</code>
          <button type="button" class="btn-secondary btn-small" @click="copyCommand">
            {{ copiedCommand ? 'Copied' : 'Copy' }}
          </button>
        </div>
      </div>

      <p v-if="outcome" class="action-result" role="status">{{ outcome }}</p>
      <!-- A refusal (409) or an unconfirmed change (503) is reported beside the
           state the machine answered with, and the retry is a re-read: what is
           true now is a question, and repeating the write would not answer it. -->
      <p v-if="actionError" class="action-result action-result--error" role="alert">
        {{ actionError }}
        <button type="button" class="set-link set-link--quiet" :disabled="loading" @click="load()">
          {{ loading ? 'Checking…' : 'Check again' }}
        </button>
      </p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../../lib/api'
import { COPY_FEEDBACK_MS, writeClipboard } from '../../lib/codeCopy'
import { apiErrorMessage, errorMessage, errorPayload } from '../../lib/errorMessage'

const LOGIN_ROUTE = '/api/service/login'

/** `GET`/`PATCH /api/service/login` (#913), mirrored field for field. */
interface LoginStatus {
  platform: string
  supported: boolean
  /** Tri-state: `null` means the machine did not say, and is never a position. */
  installed: boolean | null
  enabled: boolean | null
  /** The only field that licenses a write. */
  can_change: boolean
  reason: string
  setup_command: string | null
}

type Tone = 'ok' | 'warn' | 'off'

const status = ref<LoginStatus | null>(null)
const loaded = ref(false)
const loading = ref(false)
// A failed read and a failed write are different things: a refresh must not
// erase an unread refusal, and a refusal must not blank the row.
const loadError = ref('')
const actionError = ref('')
const outcome = ref('')
const pending = ref(false)
const copiedCommand = ref(false)
let revertTimer: ReturnType<typeof setTimeout> | null = null

/**
 * One boolean that may also be "the machine did not say", or undefined for
 * anything this panel does not read as one.
 */
function triState(value: unknown): boolean | null | undefined {
  if (value === true || value === false) return value
  if (value === null) return null
  return undefined
}

/**
 * The payload read through a narrow shape rather than trusted.
 *
 * `null`/`null` is a real answer here and passes. A body missing a field, or
 * carrying one this page cannot read, is not an answer at all: it is reported
 * as a failed read with a retry rather than rendered as a position, because a
 * partial body is exactly how "the machine did not say" would arrive by
 * accident.
 */
function readStatus(value: unknown): LoginStatus | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  const installed = triState(raw.installed)
  const enabled = triState(raw.enabled)
  if (typeof raw.supported !== 'boolean' || typeof raw.can_change !== 'boolean') return null
  if (installed === undefined || enabled === undefined) return null
  return {
    platform: typeof raw.platform === 'string' ? raw.platform : '',
    supported: raw.supported,
    installed,
    enabled,
    can_change: raw.can_change,
    reason: typeof raw.reason === 'string' ? raw.reason : '',
    setup_command: typeof raw.setup_command === 'string' && raw.setup_command ? raw.setup_command : null,
  }
}

/**
 * The row's words.
 *
 * A proven position gets no reason line: the route's own sentence for it says
 * the same thing as the label, and two lines making one claim read as two
 * problems. Every branch where there is no position carries the reason instead,
 * because that is the only thing there is to say.
 */
const stateView = computed<{ label: string; tone: Tone; detail: string }>(() => {
  const current = status.value
  if (!current) return { label: 'Unknown', tone: 'off', detail: '' }
  const reason = current.reason.trim()
  if (current.installed === false) {
    return {
      label: 'Not installed for this user',
      tone: 'warn',
      detail: reason || 'There is no engine service on this host yet, so there is nothing to start at sign-in.',
    }
  }
  if (!current.supported) {
    return {
      label: 'Not read on this platform',
      tone: 'warn',
      detail: reason || 'This platform has no service backend Ciaobot can read or change.',
    }
  }
  if (current.enabled === true) return { label: 'Starts when I sign in', tone: 'ok', detail: '' }
  if (current.enabled === false) return { label: 'Does not start when I sign in', tone: 'off', detail: '' }
  // Tri-state, and this is the branch that matters: `null` is what the machine
  // did not say, so it reads as Unknown with the reason. It is never drawn as a
  // position in either direction.
  return { label: 'Unknown', tone: 'warn', detail: reason }
})

/** The write needs both a licence and a position to invert. */
const showAction = computed(
  () => !!status.value && status.value.can_change === true && status.value.enabled !== null,
)

const actionLabel = computed(() => (
  status.value?.enabled ? 'Disable at sign-in' : 'Enable at sign-in'
))

const setupCommand = computed(() => status.value?.setup_command || '')

const setupLead = computed(() => (
  status.value?.installed === false
    ? 'Set the service up by running this on this computer:'
    : 'To look at the registered task yourself, run this on this computer:'
))

async function load(): Promise<void> {
  loadError.value = ''
  loading.value = true
  try {
    const next = readStatus(await api.get<LoginStatus>(LOGIN_ROUTE))
    if (!next) throw new Error('the route answered with a state this page cannot read')
    status.value = next
  } catch (err) {
    // A failed read keeps the last known state: it is still the best answer
    // the user has, and clearing it would read as "nothing to start".
    loadError.value = errorMessage(err, 'the request failed')
  } finally {
    loaded.value = true
    loading.value = false
  }
}

async function toggle(): Promise<void> {
  const current = status.value
  if (!current || pending.value || !showAction.value) return
  pending.value = true
  actionError.value = ''
  outcome.value = ''
  try {
    // The one key this route accepts, and nothing else: no workspace, no
    // platform, no "restart now".
    const body = await api.patch<LoginStatus>(LOGIN_ROUTE, { enabled: !current.enabled })
    // The 200 body is the machine's re-read, not the request echoed, and it is
    // what the row becomes. Nothing is flipped locally: the position on screen
    // is only ever one the engine read back.
    const next = readStatus(body)
    if (!next) throw new Error('the route answered with a state this page cannot read')
    status.value = next
    outcome.value = next.enabled === true
      ? 'The engine service is now set to start when you sign in.'
      : next.enabled === false
        ? 'The engine service is now set not to start when you sign in.'
        : 'The engine service was changed, but this host no longer reports a sign-in state.'
  } catch (err) {
    // A 409/503 body carries the freshest status beside the reason. That status
    // is the machine's own answer and replaces what is on screen, which is how
    // the row keeps the position that is true instead of the one that was
    // asked for.
    const fresh = readStatus(errorPayload(err))
    if (fresh) status.value = fresh
    actionError.value = apiErrorMessage(err, 'The change did not happen.')
  } finally {
    pending.value = false
  }
}

async function copyCommand(): Promise<void> {
  const command = setupCommand.value
  if (!command) return
  if (!(await writeClipboard(command))) {
    actionError.value = 'The browser blocked the copy. Select the command above and copy it by hand.'
    return
  }
  copiedCommand.value = true
  if (revertTimer) clearTimeout(revertTimer)
  revertTimer = setTimeout(() => {
    revertTimer = null
    copiedCommand.value = false
  }, COPY_FEEDBACK_MS)
}

onMounted(load)
onUnmounted(() => {
  if (revertTimer) clearTimeout(revertTimer)
  revertTimer = null
})
</script>

<!-- The card scaffolding (`.card`, `.section-title`, `.settings-card-header`,
     `.hint`, `.action-row`, `.action-result`, `.set-link`) lives in the sheet
     SettingsView also loads, which is what keeps this panel looking like the
     rest of Settings. -->
<style scoped src="./settingsPanels.css"></style>

<style scoped>
/* The error variant of the shared .action-result lives in SettingsView's own
   scoped block, which cannot reach a child's markup, so it is restated here. */
.action-result--error {
  color: var(--error);
}
/* The key/value status row, the same shape the notifications and install cards
   use for their one fact about this window. */
.login-rows { border-top: 1px solid var(--border); }
.login-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-4);
  min-height: var(--touch);
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}
.login-key {
  width: 116px;
  flex: none;
  color: var(--fg3);
  line-height: 1.5;
}
.login-value {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.login-state {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--fg);
  font-weight: 600;
  line-height: 1.5;
}
.login-dot {
  width: 7px;
  height: 7px;
  flex: none;
  border-radius: 50%;
  background: var(--fg3);
}
.login-dot--ok { background: var(--success); }
.login-dot--warn { background: var(--warning); }
.login-detail {
  color: var(--fg3);
  line-height: 1.5;
  max-width: 72ch;
  overflow-wrap: anywhere;
}
.login-end {
  flex: none;
  margin-left: auto;
}
/* A routine, reversible switch is never the primary action on the card. Its 44px
   on a coarse pointer is the shared settings button rule, not a local one: this
   panel's buttons live inside `.settings-main`, which already sizes every
   settings button to `--touch` there and to 34px on a fine pointer. */
/* The setup command: a monospace block to read, with the copy control beside
   it rather than inside it, so the text is selectable either way. */
.login-setup {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-top: var(--space-3);
}
.login-command {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.login-command-text {
  flex: 1 1 16rem;
  min-width: 0;
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  overflow-wrap: anywhere;
}
/* A pane can be narrow while the window is wide (an expanded sidebar, page
   zoom), so this follows the pane the way the shared settings sheet does
   rather than the viewport. The action then goes full width under the row it
   acts on, which is both the only place a 44px target fits at this width and
   the only order that reads as state-then-choice. */
@container (max-width: 720px) {
  .login-row { flex-wrap: wrap; gap: var(--space-2) var(--space-4); }
  .login-key { width: auto; }
  .login-value { flex-basis: 100%; order: 3; }
  .login-end { flex: 1 1 100%; margin-left: 0; order: 4; }
  .login-end > button { width: 100%; }
}
</style>
