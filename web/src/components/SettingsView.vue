<template>
  <div class="settings-pane">
    <PaneHeader title="Settings" @open-sidebar="emit('open-sidebar')" />
    <div class="pane-body">

      <!-- HOME TAB -->
      <template v-if="currentTab === 'home'">
        <!-- Actions -->
        <div class="card">
          <p class="section-title">Actions</p>
          <div class="action-row">
            <button class="btn-primary" @click="() => localStatus?.direct_main ? localHandback() : doSnapshot()" :disabled="!!actionPending || !!localPending">
              {{ actionPending === 'snapshot' || localPending === 'handback' ? (localStatus?.direct_main ? 'Syncing...' : 'Snapshotting...') : (localStatus?.direct_main ? 'Sync with Remote' : 'Git Snapshot') }}
            </button>
            <button v-if="localStatus?.dev_mode" class="btn-primary" @click="() => doDeploy()" :disabled="!!actionPending" title="Reinstall deps, rebuild the frontend, and restart with the latest code">
              {{ actionPending === 'deploy' ? 'Deploying...' : 'Deploy' }}
            </button>
          </div>
          <div v-if="actionResult" class="action-result">{{ actionResult }}</div>
          <div v-if="deploySteps.length" class="deploy-steps">
            <div v-for="step in deploySteps" :key="step.step" class="deploy-step" :class="{ ok: step.ok, fail: !step.ok }">
              <span class="step-icon">{{ step.ok ? '&#10003;' : '&#10007;' }}</span>
              {{ step.step }}
              <span v-if="step.output" class="step-output">{{ step.output }}</span>
            </div>
          </div>
        </div>

        <!-- Package Update -->
        <div class="card">
          <p class="section-title">Package Update</p>
          <div v-if="packageLoading && !packageStatus" class="loading">
            Checking package status...
          </div>
          <div v-else-if="packageStatus">
            <div class="dev-meta">
              <div>
                <span class="dev-label">Current Version</span> <code>{{ packageStatus.current_version }}</code>
              </div>
              <div v-if="packageStatus.latest_version">
                <span class="dev-label">Latest Version</span> <code>{{ packageStatus.latest_version }}</code>
              </div>
              <div v-if="packageStatus.error" class="hint hint--warn" style="margin-top: 6px;">
                Update check failed: {{ packageStatus.error }}
              </div>
            </div>

            <div v-if="packageStatus.update_available" class="action-row" style="margin-top: 10px;">
              <button class="btn-primary" @click="doPackageUpdate" :disabled="packageUpdating">
                {{ packageUpdating ? 'Upgrading...' : 'Update Package' }}
              </button>
            </div>
            <p v-else class="hint" style="margin-top: 6px;">
              Ciao is up to date.
            </p>
          </div>
          <div v-if="packageResult" class="action-result">{{ packageResult }}</div>
        </div>

        <!-- Notifications -->
        <div class="card">
          <p class="section-title">Notifications</p>
          <div v-if="needsIosInstall" class="hint hint--warn">
            On iOS, push notifications only work after you "Add to Home Screen" and open the app from there.
          </div>
          <div v-else-if="permissionDenied" class="hint hint--warn">
            Notifications are blocked at the OS level. Re-enable them in your phone's Settings &rarr; Notifications &rarr; Ciao.
          </div>
          <div v-else-if="!pushSupportedFlag" class="loading">
            Push notifications are not supported in this browser.
          </div>
          <div v-else class="action-row">
            <button class="btn-primary" @click="togglePush" :disabled="pushPending">
              {{ pushPending ? 'Working...' : (pushEnabledFlag ? 'Disable on this device' : 'Enable on this device') }}
            </button>
          </div>
          <div v-if="pushError" class="action-result">{{ pushError }}</div>
          <p class="hint">
            Enable to get a phone notification when a chat replies and the app isn't focused.
            Notifications include Open / Dismiss actions and update the app badge.
          </p>
        </div>

        <!-- Theme -->
        <div class="card">
          <p class="section-title">Theme</p>
          <div class="instance-toggle">
            <button
              class="toggle-btn"
              :class="{ active: activeTheme === 'dark' }"
              @click="setTheme('dark')"
            >
              Dark Mode
            </button>
            <button
              class="toggle-btn"
              :class="{ active: activeTheme === 'light' }"
              @click="setTheme('light')"
            >
              Light Mode
            </button>
            <button
              class="toggle-btn"
              :class="{ active: activeTheme === 'system' }"
              @click="setTheme('system')"
            >
              System
            </button>
          </div>
          <p class="hint">
            Choose light, dark, or match your device's system appearance.
          </p>
        </div>

        <!-- Font Size -->
        <div class="card">
          <p class="section-title">Font Size</p>
          <div class="font-scale-row">
            <button class="btn-small" @click="adjustFontScale(-0.05)" :disabled="fontScale <= 0.8">- Decrease</button>
            <span class="font-scale-display">{{ Math.round(fontScale * 100) }}%</span>
            <button class="btn-small" @click="adjustFontScale(0.05)" :disabled="fontScale >= 1.5">+ Increase</button>
            <button class="btn-small" style="margin-left: auto;" @click="resetFontScale" :disabled="fontScale === 1.0">Reset</button>
          </div>
          <p class="hint">
            Increase or decrease the font size across messages, code blocks, sidebars, and menus.
          </p>
        </div>

        <!-- Commit to main (branch-per-device) -->
        <div v-if="localStatus && !localStatus.direct_main" class="card">
          <p class="section-title">Commit to main</p>
          <div class="dev-meta">
            <div>
              <span class="dev-label">Device</span> <code>{{ localStatus?.device_name || '...' }}</code>
            </div>
            <div>
              <span class="dev-label">Branch</span> <code>{{ localStatus?.branch || '...' }}</code>
              <span v-if="localStatus?.dirty" class="hint" style="margin-left:6px;">(uncommitted changes)</span>
            </div>
          </div>
          <div class="action-row">
            <button class="btn-primary" @click="() => localHandback()" :disabled="!!localPending">
              {{ localPending === 'handback' ? 'Committing...' : 'Commit to main' }}
            </button>
            <button class="btn-primary" @click="localResync" :disabled="!!localPending">
              {{ localPending === 'resync' ? 'Syncing...' : 'Sync to main' }}
            </button>
          </div>
          <p class="hint">
            "Commit to main" lands this device's branch on <code>main</code>. If it can't merge cleanly,
            it opens a chat to resolve the conflict; then use "Sync to main".
          </p>
          <div v-if="localResult" class="action-result" style="white-space: pre-wrap;">{{ localResult }}</div>
        </div>
      </template>


      <!-- MODELS TAB -->
      <template v-if="currentTab === 'models'">
        <div v-if="!routinesLoaded" class="card"><span class="loading">Loading&hellip;</span></div>
        <template v-else-if="routinesError">
          <div class="card"><p class="hint">{{ routinesError }}</p></div>
        </template>
        <template v-else-if="routines">
          <!-- Internal routines -->
          <div class="card">
            <p class="section-title">Routine models</p>
            <p class="hint">
              Internal tasks that run on their own model, independent of the chat's model.
              "Automatic" keeps the built-in default. Local models run free on this machine's
              Ollama daemon.
            </p>

            <div class="routine-row">
              <div class="routine-info">
                <span class="routine-name">Chat titles</span>
                <span class="routine-detail">Names a new chat after the first message. Currently: <code>{{ routines.title_model_effective }}</code></span>
              </div>
              <select
                class="routine-select"
                :value="routines.title_model"
                :disabled="routinesSaving"
                @change="saveRoutines({ title_model: ($event.target as HTMLSelectElement).value })"
              >
                <option value="">Automatic</option>
                <option value="apfel">apfel (local, Apple Intelligence)</option>
                <optgroup v-if="routines.model_options.ollama_local.length" label="Ollama (local, free)">
                  <option v-for="m in routines.model_options.ollama_local" :key="m" :value="m">{{ m }}</option>
                </optgroup>
                <optgroup v-if="routines.model_options.ollama_cloud.length" label="Ollama cloud">
                  <option v-for="m in routines.model_options.ollama_cloud" :key="m" :value="m">{{ m }}</option>
                </optgroup>
                <optgroup label="Anthropic">
                  <option v-for="m in routines.model_options.anthropic" :key="m" :value="m">{{ m }}</option>
                </optgroup>
              </select>
            </div>

            <div class="routine-row">
              <div class="routine-info">
                <span class="routine-name">Session insights</span>
                <span class="routine-detail">Extracts learnings when a chat is archived. Currently: <code>{{ routines.insights_model_effective }}</code></span>
              </div>
              <select
                class="routine-select"
                :value="routines.insights_model"
                :disabled="routinesSaving"
                @change="saveRoutines({ insights_model: ($event.target as HTMLSelectElement).value })"
              >
                <option value="">Automatic</option>
                <optgroup v-if="routines.model_options.ollama_local.length" label="Ollama (local, free)">
                  <option v-for="m in routines.model_options.ollama_local" :key="m" :value="m">{{ m }}</option>
                </optgroup>
                <optgroup v-if="routines.model_options.ollama_cloud.length" label="Ollama cloud">
                  <option v-for="m in routines.model_options.ollama_cloud" :key="m" :value="m">{{ m }}</option>
                </optgroup>
                <optgroup label="Anthropic">
                  <option v-for="m in routines.model_options.anthropic" :key="m" :value="m">{{ m }}</option>
                </optgroup>
              </select>
            </div>

            <div class="routine-row">
              <div class="routine-info">
                <span class="routine-name">Critique models</span>
                <span class="routine-detail">Models used for adversarial review (comma-separated). Currently: <code>{{ routines.critique_models_effective }}</code></span>
              </div>
              <input
                type="text"
                class="routine-input"
                :value="routines.critique_models"
                :disabled="routinesSaving"
                @change="saveRoutines({ critique_models: ($event.target as HTMLInputElement).value })"
                placeholder="Automatic default"
              />
            </div>
          </div>

          <!-- Voice transcription -->
          <div class="card">
            <p class="section-title">Voice transcription</p>
            <div class="routine-row" style="border-top: none; margin-top: 0; padding-top: 0;">
              <div class="routine-info">
                <span class="routine-name">Engine</span>
                <span class="routine-detail">
                  <template v-if="routines.transcription.engine === 'local'">
                    Dictation runs on-device via mlx-whisper (<code>{{ routines.transcription.local_model }}</code>).
                    The first transcription downloads the model. Falls back to cloud if the local engine fails.
                  </template>
                  <template v-else>
                    Dictation uses the OpenAI transcription API (needs <code>OPENAI_API_KEY</code>, ~$0.003/min).
                  </template>
                </span>
              </div>
              <select
                class="routine-select"
                :value="routines.transcription.engine"
                :disabled="routinesSaving"
                @change="saveRoutines({ transcription_engine: ($event.target as HTMLSelectElement).value })"
              >
                <option value="local">Local (free)</option>
                <option value="cloud" :disabled="!routines.transcription.cloud_available">Cloud (OpenAI)</option>
              </select>
            </div>
            <p v-if="!routines.transcription.local_available" class="hint hint--warn" style="margin-top: 10px; display: flex; align-items: center; gap: 8px;">
              <span v-if="routines.transcription.engine === 'local'">
                <strong>Local Whisper engine is selected but not installed.</strong> Run <code>pip install 'ciao[voice-local]'</code> or install now:
              </span>
              <span v-else>
                Local engine is available for Apple Silicon (requires installing <code>mlx-whisper</code>).
              </span>
              <button
                class="btn-primary btn-small"
                style="padding: 2px 8px; font-size: 0.8rem;"
                :disabled="voiceInstalling"
                @click="installLocalVoice"
              >
                {{ voiceInstalling ? 'Installing...' : 'Install engine' }}
              </button>
            </p>
          </div>
          <div v-if="routinesResult" class="action-result">{{ routinesResult }}</div>
        </template>
      </template>

      <!-- PROVIDERS TAB -->
      <template v-if="currentTab === 'providers'">
        <div v-if="!providerKeysLoaded" class="card"><span class="loading">Loading&hellip;</span></div>
        <template v-else-if="providerKeysError">
          <div class="card"><p class="hint">{{ providerKeysError }}</p></div>
        </template>
        <template v-else-if="providerKeys">
          <div class="card">
            <p class="section-title">LLM Providers Configuration</p>
            <p class="hint">
              Manage API keys and developer credentials. Changes are written directly to your local <code>.env</code> file and will automatically reboot the server.
            </p>

            <div v-for="(meta, key) in providerKeys.keys" :key="key" class="routine-row" style="flex-direction: column; align-items: stretch; gap: 8px;">
              <div class="routine-info" style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
                <span class="routine-name">{{ meta.label }}</span>
                <span class="badge" :class="meta.configured ? 'badge--success' : 'badge--error'">
                  {{ meta.configured ? '✓ Configured' : '✗ Unconfigured' }}
                </span>
              </div>
              <p class="hint" style="margin-top: 0; margin-bottom: 4px;">{{ meta.description }}</p>
              <input
                type="password"
                class="routine-input"
                v-model="providerKeyInputs[key]"
                :placeholder="meta.configured ? '•••••••••••• (Leave blank to keep existing, or type empty space to clear)' : 'Enter API Key'"
                :disabled="providerKeysSaving"
                style="max-width: 100%; width: 100%; font-family: monospace; box-sizing: border-box;"
              />
            </div>

            <!-- System Settings -->
            <div class="routine-row" style="flex-direction: column; align-items: stretch; gap: 8px; margin-top: 16px; border-top: 1px solid var(--border); padding-top: 16px;">
              <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
                <span class="routine-name">Auto-Update GitHub Skills</span>
                <input
                  type="checkbox"
                  v-model="autoUpdateGithubSkills"
                  :disabled="providerKeysSaving"
                  style="width: 18px; height: 18px; cursor: pointer;"
                />
              </div>
              <p class="hint" style="margin-top: 0; margin-bottom: 0;">
                If enabled, Ciao automatically checks GitHub for updates to your locked package skills on boot.
              </p>
            </div>

            <div class="action-row" style="margin-top: 20px;">
              <button class="btn-primary" @click="saveProviderKeys" :disabled="providerKeysSaving">
                {{ providerKeysSaving ? 'Saving...' : 'Save Keys' }}
              </button>
            </div>
            <div v-if="providerKeysResult" class="action-result">{{ providerKeysResult }}</div>
          </div>
        </template>
      </template>

      <!-- SKILLS TAB -->
      <template v-if="currentTab === 'skills'">
        <div class="card">
          <p class="section-title">Skills</p>
          <div v-if="!skillsLoaded" class="action-row"><span class="loading">Loading&hellip;</span></div>
          <template v-else-if="skillsError">
            <p class="hint">{{ skillsError }}</p>
          </template>
          <template v-else-if="skillsInventory">
            <p class="hint">
              {{ skillsInventory.counts.custom }} custom &middot; {{ skillsInventory.counts.github }} GitHub/package
            </p>
            <div class="skill-list">
              <div
                v-for="skill in skillsInventory.skills"
                :key="skill.name"
                class="skill-row"
                :class="{ expanded: isSkillExpanded(skill.name) }"
                @click="toggleSkill(skill.name)"
              >
                <div class="skill-main">
                  <div class="skill-title-row">
                    <span class="skill-chevron">{{ isSkillExpanded(skill.name) ? '&#9662;' : '&#9656;' }}</span>
                    <span class="skill-name">{{ skill.name }}</span>
                    <span class="badge" :class="skill.label === 'custom' ? '--accent' : '--accent2'">
                      {{ skill.label === 'custom' ? 'custom' : 'github/package' }}
                    </span>
                  </div>
                  <p v-if="skill.description" class="skill-description">{{ skill.description }}</p>
                  <p class="skill-source">{{ skill.source }}</p>
                  <div v-if="isSkillExpanded(skill.name)" class="skill-detail">
                    <p v-if="skill.source_type" class="skill-meta"><span class="skill-meta-label">Type</span> {{ skill.source_type }}</p>
                    <div v-if="skill.installed_targets.length" class="skill-meta">
                      <span class="skill-meta-label">Installed on</span>
                      <div class="skill-targets-inline">
                        <span v-for="target in skill.installed_targets" :key="target" class="skill-target">{{ target }}</span>
                      </div>
                    </div>
                  </div>
                </div>
                <div v-if="!isSkillExpanded(skill.name) && skill.installed_targets.length" class="skill-targets">
                  <span v-for="target in skill.installed_targets" :key="target" class="skill-target">{{ target }}</span>
                </div>
              </div>
            </div>
          </template>
        </div>
      </template>

      <!-- AUTOMATION TAB -->
      <template v-if="currentTab === 'automation'">
        <div class="card">
          <div class="auto-intro">
            <div>
              <p class="section-title">Automation</p>
              <p class="hint">
                Background processes Ciao runs on its own. Check that each is working,
                when it last ran, how long it took, and what failed.
              </p>
            </div>
            <button class="btn-small" @click="fetchAutomation" :disabled="!automationLoaded">Refresh</button>
          </div>
          <div v-if="!automationLoaded" class="action-row"><span class="loading">Loading&hellip;</span></div>
          <p v-else-if="automationError" class="hint">{{ automationError }}</p>
        </div>

        <template v-if="automationLoaded && !automationError">
          <div v-for="group in automationGroups" :key="group.key" class="card">
            <p class="section-title">{{ group.title }}</p>
            <div class="auto-list">
              <div
                v-for="item in group.items"
                :key="item.job"
                class="auto-row"
                :class="{ expanded: isJobExpanded(item.job) }"
                @click="toggleJob(item.job)"
              >
                <div class="auto-head">
                  <span class="auto-chevron">{{ isJobExpanded(item.job) ? '&#9662;' : '&#9656;' }}</span>
                  <span class="auto-name">{{ item.label }}</span>
                  <span class="badge" :class="statusBadgeClass(item)">{{ statusLabel(item) }}</span>
                  <span class="auto-when">{{ lastRunLabel(item) }}</span>
                </div>
                <div class="auto-sub">
                  <span v-if="item.last_run" class="auto-meta">{{ formatDuration(item.last_run.duration_ms) || '0ms' }}</span>
                  <span v-if="item.last_run && item.last_run.model" class="badge --muted">
                    {{ item.last_run.model }}<template v-if="item.last_run.provider"> &middot; {{ item.last_run.provider }}</template>
                  </span>
                  <span v-if="item.stats.total_runs" class="auto-meta">
                    {{ item.stats.total_runs }} run{{ item.stats.total_runs === 1 ? '' : 's' }}<template v-if="item.stats.success_rate != null"> &middot; {{ Math.round(item.stats.success_rate * 100) }}% ok</template>
                  </span>
                </div>
                <div v-if="item.last_run?.status === 'error' && lastError(item) && !isJobExpanded(item.job)" class="auto-error">{{ lastError(item) }}</div>

                <div v-if="isJobExpanded(item.job)" class="auto-detail" @click.stop>
                  <p v-if="item.description" class="auto-desc">{{ item.description }}</p>
                  <p v-if="!item.recent.length" class="hint">No runs recorded yet.</p>
                  <div v-for="(run, i) in item.recent" :key="i" class="auto-run">
                    <span class="badge" :class="runBadgeClass(run.status)">{{ run.status }}</span>
                    <span class="auto-run-when">{{ formatTime(run.ended_at || run.started_at) }}</span>
                    <span class="auto-meta">{{ formatDuration(run.duration_ms) || '0ms' }}</span>
                    <span v-if="run.model" class="auto-run-model">{{ run.model }}</span>
                    <span v-if="run.error" class="auto-run-error">{{ run.error }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </template>
      </template>


    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../lib/api'
import { formatTime, formatDuration } from '../lib/time'
import type { AutomationProcess, DeployResult, LocalStatus, RoutineSettings, SkillInventory, ProviderConfigSettings } from '../lib/types'
import { currentSubscription, disablePush, enablePush, isPushEnabled, pushSupported } from '../lib/push'
import { useAuthStore } from '../stores/auth'
import PaneHeader from './PaneHeader.vue'

const emit = defineEmits<{ 'open-sidebar': [] }>()

const route = useRoute()
const currentTab = computed(() => (route.params.tab as string) || 'home')

const expandedSkills = ref<Record<string, boolean>>({})

// ── Appearance settings (Theme & Font Size) ─────────────────────────────────
const activeTheme = ref('dark')
const fontScale = ref(1.0)

function loadAppearanceSettings() {
  try {
    activeTheme.value = localStorage.getItem('ciao-theme') || 'dark'
    const savedScale = localStorage.getItem('ciao-font-scale')
    if (savedScale) {
      fontScale.value = parseFloat(savedScale) || 1.0
    }
  } catch (e) {
    // Ignore localStorage block
  }
}

function setTheme(theme: 'dark' | 'light' | 'system') {
  activeTheme.value = theme
  try {
    localStorage.setItem('ciao-theme', theme)
  } catch (e) {}

  if (theme === 'light') {
    document.documentElement.classList.add('theme-light')
  } else if (theme === 'dark') {
    document.documentElement.classList.remove('theme-light')
  } else {
    const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches
    if (isDark) {
      document.documentElement.classList.remove('theme-light')
    } else {
      document.documentElement.classList.add('theme-light')
    }
  }
}

function adjustFontScale(delta: number) {
  let next = parseFloat((fontScale.value + delta).toFixed(2))
  if (next < 0.8) next = 0.8
  if (next > 1.5) next = 1.5
  fontScale.value = next
  try {
    localStorage.setItem('ciao-font-scale', next.toString())
  } catch (e) {}
  document.documentElement.style.setProperty('--font-scale', next.toString())
}

function resetFontScale() {
  fontScale.value = 1.0
  try {
    localStorage.setItem('ciao-font-scale', '1.0')
  } catch (e) {}
  document.documentElement.style.setProperty('--font-scale', '1.0')
}
function isSkillExpanded(name: string) {
  return expandedSkills.value[name] || false
}
function toggleSkill(name: string) {
  expandedSkills.value[name] = !isSkillExpanded(name)
}

const actionPending = ref<string | null>(null)
const actionResult = ref('')
const deploySteps = ref<{ step: string; ok: boolean; output?: string }[]>([])

const skillsInventory = ref<SkillInventory | null>(null)
const skillsLoaded = ref(false)
const skillsError = ref('')

// ── Routine settings (Models tab) ─────────────────────────────────────────
const routines = ref<RoutineSettings | null>(null)
const routinesLoaded = ref(false)
const routinesError = ref('')
const routinesSaving = ref(false)
const routinesResult = ref('')

async function fetchRoutines() {
  try {
    routines.value = await api.get<RoutineSettings>('/api/settings/routines')
  } catch (e: any) {
    routinesError.value = `Failed to load model settings: ${e?.message || e}`
  } finally {
    routinesLoaded.value = true
  }
}

async function saveRoutines(patch: Record<string, string>) {
  routinesSaving.value = true
  routinesResult.value = ''
  try {
    routines.value = await api.patch<RoutineSettings>('/api/settings/routines', patch)
    routinesResult.value = 'Saved.'
    setTimeout(() => { routinesResult.value = '' }, 2000)
  } catch (e: any) {
    routinesResult.value = `Error: ${e?.message || e}`
  } finally {
    routinesSaving.value = false
  }
}

// ── Provider API Key settings (Providers tab) ─────────────────────────────────
const providerKeys = ref<ProviderConfigSettings | null>(null)
const providerKeysLoaded = ref(false)
const providerKeysError = ref('')
const providerKeysSaving = ref(false)
const providerKeysResult = ref('')
const providerKeyInputs = ref<Record<string, string>>({})
const autoUpdateGithubSkills = ref(true)

async function fetchProviderKeys() {
  try {
    const res = await api.get<ProviderConfigSettings>('/api/settings/providers')
    providerKeys.value = res
    for (const key in res.keys) {
      providerKeyInputs.value[key] = ''
    }
    if (res.auto_update_github_skills !== undefined) {
      autoUpdateGithubSkills.value = res.auto_update_github_skills
    }
  } catch (e: any) {
    providerKeysError.value = `Failed to load provider keys: ${e?.message || e}`
  } finally {
    providerKeysLoaded.value = true
  }
}

async function saveProviderKeys() {
  if (!providerKeys.value) return
  providerKeysSaving.value = true
  providerKeysResult.value = ''
  
  const patchKeys: Record<string, string> = {}
  for (const key in providerKeys.value.keys) {
    const val = providerKeyInputs.value[key]
    if (val !== '') {
      patchKeys[key] = val
    }
  }
  
  const hasKeyChanges = Object.keys(patchKeys).length > 0
  const hasSettingChanges = autoUpdateGithubSkills.value !== providerKeys.value.auto_update_github_skills
  
  if (!hasKeyChanges && !hasSettingChanges) {
    providerKeysResult.value = 'No changes to save.'
    providerKeysSaving.value = false
    setTimeout(() => { providerKeysResult.value = '' }, 2000)
    return
  }
  
  try {
    const payload: any = {}
    if (hasKeyChanges) {
      payload.keys = patchKeys
    }
    if (hasSettingChanges) {
      payload.auto_update_github_skills = autoUpdateGithubSkills.value
    }
    
    const res = await api.patch<ProviderConfigSettings>('/api/settings/providers', payload)
    providerKeys.value = res
    for (const key in res.keys) {
      providerKeyInputs.value[key] = ''
    }
    if (res.auto_update_github_skills !== undefined) {
      autoUpdateGithubSkills.value = res.auto_update_github_skills
    }
    providerKeysResult.value = 'Saved configuration. Restarting server to apply...'
    setTimeout(() => {
      providerKeysResult.value = ''
      window.location.reload()
    }, 2500)
  } catch (e: any) {
    providerKeysResult.value = `Error: ${e?.message || e}`
  } finally {
    providerKeysSaving.value = false
  }
}


const voiceInstalling = ref(false)

async function installLocalVoice() {
  voiceInstalling.value = true
  routinesResult.value = 'Installing local whisper engine...'
  try {
    const res = await api.post<{ ok: boolean; output?: string }>('/api/voice/install-local', {})
    if (res.ok) {
      routinesResult.value = 'Local whisper engine installed successfully! Restarting server...'
      setTimeout(async () => {
        routinesResult.value = ''
        await fetchRoutines()
      }, 5000)
    } else {
      routinesResult.value = 'Installation failed.'
    }
  } catch (e: any) {
    routinesResult.value = `Error installing engine: ${e?.message || e}`
  } finally {
    voiceInstalling.value = false
  }
}

async function fetchSkills() {
  try {
    skillsInventory.value = await api.get<SkillInventory>('/api/admin/skills')
  } catch (e: any) {
    skillsError.value = `Failed to load skills: ${e?.message || e}`
  } finally {
    skillsLoaded.value = true
  }
}

// ── Automation tab ─────────────────────────────────────────────────────────
const automationItems = ref<AutomationProcess[]>([])
const automationLoaded = ref(false)
const automationError = ref('')
const expandedJobs = ref<Record<string, boolean>>({})

function isJobExpanded(job: string) {
  return expandedJobs.value[job] || false
}
function toggleJob(job: string) {
  expandedJobs.value[job] = !isJobExpanded(job)
}

const automationGroups = computed(() => [
  {
    key: 'content',
    title: 'Content automations',
    items: automationItems.value.filter((i) => i.category === 'content'),
  },
  {
    key: 'system',
    title: 'System',
    items: automationItems.value.filter((i) => i.category === 'system'),
  },
])

function statusLabel(item: AutomationProcess): string {
  return item.last_run ? item.last_run.status : 'never run'
}
function badgeClass(status: string | undefined): string {
  if (status === 'ok') return '--success'
  if (status === 'error') return '--error'
  if (status === 'skipped') return '--warn'
  return '--muted'
}
function statusBadgeClass(item: AutomationProcess): string {
  return badgeClass(item.last_run?.status)
}
function runBadgeClass(status: string): string {
  return badgeClass(status)
}
function lastRunLabel(item: AutomationProcess): string {
  if (!item.last_run) return ''
  return formatTime(item.last_run.ended_at || item.last_run.started_at)
}
function lastError(item: AutomationProcess): string {
  return item.stats.last_error?.error || ''
}

async function fetchAutomation() {
  automationError.value = ''
  try {
    automationItems.value = await api.get<AutomationProcess[]>('/api/automation')
  } catch (e: any) {
    automationError.value = `Failed to load automation: ${e?.message || e}`
  } finally {
    automationLoaded.value = true
  }
}


const pushSupportedFlag = ref(false)
const pushEnabledFlag = ref(false)
const pushPending = ref(false)
const pushError = ref('')
const permissionDenied = ref(false)
const needsIosInstall = ref(false)

function isIos(): boolean {
  return /iphone|ipad|ipod/i.test(navigator.userAgent)
}
function isStandalone(): boolean {
  return (
    (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) ||
    (navigator as any).standalone === true
  )
}

onMounted(async () => {
  loadAppearanceSettings()
  fetchSkills()
  fetchLocalStatus()
  fetchRoutines()
  fetchAutomation()
  fetchPackageStatus()
  fetchProviderKeys()
  pushSupportedFlag.value = pushSupported()
  if (isIos() && !isStandalone()) {
    needsIosInstall.value = true
  }
  if (typeof Notification !== 'undefined' && Notification.permission === 'denied') {
    permissionDenied.value = true
  }
  if (pushSupportedFlag.value) {
    pushEnabledFlag.value = await isPushEnabled()
    // Self-heal: if the browser thinks it has a subscription but the server
    // forgot it (state file moved, fresh deploy), silently re-register so
    // pushes actually arrive without making the user click anything.
    if (pushEnabledFlag.value && Notification.permission === 'granted') {
      try {
        const sub = await currentSubscription()
        if (sub) {
          const r = await api.get<{ registered: boolean }>(
            `/api/push/subscription?endpoint=${encodeURIComponent(sub.endpoint)}`
          )
          if (!r.registered) {
            await api.post('/api/push/subscribe', { subscription: sub.toJSON() })
          }
        }
      } catch { /* best-effort */ }
    }
  }
})


async function togglePush() {
  pushPending.value = true
  pushError.value = ''
  try {
    if (pushEnabledFlag.value) {
      await disablePush()
      pushEnabledFlag.value = false
    } else {
      await enablePush()
      pushEnabledFlag.value = true
    }
  } catch (e: any) {
    pushError.value = e?.message || String(e)
  } finally {
    pushPending.value = false
  }
}

async function doSnapshot(confirmWarnings = false) {
  actionPending.value = 'snapshot'
  actionResult.value = ''
  deploySteps.value = []
  try {
    const r = await api.post<{ message: string }>('/api/admin/snapshot', { confirm_warnings: confirmWarnings })
    actionResult.value = r.message
  } catch (e: any) {
    const payload = e?.payload
    if (payload?.blockers) {
      alert(`Snapshot blocked by secrets:\n\n${payload.blockers.join('\n')}`)
      actionResult.value = 'Blocked by secrets.'
    } else if (payload?.warnings) {
      if (confirm(`Warnings found:\n\n${payload.warnings.join('\n')}\n\nDo you want to proceed anyway?`)) {
        actionPending.value = null
        return doSnapshot(true)
      }
      actionResult.value = 'Cancelled by user due to warnings.'
    } else {
      actionResult.value = `Error: ${e.message}`
    }
  }
  actionPending.value = null
}

async function doDeploy(confirmWarnings = false) {
  if (!confirmWarnings && !confirm('Redeploy? This will pull latest, rebuild, and restart.')) return
  actionPending.value = 'deploy'
  actionResult.value = ''
  deploySteps.value = []
  try {
    const r = await api.post<DeployResult>('/api/admin/deploy', { confirm_warnings: confirmWarnings })
    deploySteps.value = r.steps
    if (r.ok) {
      actionResult.value = 'Deploy complete. Page will reload shortly...'
      setTimeout(() => location.reload(), 10000)
    } else {
      actionResult.value = 'Deploy failed. See steps above.'
    }
  } catch (e: any) {
    const payload = e?.payload
    if (Array.isArray(payload?.steps)) deploySteps.value = payload.steps
    if (payload?.blockers) {
      alert(`Deploy blocked by secrets:\n\n${payload.blockers.join('\n')}`)
      actionResult.value = 'Blocked by secrets.'
    } else if (payload?.warnings) {
      if (confirm(`Warnings found:\n\n${payload.warnings.join('\n')}\n\nDo you want to proceed anyway?`)) {
        actionPending.value = null
        return doDeploy(true)
      }
      actionResult.value = 'Cancelled by user due to warnings.'
    } else {
      actionResult.value = `Error: ${e.message || 'unknown error'}`
    }
  }
  actionPending.value = null
}

async function doLogout() {
  // Clears the HttpOnly session cookie via /api/auth/logout (which JS can't
  // delete from document.cookie). After success the auth store routes back
  // to /login and the next login re-issues the cookie with the wider
  // Host-only cookie: no Domain attribute, scoped to the exact host.
  if (!confirm('Log out of CiaoBot?')) return
  actionPending.value = 'logout'
  actionResult.value = ''
  try {
    await useAuthStore().logout()
  } catch (e: any) {
    actionResult.value = `Error: ${e.message || 'logout failed'}`
  }
  actionPending.value = null
}

// ── Commit to main (branch-per-device) ───────────────────────────────────
const localStatus = ref<LocalStatus | null>(null)
const localPending = ref<string | null>(null)
const localResult = ref('')

async function fetchLocalStatus() {
  try {
    localStatus.value = await api.get<LocalStatus>('/api/local/status')
  } catch {
    /* leave null on failure */
  }
}

async function localHandback(confirmWarnings = false) {
  const isDirect = !!localStatus.value?.direct_main
  const promptMsg = isDirect ? 'Sync changes with the remote repository?' : 'Commit this device\'s branch to main?'
  if (!confirmWarnings && !confirm(promptMsg)) return

  if (isDirect) {
    actionPending.value = 'snapshot'
    actionResult.value = ''
  } else {
    localPending.value = 'handback'
    localResult.value = ''
  }

  try {
    const r = await api.post<any>('/api/local/handback', { confirm_warnings: confirmWarnings })
    if (r?.ok === false) {
      const errText = `${r.step}: ${r.error}`
      if (isDirect) actionResult.value = errText
      else localResult.value = errText
    } else if (r?.merged === true) {
      const successText = isDirect ? 'Synced with remote repository.' : 'Merged to main.'
      if (isDirect) actionResult.value = successText
      else localResult.value = successText
    } else if (r?.conflict === true) {
      const conflictText = isDirect
        ? 'Sync conflict — opened a chat to resolve it. Answer it, then Sync again.'
        : 'Merge conflict — opened a chat to resolve it. Answer it, then Sync to main.'
      if (isDirect) actionResult.value = conflictText
      else localResult.value = conflictText
    }
    await fetchLocalStatus()
  } catch (e: any) {
    const payload = e?.payload
    if (payload?.blockers) {
      alert(`${isDirect ? 'Sync' : 'Commit'} blocked by secrets:\n\n${payload.blockers.join('\n')}`)
      if (isDirect) actionResult.value = 'Blocked by secrets.'
      else localResult.value = 'Blocked by secrets.'
    } else if (payload?.warnings) {
      if (confirm(`Warnings found:\n\n${payload.warnings.join('\n')}\n\nDo you want to proceed anyway?`)) {
        if (isDirect) actionPending.value = null
        else localPending.value = null
        return localHandback(true)
      }
      const cancelText = 'Cancelled by user due to warnings.'
      if (isDirect) actionResult.value = cancelText
      else localResult.value = cancelText
    } else {
      const errText = `Error: ${e.message || 'sync failed'}`
      if (isDirect) actionResult.value = errText
      else localResult.value = errText
    }
  }
  if (isDirect) actionPending.value = null
  else localPending.value = null
}

async function localResync() {
  localPending.value = 'resync'
  localResult.value = ''
  try {
    const r = await api.post<{ ok: boolean; detail: string }>('/api/local/resync')
    localResult.value = r?.detail || (r?.ok ? 'Synced to main.' : 'Sync failed.')
    await fetchLocalStatus()
  } catch (e: any) {
    localResult.value = `Error: ${e.message || 'resync failed'}`
  }
  localPending.value = null
}

// ── Package Update ────────────────────────────────────────────────────────
const packageStatus = ref<any>(null)
const packageLoading = ref(false)
const packageUpdating = ref(false)
const packageResult = ref('')

async function fetchPackageStatus() {
  packageLoading.value = true
  try {
    packageStatus.value = await api.get<any>('/api/package/status')
  } catch (e: any) {
    // best-effort
  } finally {
    packageLoading.value = false
  }
}

async function doPackageUpdate() {
  if (!confirm('Upgrade Ciao package and restart?')) return
  packageUpdating.value = true
  packageResult.value = 'Upgrading ciao package...'
  try {
    const res = await api.post<any>('/api/package/update')
    if (res.ok) {
      packageResult.value = 'Upgrade complete. Page will reload shortly...'
      setTimeout(() => location.reload(), 10000)
    } else {
      packageResult.value = `Upgrade failed: ${res.error || 'unknown error'}`
    }
  } catch (e: any) {
    packageResult.value = `Upgrade failed: ${e.message || 'unknown error'}`
  } finally {
    packageUpdating.value = false
    await fetchPackageStatus()
  }
}

</script>

<style scoped>
.settings-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
}
.pane-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.loading {
  color: var(--fg2);
  font-size: var(--text-base);
}

