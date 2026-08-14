<template>
  <div class="login-page">
    <div class="login-shell">
      <div class="login-shell-bar">
        <span class="dot dot--r"></span>
        <span class="dot dot--y"></span>
        <span class="dot dot--g"></span>
        <span class="login-shell-title">
          {{ isRestarting ? 'ciaobot@local · restarting' : (isBootstrap ? 'ciaobot@local · first-run setup' : 'ciaobot@local · session') }}
        </span>
      </div>

      <!-- RESTARTING SCREEN -->
      <div v-if="isRestarting" class="login-body restarting-body">
        <p class="line line--banner">
          <span class="wordmark wordmark--md">restarting</span>
          <span class="banner-meta">// ciaobot is loading config ...</span>
        </p>
        <p class="line line--sys">
          Ciaobot is finishing setup. If you started it with ciao run, keep that terminal open until it says setup is complete and Ciaobot is moving to the background service. Then close the terminal and open Ciaobot Server.app.
        </p>
        <p class="line line--sys">
          When it comes back, log in with the password you just set.
        </p>
        <div class="spinner-container">
          <span class="caret"></span>
        </div>
      </div>

      <!-- SETUP WIZARD SCREEN -->
      <form v-else-if="isBootstrap" class="login-body setup-wizard" @submit.prevent="doFinish">
        <p class="line line--banner">
          <span class="wordmark wordmark--md">ciaobot setup</span>
          <span class="banner-meta">// tour + local setup</span>
        </p>

        <section class="setup-run-note" aria-label="First launch instructions">
          <span class="run-note-kicker">First launch from Terminal</span>
          <p>
            Keep the terminal running ciao run open while you finish this setup. When setup completes, Ciaobot moves to the background service, then you can close the terminal and open Ciaobot Server.app.
          </p>
        </section>

        <section class="setup-tour" aria-label="Ciaobot setup tour">
          <p class="tour-title">Your coding-agent subscription, with a real interface and memory.</p>
          <ul class="tour-list">
            <li>
              <strong>Bring your own backend.</strong>
              <span>Use Claude Code, OpenAI Codex, or opencode — which reaches everything else, including local models.</span>
            </li>
            <li>
              <strong>Split your life into workspaces.</strong>
              <span>Keep personal, work, clients, and long-running areas separate.</span>
            </li>
            <li>
              <strong>Work by project.</strong>
              <span>Project files become durable context, so the assistant does not rediscover the same facts every turn.</span>
            </li>
            <li>
              <strong>Schedule routines.</strong>
              <span>Run workspace-specific chats when you want: reviews, briefs, checks, and maintenance.</span>
            </li>
            <li>
              <strong>Archive into a second brain.</strong>
              <span>Archived chats produce session insights, trajectories, and memory proposals for review.</span>
            </li>
            <li>
              <strong>Files, with history.</strong>
              <span>Create, preview, edit, and restore workspace files right from the UI.</span>
            </li>
          </ul>
        </section>

        <div class="form-group">
          <label for="setup-workspace">Workspace Folder</label>
          <div class="input-row">
            <input
              id="setup-workspace"
              v-model="workspace"
              type="text"
              class="form-input"
              placeholder="~/ciaobot"
              required
              :disabled="loading"
            />
            <button
              id="setup-workspace-browse"
              type="button"
              class="btn-small"
              :disabled="loading"
              @click="openPicker()"
            >Browse…</button>
          </div>
          <span class="hint">Type a path, or press <strong>Browse…</strong> to pick an existing folder or
            create a new one. Either a brand-new folder or the notes folder you already have works —
            Ciaobot detects what's inside and adjusts automatically: an empty folder gets a fresh
            second brain; existing notes are adapted in place into its structure.</span>
        </div>

        <div class="form-group">
          <label>Workspaces Found</label>
          <div v-if="folderInspecting" class="hint">
            Checking the folder for existing workspaces…
          </div>
          <div v-else-if="detectedWorkspaces.length" class="detected-workspaces">
            <p class="hint">
              Found {{ detectedWorkspaces.length }} workspace{{ detectedWorkspaces.length === 1 ? '' : 's' }}
              already in this folder. Ciaobot will adopt them in place —
              no need to create one.
            </p>
            <ul class="workspace-chips" aria-label="Detected workspaces">
              <li
                v-for="name in detectedWorkspaces"
                :key="name"
                class="workspace-chip"
              >{{ name }}</li>
            </ul>
            <p class="hint hint--muted">
              You can rename or add more in Settings → Workspaces after setup.
            </p>
          </div>
          <template v-else>
            <label for="setup-workspace-name">First Workspace</label>
            <input
              id="setup-workspace-name"
              v-model="workspaceName"
              type="text"
              class="form-input"
              placeholder="personal"
              :disabled="loading"
            />
            <span class="hint">A workspace is a life area — personal, work, a client. You start with
              one and can add more later in Settings → Workspaces.</span>
          </template>
        </div>



        <div class="form-group">
          <label for="setup-password">Dashboard Password</label>
          <input
            id="setup-password"
            v-model="password"
            type="password"
            class="form-input"
            placeholder="choose a password"
            autocomplete="new-password"
            required
            :disabled="loading"
          />
          <label for="setup-password-confirm">Confirm Password</label>
          <input
            id="setup-password-confirm"
            v-model="passwordConfirm"
            type="password"
            class="form-input"
            placeholder="type it again"
            autocomplete="new-password"
            required
            :disabled="loading"
          />
          <span class="hint">Ciaobot is password-protected: this is what you type to open it
            in a browser, and what another device needs to connect as a client. At least
            {{ minPasswordLength }} characters — you can change it later in Settings → PWA password.</span>
          <span v-if="passwordProblem" class="hint hint--warn">{{ passwordProblem }}</span>
        </div>

        <div class="form-group">
          <label>AI Provider Choice</label>
          <span class="hint">Pick one to get started — you can add more providers later in Settings.</span>
          <div class="provider-choices">
            <label class="choice-label">
              <input type="radio" v-model="provider" value="claude" :disabled="loading" /> Claude Code
            </label>
            <label class="choice-label">
              <input type="radio" v-model="provider" value="codex" :disabled="loading" /> OpenAI Codex
            </label>
            <label class="choice-label">
              <input type="radio" v-model="provider" value="opencode" :disabled="loading" /> opencode
            </label>
          </div>
        </div>

        <!-- PROVIDER STATUS INFO -->
        <div v-if="setupStatus?.providers?.[provider]" class="provider-status-card">
          <div class="status-header">
            <span
              class="badge"
              :class="setupStatus.providers[provider].ok ? 'badge--success' : 'badge--error'"
            >
              {{ providerBadgeLabel }}
            </span>
            <span class="provider-detail">{{ setupStatus.providers[provider].detail }}</span>
          </div>

          <div v-if="!setupStatus.providers[provider].ok" class="command-box">
            <p class="hint">{{ providerInstruction }}</p>
            <div class="command-row">
              <code>{{ setupStatus.providers[provider].command }}</code>
              <button
                class="btn-small"
                type="button"
                :disabled="loading"
                @click="copyCommand(setupStatus.providers[provider].command || '')"
              >
                {{ copyStatus || 'Copy' }}
              </button>
            </div>
            <p v-if="setupStatus.providers[provider].install_url" class="hint">
              Other ways to install (Homebrew, WinGet, Linux packages):
              <a
                class="install-link"
                :href="setupStatus.providers[provider].install_url"
                target="_blank"
                rel="noopener noreferrer"
              >installation guide ↗</a>
            </p>
            <p v-if="setupStatus.providers[provider].auth === 'not_installed'" class="hint">
              This check refreshes on its own — finish the install in a terminal and
              the badge above turns green.
            </p>
          </div>
        </div>

        <div class="advanced-section">
          <button
            id="setup-advanced-toggle"
            type="button"
            class="advanced-toggle"
            :aria-expanded="advancedOpen"
            :disabled="loading"
            @click="advancedOpen = !advancedOpen"
          >
            <span class="advanced-caret">{{ advancedOpen ? '▾' : '▸' }}</span> Advanced
          </button>
          <div v-if="advancedOpen" class="advanced-options">
            <div class="form-grid">
              <div class="form-group">
                <label for="setup-port">Port</label>
                <input
                  id="setup-port"
                  v-model.number="port"
                  type="number"
                  class="form-input"
                  placeholder="8443"
                  required
                  :disabled="loading"
                />
              </div>
              <div class="form-group">
                <label for="setup-python">Python Path (Optional)</label>
                <input
                  id="setup-python"
                  v-model="python"
                  type="text"
                  class="form-input"
                  placeholder="blank for default"
                  :disabled="loading"
                />
              </div>
            </div>
            <div class="checkbox-row">
              <label class="choice-label">
                <input
                  id="setup-api-fallback"
                  type="checkbox"
                  v-model="apiFallback"
                  :disabled="loading"
                />
                I will set provider keys manually in .env later
              </label>
            </div>
          </div>
        </div>

        <div class="wizard-footer">
          <button
            class="prompt-submit btn-primary"
            :disabled="!canFinish || loading"
            type="submit"
          >
            {{ loading ? 'Configuring...' : 'Finish Setup' }}
          </button>
          <p v-if="error" class="line line--error">
            <span class="prompt prompt--err">!</span>{{ error }}
          </p>
        </div>

        <!-- FOLDER PICKER MODAL -->
        <div v-if="pickerOpen" class="picker-overlay" @click.self="closePicker">
          <div
            class="picker-modal"
            role="dialog"
            aria-label="Choose workspace folder"
          >
            <div class="picker-head">
              <span class="picker-title">Choose Workspace Folder</span>
              <span class="picker-help">
                Click a folder below to open it, or create one at the bottom. Nothing is
                selected until you press <strong>Use this folder</strong>.
              </span>
              <span class="picker-current-label">Currently viewing</span>
              <code class="picker-path">{{ pickerDisplayPath || '…' }}</code>
            </div>
            <div class="picker-toolbar">
              <button
                type="button"
                class="btn-small"
                :disabled="!pickerParent || pickerLoading"
                @click="loadPickerDirs(pickerParent!)"
              >↑ Up</button>
              <button
                type="button"
                class="btn-small"
                :disabled="pickerLoading"
                @click="loadPickerDirs()"
              >~ Home</button>
            </div>
            <ul class="picker-list">
              <li v-for="dir in pickerDirs" :key="dir.path">
                <button
                  type="button"
                  class="picker-dir"
                  :disabled="pickerLoading"
                  @click="loadPickerDirs(dir.path)"
                >{{ dir.name }}/</button>
              </li>
              <li v-if="!pickerLoading && !pickerDirs.length" class="picker-empty">
                No subfolders here — create one below, or use this folder as it is.
              </li>
            </ul>
            <div class="picker-new-block">
              <span class="picker-new-label">Or create a new folder here</span>
              <div class="picker-new">
                <input
                  v-model="newFolderName"
                  type="text"
                  class="form-input"
                  placeholder="e.g. ciaobot"
                  :disabled="pickerLoading"
                  @keydown.enter.prevent="createPickerFolder"
                />
                <button
                  type="button"
                  class="btn-small"
                  :disabled="!newFolderName.trim() || pickerLoading"
                  @click="createPickerFolder"
                >Create folder</button>
              </div>
            </div>
            <p v-if="pickerError" class="line line--error">
              <span class="prompt prompt--err">!</span>{{ pickerError }}
            </p>
            <div class="picker-footer">
              <button type="button" class="btn-small" @click="closePicker">Cancel</button>
              <button
                type="button"
                class="prompt-submit picker-select"
                :disabled="!pickerPath || pickerLoading"
                @click="selectPickerFolder"
              >Use this folder{{ pickerFolderName ? `: ${pickerFolderName}` : '' }}</button>
            </div>
          </div>
        </div>
      </form>

      <!-- STANDARD LOGIN FORM -->
      <form v-else class="login-body" @submit.prevent="doLogin">
        <p class="line line--banner">
          <span class="wordmark wordmark--md">ciaobot</span>
          <span class="banner-meta">// personal assistant · {{ loginModeHint }}</span>
        </p>
        <p class="line line--sys">{{ loginConnectingText }}<span v-if="loading"> ...</span></p>
        <p class="line">
          <span class="prompt">$</span>
          <label class="prompt-label" for="login-token">{{ loginTokenLabel }}:</label>
          <input
            id="login-token"
            v-model="token"
            type="password"
            class="prompt-input"
            :placeholder="loginTokenPlaceholder"
            autofocus
            autocomplete="current-password"
            :disabled="loading"
          />
          <button
            class="prompt-submit"
            :disabled="!token || loading"
            type="submit"
            :title="loading ? 'Authenticating' : 'Submit'"
            aria-label="Submit"
          >{{ loading ? '…' : '↵' }}</button>
        </p>
        <p v-if="error" class="line line--error">
          <span class="prompt prompt--err">!</span>{{ error }}
        </p>
        <p v-else-if="!loading" class="line line--hint">
          <span class="caret"></span>
        </p>
        <div v-if="isClientLogin" class="client-bailout">
          <p class="line line--sys">
            Can’t reach the host or don’t have the password? Stop tunneling and use this machine as host again.
          </p>
          <button
            type="button"
            class="btn-small client-bailout-btn"
            :disabled="loading || switchingToHost"
            @click="switchBackToHost"
          >
            {{ switchingToHost ? 'Switching…' : 'Switch back to host' }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useAuthStore } from '../stores/auth'
