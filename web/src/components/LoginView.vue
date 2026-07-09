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
          Ciaobot is finishing setup. If you started it with ciao run, keep that terminal open until it says setup is complete and Ciaobot is moving to the background service. Then close the terminal and open Ciaobot.app.
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
            Keep the terminal running ciao run open while you finish this setup. When setup completes, Ciaobot moves to the background service, then you can close the terminal and open Ciaobot.app.
          </p>
        </section>

        <section class="setup-tour" aria-label="Ciaobot setup tour">
          <p class="tour-title">Claude Code, with a real interface and memory.</p>
          <ul class="tour-list">
            <li>
              <strong>Bring your own backend.</strong>
              <span>Use Claude Code, Ollama Cloud or local Ollama, or an OpenRouter API key.</span>
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
          <span class="hint">Pick a brand-new folder or the notes folder you already have — Ciaobot
            detects what's inside and adjusts automatically: an empty folder gets a fresh
            second brain; existing notes are adapted in place into its structure.</span>
        </div>

        <div class="form-group">
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
        </div>

        <div class="form-group">
          <label for="setup-push">Notification Email (Optional)</label>
          <input
            id="setup-push"
            v-model="pushContact"
            type="text"
            inputmode="email"
            autocomplete="email"
            class="form-input"
            placeholder="you@example.com"
            :disabled="loading"
          />
          <span class="hint">Optional — enables push notifications. The Web Push standard requires an operator contact; nothing is ever emailed to you. You can set it later in Settings.</span>
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
          <div v-if="advancedOpen" class="form-grid">
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
        </div>

        <div class="form-group">
          <label>AI Provider Choice</label>
          <span class="hint">Pick one to get started — you can add more providers later in Settings.</span>
          <div class="provider-choices">
            <label class="choice-label">
              <input type="radio" v-model="provider" value="claude" :disabled="loading" /> Claude Code
            </label>
            <label class="choice-label">
              <input type="radio" v-model="provider" value="ollama" :disabled="loading" /> Ollama
            </label>
            <label class="choice-label">
              <input type="radio" v-model="provider" value="openrouter" :disabled="loading" /> OpenRouter
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
              {{ setupStatus.providers[provider].ok ? '[ok] Ready' : '[!] Not Configured' }}
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
                @click="copyCommand(setupStatus.providers[provider].command)"
              >
                {{ copyStatus || 'Copy' }}
              </button>
            </div>
          </div>
        </div>

        <div class="checkbox-row">
          <label class="choice-label">
            <input type="checkbox" v-model="authRequired" :disabled="loading" />
            Require password for PWA access
          </label>
        </div>

        <div class="checkbox-row">
          <label class="choice-label">
            <input type="checkbox" v-model="apiFallback" :disabled="loading" />
            I will set provider keys manually in .env later
          </label>
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
              <li v-if="!pickerLoading && !pickerDirs.length" class="picker-empty">no subfolders</li>
            </ul>
            <div class="picker-new">
              <input
                v-model="newFolderName"
                type="text"
                class="form-input"
                placeholder="new folder name"
                :disabled="pickerLoading"
                @keydown.enter.prevent="createPickerFolder"
              />
              <button
                type="button"
                class="btn-small"
                :disabled="!newFolderName.trim() || pickerLoading"
                @click="createPickerFolder"
              >New folder</button>
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
              >Select this folder</button>
            </div>
          </div>
        </div>
      </form>

      <!-- STANDARD LOGIN FORM -->
      <form v-else class="login-body" @submit.prevent="doLogin">
        <p class="line line--banner">
          <span class="wordmark wordmark--md">ciaobot</span>
          <span class="banner-meta">// personal assistant · auth required</span>
        </p>
        <p class="line line--sys">connecting to Ciaobot<span v-if="loading"> ...</span></p>
        <p class="line">
          <span class="prompt">$</span>
          <label class="prompt-label" for="login-token">auth_token:</label>
          <input
            id="login-token"
            v-model="token"
            type="password"
            class="prompt-input"
            placeholder="paste token"
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
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useAuthStore } from '../stores/auth'
import { api } from '../lib/api'