.update-list {
  margin: 0;
  padding-left: 18px;
  font-size: var(--text-base);
  line-height: 1.5;
}

.update-list code {
  font-size: var(--text-sm);
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--bg);
  color: var(--fg);
}

.action-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.action-row > button {
  flex: 1 1 0;
}

.action-result {
  font-size: var(--text-sm);
  color: var(--fg2);
  padding: 4px 0;
}

.deploy-steps {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 4px;
}

.deploy-step {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-sm);
  padding: 4px 8px;
  border-radius: 4px;
  background: var(--bg);
}

.deploy-step.ok { color: var(--success); }
.deploy-step.fail { color: var(--error); }

.step-icon { font-size: var(--text-base); }

.step-output {
  color: var(--fg2);
  font-size: var(--text-xs);
  margin-left: auto;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pause-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: var(--text-sm);
  font-weight: 600;
  background: var(--success, #4caf50);
  color: white;
}

.instance-toggle {
  display: flex;
  gap: 0;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--border);
  margin-top: 4px;
}
.toggle-btn {
  flex: 1;
  padding: 10px 16px;
  font-size: var(--text-sm);
  font-weight: 600;
  border: none;
  cursor: pointer;
  background: var(--bg);
  color: var(--fg);
  transition: background 0.15s, color 0.15s;
}
.toggle-btn:not(:last-child) {
  border-right: 1px solid var(--border);
}
.toggle-btn.active {
  background: var(--accent);
  color: white;
}
.toggle-btn:disabled {
  opacity: 0.6;
  cursor: default;
}
.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: middle;
}
.status-dot.online {
  background: #4caf50;
}
.status-dot.offline {
  background: var(--error);
}