import type { SetupStatus } from '../lib/types'
import { errorMessage } from '../lib/errorMessage'
import { api } from '../lib/api'
import { askConfirm } from '../lib/confirm'

const auth = useAuthStore()
const token = ref('')
const error = ref('')
const loading = ref(false)
const clientHostUrl = ref('')
const switchingToHost = ref(false)
const isClientLogin = computed(() => Boolean(clientHostUrl.value))
const loginModeHint = computed(() =>
  isClientLogin.value ? 'client · host password required' : 'auth required',
)
const loginConnectingText = computed(() => {
  if (!isClientLogin.value) return 'connecting to Ciaobot'
  try {
    return `connecting to host ${new URL(clientHostUrl.value).host}`
  } catch {
    return `connecting to host ${clientHostUrl.value}`
  }
})
const loginTokenLabel = computed(() => (isClientLogin.value ? 'host_password' : 'auth_token'))
const loginTokenPlaceholder = computed(() =>
  isClientLogin.value ? 'password set on the host' : 'paste token',
)

async function switchBackToHost() {
  if (switchingToHost.value) return
  if (!await askConfirm(
    'Stop client mode and become host on this machine? Skips asking the remote to push (use this when the host is unreachable or you do not have the password).',
    {
      title: 'Become host on this device?',
      confirmLabel: 'Disconnect and become host',
    },
  )) {
    return
  }
  switchingToHost.value = true
  error.value = ''
  try {
    await api.post('/api/node/handover', {
      target_node_url: clientHostUrl.value,
      force: true,
    })
    window.location.assign('/')
  } catch (e) {
    error.value = errorMessage(e, 'Failed to switch back to host')
    switchingToHost.value = false
  }
}