const auth = useAuthStore()
const token = ref('')
const error = ref('')
const loading = ref(false)

// Setup Wizard states
const isBootstrap = ref(false)
const bootstrapLoading = ref(true)
const setupStatus = ref<any>(null)
const workspace = ref('~/ciaobot')
const pushContact = ref('')
const port = ref(8443)
const python = ref('')
const provider = ref('claude')
const apiFallback = ref(false)
const authRequired = ref(false)
const isRestarting = ref(false)
const workspaceName = ref('personal')
const copyStatus = ref('')
const advancedOpen = ref(false)

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
  } catch (e: any) {
    pickerError.value = e.message || 'failed to list folder'
  } finally {
    pickerLoading.value = false
  }
}

async function loadPickerDirs(path?: string) {
  pickerLoading.value = true
  pickerError.value = ''
  try {
    applyPickerListing(await fetchListing(path))
  } catch (e: any) {
    pickerError.value = e.message || 'failed to list folder'
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
    applyPickerListing(listing)
    newFolderName.value = ''
  } catch (e: any) {
    pickerError.value = e.message || 'failed to create folder'
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
  if (provider.value === 'openrouter') {
    return 'Add this environment variable in your workspace .env:'
  }
  return 'To authorize, run this command in your Terminal:'
})

async function doLogin() {
  loading.value = true
  error.value = ''
  try {
    await auth.login(token.value)
  } catch (e: any) {
    error.value = e.message || 'login failed'
  } finally {
    loading.value = false
  }
}

async function fetchSetupStatus() {
  try {
    const status = await api.get<any>('/api/setup-status')
    setupStatus.value = status
    isBootstrap.value = !!status.bootstrap
  } catch (e) {
    isBootstrap.value = false
  }
}

// The field reads as a plain email input: show "you@example.com" even when a
// mailto: URI is pasted in (the prefix is re-added on submit). Full mailto:/
// https: URIs typed by power users stay valid either way.
watch(pushContact, (value) => {
  if (value.toLowerCase().startsWith('mailto:')) {
    pushContact.value = value.slice('mailto:'.length)
  }
})

// Web Push VAPID subjects are mailto:/https: URIs: wrap a plain email on
// submit, pass URIs through, and send '' to leave push unconfigured.
function normalizedPushContact(): string {
  const value = pushContact.value.trim()
  if (!value || /^(mailto:|https:)/i.test(value)) return value
  return `mailto:${value}`
}

const canFinish = computed(() => {
  if (!workspace.value.trim()) {
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
    await api.post('/api/setup/finish', {
      workspace: workspace.value,
      workspace_name: workspaceName.value.trim() || 'personal',
      // vault_root and vault_mode are intentionally omitted: the server
      // inspects the chosen folder — empty scaffolds a fresh vault at
      // memory-vault/, existing notes are adapted in place by the
      // onboarding agent.
      push_contact: normalizedPushContact(),
      port: Number(port.value),
      python: python.value || undefined,
      auth_required: authRequired.value,
      restart: true,
    })
    isRestarting.value = true
    if (pollInterval) {
      clearInterval(pollInterval)
      pollInterval = null
    }
  } catch (e: any) {
    error.value = e.message || 'setup finish failed'
  } finally {
    loading.value = false
  }
}

let pollInterval: any = null

watch(isBootstrap, (newVal) => {
  if (newVal) {
    if (!pollInterval) {
      pollInterval = setInterval(async () => {
        try {
          const status = await api.get<any>('/api/setup-status')
          setupStatus.value = status
          isBootstrap.value = !!status.bootstrap
        } catch (e) {
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

onMounted(async () => {
  bootstrapLoading.value = true
  try {
    await fetchSetupStatus()
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