.routine-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
  border-top: 1px solid var(--border);
  margin-top: 10px;
}
.routine-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.routine-name {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--fg);
}
.routine-detail {
  font-size: var(--text-xs);
  color: var(--fg2);
  line-height: 1.35;
}
.routine-detail code {
  font-size: var(--text-xs);
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--bg);
  color: var(--fg);
}
.routine-select,
.routine-input {
  flex-shrink: 0;
  max-width: 46%;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg);
  color: var(--fg);
  font-size: var(--text-sm);
  /* 44px min tap target height on mobile is handled by padding + font */
  min-height: 32px;
}
@media (max-width: 520px) {
  .routine-row {
    flex-direction: column;
    align-items: stretch;
  }
  .routine-select,
  .routine-input {
    max-width: none;
    min-height: 44px;
  }
}

.dev-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: var(--space-3);
  font-size: var(--text-base);
}
.dev-label {
  display: inline-block;
  min-width: 84px;
  color: var(--fg2);
}

.skill-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: var(--space-3);
}
.skill-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  cursor: pointer;
}
.skill-main {
  min-width: 0;
}
.skill-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.skill-name {
  color: var(--fg);
  font-size: var(--text-sm);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.skill-description,
.skill-source {
  margin: 4px 0 0;
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.35;
}
.skill-description {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.skill-source {
  color: var(--fg2);
  opacity: 0.7;
}
.skill-chevron {
  font-size: var(--text-xs);
  color: var(--fg2);
  flex-shrink: 0;
}
.skill-detail {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.skill-meta {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: var(--text-xs);
  color: var(--fg2);
  margin: 0;
}
.skill-meta-label {
  display: inline-block;
  min-width: 84px;
  color: var(--fg2);
  opacity: 0.7;
  flex-shrink: 0;
}
.skill-targets-inline {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.skill-targets {
  display: flex;
  flex-wrap: wrap;
  align-content: flex-start;
  justify-content: flex-end;
  gap: 4px;
  flex: 0 0 auto;
}
.skill-target {
  padding: 2px 6px;
  border-radius: 999px;
  background: var(--bg3);
  color: var(--fg2);
  font-size: var(--text-xs);
}

.cost-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.cost-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: var(--text-sm);
  color: var(--fg);
}
.cost-row.total {
  border-top: 1px solid var(--border);
  padding-top: 6px;
  margin-top: 2px;
  font-weight: 600;
}
.cost-sub {
  color: var(--fg2);
  font-weight: 400;
}
.cost-subgrid {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}
.cost-subtitle {
  margin: 0 0 2px 0;
  font-size: var(--text-xs);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

/* Subscription rate-limit buckets */
.limits-grid {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.limit-row {
  display: grid;
  grid-template-columns: 120px 1fr auto;
  gap: 10px;
  align-items: center;
  font-size: var(--text-sm);
}
.limit-label {
  color: var(--fg);
  font-weight: 500;
}
.limit-bar-wrap {
  height: 8px;
  background: var(--border);
  border-radius: 4px;
  overflow: hidden;
}
.limit-bar {
  height: 100%;
  background: #4caf50;
  transition: width 0.3s ease;
}
.limit-row.status-allowed_warning .limit-bar {
  background: #f39c12;
}
.limit-row.status-rejected .limit-bar {
  background: #e74c3c;
}
.limit-row.unreported,
.limit-row.nopct {
  opacity: 0.55;
}
.limit-row.unreported .limit-bar-wrap,
.limit-row.nopct .limit-bar-wrap {
  background: var(--border);
  opacity: 0.5;
}
.limit-meta {
  color: var(--fg2);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
@media (max-width: 520px) {
  .limit-row { grid-template-columns: 80px 1fr auto; }
}

/* Automation tab */
.auto-intro {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.auto-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: var(--space-3);
}
.auto-row {
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  cursor: pointer;
}
.auto-row:hover {
  border-color: var(--accent);
}
.auto-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.auto-chevron {
  font-size: var(--text-xs);
  color: var(--fg2);
  flex-shrink: 0;
}
.auto-name {
  color: var(--fg);
  font-size: var(--text-sm);
  font-weight: 600;
}
.auto-when {
  margin-left: auto;
  color: var(--fg2);
  font-size: var(--text-xs);
  white-space: nowrap;
}
.auto-sub {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 6px;
}
.auto-meta {
  color: var(--fg2);
  font-size: var(--text-xs);
}
.auto-desc {
  margin: 6px 0 0;
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.35;
}
.auto-error {
  margin-top: 8px;
  padding: 8px;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--error) 12%, transparent);
  color: var(--error);
  font-size: var(--text-xs);
  white-space: pre-wrap;
  word-break: break-word;
}
.auto-detail {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.auto-run {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: var(--text-xs);
  color: var(--fg2);
}
.auto-run-when,
.auto-run-model {
  color: var(--fg2);
}
.auto-run-error {
  width: 100%;
  color: var(--error);
  white-space: pre-wrap;
  word-break: break-word;
}

.font-scale-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: 4px;
}
.font-scale-display {
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--fg);
  min-width: 48px;
  text-align: center;
}
</style>