// Setup Wizard states
const isBootstrap = ref(false)
const bootstrapLoading = ref(true)
const setupStatus = ref<SetupStatus | null>(null)
const workspace = ref('~/ciaobot')
const port = ref(8443)
const python = ref('')
const provider = ref('claude')
const apiFallback = ref(false)
// Password protection is not optional (server-side too: /api/setup/finish
// rejects a setup without one), so the wizard asks for it up front instead of
// hiding a toggle under Advanced.
const minPasswordLength = 4
const password = ref('')
const passwordConfirm = ref('')
const passwordProblem = computed(() => {
  const value = password.value
  if (!value) return ''
  if (value.length < minPasswordLength) {
    return `Use at least ${minPasswordLength} characters.`
  }
  if (passwordConfirm.value && passwordConfirm.value !== value) {
    return 'The two passwords do not match.'
  }
  return ''
})
const passwordReady = computed(
  () => password.value.length >= minPasswordLength && passwordConfirm.value === password.value,
)
const isRestarting = ref(false)
const workspaceName = ref('personal')
const copyStatus = ref('')
const advancedOpen = ref(false)

// Folder inspection: when the chosen workspace folder already contains
// nested workspace directories (e.g. memory-vault/personal/, memory-vault/work/),
// the wizard hides the "First Workspace" text field and shows them as
// read-only chips. The server adopts them on /api/setup/finish.
const folderInspecting = ref(false)
const detectedWorkspaces = ref<string[]>([])
let inspectToken = 0

