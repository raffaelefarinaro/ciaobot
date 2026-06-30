<template>
  <div class="login-page">
    <div class="login-shell">
      <div class="login-shell-bar">
        <span class="dot dot--r"></span>
        <span class="dot dot--y"></span>
        <span class="dot dot--g"></span>
        <span class="login-shell-title">
          {{ isRestarting ? 'ciao@bot · restarting' : (isBootstrap ? 'ciao@bot · first-run setup' : 'ciao@bot · session') }}
        </span>
      </div>

      <!-- RESTARTING SCREEN -->
      <div v-if="isRestarting" class="login-body restarting-body">
        <p class="line line--banner">
          <span class="wordmark wordmark--md">restarting</span>
          <span class="banner-meta">// ciao is loading config ...</span>
        </p>
        <p class="line line--sys">
          Ciao is restarting. Reopen Ciao.app if this page does not reconnect.
        </p>
        <div class="spinner-container">
          <span class="caret"></span>
        </div>
      </div>

      <!-- SETUP WIZARD SCREEN -->
      <form v-else-if="isBootstrap" class="login-body setup-wizard" @submit.prevent="doFinish">
        <p class="line line--banner">
          <span class="wordmark wordmark--md">ciao setup</span>
          <span class="banner-meta">// welcome · let's configure your assistant</span>
        </p>

        <div class="form-group">
          <label for="setup-workspace">Workspace Folder</label>
          <input
            id="setup-workspace"
            v-model="workspace"
            type="text"
            class="form-input"
            placeholder="~/ciao"
            required
            :disabled="loading"
          />
          <span class="hint">The directory where your configuration and chats will reside.</span>
        </div>

        <div class="form-group">
          <label for="setup-vault">Vault Folder</label>
          <input
            id="setup-vault"
            v-model="vaultRoot"
            type="text"
            class="form-input"
            placeholder="~/ciao/memory-vault"
            required
            :disabled="loading"
            @input="userEditedVault = true"
          />
          <span class="hint">Durable markdown files / personal knowledge base folder.</span>
        </div>

        <div class="form-group">
          <label for="setup-push">Push Contact</label>
          <input
            id="setup-push"
            v-model="pushContact"
            type="text"
            class="form-input"
            placeholder="mailto:you@example.com"
            required
            :disabled="loading"
          />
          <span class="hint">Required. Used to register push notifications and security certificates.</span>
        </div>

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

        <div class="form-group">
          <label>AI Provider Choice</label>
          <div class="provider-choices">
            <label class="choice-label">
              <input type="radio" v-model="provider" value="claude" :disabled="loading" /> Claude
            </label>
            <label class="choice-label">
              <input type="radio" v-model="provider" value="pi" :disabled="loading" /> Pi
            </label>
            <label class="choice-label">
              <input type="radio" v-model="provider" value="ollama" :disabled="loading" /> Ollama
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
              {{ setupStatus.providers[provider].ok ? '✓ Ready' : '✗ Not Configured' }}
            </span>
            <span class="provider-detail">{{ setupStatus.providers[provider].detail }}</span>
          </div>

          <div v-if="!setupStatus.providers[provider].ok" class="command-box">
            <p class="hint">To authorize, run this command in your Terminal:</p>
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
            I will set API keys manually in .env later (API key fallback)
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
      </form>

      <!-- STANDARD LOGIN FORM -->
      <form v-else class="login-body" @submit.prevent="doLogin">
        <p class="line line--banner">
          <span class="wordmark wordmark--md">ciao</span>
          <span class="banner-meta">// personal assistant · auth required</span>
        </p>
        <p class="line line--sys">connecting to Ciao<span v-if="loading"> ...</span></p>
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
const workspace = ref('~/ciao')
const vaultRoot = ref('~/ciao/memory-vault')
const pushContact = ref('')
const port = ref(8443)
const python = ref('')
const provider = ref('claude')
const apiFallback = ref(false)
const authRequired = ref(true)
const isRestarting = ref(false)
const userEditedVault = ref(false)
const copyStatus = ref('')

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

// Watch workspace path changes to automatically update vault root if user hasn't touched it
watch(workspace, (newVal) => {
  if (!userEditedVault.value) {
    const ws = newVal.trim()
    if (ws) {
      vaultRoot.value = ws.endsWith('/') ? `${ws}memory-vault` : `${ws}/memory-vault`
    } else {
      vaultRoot.value = ''
    }
  }
})

const canFinish = computed(() => {
  if (!workspace.value.trim() || !vaultRoot.value.trim() || !pushContact.value.trim()) {
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
      vault_root: vaultRoot.value,
      push_contact: pushContact.value,
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
  align-items: center;
  justify-content: center;
  min-height: 100dvh;
  padding: 20px;
}

.login-shell {
  width: 100%;
  max-width: 520px;
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

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.provider-choices {
  display: flex;
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
  .form-grid {
    grid-template-columns: 1fr;
    gap: 8px;
  }
}
</style>