async function inspectWorkspaceFolder(rawPath: string) {
  const path = rawPath.trim()
  if (!path) {
    detectedWorkspaces.value = []
    folderInspecting.value = false
    return
  }
  const token = ++inspectToken
  folderInspecting.value = true
  try {
    const data = await api.get<{
      mode: string
      existing_workspaces: string[]
    }>(`/api/setup/inspect-folder?path=${encodeURIComponent(path)}`)
    if (token !== inspectToken) return
    detectedWorkspaces.value = data.existing_workspaces || []
  } catch {
    // Network or guard error: fall back to the text field, the same as
    // before this probe existed.
    if (token !== inspectToken) return
    detectedWorkspaces.value = []
  } finally {
    if (token === inspectToken) folderInspecting.value = false
  }
}

// Folder picker modal (server-backed: browsers cannot give absolute paths)
interface DirListing {
  path: string
  display_path: string
  parent: string | null
  dirs: Array<{ name: string; path: string }>
  home: string
}
const pickerOpen = ref(false)
const pickerPath = ref('')
const pickerDisplayPath = ref('')
const pickerParent = ref<string | null>(null)
const pickerDirs = ref<Array<{ name: string; path: string }>>([])
const pickerError = ref('')
const pickerLoading = ref(false)
const newFolderName = ref('')

// Basename of the folder the picker is currently showing, so the confirm button
// names what it will select instead of an anonymous "this folder".
const pickerFolderName = computed(() => {
  const path = pickerDisplayPath.value || pickerPath.value
  const trimmed = path.replace(/\/+$/, '')
  if (!trimmed || trimmed === '~') return ''
  return trimmed.slice(trimmed.lastIndexOf('/') + 1)
})

function fetchListing(path?: string): Promise<DirListing> {
  const query = path ? `?path=${encodeURIComponent(path)}` : ''
  return api.get<DirListing>(`/api/setup/list-dirs${query}`)
}

function applyPickerListing(listing: DirListing) {
  pickerPath.value = listing.path
  pickerDisplayPath.value = listing.display_path
  pickerParent.value = listing.parent
  pickerDirs.value = listing.dirs || []
}

async function openPicker() {
  pickerOpen.value = true
  pickerPath.value = ''
  pickerDisplayPath.value = ''
  pickerParent.value = null
  pickerDirs.value = []
  pickerError.value = ''
  newFolderName.value = ''
  pickerLoading.value = true
  try {
    const current = workspace.value.trim()
    let listing: DirListing
    if (current) {
      try {
        listing = await fetchListing(current)
      } catch {
        // field value is not an existing folder on the server: start at home
        listing = await fetchListing()
      }
    } else {
      listing = await fetchListing()
    }
    applyPickerListing(listing)
  } catch (e) {
    pickerError.value = errorMessage(e, 'failed to list folder')
  } finally {
    pickerLoading.value = false
  }
}

async function loadPickerDirs(path?: string) {
  pickerLoading.value = true
  pickerError.value = ''
  try {
    applyPickerListing(await fetchListing(path))
  } catch (e) {
    pickerError.value = errorMessage(e, 'failed to list folder')
  } finally {
    pickerLoading.value = false
  }
}

async function createPickerFolder() {
  const name = newFolderName.value.trim()
  if (!name || !pickerPath.value) return
  pickerLoading.value = true
  pickerError.value = ''
  try {
    const listing = await api.post<DirListing>('/api/setup/mkdir', {
      path: pickerPath.value,
      name,
    })
    // The server returns the parent listing. Step into the folder that was just
    // created so "Use this folder" means the new one — leaving the picker on the
    // parent is how people end up selecting their home directory by accident.
    const created = (listing.dirs || []).find((dir) => dir.name === name)
    applyPickerListing(created ? await fetchListing(created.path) : listing)
    newFolderName.value = ''
  } catch (e) {
    pickerError.value = errorMessage(e, 'failed to create folder')
  } finally {
    pickerLoading.value = false
  }
}

function selectPickerFolder() {
  if (!pickerPath.value) return
  workspace.value = pickerPath.value
  pickerOpen.value = false
}

function closePicker() {
  pickerOpen.value = false
}

const providerInstruction = computed(() => {
  const status = setupStatus.value?.providers?.[provider.value]
  if (status?.auth === 'not_installed') {
    return status.app_path
      ? 'The desktop app is installed, but Ciaobot drives the CLI. Install it in your Terminal:'
      : 'Not installed yet. Run this in your Terminal to install it:'
  }
  if (provider.value === 'codex' && setupStatus.value?.providers?.codex?.auth === 'missing') {
    return 'Install Codex if needed, then run `ciao auth codex` and refresh this check:'
  }
  if (provider.value === 'opencode' && setupStatus.value?.providers?.opencode?.auth === 'missing') {
    return 'Install opencode if needed, then run `ciao auth opencode` and refresh this check:'
  }
  return 'To authorize, run this command in your Terminal:'
})

const providerBadgeLabel = computed(() => {
  const status = setupStatus.value?.providers?.[provider.value]
  if (status?.ok) return '[ok] Ready'
  if (status?.auth === 'not_installed') return '[!] Not Installed'
  return '[!] Not Configured'
})

async function doLogin() {
  loading.value = true
  error.value = ''
  try {
    await auth.login(token.value)
  } catch (e) {
    error.value = errorMessage(e, 'login failed')
  } finally {
    loading.value = false
  }
}

async function fetchSetupStatus() {
  try {
    const status = await api.get<SetupStatus>('/api/setup-status')
    setupStatus.value = status
    isBootstrap.value = !!status.bootstrap
  } catch {
    isBootstrap.value = false
  }
}

const canFinish = computed(() => {
  if (!workspace.value.trim()) {
    return false
  }
  if (!passwordReady.value) {
    return false
  }
  const currentProvider = provider.value
  const providerOk = setupStatus.value?.providers?.[currentProvider]?.ok
  if (!providerOk && !apiFallback.value) {
    return false
  }
  return true
})

async function copyCommand(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    copyStatus.value = 'Copied!'
    setTimeout(() => { copyStatus.value = '' }, 2000)
  } catch {
    copyStatus.value = 'Failed'
    setTimeout(() => { copyStatus.value = '' }, 2000)
  }
}

async function doFinish() {
  loading.value = true
  error.value = ''
  try {
    // When the chosen folder already contains nested workspace directories,
    // the server adopts them via detect_nested_workspaces. The text field is
    // hidden in that case, so skip sending workspace_name to avoid clobbering
    // the discovered set.
    const sendName = detectedWorkspaces.value.length === 0
    const finishBody: Record<string, unknown> = {
      workspace: workspace.value,
      // vault_root and vault_mode are intentionally omitted: the server
      // inspects the chosen folder — empty scaffolds a fresh vault at
      // memory-vault/, existing notes are adapted in place by the
      // onboarding agent.
      // push_contact is intentionally omitted: Web Push works out of the box
      // with a default VAPID subject; no email is collected during setup.
      port: Number(port.value),
      python: python.value || undefined,
      password: password.value,
      provider: provider.value,
      restart: true,
    }
    if (sendName) {
      finishBody.workspace_name = workspaceName.value.trim() || 'personal'
    }
    await api.post('/api/setup/finish', finishBody)
    isRestarting.value = true
    if (pollInterval) {
      clearInterval(pollInterval)
      pollInterval = null
    }
  } catch (e) {
    error.value = errorMessage(e, 'setup finish failed')
  } finally {
    loading.value = false
  }
}

let pollInterval: ReturnType<typeof setInterval> | null = null

watch(isBootstrap, (newVal) => {
  if (newVal) {
    if (!pollInterval) {
      pollInterval = setInterval(async () => {
        try {
          const status = await api.get<SetupStatus>('/api/setup-status')
          setupStatus.value = status
          isBootstrap.value = !!status.bootstrap
        } catch {
          // ignore transient poll errors
        }
      }, 2000)
    }
  } else {
    if (pollInterval) {
      clearInterval(pollInterval)
      pollInterval = null
    }
  }
}, { immediate: true })

// Re-probe whenever the chosen folder changes, so the chips ↔ text-field
// swap is responsive to Browse… picks. Only runs in bootstrap mode.
watch(workspace, (path) => {
  if (isBootstrap.value) inspectWorkspaceFolder(path || '')
})

onMounted(async () => {
  bootstrapLoading.value = true
  try {
    try {
      const startup = await fetch('/api/startup-status').then((r) => r.json())
      const role = String(startup?.node_role || '')
      if (role === 'client' || role === 'standby') {
        clientHostUrl.value = String(startup?.host_url || startup?.active_peer_url || '')
      }
    } catch {
      /* ignore */
    }
    await fetchSetupStatus()
    if (isBootstrap.value) {
      inspectWorkspaceFolder(workspace.value || '')
    }
  } finally {
    bootstrapLoading.value = false
  }
})

onUnmounted(() => {
  if (pollInterval) {
    clearInterval(pollInterval)
    pollInterval = null
  }
})
</script>

<style scoped>
.login-page {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  height: 100dvh;
  overflow-y: auto;
  padding: 20px;
}

.login-shell {
  width: 100%;
  max-width: 680px;
  margin: auto 0;
  background: var(--bg2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  overflow: hidden;
  box-shadow:
    0 24px 48px rgba(0, 0, 0, 0.45),
    0 0 0 1px rgba(255, 77, 109, 0.08);
}

.login-shell-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  background: var(--bg-elev);
  border-bottom: 1px solid var(--border);
}
.dot {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  display: inline-block;
}
.dot--r { background: var(--error); }
.dot--y { background: var(--warning); }
.dot--g { background: var(--success); }
.login-shell-title {
  margin-left: 8px;
  font-size: var(--text-xs);
  color: var(--fg3);
  letter-spacing: 0.5px;
}

.login-body {
  padding: 20px 22px 26px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: var(--text-base);
  line-height: 1.6;
}

.setup-wizard {
  gap: 12px;
}

.setup-run-note {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-sm);
}
.setup-run-note p {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.45;
}
.run-note-kicker {
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.setup-tour {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 0;
  border-top: 1px dashed var(--border);
  border-bottom: 1px dashed var(--border);
}
.tour-title {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-base);
  font-weight: 700;
}
.tour-list {
  list-style: none;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 14px;
  margin: 0;
  padding: 0;
}
.tour-list li {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-left: 10px;
  border-left: 2px solid var(--border-strong);
}
.tour-list strong {
  color: var(--fg2);
  font-size: var(--text-sm);
}
.tour-list span {
  color: var(--fg3);
  font-size: var(--text-xs);
  line-height: 1.4;
}

.line {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.line--banner {
  align-items: baseline;
  gap: 12px;
  margin-bottom: 8px;
}
.banner-meta {
  color: var(--fg3);
  font-size: var(--text-xs);
  letter-spacing: 0.3px;
}

.line--sys {
  color: var(--fg3);
  font-size: var(--text-sm);
}

.prompt {
  color: var(--accent);
  font-weight: 700;
  flex-shrink: 0;
}
.prompt--err {
  color: var(--error);
}

.prompt-label {
  color: var(--fg2);
  flex-shrink: 0;
}

.prompt-input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  padding: 4px 0;
  color: var(--fg);
  font-family: var(--font);
  font-size: 16px;
  caret-color: var(--accent);
  border-radius: 0;
  border-bottom: 1px solid transparent;
  transition: border-color 120ms var(--ease);
}
.prompt-input:focus {
  outline: none;
  border-bottom-color: var(--accent);
  box-shadow: none;
}
.prompt-input::placeholder {
  color: var(--fg3);
  opacity: 0.6;
}

.prompt-submit {
  flex-shrink: 0;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: var(--radius-sm);
  padding: 4px 10px;
  font-family: var(--font);
  font-size: var(--text-base);
  font-weight: 700;
  cursor: pointer;
  min-width: 36px;
  transition: background 120ms var(--ease), transform 120ms var(--ease);
}
.prompt-submit:hover:not(:disabled) { background: var(--accent-strong); }
.prompt-submit:active:not(:disabled) { transform: scale(0.96); }
.prompt-submit:disabled {
  background: var(--bg3);
  color: var(--fg3);
  cursor: not-allowed;
}

.line--error {
  color: var(--error);
  font-size: var(--text-sm);
}
.line--hint {
  min-height: 1.2em;
}

.client-bailout {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px dashed var(--border);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.client-bailout .line--sys {
  margin: 0;
  opacity: 0.85;
}
.client-bailout-btn {
  align-self: flex-start;
  background: transparent;
  border: 1px solid var(--warning, #ff9800);
  color: var(--warning, #ff9800);
  border-radius: var(--radius-sm);
  padding: 6px 12px;
  font-family: var(--font);
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
}
.client-bailout-btn:hover:not(:disabled) {
  background: color-mix(in srgb, var(--warning, #ff9800) 14%, transparent);
}
.client-bailout-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Form inputs styling */
.form-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.form-group label {
  color: var(--fg2);
  font-size: var(--text-sm);
  font-weight: 600;
}
.form-input {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 12px;
  color: var(--fg);
  font-family: inherit;
  font-size: var(--text-sm);
  transition: border-color 120ms var(--ease);
}
.form-input:focus {
  outline: none;
  border-color: var(--accent);
}
.form-input:disabled {
  background: var(--bg3);
  color: var(--fg3);
  cursor: not-allowed;
}
.hint {
  color: var(--fg3);
  font-size: var(--text-xs);
  line-height: 1.4;
}
.hint strong {
  color: var(--fg2);
}
.hint--muted {
  opacity: 0.75;
}
.hint--warn {
  color: var(--warning);
}

.detected-workspaces {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.workspace-chips {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.workspace-chip {
  display: inline-flex;
  align-items: center;
  padding: 3px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg);
  color: var(--fg2);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, monospace);
  font-size: var(--text-xs);
  letter-spacing: 0.3px;
}

.input-row {
  display: flex;
  gap: 6px;
  align-items: stretch;
}
.input-row .form-input {
  flex: 1;
  min-width: 0;
}

.vault-derived-hint code {
  color: var(--fg2);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 5px;
  font-family: inherit;
  font-size: var(--text-xs);
  word-break: break-all;
}
.link-btn {
  background: none;
  border: none;
  padding: 0;
  margin-left: 6px;
  color: var(--accent);
  font-family: inherit;
  font-size: var(--text-xs);
  text-decoration: underline;
  cursor: pointer;
}
.link-btn:disabled {
  color: var(--fg3);
  cursor: not-allowed;
}

/* Folder picker modal */
.picker-overlay {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(0, 0, 0, 0.55);
}
.picker-modal {
  width: 100%;
  max-width: 460px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 16px 16px;
  background: var(--bg2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  box-shadow: 0 24px 48px rgba(0, 0, 0, 0.45);
}
.picker-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.picker-title {
  color: var(--fg2);
  font-size: var(--text-sm);
  font-weight: 600;
}
.picker-help {
  color: var(--fg3);
  font-size: var(--text-xs);
  line-height: 1.45;
}
.picker-help strong {
  color: var(--fg2);
}
.picker-current-label,
.picker-new-label {
  color: var(--fg3);
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
.picker-new-block {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.picker-path {
  font-family: inherit;
  font-size: var(--text-xs);
  color: var(--accent);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 4px 6px;
  word-break: break-all;
}
.picker-toolbar {
  display: flex;
  gap: 6px;
}
.picker-list {
  list-style: none;
  margin: 0;
  padding: 4px;
  max-height: 240px;
  overflow-y: auto;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.picker-dir {
  width: 100%;
  text-align: left;
  background: transparent;
  border: none;
  border-radius: 4px;
  padding: 5px 8px;
  color: var(--fg);
  font-family: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}
.picker-dir:hover:not(:disabled) {
  background: var(--bg3);
}
.picker-dir:disabled {
  color: var(--fg3);
  cursor: not-allowed;
}
.picker-empty {
  padding: 5px 8px;
  color: var(--fg3);
  font-size: var(--text-xs);
}
.picker-new {
  display: flex;
  gap: 6px;
}
.picker-new .form-input {
  flex: 1;
  min-width: 0;
}
.picker-footer {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
}
.picker-select {
  font-size: var(--text-sm);
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.advanced-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.advanced-toggle {
  align-self: flex-start;
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  padding: 0;
  color: var(--fg3);
  font-family: inherit;
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
}
.advanced-toggle:hover:not(:disabled) {
  color: var(--fg2);
}
.advanced-toggle:disabled {
  cursor: not-allowed;
}
.advanced-caret {
  color: var(--accent);
}

.advanced-options {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.provider-choices {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-top: 4px;
}
.choice-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-sm);
  color: var(--fg2);
  cursor: pointer;
}

.provider-status-card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 12px;
  margin-top: 4px;
  font-size: var(--text-sm);
}

.status-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.badge {
  display: inline-block;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: var(--text-xs);
  font-weight: 700;
  text-transform: uppercase;
}
.badge--success {
  background: rgba(46, 204, 113, 0.15);
  color: var(--success);
  border: 1px solid rgba(46, 204, 113, 0.3);
}
.badge--error {
  background: rgba(231, 76, 60, 0.15);
  color: var(--error);
  border: 1px solid rgba(231, 76, 60, 0.3);
}

.provider-detail {
  color: var(--fg2);
  font-size: var(--text-xs);
}

.command-box {
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.command-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.install-link {
  color: var(--accent);
}

.command-row code {
  font-family: monospace;
  font-size: var(--text-xs);
  color: var(--accent);
  background: var(--bg2);
  padding: 4px 6px;
  border-radius: 4px;
  word-break: break-all;
  flex: 1;
}

.btn-small {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-elev);
  border: 1px solid var(--border-strong);
  color: var(--fg);
  border-radius: var(--radius-sm);
  padding: 4px 8px;
  font-size: var(--text-xs);
  cursor: pointer;
  white-space: nowrap;
}
.btn-small:hover {
  background: var(--bg3);
}

.checkbox-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.wizard-footer {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.wizard-footer .prompt-submit {
  width: 100%;
  padding: 10px;
  text-align: center;
}

.restarting-body {
  align-items: center;
  justify-content: center;
  min-height: 200px;
  text-align: center;
  gap: 16px;
}
.spinner-container {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 40px;
}

@media (min-width: 769px) {
  .prompt-input { font-size: var(--text-base); }
}

@media (max-width: 600px) {
  .login-shell-title { font-size: 10px; }
  .login-body { padding: 16px; }
  .tour-list {
    grid-template-columns: 1fr;
  }
  .form-grid {
    grid-template-columns: 1fr;
    gap: 8px;
  }
}
</style>
