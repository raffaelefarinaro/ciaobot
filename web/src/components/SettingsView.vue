<template>
  <div class="settings-pane">
    <RestartOverlay v-if="restarting" :message="restartMessage" />
    <PaneHeader title="Settings" @open-sidebar="emit('open-sidebar')" />
    <div class="pane-body">

      <!-- HOME TAB -->
      <template v-if="currentTab === 'home'">
        <!-- Actions -->
        <div class="card">
          <div class="settings-card-header">
            <p class="section-title">App actions</p>
            <p class="hint">Snapshot, sync, or restart this local Ciaobot instance.</p>
          </div>
          <div class="action-row action-row--spaced">
            <button class="btn-primary" @click="() => localStatus?.git_repo ? localHandback() : doSnapshot()" :disabled="!!actionPending">
              {{ actionPending === 'snapshot' ? (localStatus?.git_repo ? 'Syncing...' : 'Snapshotting...') : (localStatus?.git_repo ? 'Sync with Remote' : 'Git Snapshot') }}
            </button>
            <button class="btn-primary" @click="() => doDeploy()" :disabled="!!actionPending" title="Pull latest, reinstall deps, rebuild the frontend, and restart with the latest code">
              {{ actionPending === 'deploy' ? 'Restarting...' : 'Restart' }}
            </button>
          </div>
          <div v-if="actionResult" class="action-result" :class="{ 'action-result--error': hasDeployError }">{{ actionResult }}</div>
          <div v-if="hasDeployError" class="deploy-steps">
            <div v-for="step in deploySteps.filter(s => !s.ok)" :key="step.step" class="deploy-step fail">
              <span class="step-icon">&#10007;</span>
              <div style="flex: 1; min-width: 0;">
                <strong>{{ step.step }} failed</strong>
                <pre v-if="step.output" class="deploy-step-error-output">{{ step.output }}</pre>
              </div>
            </div>
            <div class="action-row action-row--spaced">
              <button class="btn-primary" @click="fixDeployErrorInChat">
                Fix in Chat
              </button>
            </div>
          </div>
        </div>

        <!-- Workspace health -->
        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">Workspace health</p>
              <p class="hint">Checks Claude Code discovery files, vault writability, and generated asset links.</p>
            </div>
            <span class="badge" :class="healthBadgeClass(workspaceHealth?.status || '')">
              {{ workspaceHealth?.status || (agentAssetsLoaded ? 'unknown' : 'loading') }}
            </span>
          </div>
          <div v-if="!agentAssetsLoaded" class="action-row"><span class="loading">Scanning&hellip;</span></div>
          <p v-else-if="agentAssetsError" class="hint hint--warn">{{ agentAssetsError }}</p>
          <div v-else-if="workspaceHealth && prioritizedHealthChecks.length" class="health-list">
            <div
              v-for="check in prioritizedHealthChecks"
              :key="check.id"
              class="health-row"
              :class="`health-row--${check.status}`"
            >
              <span class="health-dot" aria-hidden="true"></span>
              <div class="health-main">
                <div class="health-title-row">
                  <span class="health-title">{{ check.title }}</span>
                  <span v-if="check.path" class="health-path">{{ check.path }}</span>
                </div>
                <p class="hint hint--compact">{{ check.detail }}</p>
                <p v-if="check.action" class="hint hint--compact hint--warn">{{ check.action }}</p>
              </div>
            </div>
            <div v-if="workspaceHealth.status !== 'ok'" class="action-row">
              <button
                id="workspace-health-fix"
                class="btn-primary"
                :disabled="healthFixPending"
                @click="fixWorkspaceHealth"
              >{{ healthFixPending ? 'Fixing…' : 'Fix issues' }}</button>
              <span v-if="healthFixError" class="hint hint--warn">{{ healthFixError }}</span>
            </div>
          </div>
        </div>

        <!-- Package update -->
        <div class="card">
          <div class="settings-card-header">
            <p class="section-title">Package update</p>
            <p class="hint">Check the installed package version and upgrade this local app.</p>
          </div>
          <div v-if="packageLoading && !packageStatus" class="loading">
            Checking package status...
          </div>
          <div v-else-if="packageStatus">
            <div v-if="packageStatus.error" class="hint hint--warn hint--spaced">
              Update check failed: {{ packageStatus.error }}
            </div>

            <div class="action-row action-row--spaced">
              <button
                class="btn-primary"
                @click="openUpdatePanel"
                :disabled="!packageStatus.update_available || packageUpdating || showUpdatePanel"
              >
                {{ packageStatus.update_available
                    ? `Update to ${packageStatus.latest_version}`
                    : 'Up to date' }}
              </button>
            </div>

            <div v-if="showUpdatePanel" class="settings-form-panel">
              <p class="section-title">What&rsquo;s new in {{ packageStatus.latest_version }}</p>
              <div v-if="changelogLoading" class="loading">Loading changelog&hellip;</div>
              <template v-else>
                <ul v-if="changelog.commits && changelog.commits.length" class="changelog-list">
                  <li v-for="c in changelog.commits" :key="c.sha || c.subject">
                    <code v-if="c.sha" class="changelog-sha">{{ c.sha }}</code>
                    <span class="changelog-subject">{{ c.subject }}</span>
                  </li>
                </ul>
                <p v-else class="hint">
                  {{ changelog.error
                      ? `Could not load changelog: ${changelog.error}`
                      : 'No changelog details available.' }}
                </p>
                <p v-if="changelog.compare_url" class="hint hint--spaced">
                  <a :href="changelog.compare_url" target="_blank" rel="noopener">View full diff on GitHub</a>
                </p>
              </template>
              <div class="action-row settings-actions">
                <button class="btn-primary" @click="doPackageUpdate" :disabled="packageUpdating">
                  {{ packageUpdating ? 'Updating&hellip;' : 'Update &amp; Restart' }}
                </button>
                <button class="btn-small" @click="showUpdatePanel = false" :disabled="packageUpdating">
                  Cancel
                </button>
              </div>
            </div>
          </div>
          <div v-if="packageResult" class="action-result">{{ packageResult }}</div>
        </div>

        <!-- Notifications -->
        <div class="card">
          <div class="settings-card-header">
            <p class="section-title">Notifications</p>
            <p class="hint">
              Get a notification when a chat replies and the app is not focused.
            </p>
          </div>
          <div v-if="needsIosInstall" class="hint hint--warn">
            On iOS, push notifications only work after you "Add to Home Screen" and open the app from there.
          </div>
          <div v-else-if="permissionDenied" class="hint hint--warn">
            Notifications are blocked at the OS level. Re-enable them in your phone's Settings &rarr; Notifications &rarr; Ciaobot.
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
        </div>

        <!-- Appearance -->
        <div class="card">
          <div class="settings-card-header">
            <p class="section-title">Appearance</p>
            <p class="hint">Control the visual theme and type scale used across Ciaobot.</p>
          </div>
          <div class="setting-row setting-row--inline setting-row--flush">
            <div class="routine-info">
              <span class="routine-name">Theme</span>
              <span class="routine-detail">Choose light, dark, or match the device appearance.</span>
            </div>
            <div class="settings-control">
              <div class="instance-toggle">
                <button
                  class="toggle-btn"
                  :class="{ active: activeTheme === 'dark' }"
                  @click="setTheme('dark')"
                >
                  Dark
                </button>
                <button
                  class="toggle-btn"
                  :class="{ active: activeTheme === 'light' }"
                  @click="setTheme('light')"
                >
                  Light
                </button>
                <button
                  class="toggle-btn"
                  :class="{ active: activeTheme === 'system' }"
                  @click="setTheme('system')"
                >
                  System
                </button>
              </div>
            </div>
          </div>
          <div class="setting-row setting-row--inline">
            <div class="routine-info">
              <span class="routine-name">Font size</span>
              <span class="routine-detail">Adjust messages, code blocks, sidebars, and menus together.</span>
            </div>
            <div class="settings-control">
              <div class="font-scale-row">
                <button class="btn-small" @click="adjustFontScale(-0.05)" :disabled="fontScale <= 0.8">Decrease</button>
                <span class="font-scale-display">{{ Math.round(fontScale * 100) }}%</span>
                <button class="btn-small" @click="adjustFontScale(0.05)" :disabled="fontScale >= 1.5">Increase</button>
                <button class="btn-small font-reset" @click="resetFontScale" :disabled="fontScale === 1.0">Reset</button>
              </div>
            </div>
          </div>
        </div>

        <!-- Product tour -->
        <div class="card">
          <div class="settings-card-header">
            <p class="section-title">Product tour</p>
            <p class="hint">Walk through workspaces, chat comments, inline file previews, pinning, and rich document viewing.</p>
          </div>
          <div class="action-row action-row--spaced">
            <button type="button" class="btn-primary" @click="replayProductTour">Replay tour</button>
          </div>
        </div>

        <!-- Getting started checklist -->
        <GettingStartedChecklist variant="settings" @open-sidebar="emit('open-sidebar')" />

        <!-- Debug (dev mode only) -->
        <div v-if="localStatus?.dev_mode" class="card">
          <div class="settings-card-header">
            <p class="section-title">Debug</p>
            <p class="hint">Runtime issue log: server errors and failed background jobs. Send it to a chat so the agent can self-fix.</p>
          </div>
          <div class="action-row action-row--spaced">
            <button class="btn-primary" @click="fixIssuesInChat" :disabled="debugPending">
              {{ debugPending ? 'Collecting issues...' : 'Fix issues in chat' }}
            </button>
            <button class="btn-small" @click="refreshDebugIssues" :disabled="debugPending">Refresh</button>
          </div>
          <div v-if="debugSummary" class="action-result">{{ debugSummary }}</div>
        </div>

        <!-- Open source -->
        <div class="card">
          <div class="settings-card-header">
            <p class="section-title">Open source</p>
            <p class="hint">
              Ciaobot is an open-source project. Support and contributions are welcome:
              report issues, suggest features, or open a pull request on
              <a href="https://github.com/raffaelefarinaro/ciaobot" target="_blank" rel="noopener">GitHub</a>.
            </p>
          </div>
        </div>
      </template>


      <!-- MODELS TAB -->
      <template v-if="currentTab === 'models'">
        <div v-if="!routinesLoaded" class="card"><span class="loading">Loading&hellip;</span></div>
        <template v-else-if="routinesError">
          <div class="card"><p class="hint hint--warn">{{ routinesError }}</p></div>
        </template>
        <template v-else-if="routines">
          <!-- Internal routines -->
          <div class="card">
            <div class="settings-card-header">
              <p class="section-title">Internal models</p>
              <p class="hint">
                These tasks use their own model setting, separate from the active chat model.
                "Automatic" keeps the built-in default. Local Ollama models run on this machine.
                System automations without a model picker are tracked in Settings &rarr; Automation.
              </p>
            </div>
            <div v-if="routines.workspace_context" class="routine-context">
              <div>
                <div class="settings-label-row">
                  <span class="dev-label">Main workspace</span>
                  <details class="field-info">
                    <summary aria-label="How to change the main workspace" title="How to change the main workspace">i</summary>
                    <div class="field-info-panel">
                      <p>
                        This is the server filesystem root for routines, skills, scripts, and runtime state.
                        Set <code>CIAO_WORKSPACE</code> in your <code>.env</code> file, then restart Ciaobot.
                      </p>
                      <p>
                        Logical chat workspaces (sidebar switcher) are managed separately under Settings &rarr; Workspaces.
                      </p>
                    </div>
                  </details>
                </div>
                <code>{{ routines.workspace_context.workspace_root }}</code>
              </div>
            </div>

            <div class="routine-row">
              <div class="routine-info">
                <span class="routine-name">Chat titles</span>
                <span class="routine-detail">Names a new chat after the first message.</span>
                <div v-if="getJobTelemetry('title')" class="routine-telemetry">
                  <span class="badge" :class="getJobBadgeClass('title')">
                    {{ getJobStatus('title') }}
                  </span>
                  <span v-if="hasJobLastRun('title')" class="telemetry-meta">
                    Last run: {{ getJobLastRunLabel('title') }} ({{ getJobDuration('title') }})
                  </span>
                  <span v-if="getJobStatus('title') === 'error' && getJobLastError('title')" class="telemetry-error" :title="getJobLastError('title')">
                    &middot; {{ getJobLastError('title') }}
                  </span>
                </div>
              </div>
              <div
                class="routine-model-controls"
                :class="{ 'routine-model-controls--single': routineProviderValue('title_model') === 'apple' }"
              >
                <select
                  class="routine-select routine-select--provider"
                  :value="routineProviderValue('title_model')"
                  :disabled="routinesSaving"
                  @change="saveRoutineProvider('title_model', ($event.target as HTMLSelectElement).value)"
                >
                  <option value="automatic">Automatic</option>
                  <option value="apple">Local (free)</option>
                  <option v-for="provider in aliasProviderSections" :key="provider.key" :value="provider.key">
                    {{ provider.label }}
                  </option>
                  <option v-if="routineProviderValue('title_model') === 'custom'" value="custom">Custom model</option>
                </select>
                <select
                  v-if="routineTierSelectable('title_model')"
                  class="routine-select routine-select--tier"
                  :value="routineTierValue('title_model')"
                  :disabled="routinesSaving"
                  @change="saveRoutineTier('title_model', ($event.target as HTMLSelectElement).value)"
                >
                  <option v-for="tier in modelTiers" :key="`title-${tier.key}`" :value="tier.key">
                    {{ tier.label }}
                  </option>
                </select>
                <span class="routine-model-hint">
                  <template v-if="routineProviderValue('title_model') === 'apple'">
                    Runs on-device for free via <a :href="APFEL_REPO_URL" target="_blank" rel="noopener">apfel</a> (Apple Intelligence CLI).
                  </template>
                  <template v-else>{{ routineModelSummary('title_model') }}</template>
                </span>
              </div>
            </div>

            <div class="routine-row">
              <div class="routine-info">
                <span class="routine-name">Session insights</span>
                <span class="routine-detail">Extracts learnings when a chat is archived and appends them to that archive.</span>
                <div v-if="getJobTelemetry('insights')" class="routine-telemetry">
                  <span class="badge" :class="getJobBadgeClass('insights')">
                    {{ getJobStatus('insights') }}
                  </span>
                  <span v-if="hasJobLastRun('insights')" class="telemetry-meta">
                    Last run: {{ getJobLastRunLabel('insights') }} ({{ getJobDuration('insights') }})
                  </span>
                  <span v-if="getJobStatus('insights') === 'error' && getJobLastError('insights')" class="telemetry-error" :title="getJobLastError('insights')">
                    &middot; {{ getJobLastError('insights') }}
                  </span>
                </div>
              </div>
              <div class="routine-model-controls">
                <select
                  class="routine-select routine-select--provider"
                  :value="routineProviderValue('insights_model')"
                  :disabled="routinesSaving"
                  @change="saveRoutineProvider('insights_model', ($event.target as HTMLSelectElement).value)"
                >
                  <option value="automatic">Automatic</option>
                  <option v-for="provider in aliasProviderSections" :key="provider.key" :value="provider.key">
                    {{ provider.label }}
                  </option>
                  <option v-if="routineProviderValue('insights_model') === 'custom'" value="custom">Custom model</option>
                </select>
                <select
                  class="routine-select routine-select--tier"
                  :value="routineTierValue('insights_model')"
                  :disabled="routinesSaving || !routineTierSelectable('insights_model')"
                  @change="saveRoutineTier('insights_model', ($event.target as HTMLSelectElement).value)"
                >
                  <option v-for="tier in modelTiers" :key="`insights-${tier.key}`" :value="tier.key">
                    {{ tier.label }}
                  </option>
                </select>
                <span class="routine-model-hint">{{ routineModelSummary('insights_model') }}</span>
              </div>
            </div>

            <div class="routine-row">
              <div class="routine-info">
                <span class="routine-name">Critique models</span>
                <span class="routine-detail">Select one or more models for adversarial review.</span>
              </div>
              <div class="critique-model-picker">
                <div class="critique-picker-header">
                  <div class="critique-picker-summary">
                    <div v-if="selectedCritiqueModels.length" class="critique-chip-list">
                      <button
                        v-for="model in selectedCritiqueModels"
                        :key="model"
                        type="button"
                        class="critique-chip"
                        :disabled="routinesSaving"
                        title="Remove model"
                        @click="removeCritiqueModel(model)"
                      >
                        <span>{{ model }}</span>
                        <span>&times;</span>
                      </button>
                    </div>
                    <span v-else>Automatic default</span>
                  </div>
                  <button
                    type="button"
                    class="btn-small"
                    :disabled="routinesSaving || selectedCritiqueModels.length === 0"
                    @click="setCritiqueModels([])"
                  >
                    Reset
                  </button>
                </div>
                <ModelSelector
                  multiple
                  :model-value="selectedCritiqueModels"
                  :sections="critiqueModelSections"
                  placeholder="Select critique models"
                  empty-placeholder="Automatic default"
                  :disabled="routinesSaving"
                  @update:model-value="setCritiqueModels"
                />
              </div>
            </div>
          </div>

          <!-- Voice: hear (dictation) and speak (read aloud) -->
          <div class="card">
            <div class="settings-card-header">
              <p class="section-title">Voice</p>
              <p class="hint">Choose the engines used to hear you (dictation) and to speak messages aloud.</p>
            </div>
            <div class="routine-row routine-row--flush">
              <div class="routine-info">
                <span class="routine-name">Hear</span>
              </div>
              <div class="routine-model-controls routine-model-controls--single">
                <select
                  class="routine-select"
                  :value="routines.transcription.engine"
                  :disabled="routinesSaving"
                  @change="saveRoutines({ transcription_engine: ($event.target as HTMLSelectElement).value })"
                >
                  <option value="local">Local (free)</option>
                  <option value="cloud" :disabled="!routines.transcription.cloud_available">Cloud (OpenAI)</option>
                </select>
                <span class="routine-model-hint">
                  <template v-if="routines.transcription.engine === 'local'">
                    Dictation runs on-device via mlx-whisper (<code>{{ routines.transcription.local_model }}</code>).
                    The first transcription downloads the model.
                  </template>
                  <template v-else>
                    Dictation uses the OpenAI transcription API (needs <code>OPENAI_API_KEY</code>, ~$0.003/min).
                  </template>
                </span>
              </div>
            </div>
            <p v-if="!routines.transcription.local_available" class="hint hint--warn voice-warning">
              <span v-if="routines.transcription.engine === 'local'">
                <strong>Local Whisper engine is selected but not installed.</strong> Run <code>pip install 'ciao[voice-local]'</code> or install now:
              </span>
              <span v-else>
                Local engine is available for Apple Silicon (requires installing <code>mlx-whisper</code>).
              </span>
              <button
                class="btn-primary btn-small voice-install-btn"
                :disabled="voiceInstalling"
                @click="installLocalVoice"
              >
                {{ voiceInstalling ? 'Installing...' : 'Install engine' }}
              </button>
            </p>
            <div class="routine-row routine-row--flush">
              <div class="routine-info">
                <span class="routine-name">Speak</span>
              </div>
              <div class="routine-model-controls routine-model-controls--single">
                <select
                  class="routine-select"
                  :value="routines.speech.engine"
                  :disabled="routinesSaving"
                  @change="saveRoutines({ tts_engine: ($event.target as HTMLSelectElement).value })"
                >
                  <option value="local">Local (free)</option>
                  <option value="cloud" :disabled="!routines.speech.cloud_available">Cloud (OpenAI)</option>
                </select>
                <span class="routine-model-hint">
                  <template v-if="routines.speech.engine === 'local'">
                    Read-aloud runs on-device via Kokoro (voice <code>{{ routines.speech.local_voice }}</code>).
                    The first playback downloads the model (~340 MB).
                  </template>
                  <template v-else>
                    Read-aloud uses the OpenAI speech API (needs <code>OPENAI_API_KEY</code>, voice <code>{{ routines.speech.cloud_voice }}</code>, ~$0.015/min).
                  </template>
                </span>
              </div>
            </div>
            <p v-if="!routines.speech.local_available" class="hint hint--warn voice-warning">
              <span v-if="routines.speech.engine === 'local'">
                <strong>Local Kokoro engine is selected but not installed.</strong> Run <code>pip install 'ciao[tts-local]'</code> or install now:
              </span>
              <span v-else>
                Local speech engine is available (requires installing <code>kokoro-onnx</code>).
              </span>
              <button
                class="btn-primary btn-small voice-install-btn"
                :disabled="ttsInstalling"
                @click="installLocalTts"
              >
                {{ ttsInstalling ? 'Installing...' : 'Install engine' }}
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
          <div class="card"><p class="hint hint--warn">{{ providerKeysError }}</p></div>
        </template>
        <template v-else-if="providerKeys">
          <div class="card">
            <div class="settings-card-header">
              <div>
                <p class="section-title">Providers</p>
                <p class="hint">
                  Manage API keys and developer credentials. Changes are written to your local <code>.env</code> file and restart the server.
                </p>
              </div>
            </div>

            <div v-if="providerKeys.connections" class="setting-row setting-row--stack">
              <div v-for="(conn, connKey) in providerKeys.connections" :key="connKey" class="setting-row-main setting-row-main--inline">
                <span class="routine-name">Codex <span class="muted-text">via Pi</span></span>
                <span class="badge" :class="conn.ok ? 'badge--success' : 'badge--error'">
                  {{ conn.ok ? 'Connected' : 'Not connected' }}
                </span>
              </div>
              <p v-if="providerKeys.connections.codex?.detail" class="hint hint--compact">
                {{ providerKeys.connections.codex.detail }}
              </p>
            </div>

            <div v-for="(meta, key) in providerKeys.keys" :key="key" class="credential-row">
              <div class="setting-row-main setting-row-main--inline">
                <div class="routine-info">
                  <span class="routine-name">{{ meta.label }}</span>
                  <p class="hint hint--compact">{{ meta.description }}</p>
                </div>
                <span class="badge" :class="meta.configured ? 'badge--success' : 'badge--error'">
                  {{ meta.configured ? (meta.auth_method === 'oauth' ? 'OAuth' : 'Configured') : 'Unconfigured' }}
                </span>
              </div>
              <input
                type="password"
                class="routine-input"
                v-model="providerKeyInputs[key]"
                :placeholder="meta.configured ? '•••••••••••• (Leave blank to keep existing, or type empty space to clear)' : 'Enter API Key'"
                :disabled="providerKeysSaving"
              />
            </div>


            <div class="action-row settings-actions">
              <button class="btn-primary" @click="saveProviderKeys" :disabled="providerKeysSaving">
                {{ providerKeysSaving ? 'Saving...' : 'Save Keys' }}
              </button>
            </div>
            <div v-if="providerKeysResult" class="action-result">{{ providerKeysResult }}</div>
          </div>

          <!-- Google Workspace integration -->
          <div class="card">
            <div class="settings-card-header settings-card-header--split">
              <div>
                <div class="settings-label-row">
                  <p class="section-title">Google Workspace</p>
                  <details class="field-info">
                    <summary aria-label="About Google Workspace integration" title="About Google Workspace integration">i</summary>
                    <div class="field-info-panel">
                      <p>
                        Ciaobot uses the
                        <a href="https://github.com/googleworkspace/cli" target="_blank" rel="noopener noreferrer">Google Workspace CLI (<code>gws</code>)</a>
                        to reach Gmail, Calendar, Drive, Docs, Sheets, Slides, and Tasks from chats and schedules.
                        Stock <code>gws-*</code> skills ship with the app once <code>gws</code> is installed and authenticated.
                      </p>
                      <p>
                        Use separate <strong>personal</strong> and <strong>work</strong> profiles so a personal chat never inherits work Drive or calendar access.
                        Each workspace picks its profile under Settings &rarr; Workspaces.
                      </p>
                      <p><strong>One-time setup per profile</strong></p>
                      <ol class="field-info-steps">
                        <li>Install <code>gws</code> (button below or <code>npm install -g @googleworkspace/cli</code>).</li>
                        <li>
                          In
                          <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener noreferrer">Google Cloud Console &rarr; Credentials</a>,
                          create an OAuth client (Desktop app, or Web app with redirect URI <code>http://localhost</code>).
                        </li>
                        <li>Download the JSON file Google gives you (often named like <code>client_secret_….json</code>).</li>
                        <li>Upload it below as <code>client_secret.json</code>, then click <strong>Connect Google Account</strong>.</li>
                      </ol>
                      <p>
                        Enable the APIs you need in your GCP project (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks).
                        Terminal alternative: <code>scripts/gws-profile.sh &lt;profile&gt; auth login --full</code>.
                      </p>
                    </div>
                  </details>
                </div>
                <p class="hint">
                  Connect Gmail, Calendar, Drive, Docs, Sheets, Slides, and Tasks through separate local <code>gws</code> profiles.
                  Workspaces choose which profile to use in Settings &rarr; Workspaces.
                </p>
              </div>
              <span
                v-if="gwsIntegration"
                class="badge"
                :class="gwsIntegration.installed ? 'badge--success' : 'badge--error'"
              >
                {{ gwsIntegration.installed ? 'gws installed' : 'gws missing' }}
              </span>
            </div>

            <div v-if="!gwsIntegrationLoaded" class="loading">
              Loading Google Workspace status&hellip;
            </div>
            <p v-else-if="gwsIntegrationError" class="hint hint--warn">
              {{ gwsIntegrationError }}
            </p>
            <template v-else-if="gwsIntegration">
              <div v-if="!gwsIntegration.installed" class="integration-warning">
                <p class="hint hint--warn">
                  Install <code>@googleworkspace/cli</code> before enabling Google Workspace tools for chats and schedules.
                </p>
                <div class="action-row">
                  <button
                    class="btn-primary btn-small"
                    :disabled="gwsInstalling"
                    @click="installGws"
                  >
                    {{ gwsInstalling ? 'Installing…' : 'Install gws' }}
                  </button>
                </div>
                <p v-if="gwsInstallResult" class="hint hint--compact gws-install-result">{{ gwsInstallResult }}</p>
              </div>
              <div class="gws-profile-list">
                <div
                  v-for="profile in gwsIntegration.profiles"
                  :key="profile.name"
                  class="gws-profile-card"
                >
                  <div class="gws-profile-header">
                    <div class="gws-profile-heading">
                      <p class="gws-profile-title">{{ profile.label }}</p>
                      <p v-if="profile.email" class="gws-profile-email">{{ profile.email }}</p>
                      <p class="hint hint--compact"><code>{{ profile.name }}</code> profile</p>
                    </div>
                    <span class="badge" :class="gwsProfileBadgeClass(profile)">
                      {{ gwsProfileStatus(profile) }}
                    </span>
                  </div>
                  <p class="gws-profile-purpose">{{ profile.purpose }}</p>
                  <div v-if="profile.examples.length" class="gws-example-row">
                    <span v-for="example in profile.examples" :key="example" class="gws-chip">
                      {{ example }}
                    </span>
                  </div>
                  <div class="gws-profile-meta">
                    <div>
                      <span class="dev-label">Used by</span>
                      <span v-if="!profile.workspaces.length" class="muted-text">No workspace</span>
                      <span v-else class="gws-workspace-chips">
                        <span v-for="workspace in profile.workspaces" :key="workspace" class="gws-chip gws-chip--workspace">
                          {{ workspace }}
                        </span>
                      </span>
                    </div>
                    <div v-if="profile.config_dir">
                      <span class="dev-label">Config</span>
                      <code>{{ profile.config_dir }}</code>
                    </div>
                    <div>
                      <span class="dev-label">OAuth client</span>
                      <span :class="profile.client_secret_present ? 'status-text--ok' : 'status-text--warn'">
                        {{ profile.client_secret_present ? 'present' : 'missing' }}
                      </span>
                    </div>
                    <div v-if="profile.setup_command">
                      <span class="dev-label">Login</span>
                      <code class="gws-command">{{ profile.setup_command }}</code>
                    </div>
                    <div v-if="profile.headless_auth_command">
                      <span class="dev-label">Headless</span>
                      <code class="gws-command">{{ profile.headless_auth_command }}</code>
                    </div>
                  </div>

                  <!-- Interactive account connection controls -->
                  <div class="gws-profile-actions">
                    <!-- State 1: Needs client_secret.json -->
                    <template v-if="!profile.client_secret_present">
                      <p class="gws-action-hint">
                        Upload your OAuth <code>client_secret.json</code> to start (see the ⓘ button above for Google Cloud setup steps).
                      </p>
                      <label class="btn-small file-upload-btn">
                        Choose JSON file
                        <input
                          type="file"
                          accept=".json"
                          style="display: none;"
                          @change="handleClientSecretUpload($event, profile.name)"
                          :disabled="gwsSavingProfile === profile.name"
                        />
                      </label>
                    </template>

                    <!-- State 2: Ready to authenticate -->
                    <template v-else-if="!profile.configured">
                      <div v-if="!gwsAuthUrls[profile.name]" class="gws-btn-group">
                        <button
                          class="btn-primary btn-small"
                          @click="startGwsAuth(profile.name)"
                          :disabled="gwsSavingProfile === profile.name"
                        >
                          Connect Google Account
                        </button>
                        <button
                          class="btn-small btn-danger"
                          @click="disconnectGwsProfile(profile.name, true)"
                          :disabled="gwsSavingProfile === profile.name"
                        >
                          Remove OAuth Client
                        </button>
                      </div>
                      <div v-else class="gws-auth-flow-box">
                        <p class="gws-flow-step">
                          1. Follow the Google auth flow. If the browser did not open, click here:
                          <a :href="gwsAuthUrls[profile.name]" target="_blank" class="gws-auth-link">Open authorization page</a>
                        </p>
                        <p class="gws-flow-step">
                          2. After signing in, copy the full redirect URL (even if it fails to load) or authorization code, and paste it below:
                        </p>
                        <input
                          type="text"
                          class="routine-input gws-auth-input"
                          v-model="gwsRedirectUrls[profile.name]"
                          placeholder="Paste redirect URL (http://localhost/?code=...) or code"
                          :disabled="gwsSavingProfile === profile.name"
                          @keyup.enter="exchangeGwsCode(profile.name)"
                        />
                        <div class="gws-flow-buttons">
                          <button
                            class="btn-primary btn-small"
                            @click="exchangeGwsCode(profile.name)"
                            :disabled="!gwsRedirectUrls[profile.name]?.trim() || gwsSavingProfile === profile.name"
                          >
                            {{ gwsSavingProfile === profile.name ? 'Connecting...' : 'Complete Sign-In' }}
                          </button>
                          <button
                            class="btn-small"
                            @click="cancelGwsAuth(profile.name)"
                            :disabled="gwsSavingProfile === profile.name"
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    </template>

                    <!-- State 3: Authenticated -->
                    <template v-else>
                      <div class="gws-btn-group">
                        <button
                          class="btn-small btn-danger"
                          @click="disconnectGwsProfile(profile.name, false)"
                          :disabled="gwsSavingProfile === profile.name"
                        >
                          Disconnect Account
                        </button>
                        <button
                          class="btn-small btn-outline-danger"
                          style="border-color: var(--error); color: var(--error);"
                          @click="disconnectGwsProfile(profile.name, true)"
                          :disabled="gwsSavingProfile === profile.name"
                        >
                          Remove Client & Credentials
                        </button>
                      </div>
                    </template>
                  </div>
                </div>
              </div>
              <p class="hint hint--info gws-boundary-note">
                Keep personal and work Google accounts in different profiles. A personal chat should not inherit work Drive, calendar, or connector access by accident.
              </p>
            </template>
          </div>

          <!-- Provider alias tiers -->
          <div v-if="tierProviderSections.length" class="card">
            <div class="settings-card-header">
              <p class="section-title">Provider alias models</p>
              <p class="hint">
                Pick a provider, then set the model behind <code>opus</code>, <code>sonnet</code>, and <code>haiku</code>.
              </p>
            </div>
            <div class="alias-provider-bar">
              <label class="settings-field alias-provider-field">
                <span class="ws-label">Provider</span>
                <select
                  class="routine-input alias-provider-select"
                  :value="selectedTierProviderSection?.key || ''"
                  :disabled="routinesSaving"
                  @change="selectedTierProvider = ($event.target as HTMLSelectElement).value as AliasProviderKey"
                >
                  <option v-for="section in tierProviderSections" :key="section.key" :value="section.key">
                    {{ section.label }}<template v-if="!section.available"> (not configured)</template>
                  </option>
                </select>
              </label>
            </div>
            <div v-if="selectedTierProviderSection" class="tier-provider-section">
              <div class="settings-field-grid">
                <label v-for="tier in modelTiers" :key="`${selectedTierProviderSection.key}-${tier.key}`" class="settings-field">
                  <span class="ws-label">{{ tier.label }}</span>
                  <ModelSelector
                    v-if="selectedTierProviderSection.configurable"
                    :model-value="tierOverrideValue(selectedTierProviderSection.key as TierProviderKey, tier.key)"
                    :sections="tierModelSections"
                    :disabled="routinesSaving || !selectedTierProviderSection.available"
                    :placeholder="`Default (${tierEffectiveValue(selectedTierProviderSection.key as TierProviderKey, tier.key) || 'automatic'})`"
                    :empty-placeholder="`Default (${tierEffectiveValue(selectedTierProviderSection.key as TierProviderKey, tier.key) || 'automatic'})`"
                    @update:model-value="saveTierModel(selectedTierProviderSection.key as TierProviderKey, tier.key, $event)"
                  />
                  <input
                    v-else
                    class="routine-input"
                    :value="tier.key"
                    disabled
                  />
                </label>
              </div>
              <p v-if="!selectedTierProviderSection.configurable" class="hint hint--info tier-provider-note">
                Claude uses the native tier aliases directly.
              </p>
              <p v-else-if="!selectedTierProviderSection.available" class="hint hint--info tier-provider-note">
                {{ tierProviderUnavailableHint }}
              </p>
            </div>
          </div>
        </template>
      </template>

      <!-- WORKSPACES TAB -->
      <template v-if="currentTab === 'workspaces'">
        <div v-if="!workspacesLoaded" class="card"><span class="loading">Loading&hellip;</span></div>
        <template v-else-if="workspacesError">
          <div class="card"><p class="hint hint--warn">{{ workspacesError }}</p></div>
        </template>
        <template v-else>
          <div class="card">
            <div class="settings-card-header settings-card-header--split">
              <div>
                <p class="section-title">Workspaces</p>
                <p class="hint">
                  Logical chat spaces that route projects, chats, vault names, model defaults, and integration profiles.
                </p>
              </div>
              <button class="btn-small" @click="showNewWorkspace = !showNewWorkspace">
                {{ showNewWorkspace ? 'Cancel' : '+ Add workspace' }}
              </button>
            </div>

            <div v-if="showNewWorkspace" class="workspace-card workspace-card--new">
              <div class="workspace-card-header">
                <div>
                  <p class="workspace-title">New workspace</p>
                  <p class="hint hint--compact">Saved to <code>.runtime/workspaces.json</code> and applied immediately.</p>
                </div>
              </div>
              <div class="settings-field-grid">
                <label class="settings-field"><span class="ws-label">Name</span>
                  <input class="routine-input" v-model="newWorkspaceForm.name" :disabled="workspacesSaving === 'new'" placeholder="letters, numbers, dashes, underscores" />
                </label>
                <label class="settings-field"><span class="ws-label">Vault name</span>
                  <input class="routine-input" v-model="newWorkspaceForm.vault_root" :disabled="workspacesSaving === 'new'" placeholder="(defaults to name)" />
                </label>
                <label class="settings-field"><span class="ws-label">Provider</span>
                  <select class="routine-input" v-model="newWorkspaceForm.default_provider" :disabled="workspacesSaving === 'new'">
                    <option v-for="provider in workspaceProviderOptions" :key="provider.value" :value="provider.value">
                      {{ provider.label }}
                    </option>
                  </select>
                </label>
                <label class="settings-field"><span class="ws-label">Default model</span>
                  <ModelSelector
                    v-model="newWorkspaceForm.default_model"
                    :sections="newWorkspaceModelSections"
                    :placeholder="workspaceInheritPlaceholder"
                    :empty-placeholder="workspaceInheritPlaceholder"
                    :disabled="workspacesSaving === 'new'"
                  />
                </label>
                <label class="settings-field">
                  <div class="settings-label-row">
                    <span class="ws-label">Google profile</span>
                    <details class="field-info">
                      <summary aria-label="About GWS profiles" title="About GWS profiles">i</summary>
                      <div class="field-info-panel">
                        <p>
                          Selects the Google Workspace profile used by this workspace. Manage profiles and credentials in the Providers tab.
                        </p>
                      </div>
                    </details>
                  </div>
                  <select class="routine-input" v-model="newWorkspaceForm.gws_profile" :disabled="workspacesSaving === 'new'">
                    <option value="">Default ({{ defaultGwsProfileName }})</option>
                    <option v-for="profile in gwsProfileOptions" :key="`new-gws-${profile.name}`" :value="profile.name">
                      {{ profile.label }} ({{ profile.email || profile.name }})
                    </option>
                    <option v-if="workspaceCustomGwsProfile(newWorkspaceForm.gws_profile)" :value="newWorkspaceForm.gws_profile">
                      Custom: {{ newWorkspaceForm.gws_profile }}
                    </option>
                  </select>
                </label>
                <div class="settings-field settings-field--wide">
                  <div class="settings-label-row">
                    <span class="ws-label">Claude.ai MCPs</span>
                    <details class="field-info">
                      <summary aria-label="About Claude.ai MCP connectors" title="About Claude.ai MCP connectors">i</summary>
                      <div class="field-info-panel">
                        <p>
                          Allows this workspace to use claude.ai account connectors, for example Airtable,
                          Slack, Atlassian, BigQuery, Sentry, or similar tools.
                        </p>
                        <p>
                          Turn this off for personal workspaces when your connected accounts point to work systems,
                          so personal chats do not inherit work-only connectors.
                        </p>
                      </div>
                    </details>
                  </div>
                  <select class="routine-input" v-model="newWorkspaceForm.claude_ai_mcps" :disabled="workspacesSaving === 'new'" aria-label="Claude.ai MCPs">
                    <option value="on">On (connectors allowed)</option>
                    <option value="off">Off (connectors blocked)</option>
                  </select>
                </div>

              </div>
              <div class="action-row settings-actions">
                <button class="btn-primary" @click="createNewWorkspace" :disabled="workspacesSaving === 'new'">
                  {{ workspacesSaving === 'new' ? 'Creating...' : 'Create workspace' }}
                </button>
              </div>
            </div>

            <div class="workspace-list">
              <div
                v-for="form in workspaceForms"
                :key="form.name"
                class="workspace-card"
              >
                <div class="workspace-card-header">
                  <div>
                    <p class="workspace-title">{{ form.name }}</p>
                    <p class="hint hint--compact">{{ form.vault_root || form.name }} vault name</p>
                  </div>
                  <div class="workspace-actions">
                    <button
                      class="btn-small"
                      @click="saveWorkspace(form.name)"
                      :disabled="workspacesSaving === form.name"
                    >
                      {{ workspacesSaving === form.name ? 'Saving...' : 'Save' }}
                    </button>
                    <button
                      v-if="workspaceForms.length > 1"
                      class="btn-small btn-danger"
                      @click="removeWorkspace(form.name)"
                      :disabled="workspacesSaving === form.name"
                    >Delete</button>
                  </div>
                </div>

                <div class="settings-field-grid">
                  <label class="settings-field"><span class="ws-label">Vault name</span>
                    <input class="routine-input" v-model="form.vault_root" :disabled="workspacesSaving === form.name" placeholder="(defaults to workspace name)" />
                  </label>
                  <label class="settings-field"><span class="ws-label">Provider</span>
                    <select class="routine-input" v-model="form.default_provider" :disabled="workspacesSaving === form.name">
                      <option v-for="provider in workspaceProviderOptions" :key="provider.value" :value="provider.value">
                        {{ provider.label }}
                      </option>
                    </select>
                  </label>
                  <label class="settings-field"><span class="ws-label">Default model</span>
                    <ModelSelector
                      v-model="form.default_model"
                      :sections="workspaceModelSectionsForForm(form)"
                      :placeholder="workspaceInheritPlaceholder"
                      :empty-placeholder="workspaceInheritPlaceholder"
                      :disabled="workspacesSaving === form.name"
                    />
                  </label>
                  <label class="settings-field">
                    <div class="settings-label-row">
                      <span class="ws-label">Google profile</span>
                      <details class="field-info">
                        <summary aria-label="About GWS profiles" title="About GWS profiles">i</summary>
                        <div class="field-info-panel">
                          <p>
                            Selects the Google Workspace profile used by this workspace. Manage profiles and credentials in the Providers tab.
                          </p>
                        </div>
                      </details>
                    </div>
                    <select class="routine-input" v-model="form.gws_profile" :disabled="workspacesSaving === form.name">
                      <option value="">Default ({{ defaultGwsProfileName }})</option>
                      <option v-for="profile in gwsProfileOptions" :key="`${form.name}-gws-${profile.name}`" :value="profile.name">
                        {{ profile.label }} ({{ profile.email || profile.name }})
                      </option>
                      <option v-if="workspaceCustomGwsProfile(form.gws_profile)" :value="form.gws_profile">
                        Custom: {{ form.gws_profile }}
                      </option>
                    </select>
                  </label>
                  <div class="settings-field settings-field--wide">
                    <div class="settings-label-row">
                      <span class="ws-label">Claude.ai MCPs</span>
                      <details class="field-info">
                        <summary aria-label="About Claude.ai MCP connectors" title="About Claude.ai MCP connectors">i</summary>
                        <div class="field-info-panel">
                          <p>
                            Allows this workspace to use claude.ai account connectors, for example Airtable,
                            Slack, Atlassian, BigQuery, Sentry, or similar tools.
                          </p>
                          <p>
                            Turn this off for personal workspaces when your connected accounts point to work systems,
                            so personal chats do not inherit work-only connectors.
                          </p>
                        </div>
                      </details>
                    </div>
                    <select class="routine-input" v-model="form.claude_ai_mcps" :disabled="workspacesSaving === form.name" aria-label="Claude.ai MCPs">
                      <option value="on">On (connectors allowed)</option>
                      <option value="off">Off (connectors blocked)</option>
                    </select>
                  </div>

                </div>
              </div>
            </div>

            <div v-if="workspacesResult" class="action-result">{{ workspacesResult }}</div>
          </div>
        </template>
      </template>

      <!-- INSTRUCTIONS TAB -->
      <template v-if="currentTab === 'instructions'">
        <div class="card">
          <div class="settings-card-header">
            <p class="section-title">Instructions</p>
            <p class="hint">
              Claude Code files and Ciaobot-generated prompt blocks that shape what the agent sees.
            </p>
          </div>

          <div v-if="!agentAssetsLoaded" class="action-row"><span class="loading">Loading&hellip;</span></div>
          <template v-else-if="agentAssetsError">
            <p class="hint hint--warn">{{ agentAssetsError }}</p>
          </template>
          <template v-else>
            <p v-if="!instructionAssets.length" class="hint hint--section-empty">No instruction sources found.</p>
            <div v-else class="skill-list">
              <div
                v-for="item in instructionAssets"
                :key="item.id"
                class="skill-row instruction-row"
                :class="{ expanded: isInstructionExpanded(item) }"
                :style="{ paddingLeft: `${10 + Math.min(item.level || 0, 4) * 18}px` }"
                @click="toggleInstruction(item)"
              >
                <div class="skill-main">
                  <div class="skill-title-row command-title-row">
                    <span class="skill-chevron">{{ isInstructionExpanded(item) ? '&#9662;' : '&#9656;' }}</span>
                    <span class="skill-name">{{ item.title }}</span>
                    <span class="skill-badges">
                      <span v-if="item.scope" class="badge badge--muted command-source">{{ item.scope }}</span>
                      <span class="badge badge--muted command-source">{{ item.source }}</span>
                      <span v-if="item.status && item.status !== 'ok'" class="badge command-source" :class="item.status === 'missing' ? 'badge--warn' : 'badge--error'">{{ item.status }}</span>
                      <span v-if="item.editable" class="badge badge--success command-source">editable</span>
                      <span v-else class="badge badge--muted command-source">read-only</span>
                    </span>
                  </div>
                  <p class="skill-description">{{ item.description }}</p>
                  <div v-if="isInstructionExpanded(item)" class="skill-detail">
                    <p v-if="item.path" class="skill-meta">
                      <span class="skill-meta-label">Path</span>
                      <button class="inline-path-button" @click.stop="openAssetPath(item.path)">{{ item.path }}</button>
                    </p>
                    <p v-if="item.parent_id" class="skill-meta">
                      <span class="skill-meta-label">Imported by</span>
                      <code class="command-path">{{ item.parent_id }}</code>
                    </p>
                    <p v-if="item.imports?.length" class="skill-meta">
                      <span class="skill-meta-label">Imports</span>
                      <code class="command-path">{{ item.imports.join(', ') }}</code>
                    </p>
                    <pre v-if="item.content" class="asset-code-preview"><code>{{ item.content }}</code></pre>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>
      </template>

      <!-- SKILLS TAB -->
      <template v-if="currentTab === 'skills'">
        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">Skills</p>
              <p class="hint">
                Manage Ciaobot-specific custom skills and locked GitHub/package skills.
              </p>
            </div>
            <div class="settings-card-header-actions">
              <button class="btn-small" @click="createSkillViaChat">Add via chat</button>
              <button class="btn-small" @click="toggleAddGithubSkill">
                {{ showAddGithubSkill ? 'Cancel' : '+ Add from GitHub' }}
              </button>
            </div>
          </div>

          <p class="hint hint--info skill-scope-note">
            Ciaobot runs on Claude Code, so any plugins and skills you install globally in Claude Code are also loaded and available to Ciaobot. This page only lists the skills managed here, the Ciaobot-specific ones (custom and GitHub/package skills).
          </p>

          <!-- Auto-update GitHub skills -->
          <div class="setting-row setting-row--inline">
            <div class="routine-info">
              <span class="routine-name">Auto-update GitHub skills</span>
              <p class="hint hint--compact">
                If enabled, Ciaobot checks GitHub for updates to locked package skills on boot.
              </p>
            </div>
            <input
              type="checkbox"
              class="settings-checkbox"
              v-model="autoUpdateGithubSkills"
              :disabled="autoUpdateSaving"
              @change="saveAutoUpdateGithubSkills"
            />
          </div>
          <div v-if="autoUpdateResult" class="action-result">{{ autoUpdateResult }}</div>

          <!-- Add Github Skill Form -->
          <div
            v-if="showAddGithubSkill"
            class="settings-form-panel"
          >
            <label class="settings-field"><span class="ws-label">GitHub URL / owner/repo</span>
              <input class="routine-input" v-model="githubSource" :disabled="addingGithubSkill" placeholder="e.g. owner/repo or github URL" />
            </label>
            <label class="settings-field"><span class="ws-label">Skill name (optional)</span>
              <input class="routine-input" v-model="githubSkillName" :disabled="addingGithubSkill" placeholder="(inferred from URL if omitted)" />
            </label>
            <div class="action-row settings-actions">
              <button class="btn-primary" @click="addGithubSkill" :disabled="addingGithubSkill || !githubSource.trim()">
                {{ addingGithubSkill ? 'Adding...' : 'Add skill' }}
              </button>
            </div>
            <div v-if="addGithubSkillResult" class="action-result" :class="{ '--error': addGithubSkillError }">{{ addGithubSkillResult }}</div>
          </div>

          <div v-if="!skillsLoaded" class="action-row"><span class="loading">Loading&hellip;</span></div>
          <template v-else-if="skillsError">
            <p class="hint hint--warn">{{ skillsError }}</p>
          </template>
          <template v-else-if="skillsInventory">
            <!-- Custom Skills Section -->
            <div class="skill-section">
              <p class="subsection-title subsection-title--spaced">Custom Skills</p>
              <p v-if="!customSkills.length" class="hint hint--section-empty">No custom skills created yet.</p>
              <div v-else class="skill-list skill-list--section">
                <div
                  v-for="skill in customSkills"
                  :key="skill.name"
                  class="skill-row"
                  :class="{ expanded: isSkillExpanded(skill.name) }"
                  @click="toggleSkill(skill.name)"
                >
                  <div class="skill-main">
                    <div class="skill-title-row">
                      <span class="skill-chevron">{{ isSkillExpanded(skill.name) ? '&#9662;' : '&#9656;' }}</span>
                      <span class="skill-name">{{ skill.name }}</span>
                    </div>
                    <p v-if="skill.description" class="skill-description">{{ skill.description }}</p>
                    <div v-if="isSkillExpanded(skill.name)" class="skill-detail">
                      <pre v-if="skill.content" class="asset-code-preview"><code>{{ skill.content }}</code></pre>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <!-- GitHub Skills Section -->
            <div class="skill-section skill-section--spaced">
              <p class="subsection-title subsection-title--spaced">GitHub / Package Skills</p>
              <p v-if="!githubSkills.length" class="hint hint--section-empty">No GitHub/package skills installed yet.</p>
              <div v-else class="skill-list">
                <div
                  v-for="skill in githubSkills"
                  :key="skill.name"
                  class="skill-row"
                  :class="{ expanded: isSkillExpanded(skill.name) }"
                  @click="toggleSkill(skill.name)"
                >
                  <div class="skill-main">
                    <div class="skill-title-row">
                      <span class="skill-chevron">{{ isSkillExpanded(skill.name) ? '&#9662;' : '&#9656;' }}</span>
                      <a
                        v-if="skill.source && skill.source !== 'skills-lock.json'"
                        :href="'https://github.com/' + skill.source"
                        target="_blank"
                        class="skill-name skill-link"
                        @click.stop
                      >
                        {{ skill.name }}
                      </a>
                      <span v-else class="skill-name">{{ skill.name }}</span>
                    </div>
                    <p v-if="skill.description" class="skill-description">{{ skill.description }}</p>
                    <div v-if="isSkillExpanded(skill.name)" class="skill-detail">
                      <pre v-if="skill.content" class="asset-code-preview"><code>{{ skill.content }}</code></pre>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>

        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">Subagents</p>
              <p class="hint">
                Claude Code subagents available to Ciaobot. Custom subagents are saved in <code>subagents/</code> and mirrored into the vault.
              </p>
            </div>
            <button class="btn-small" @click="toggleAddSubagent">
              {{ showAddSubagent ? 'Cancel' : '+ New subagent' }}
            </button>
          </div>

          <div v-if="showAddSubagent" class="settings-form-panel">
            <div class="settings-field-grid">
              <label class="settings-field"><span class="ws-label">Name</span>
                <input class="routine-input" v-model="newSubagentName" :disabled="addingSubagent" placeholder="e.g. pr-reviewer" />
              </label>
              <label class="settings-field"><span class="ws-label">Description</span>
                <input class="routine-input" v-model="newSubagentDescription" :disabled="addingSubagent" placeholder="When this subagent should be used" />
              </label>
              <label class="settings-field settings-field--wide"><span class="ws-label">Instructions</span>
                <textarea class="routine-textarea" v-model="newSubagentPrompt" :disabled="addingSubagent" rows="8" placeholder="Write the subagent behavior, constraints, and output format."></textarea>
              </label>
            </div>
            <div class="action-row settings-actions">
              <button class="btn-primary" @click="addSubagent" :disabled="addingSubagent || !newSubagentName.trim() || !newSubagentDescription.trim() || !newSubagentPrompt.trim()">
                {{ addingSubagent ? 'Creating...' : 'Create subagent' }}
              </button>
            </div>
            <div v-if="addSubagentResult" class="action-result" :class="{ '--error': addSubagentError }">{{ addSubagentResult }}</div>
          </div>
          <div v-if="assetLifecycleResult" class="action-result" :class="{ '--error': assetLifecycleError }">{{ assetLifecycleResult }}</div>

          <div v-if="!agentAssetsLoaded" class="action-row"><span class="loading">Loading&hellip;</span></div>
          <template v-else-if="agentAssetsError">
            <p class="hint hint--warn">{{ agentAssetsError }}</p>
          </template>
          <template v-else>
            <p v-if="!subagentAssets.length" class="hint hint--section-empty">No subagents found.</p>
            <div v-else class="skill-list">
              <div
                v-for="agent in subagentAssets"
                :key="`${agent.source}:${agent.name}:${agent.path}`"
                class="skill-row"
                :class="{ expanded: isSubagentExpanded(agent) }"
                @click="toggleSubagent(agent)"
              >
                <div class="skill-main">
                  <div class="skill-title-row command-title-row">
                    <span class="skill-chevron">{{ isSubagentExpanded(agent) ? '&#9662;' : '&#9656;' }}</span>
                    <span class="skill-name">{{ agent.name }}</span>
                    <span class="skill-badges">
                      <span class="badge badge--muted command-source">{{ agent.scope }}</span>
                      <span v-if="agent.editable" class="badge badge--success command-source">custom</span>
                    </span>
                  </div>
                  <p v-if="agent.description" class="skill-description">{{ agent.description }}</p>
                  <p v-else class="skill-description muted-text">No description.</p>
                  <div v-if="isSubagentExpanded(agent)" class="skill-detail">
                    <p class="skill-meta">
                      <span class="skill-meta-label">Path</span>
                      <button class="inline-path-button" @click.stop="openAssetPath(agent.path)">{{ agent.path }}</button>
                    </p>
                    <p v-if="agent.vault_path" class="skill-meta">
                      <span class="skill-meta-label">Vault</span>
                      <button class="inline-path-button" @click.stop="openAssetPath(agent.vault_path)">{{ agent.vault_path }}</button>
                    </p>
                    <div v-if="agent.editable" class="asset-actions">
                      <button class="btn-small" @click.stop="startEditSubagent(agent)" :disabled="savingSubagent === agent.name">
                        Edit
                      </button>
                      <button class="btn-small btn-danger" @click.stop="deleteSubagent(agent)" :disabled="savingSubagent === agent.name">
                        {{ savingSubagent === agent.name ? 'Working...' : 'Delete' }}
                      </button>
                    </div>
                    <div v-if="editingSubagent === agent.name" class="settings-form-panel asset-edit-panel" @click.stop>
                      <label class="settings-field"><span class="ws-label">Description</span>
                        <input class="routine-input" v-model="editSubagentDescription" :disabled="savingSubagent === agent.name" />
                      </label>
                      <label class="settings-field"><span class="ws-label">Instructions</span>
                        <textarea class="routine-textarea" v-model="editSubagentContent" :disabled="savingSubagent === agent.name" rows="10"></textarea>
                      </label>
                      <div class="action-row settings-actions">
                        <button class="btn-primary" @click.stop="saveSubagent(agent)" :disabled="savingSubagent === agent.name || !editSubagentDescription.trim() || !editSubagentContent.trim()">
                          {{ savingSubagent === agent.name ? 'Saving...' : 'Save subagent' }}
                        </button>
                        <button class="btn-small" @click.stop="cancelEditSubagent" :disabled="savingSubagent === agent.name">Cancel</button>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>

        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">Commands</p>
              <p class="hint">
                Slash commands available to Claude Code. Custom commands are saved in <code>commands/</code> and mirrored into the vault.
              </p>
            </div>
            <div class="settings-card-header-actions">
              <span class="badge badge--muted">{{ commandAssets.length || commands.length }} loaded</span>
              <button class="btn-small" @click="toggleAddCommand">
                {{ showAddCommand ? 'Cancel' : '+ New command' }}
              </button>
            </div>
          </div>

          <div v-if="showAddCommand" class="settings-form-panel">
            <div class="settings-field-grid">
              <label class="settings-field"><span class="ws-label">Name</span>
                <input class="routine-input" v-model="newCommandName" :disabled="addingCommand" placeholder="e.g. summarize-decision" />
              </label>
              <label class="settings-field"><span class="ws-label">Argument hint</span>
                <input class="routine-input" v-model="newCommandArgumentHint" :disabled="addingCommand" placeholder="e.g. &lt;notes&gt;" />
              </label>
              <label class="settings-field settings-field--wide"><span class="ws-label">Description</span>
                <input class="routine-input" v-model="newCommandDescription" :disabled="addingCommand" placeholder="What this slash command does" />
              </label>
              <label class="settings-field settings-field--wide"><span class="ws-label">Prompt</span>
                <textarea class="routine-textarea" v-model="newCommandPrompt" :disabled="addingCommand" rows="8" placeholder="Write the command prompt. Use $ARGUMENTS where the user's command text should be inserted."></textarea>
              </label>
            </div>
            <div class="action-row settings-actions">
              <button class="btn-primary" @click="addCommand" :disabled="addingCommand || !newCommandName.trim() || !newCommandDescription.trim() || !newCommandPrompt.trim()">
                {{ addingCommand ? 'Creating...' : 'Create command' }}
              </button>
            </div>
            <div v-if="addCommandResult" class="action-result" :class="{ '--error': addCommandError }">{{ addCommandResult }}</div>
          </div>
          <div v-if="assetLifecycleResult" class="action-result" :class="{ '--error': assetLifecycleError }">{{ assetLifecycleResult }}</div>

          <div v-if="!agentAssetsLoaded" class="action-row"><span class="loading">Loading&hellip;</span></div>
          <template v-else-if="agentAssetsError">
            <p class="hint hint--warn">{{ agentAssetsError }}</p>
          </template>
          <template v-else>
            <p v-if="!commandAssets.length" class="hint hint--section-empty">No slash commands found.</p>
            <div v-else class="skill-list">
              <div
                v-for="command in commandAssets"
                :key="commandKey(command)"
                class="skill-row command-row"
                :class="{ expanded: isCommandExpanded(command) }"
                @click="toggleCommand(command)"
              >
                <div class="skill-main">
                  <div class="skill-title-row command-title-row">
                    <span class="skill-chevron">{{ isCommandExpanded(command) ? '&#9662;' : '&#9656;' }}</span>
                    <span class="command-name">/{{ command.name }}</span>
                    <span v-if="command.argument_hint" class="command-args">{{ command.argument_hint }}</span>
                    <span class="skill-badges">
                      <span class="badge badge--muted command-source">{{ command.scope }}</span>
                      <span v-if="command.editable" class="badge badge--success command-source">custom</span>
                    </span>
                  </div>
                  <p v-if="command.description" class="skill-description">{{ command.description }}</p>
                  <p v-else class="skill-description muted-text">No description.</p>
                  <div v-if="isCommandExpanded(command)" class="skill-detail">
                    <p v-if="command.argument_hint" class="skill-meta">
                      <span class="skill-meta-label">Arguments</span>
                      <code class="command-path">{{ command.argument_hint }}</code>
                    </p>
                    <p class="skill-meta">
                      <span class="skill-meta-label">Source</span>
                      {{ command.source }}
                    </p>
                    <p v-if="command.path" class="skill-meta">
                      <span class="skill-meta-label">Path</span>
                      <button class="inline-path-button" @click.stop="openAssetPath(command.path)">{{ command.path }}</button>
                    </p>
                    <p v-if="command.vault_path" class="skill-meta">
                      <span class="skill-meta-label">Vault</span>
                      <button class="inline-path-button" @click.stop="openAssetPath(command.vault_path)">{{ command.vault_path }}</button>
                    </p>
                    <div v-if="command.editable" class="asset-actions">
                      <button class="btn-small" @click.stop="startEditCommand(command)" :disabled="savingCommand === command.name">
                        Edit
                      </button>
                      <button class="btn-small btn-danger" @click.stop="deleteCommand(command)" :disabled="savingCommand === command.name">
                        {{ savingCommand === command.name ? 'Working...' : 'Delete' }}
                      </button>
                    </div>
                    <div v-if="editingCommand === command.name" class="settings-form-panel asset-edit-panel" @click.stop>
                      <div class="settings-field-grid">
                        <label class="settings-field"><span class="ws-label">Description</span>
                          <input class="routine-input" v-model="editCommandDescription" :disabled="savingCommand === command.name" />
                        </label>
                        <label class="settings-field"><span class="ws-label">Argument hint</span>
                          <input class="routine-input" v-model="editCommandArgumentHint" :disabled="savingCommand === command.name" />
                        </label>
                        <label class="settings-field settings-field--wide"><span class="ws-label">Prompt</span>
                          <textarea class="routine-textarea" v-model="editCommandContent" :disabled="savingCommand === command.name" rows="10"></textarea>
                        </label>
                      </div>
                      <div class="action-row settings-actions">
                        <button class="btn-primary" @click.stop="saveCommand(command)" :disabled="savingCommand === command.name || !editCommandDescription.trim() || !editCommandContent.trim()">
                          {{ savingCommand === command.name ? 'Saving...' : 'Save command' }}
                        </button>
                        <button class="btn-small" @click.stop="cancelEditCommand" :disabled="savingCommand === command.name">Cancel</button>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>
      </template>




    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../lib/api'
import { formatTime, formatDuration } from '../lib/time'
import type {
  AgentAssetsResponse,
  AutomationProcess,
  CommandAsset,
  CommandsResponse,
  CreatedAgentAssetResponse,
  DebugIssueReport,
  DeployResult,
  GwsIntegrationSettings,
  LocalStatus,
  PromptAsset,
  ProviderConfigSettings,
  RoutineSettings,
  SkillInventory,
  SlashCommand,
  SubagentAsset,
  WorkspaceInfo,
  WorkspaceHealthResponse,
  WorkspaceProvider,
} from '../lib/types'
import { currentSubscription, disablePush, enablePush, isPushEnabled, pushSupported } from '../lib/push'
import { useAuthStore } from '../stores/auth'
import { useFileViewerStore } from '../stores/fileViewer'
import { useProjectStore } from '../stores/projects'
import { useProductTourStore } from '../stores/productTour'
import PaneHeader from './PaneHeader.vue'
import ModelSelector from './ModelSelector.vue'
import RestartOverlay from './RestartOverlay.vue'
import GettingStartedChecklist from './GettingStartedChecklist.vue'
import { providerModelBadges, sectionsFromModelOptions, type ModelSection } from '../lib/modelSections'

const emit = defineEmits<{ 'open-sidebar': [] }>()

const route = useRoute()
const fileViewer = useFileViewerStore()
const productTour = useProductTourStore()
const currentTab = computed(() => (route.params.tab as string) || 'home')

const expandedSkills = ref<Record<string, boolean>>({})
const expandedCommands = ref<Record<string, boolean>>({})
const expandedSubagents = ref<Record<string, boolean>>({})
const expandedInstructions = ref<Record<string, boolean>>({})

// ── Appearance settings ────────────────────────────────────────────────────
const activeTheme = ref('system')
const fontScale = ref(1.0)

async function replayProductTour() {
  const { router } = await import('../router')
  await router.push('/')
  await productTour.replay()
}

function loadAppearanceSettings() {
  try {
    activeTheme.value = localStorage.getItem('ciao-theme') || 'system'
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
function commandKey(command: SlashCommand | CommandAsset) {
  return `${command.source}:${command.name}:${command.path}`
}
function isCommandExpanded(command: SlashCommand | CommandAsset) {
  return expandedCommands.value[commandKey(command)] || false
}
function toggleCommand(command: SlashCommand | CommandAsset) {
  const key = commandKey(command)
  expandedCommands.value[key] = !isCommandExpanded(command)
}
function isSubagentExpanded(agent: SubagentAsset) {
  return expandedSubagents.value[`${agent.source}:${agent.name}:${agent.path}`] || false
}
function toggleSubagent(agent: SubagentAsset) {
  const key = `${agent.source}:${agent.name}:${agent.path}`
  expandedSubagents.value[key] = !isSubagentExpanded(agent)
}
function isInstructionExpanded(item: PromptAsset) {
  return expandedInstructions.value[item.id] || false
}
function toggleInstruction(item: PromptAsset) {
  expandedInstructions.value[item.id] = !isInstructionExpanded(item)
}
function openAssetPath(path: string) {
  if (!path) return
  void fileViewer.open(path)
}

const actionPending = ref<string | null>(null)
const actionResult = ref('')
const deploySteps = ref<{ step: string; ok: boolean; output?: string }[]>([])

const hasDeployError = computed(() => {
  if (deploySteps.value.some(s => !s.ok)) return true
  if (!actionResult.value) return false
  const successOrPending = [
    'complete',
    'waiting',
    'reloading',
    'cancelled by user',
    'synced with remote'
  ]
  const val = actionResult.value.toLowerCase()
  return !successOrPending.some(str => val.includes(str))
})

const skillsInventory = ref<SkillInventory | null>(null)
const skillsLoaded = ref(false)
const skillsError = ref('')
const commands = ref<SlashCommand[]>([])
const commandsLoaded = ref(false)
const commandsError = ref('')
const agentAssets = ref<AgentAssetsResponse | null>(null)
const agentAssetsLoaded = ref(false)
const agentAssetsError = ref('')

// ── Routine settings (Models tab) ─────────────────────────────────────────
const routines = ref<RoutineSettings | null>(null)
const routinesLoaded = ref(false)
const routinesError = ref('')
const routinesSaving = ref(false)
const routinesResult = ref('')

type AliasProviderKey = 'claude' | 'ollama' | 'openrouter'
type TierProviderKey = Exclude<AliasProviderKey, 'claude'>
type TierKey = 'haiku' | 'sonnet' | 'opus'
type RoutineModelKey = 'title_model' | 'insights_model'
type RoutineProviderValue = 'automatic' | 'apple' | 'custom' | AliasProviderKey

const APFEL_REPO_URL = 'https://github.com/Arthur-Ficial/apfel'
type AliasProviderSection = {
  key: AliasProviderKey
  label: string
  options: string[]
  configurable: boolean
  // Whether the backend is configured (API key set, or for Ollama: local
  // models OR cloud key). Routine selectors filter to available sections;
  // the Providers tab tier card shows unavailable sections disabled.
  available: boolean
}
type TierSettingKey =
  | 'ollama_haiku_model'
  | 'ollama_sonnet_model'
  | 'ollama_opus_model'
  | 'openrouter_haiku_model'
  | 'openrouter_sonnet_model'
  | 'openrouter_opus_model'

const modelTiers: { key: TierKey; label: string }[] = [
  { key: 'haiku', label: 'Haiku' },
  { key: 'sonnet', label: 'Sonnet' },
  { key: 'opus', label: 'Opus' },
]

const tierSettingKeys: Record<TierProviderKey, Record<TierKey, TierSettingKey>> = {
  ollama: {
    haiku: 'ollama_haiku_model',
    sonnet: 'ollama_sonnet_model',
    opus: 'ollama_opus_model',
  },
  openrouter: {
    haiku: 'openrouter_haiku_model',
    sonnet: 'openrouter_sonnet_model',
    opus: 'openrouter_opus_model',
  },
}

const routineEffectiveKeys: Record<RoutineModelKey, keyof RoutineSettings> = {
  title_model: 'title_model_effective',
  insights_model: 'insights_model_effective',
}

const routineDefaultTiers: Record<RoutineModelKey, TierKey> = {
  title_model: 'haiku',
  insights_model: 'sonnet',
}

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
    notifySaved('Model settings saved.')
  } catch (e: any) {
    routinesResult.value = `Error: ${e?.message || e}`
  } finally {
    routinesSaving.value = false
  }
}

function parseModelList(raw: string): string[] {
  const seen = new Set<string>()
  const models: string[] = []
  for (const item of raw.split(',')) {
    const model = item.trim()
    if (!model || seen.has(model)) continue
    seen.add(model)
    models.push(model)
  }
  return models
}

function serializeModelList(models: string[]): string {
  return parseModelList(models.join(',')).join(',')
}

const critiqueModelSections = computed<ModelSection[]>(() => {
  const options = routines.value?.model_options
  if (!options) return []
  return [
    { key: 'ollama_local', label: 'Ollama (local, free)', models: options.ollama_local || [], badge: 'local' },
    { key: 'ollama_cloud', label: 'Ollama cloud', models: options.ollama_cloud || [] },
    { key: 'openrouter', label: 'OpenRouter', models: options.openrouter || [] },
    { key: 'anthropic', label: 'Anthropic', models: options.anthropic || [] },
  ].filter((section) => section.models.length > 0)
})

const selectedCritiqueModels = computed(() => parseModelList(routines.value?.critique_models || ''))

async function setCritiqueModels(value: string | string[]) {
  const models = Array.isArray(value) ? value : [value]
  await saveRoutines({ critique_models: serializeModelList(models) })
}

function removeCritiqueModel(model: string) {
  const current = selectedCritiqueModels.value.filter((m) => m !== model)
  setCritiqueModels(current)
}

const aliasProviderSections = computed<AliasProviderSection[]>(() => {
  const settings = routines.value
  if (!settings) return []
  // Filtered to available backends — used by the routine model selectors,
  // which must not offer a backend that isn't configured.
  const sections: AliasProviderSection[] = [
    {
      key: 'claude',
      label: 'Claude',
      options: settings.model_options.anthropic || [],
      configurable: false,
      available: true,
    },
  ]
  if (settings.backends?.ollama) {
    sections.push({
      key: 'ollama',
      label: 'Ollama',
      options: parseModelList([
        ...(settings.model_options.ollama_local || []),
        ...(settings.model_options.ollama_cloud || []),
      ].join(',')),
      configurable: true,
      available: true,
    })
  }
  if (settings.backends?.openrouter) {
    sections.push({
      key: 'openrouter',
      label: 'OpenRouter',
      options: parseModelList((settings.model_options.openrouter || []).join(',')),
      configurable: true,
      available: true,
    })
  }
  return sections
})

// Tier-mapping sections for the Providers tab "Provider alias models" card.
// Always includes Ollama and OpenRouter (even when unconfigured) so the
// operator can see the option exists; unconfigured backends render disabled
// with a "set the API key" hint instead of vanishing.
const tierProviderSections = computed<AliasProviderSection[]>(() => {
  const settings = routines.value
  if (!settings) return []
  const ollamaAvailable = !!settings.backends?.ollama
  const openrouterAvailable = !!settings.backends?.openrouter
  return [
    {
      key: 'claude',
      label: 'Claude',
      options: settings.model_options.anthropic || [],
      configurable: false,
      available: true,
    },
    {
      key: 'ollama',
      label: 'Ollama',
      options: ollamaAvailable
        ? parseModelList([
            ...(settings.model_options.ollama_local || []),
            ...(settings.model_options.ollama_cloud || []),
          ].join(','))
        : [],
      configurable: true,
      available: ollamaAvailable,
    },
    {
      key: 'openrouter',
      label: 'OpenRouter',
      options: openrouterAvailable
        ? parseModelList((settings.model_options.openrouter || []).join(','))
        : [],
      configurable: true,
      available: openrouterAvailable,
    },
  ]
})

const selectedTierProvider = ref<AliasProviderKey>('claude')
const selectedTierProviderSection = computed(() =>
  tierProviderSections.value.find((section) => section.key === selectedTierProvider.value)
  || tierProviderSections.value[0]
  || null
)

// Hint shown when the selected tier provider isn't configured yet.
const tierModelSections = computed<ModelSection[]>(() => {
  const section = selectedTierProviderSection.value
  if (!section || !section.options.length) return []
  const localModels = section.key === 'ollama'
    ? routines.value?.model_options.ollama_local || []
    : []
  return [
    {
      key: section.key,
      label: section.label,
      models: section.options,
      modelBadges: providerModelBadges(section.key, section.options, routines.value?.alias_tiers, localModels),
      disabled: !section.available,
      hint: section.available ? undefined : tierProviderUnavailableHint.value,
    },
  ]
})

const tierProviderUnavailableHint = computed(() => {
  const section = selectedTierProviderSection.value
  if (!section || section.available) return ''
  if (section.key === 'ollama') {
    return 'Install local Ollama models or set the Ollama Cloud API key above to enable tier mapping.'
  }
  if (section.key === 'openrouter') {
    return 'Set the OpenRouter API key above to enable tier mapping.'
  }
  return 'Configure this provider to enable tier mapping.'
})

function tierOverrideValue(provider: TierProviderKey, tier: TierKey): string {
  const key = tierSettingKeys[provider][tier]
  return routines.value?.[key] || ''
}

function tierEffectiveValue(provider: TierProviderKey, tier: TierKey): string {
  return routines.value?.alias_tiers?.[provider]?.[tier] || ''
}

async function saveTierModel(provider: TierProviderKey, tier: TierKey, value: string | string[]) {
  const model = Array.isArray(value) ? value[0] || '' : value
  const key = tierSettingKeys[provider][tier]
  await saveRoutines({ [key]: model.trim() })
}

function tierModelForProvider(provider: AliasProviderKey, tier: TierKey): string {
  if (provider === 'claude') return tier
  return tierEffectiveValue(provider, tier) || ''
}

function aliasProviderLabel(provider: AliasProviderKey): string {
  return aliasProviderSections.value.find((section) => section.key === provider)?.label || provider
}

function routineEffectiveModel(key: RoutineModelKey): string {
  const settings = routines.value
  if (!settings) return ''
  const effectiveKey = routineEffectiveKeys[key]
  const value = settings[effectiveKey]
  return typeof value === 'string' ? value : ''
}

function inferRoutineModel(model: string): { provider: RoutineProviderValue; tier: TierKey } {
  const raw = model.trim()
  if (!raw) return { provider: 'automatic', tier: 'sonnet' }
  if (raw === 'apfel') return { provider: 'apple', tier: 'haiku' }
  if (raw === 'haiku' || raw === 'sonnet' || raw === 'opus') {
    return { provider: 'claude', tier: raw }
  }

  const providers: TierProviderKey[] = ['ollama', 'openrouter']
  for (const provider of providers) {
    for (const tier of modelTiers) {
      if (tierEffectiveValue(provider, tier.key) === raw) {
        return { provider, tier: tier.key }
      }
    }
  }

  return { provider: 'custom', tier: 'sonnet' }
}

function routineProviderValue(key: RoutineModelKey): RoutineProviderValue {
  return inferRoutineModel(routines.value?.[key] || '').provider
}

function routineTierValue(key: RoutineModelKey): TierKey {
  const raw = routines.value?.[key] || ''
  if (raw.trim()) return inferRoutineModel(raw).tier
  const effective = inferRoutineModel(routineEffectiveModel(key))
  if (effective.provider !== 'automatic' && effective.provider !== 'custom') {
    return effective.tier
  }
  return routineDefaultTiers[key]
}

function routineTierSelectable(key: RoutineModelKey): boolean {
  const provider = routineProviderValue(key)
  return provider === 'claude' || provider === 'ollama' || provider === 'openrouter'
}

function routineCustomModel(key: RoutineModelKey): string {
  return routineProviderValue(key) === 'custom' ? (routines.value?.[key] || '') : ''
}

async function saveRoutineProvider(key: RoutineModelKey, providerValue: string) {
  const provider = providerValue as RoutineProviderValue
  if (provider === 'automatic') {
    await saveRoutines({ [key]: '' })
    return
  }
  if (provider === 'apple') {
    await saveRoutines({ [key]: 'apfel' })
    return
  }
  if (provider === 'custom') return
  const tier = routineTierValue(key)
  const model = tierModelForProvider(provider, tier)
  await saveRoutines({ [key]: model })
}

async function saveRoutineTier(key: RoutineModelKey, tierValue: string) {
  const tier = tierValue as TierKey
  let provider = routineProviderValue(key)
  if (provider === 'automatic' || provider === 'apple' || provider === 'custom') {
    provider = 'claude'
  }
  const model = tierModelForProvider(provider, tier)
  await saveRoutines({ [key]: model })
}

function routineModelSummary(key: RoutineModelKey): string {
  const provider = routineProviderValue(key)
  if (provider === 'automatic') {
    return `Automatic: ${routineEffectiveModel(key) || 'default'}`
  }
  if (provider === 'apple') return 'Local (free)'
  if (provider === 'custom') return `Custom: ${routineCustomModel(key)}`
  const tier = routineTierValue(key)
  const model = tierModelForProvider(provider, tier)
  return `${aliasProviderLabel(provider)} ${tier}: ${model || 'default'}`
}

// ── Provider API Key settings (Providers tab) ─────────────────────────────────
const providerKeys = ref<ProviderConfigSettings | null>(null)
const providerKeysLoaded = ref(false)
const providerKeysError = ref('')
const providerKeysSaving = ref(false)
const providerKeysResult = ref('')
const providerKeyInputs = ref<Record<string, string>>({})
const autoUpdateGithubSkills = ref(false)
const autoUpdateSaving = ref(false)
const autoUpdateResult = ref('')
const gwsIntegration = ref<GwsIntegrationSettings | null>(null)
const gwsIntegrationLoaded = ref(false)
const gwsIntegrationError = ref('')

type GwsProfile = GwsIntegrationSettings['profiles'][number]

function gwsProfileStatus(profile: GwsProfile): string {
  if (profile.configured) return 'Authenticated'
  if (profile.client_secret_present) return 'Ready to auth'
  return 'Needs OAuth client'
}

function gwsProfileBadgeClass(profile: GwsProfile): string {
  if (profile.configured) return 'badge--success'
  if (profile.client_secret_present) return 'badge--warn'
  return 'badge--error'
}

const defaultGwsProfileName = computed(() => gwsIntegration.value?.default_profile || 'personal')

const gwsProfileOptions = computed(() => {
  const profiles = gwsIntegration.value?.profiles || []
  if (profiles.length) {
    return profiles.map((profile) => ({ name: profile.name, label: profile.label, email: profile.email }))
  }
  return [
    { name: 'personal', label: 'Personal Google account', email: '' },
    { name: 'work', label: 'Work Google account', email: '' },
  ]
})

function workspaceCustomGwsProfile(profile: string): boolean {
  const name = profile.trim()
  return Boolean(name) && !gwsProfileOptions.value.some((option) => option.name === name)
}

async function fetchGwsIntegration() {
  gwsIntegrationError.value = ''
  try {
    gwsIntegration.value = await api.get<GwsIntegrationSettings>('/api/integrations/gws')
  } catch (e: any) {
    gwsIntegrationError.value = `Failed to load Google Workspace integration: ${e?.message || e}`
  } finally {
    gwsIntegrationLoaded.value = true
  }
}

const gwsInstalling = ref(false)
const gwsInstallResult = ref('')

async function installGws() {
  gwsInstalling.value = true
  gwsInstallResult.value = 'Installing @googleworkspace/cli via npm…'
  try {
    const res = await api.post<{ ok: boolean; output?: string; error?: string; integration?: GwsIntegrationSettings }>(
      '/api/integrations/gws/install',
      {},
    )
    if (res.ok) {
      if (res.integration) gwsIntegration.value = res.integration
      gwsInstallResult.value = 'gws installed successfully.'
    } else {
      gwsInstallResult.value = res.error || 'Installation failed.'
    }
  } catch (e: any) {
    gwsInstallResult.value = `Error installing gws: ${e?.message || e}`
  } finally {
    gwsInstalling.value = false
  }
}

const gwsSavingProfile = ref<string | null>(null)
const gwsAuthUrls = ref<Record<string, string>>({})
const gwsRedirectUrls = ref<Record<string, string>>({})

async function handleClientSecretUpload(event: Event, profileName: string) {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return

  gwsSavingProfile.value = profileName
  try {
    const content = await file.text()
    const updated = await api.post<GwsIntegrationSettings>('/api/integrations/gws/client-secret', {
      profile: profileName,
      client_secret: content,
    })
    gwsIntegration.value = updated
  } catch (e: any) {
    alert(e?.message || 'Failed to upload client secret')
  } finally {
    gwsSavingProfile.value = null
    target.value = ''
  }
}

async function startGwsAuth(profileName: string) {
  gwsSavingProfile.value = profileName
  try {
    const res = await api.post<{ auth_url: string }>('/api/integrations/gws/auth-url', {
      profile: profileName,
    })
    gwsAuthUrls.value[profileName] = res.auth_url
    gwsRedirectUrls.value[profileName] = ''
    window.open(res.auth_url, '_blank')
  } catch (e: any) {
    alert(e?.message || 'Failed to generate authorization URL')
  } finally {
    gwsSavingProfile.value = null
  }
}

async function exchangeGwsCode(profileName: string) {
  const code = gwsRedirectUrls.value[profileName]?.trim()
  if (!code) return

  gwsSavingProfile.value = profileName
  try {
    const updated = await api.post<GwsIntegrationSettings>('/api/integrations/gws/exchange', {
      profile: profileName,
      code: code,
    })
    gwsIntegration.value = updated
    delete gwsAuthUrls.value[profileName]
    delete gwsRedirectUrls.value[profileName]
  } catch (e: any) {
    alert(e?.message || 'Failed to complete connection')
  } finally {
    gwsSavingProfile.value = null
  }
}

function cancelGwsAuth(profileName: string) {
  delete gwsAuthUrls.value[profileName]
  delete gwsRedirectUrls.value[profileName]
}

async function disconnectGwsProfile(profileName: string, deleteClientSecret: boolean) {
  const message = deleteClientSecret
    ? `Are you sure you want to delete the OAuth Client Secret for the ${profileName} profile?`
    : `Are you sure you want to disconnect/sign out the ${profileName} Google account?`

  if (!confirm(message)) return

  gwsSavingProfile.value = profileName
  try {
    const updated = await api.post<GwsIntegrationSettings>('/api/integrations/gws/disconnect', {
      profile: profileName,
      delete_client_secret: deleteClientSecret,
    })
    gwsIntegration.value = updated
    cancelGwsAuth(profileName)
  } catch (e: any) {
    alert(e?.message || 'Failed to update profile connection')
  } finally {
    gwsSavingProfile.value = null
  }
}

async function saveAutoUpdateGithubSkills() {
  autoUpdateSaving.value = true
  autoUpdateResult.value = ''
  try {
    const res = await api.patch<ProviderConfigSettings>('/api/settings/providers', {
      auto_update_github_skills: autoUpdateGithubSkills.value,
    })
    if (res.auto_update_github_skills !== undefined) {
      autoUpdateGithubSkills.value = res.auto_update_github_skills
    }
    if (providerKeys.value) {
      providerKeys.value = res
    }
    notifySaved('Saved.')
  } catch (e: any) {
    autoUpdateResult.value = `Error: ${e?.message || e}`
    autoUpdateGithubSkills.value = !autoUpdateGithubSkills.value
  } finally {
    autoUpdateSaving.value = false
  }
}

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
  
  if (!hasKeyChanges) {
    providerKeysResult.value = 'No changes to save.'
    providerKeysSaving.value = false
    setTimeout(() => { providerKeysResult.value = '' }, 2000)
    return
  }
  
  try {
    const payload: any = { keys: patchKeys }
    
    const res = await api.patch<ProviderConfigSettings>('/api/settings/providers', payload)
    providerKeys.value = res
    for (const key in res.keys) {
      providerKeyInputs.value[key] = ''
    }
    if (res.auto_update_github_skills !== undefined) {
      autoUpdateGithubSkills.value = res.auto_update_github_skills
    }
    providerKeysResult.value = ''
    await restartAndReload('Configuration saved. Restarting Ciaobot to apply…')
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
      routinesResult.value = ''
      await restartAndReload('Local whisper engine installed. Restarting Ciaobot to load the model…')
    } else {
      routinesResult.value = 'Installation failed.'
    }
  } catch (e: any) {
    routinesResult.value = `Error installing engine: ${e?.message || e}`
  } finally {
    voiceInstalling.value = false
  }
}

const ttsInstalling = ref(false)

async function installLocalTts() {
  ttsInstalling.value = true
  routinesResult.value = 'Installing local Kokoro engine...'
  try {
    const res = await api.post<{ ok: boolean; output?: string }>('/api/tts/install-local', {})
    if (res.ok) {
      routinesResult.value = ''
      await restartAndReload('Local Kokoro engine installed. Restarting Ciaobot to load the model…')
    } else {
      routinesResult.value = 'Installation failed.'
    }
  } catch (e: any) {
    routinesResult.value = `Error installing engine: ${e?.message || e}`
  } finally {
    ttsInstalling.value = false
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

async function fetchCommands() {
  commandsError.value = ''
  try {
    const res = await api.get<CommandsResponse>('/api/commands')
    commands.value = Array.isArray(res.commands) ? res.commands : []
  } catch (e: any) {
    commandsError.value = `Failed to load commands: ${e?.message || e}`
  } finally {
    commandsLoaded.value = true
  }
}

async function fetchAgentAssets() {
  agentAssetsError.value = ''
  try {
    agentAssets.value = await api.get<AgentAssetsResponse>('/api/agent-assets')
  } catch (e: any) {
    agentAssetsError.value = `Failed to load agent assets: ${e?.message || e}`
  } finally {
    agentAssetsLoaded.value = true
  }
}

const healthFixPending = ref(false)
const healthFixError = ref('')

async function fixWorkspaceHealth() {
  healthFixPending.value = true
  healthFixError.value = ''
  try {
    // Applies the checks' automatic remedies server-side (create missing
    // scaffold files, re-link skills), then refresh so the card reflects
    // the fresh report.
    await api.post('/api/workspace-health/fix', {})
    await fetchAgentAssets()
  } catch (e: any) {
    healthFixError.value = e?.message || 'fix failed'
  } finally {
    healthFixPending.value = false
  }
}

const customSkills = computed(() => {
  return skillsInventory.value?.skills.filter(s => s.label === 'custom') || []
})

const githubSkills = computed(() => {
  return skillsInventory.value?.skills.filter(s => s.label === 'github') || []
})

const instructionAssets = computed(() => agentAssets.value?.instructions || [])
const subagentAssets = computed(() => agentAssets.value?.subagents || [])
const commandAssets = computed(() => agentAssets.value?.commands || [])
const workspaceHealth = computed<WorkspaceHealthResponse | null>(() => agentAssets.value?.health || null)
const prioritizedHealthChecks = computed(() => {
  const checks = workspaceHealth.value?.checks || []
  const rank: Record<string, number> = { error: 0, warn: 1 }
  return [...checks]
    .filter(check => check.status === 'error' || check.status === 'warn')
    .sort((a, b) => (rank[a.status] ?? 3) - (rank[b.status] ?? 3))
})

function healthBadgeClass(status: string): string {
  if (status === 'ok') return 'badge--success'
  if (status === 'warn') return 'badge--warn'
  if (status === 'error') return 'badge--error'
  return 'badge--muted'
}

const showAddSubagent = ref(false)
const newSubagentName = ref('')
const newSubagentDescription = ref('')
const newSubagentPrompt = ref('')
const addingSubagent = ref(false)
const addSubagentResult = ref('')
const addSubagentError = ref(false)
const editingSubagent = ref<string | null>(null)
const editSubagentDescription = ref('')
const editSubagentContent = ref('')
const savingSubagent = ref<string | null>(null)

const showAddCommand = ref(false)
const newCommandName = ref('')
const newCommandDescription = ref('')
const newCommandArgumentHint = ref('')
const newCommandPrompt = ref('')
const addingCommand = ref(false)
const addCommandResult = ref('')
const addCommandError = ref(false)
const editingCommand = ref<string | null>(null)
const editCommandDescription = ref('')
const editCommandArgumentHint = ref('')
const editCommandContent = ref('')
const savingCommand = ref<string | null>(null)
const assetLifecycleResult = ref('')
const assetLifecycleError = ref(false)

function resetSubagentForm(clearResult = true) {
  newSubagentName.value = ''
  newSubagentDescription.value = ''
  newSubagentPrompt.value = ''
  if (clearResult) {
    addSubagentResult.value = ''
    addSubagentError.value = false
  }
}

function resetCommandForm(clearResult = true) {
  newCommandName.value = ''
  newCommandDescription.value = ''
  newCommandArgumentHint.value = ''
  newCommandPrompt.value = ''
  if (clearResult) {
    addCommandResult.value = ''
    addCommandError.value = false
  }
}

function bodyWithoutFrontmatter(content: string): string {
  if (content.startsWith('---')) {
    const parts = content.split('---')
    if (parts.length >= 3) {
      return parts.slice(2).join('---').trim()
    }
  }
  return content.trim()
}

function toggleAddSubagent() {
  showAddSubagent.value = !showAddSubagent.value
  resetSubagentForm()
}

function toggleAddCommand() {
  showAddCommand.value = !showAddCommand.value
  resetCommandForm()
}

async function addSubagent() {
  if (!newSubagentName.value.trim() || !newSubagentDescription.value.trim() || !newSubagentPrompt.value.trim()) return
  addingSubagent.value = true
  addSubagentResult.value = 'Creating subagent...'
  addSubagentError.value = false
  try {
    const res = await api.post<CreatedAgentAssetResponse<SubagentAsset>>('/api/agent-assets/subagents', {
      name: newSubagentName.value.trim(),
      description: newSubagentDescription.value.trim(),
      prompt: newSubagentPrompt.value.trim(),
    })
    addSubagentResult.value = ''
    notifySaved(`Created ${res.path}`, 'Subagent')
    resetSubagentForm(false)
    showAddSubagent.value = false
    await fetchAgentAssets()
  } catch (e: any) {
    addSubagentError.value = true
    addSubagentResult.value = `Error: ${e?.message || e}`
  } finally {
    addingSubagent.value = false
  }
}

async function addCommand() {
  if (!newCommandName.value.trim() || !newCommandDescription.value.trim() || !newCommandPrompt.value.trim()) return
  addingCommand.value = true
  addCommandResult.value = 'Creating command...'
  addCommandError.value = false
  try {
    const res = await api.post<CreatedAgentAssetResponse<CommandAsset>>('/api/agent-assets/commands', {
      name: newCommandName.value.trim(),
      description: newCommandDescription.value.trim(),
      argument_hint: newCommandArgumentHint.value.trim(),
      prompt: newCommandPrompt.value.trim(),
    })
    addCommandResult.value = ''
    notifySaved(`Created ${res.path}`, 'Command')
    resetCommandForm(false)
    showAddCommand.value = false
    await Promise.all([fetchAgentAssets(), fetchCommands()])
  } catch (e: any) {
    addCommandError.value = true
    addCommandResult.value = `Error: ${e?.message || e}`
  } finally {
    addingCommand.value = false
  }
}

function startEditSubagent(agent: SubagentAsset) {
  if (!agent.editable) return
  editingSubagent.value = agent.name
  editSubagentDescription.value = agent.description || ''
  editSubagentContent.value = bodyWithoutFrontmatter(agent.content || '')
  assetLifecycleResult.value = ''
  assetLifecycleError.value = false
}

function cancelEditSubagent() {
  editingSubagent.value = null
  editSubagentDescription.value = ''
  editSubagentContent.value = ''
}

async function saveSubagent(agent: SubagentAsset) {
  if (!agent.editable || !editingSubagent.value) return
  savingSubagent.value = agent.name
  assetLifecycleResult.value = 'Saving subagent...'
  assetLifecycleError.value = false
  try {
    await api.patch<CreatedAgentAssetResponse<SubagentAsset>>(`/api/agent-assets/subagents/${encodeURIComponent(agent.name)}`, {
      description: editSubagentDescription.value.trim(),
      content: editSubagentContent.value.trim(),
    })
    assetLifecycleResult.value = ''
    notifySaved(`Saved ${agent.name}. Restart or sync Claude Code sessions to pick it up.`, 'Subagent')
    cancelEditSubagent()
    await fetchAgentAssets()
  } catch (e: any) {
    assetLifecycleError.value = true
    assetLifecycleResult.value = `Error: ${e?.message || e}`
  } finally {
    savingSubagent.value = null
  }
}

async function deleteSubagent(agent: SubagentAsset) {
  if (!agent.editable) return
  if (!window.confirm(`Delete custom subagent "${agent.name}"?`)) return
  savingSubagent.value = agent.name
  assetLifecycleResult.value = 'Deleting subagent...'
  assetLifecycleError.value = false
  try {
    await api.del(`/api/agent-assets/subagents/${encodeURIComponent(agent.name)}`)
    assetLifecycleResult.value = ''
    notifySaved(`Deleted ${agent.name}. Restart or sync Claude Code sessions to pick it up.`, 'Subagent')
    if (editingSubagent.value === agent.name) cancelEditSubagent()
    await fetchAgentAssets()
  } catch (e: any) {
    assetLifecycleError.value = true
    assetLifecycleResult.value = `Error: ${e?.message || e}`
  } finally {
    savingSubagent.value = null
  }
}

function startEditCommand(command: CommandAsset) {
  if (!command.editable) return
  editingCommand.value = command.name
  editCommandDescription.value = command.description || ''
  editCommandArgumentHint.value = command.argument_hint || ''
  editCommandContent.value = bodyWithoutFrontmatter(command.content || '')
  assetLifecycleResult.value = ''
  assetLifecycleError.value = false
}

function cancelEditCommand() {
  editingCommand.value = null
  editCommandDescription.value = ''
  editCommandArgumentHint.value = ''
  editCommandContent.value = ''
}

async function saveCommand(command: CommandAsset) {
  if (!command.editable || !editingCommand.value) return
  savingCommand.value = command.name
  assetLifecycleResult.value = 'Saving command...'
  assetLifecycleError.value = false
  try {
    await api.patch<CreatedAgentAssetResponse<CommandAsset>>(`/api/agent-assets/commands/${encodeURIComponent(command.name)}`, {
      description: editCommandDescription.value.trim(),
      argument_hint: editCommandArgumentHint.value.trim(),
      content: editCommandContent.value.trim(),
    })
    assetLifecycleResult.value = ''
    notifySaved(`Saved /${command.name}. Restart or sync Claude Code sessions to pick it up.`, 'Command')
    cancelEditCommand()
    await Promise.all([fetchAgentAssets(), fetchCommands()])
  } catch (e: any) {
    assetLifecycleError.value = true
    assetLifecycleResult.value = `Error: ${e?.message || e}`
  } finally {
    savingCommand.value = null
  }
}

async function deleteCommand(command: CommandAsset) {
  if (!command.editable) return
  if (!window.confirm(`Delete custom command "/${command.name}"?`)) return
  savingCommand.value = command.name
  assetLifecycleResult.value = 'Deleting command...'
  assetLifecycleError.value = false
  try {
    await api.del(`/api/agent-assets/commands/${encodeURIComponent(command.name)}`)
    assetLifecycleResult.value = ''
    notifySaved(`Deleted /${command.name}. Restart or sync Claude Code sessions to pick it up.`, 'Command')
    if (editingCommand.value === command.name) cancelEditCommand()
    await Promise.all([fetchAgentAssets(), fetchCommands()])
  } catch (e: any) {
    assetLifecycleError.value = true
    assetLifecycleResult.value = `Error: ${e?.message || e}`
  } finally {
    savingCommand.value = null
  }
}

const showAddGithubSkill = ref(false)
const githubSource = ref('')
const githubSkillName = ref('')
const addingGithubSkill = ref(false)
const addGithubSkillResult = ref('')
const addGithubSkillError = ref(false)

function toggleAddGithubSkill() {
  showAddGithubSkill.value = !showAddGithubSkill.value
  githubSource.value = ''
  githubSkillName.value = ''
  addGithubSkillResult.value = ''
  addGithubSkillError.value = false
}

async function addGithubSkill() {
  if (!githubSource.value.trim()) return
  addingGithubSkill.value = true
  addGithubSkillResult.value = 'Adding skill...'
  addGithubSkillError.value = false
  try {
    const res = await api.post<{ ok: boolean; message?: string; error?: string }>('/api/admin/skills/add', {
      source: githubSource.value.trim(),
      skill: githubSkillName.value.trim() || undefined,
    })
    if (res.ok) {
      addGithubSkillResult.value = ''
      notifySaved(res.message || 'Skill added successfully.', 'Skills')
      githubSource.value = ''
      githubSkillName.value = ''
      showAddGithubSkill.value = false
      await fetchSkills()
    } else {
      addGithubSkillError.value = true
      addGithubSkillResult.value = res.error || 'Failed to add skill.'
    }
  } catch (e: any) {
    addGithubSkillError.value = true
    addGithubSkillResult.value = `Error: ${e?.message || e}`
  } finally {
    addingGithubSkill.value = false
  }
}

async function createSkillViaChat() {
  const activeProj = projectStore.activeProject
  let projectId = activeProj?.project_id
  if (!projectId) {
    projectId = projectStore.workspaceProjects[0]?.project_id
  }
  if (!projectId) {
    projectId = projectStore.projects[0]?.project_id
  }
  if (!projectId) {
    alert('Please create a project first before starting a chat.')
    return
  }

  try {
    const chat = await projectStore.createChat(projectId, 'New Custom Skill')
    if (chat) {
      const prompt = 'I want to create a new custom skill. Please guide me through writing a new skill (creating the SKILL.md under the skills/ directory).'
      projectStore.sendMessage(chat.chat_id, prompt)
    }
  } catch (e: any) {
    alert(`Failed to start chat: ${e?.message || e}`)
  }
}


const automationItems = ref<AutomationProcess[]>([])
const automationLoaded = ref(false)
const automationError = ref('')

function getJobTelemetry(job: string): AutomationProcess | undefined {
  return automationItems.value.find((i) => i.job === job)
}
function getTelemetryBadgeClass(status: string | undefined): string {
  if (status === 'ok') return 'badge--success'
  if (status === 'error') return 'badge--error'
  if (status === 'skipped') return 'badge--warn'
  return 'badge--muted'
}

function getJobStatus(job: string): string {
  const item = getJobTelemetry(job)
  return item?.last_run ? item.last_run.status : 'never run'
}
function getJobBadgeClass(job: string): string {
  const status = getJobTelemetry(job)?.last_run?.status
  return getTelemetryBadgeClass(status)
}
function getJobDuration(job: string): string {
  const dur = getJobTelemetry(job)?.last_run?.duration_ms
  return formatDuration(dur) || '0ms'
}
function getJobLastRunLabel(job: string): string {
  const item = getJobTelemetry(job)
  return item ? lastRunLabel(item) : ''
}
function getJobLastError(job: string): string {
  const item = getJobTelemetry(job)
  return item ? lastError(item) : ''
}
function hasJobLastRun(job: string): boolean {
  return !!getJobTelemetry(job)?.last_run
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

// ── Workspaces settings (Workspaces tab) ───────────────────────────────────
const projectStore = useProjectStore()

// Transient success feedback. Routes through the app-wide in-app toast (the
// same auto-dismissing popup used for routine/chat notifications) instead of
// leaving persistent inline text under the form.
function notifySaved(body: string, title = 'Settings') {
  projectStore.pushToast({ chat_id: '', title, body })
}
const workspacesLoaded = ref(false)
const workspacesError = ref('')
const workspacesSaving = ref<string | null>(null)
const workspacesResult = ref('')
const showNewWorkspace = ref(false)

type WorkspaceForm = {
  name: string
  vault_root: string
  default_provider: WorkspaceProvider
  default_model: string
  gws_profile: string
  model_bucket: string
  disallowed_tools: string
  claude_ai_mcps: 'on' | 'off'
}

function defaultWorkspaceProvider(): WorkspaceProvider {
  return projectStore.workspaceProviderOptions[0]?.value || 'claude'
}

function blankWorkspaceForm(): WorkspaceForm {
  return {
    name: '',
    vault_root: '',
    default_provider: defaultWorkspaceProvider(),
    default_model: '',
    gws_profile: '',
    model_bucket: '',
    disallowed_tools: '',
    claude_ai_mcps: 'on',
  }
}

function workspaceToForm(ws: WorkspaceInfo): WorkspaceForm {
  const mcps = ws.claude_ai_mcps
  return {
    name: ws.name,
    vault_root: ws.vault_root || '',
    default_provider: ws.default_provider || 'claude',
    default_model: ws.default_model || '',
    gws_profile: ws.gws_profile || '',
    model_bucket: ws.model_bucket || '',
    disallowed_tools: Array.isArray(ws.disallowed_tools) ? ws.disallowed_tools.join(', ') : '',
    claude_ai_mcps: mcps === false ? 'off' : 'on',
  }
}

function claudeAiMcpsPayload(value: 'default' | 'on' | 'off'): boolean | null {
  if (value === 'on') return true
  if (value === 'off') return false
  return null
}

function workspaceCustomDefaultModel(model: string): boolean {
  const value = model.trim()
  return Boolean(value) && !modelTiers.some((tier) => tier.key === value)
}

function isCustomWorkspaceModel(model: string): boolean {
  const value = model.trim()
  if (!value) return false
  if (modelTiers.some((tier) => tier.key === value)) return false
  const options = routines.value?.model_options
  if (!options) return true
  const allKnown = [
    ...(options.ollama_local || []),
    ...(options.ollama_cloud || []),
    ...(options.openrouter || []),
  ]
  return !allKnown.includes(value)
}

function workspaceModelSectionsForProvider(provider: WorkspaceProvider, currentModelValue: string): ModelSection[] {
  const tiers: TierKey[] = ['haiku', 'sonnet', 'opus']
  const modelBadges: Record<string, string[]> = {}
  
  for (const tier of tiers) {
    const actualModel = tierModelForProvider(provider as AliasProviderKey, tier)
    if (actualModel && actualModel !== tier) {
      modelBadges[tier] = [actualModel]
    }
  }

  const sections: ModelSection[] = [
    {
      key: provider,
      label: `${aliasProviderLabel(provider as AliasProviderKey)} Tiers`,
      models: tiers,
      modelBadges,
    }
  ]

  const v = (currentModelValue || '').trim()
  if (v && !tiers.includes(v as TierKey)) {
    sections.push({
      key: 'custom',
      label: 'Custom override',
      models: [v],
    })
  }

  return sections
}

const newWorkspaceModelSections = computed<ModelSection[]>(() => {
  return workspaceModelSectionsForProvider(newWorkspaceForm.value.default_provider, newWorkspaceForm.value.default_model)
})

function workspaceModelSectionsForForm(form: WorkspaceForm): ModelSection[] {
  return workspaceModelSectionsForProvider(form.default_provider, form.default_model)
}

const workspaceForms = ref<WorkspaceForm[]>([])
const newWorkspaceForm = ref<WorkspaceForm>(blankWorkspaceForm())

const workspaceProviderOptions = computed(() =>
  projectStore.workspaceProviderOptions.length
    ? projectStore.workspaceProviderOptions
    : [{ value: 'claude' as WorkspaceProvider, label: 'Claude' }]
)

// Empty default_model inherits the app-wide default; name it in the picker
// so "inherit" is not a mystery value.
const workspaceInheritPlaceholder = computed(() =>
  projectStore.workspaceAppDefaultModel
    ? `Inherit default (${projectStore.workspaceAppDefaultModel})`
    : 'Inherit default model'
)

function disallowedToolsPayload(raw: string): string[] | null {
  const cleaned = raw.trim()
  if (!cleaned) return null
  return cleaned.split(',').map((s) => s.trim()).filter(Boolean)
}

async function fetchWorkspacesList() {
  workspacesError.value = ''
  try {
    await projectStore.fetchWorkspaces()
    workspaceForms.value = projectStore.workspaces.map(workspaceToForm)
    if (!workspaceProviderOptions.value.some((provider) => provider.value === newWorkspaceForm.value.default_provider)) {
      newWorkspaceForm.value.default_provider = defaultWorkspaceProvider()
    }
  } catch (e: any) {
    workspacesError.value = `Failed to load workspaces: ${e?.message || e}`
  } finally {
    workspacesLoaded.value = true
  }
}

async function saveWorkspace(name: string) {
  const form = workspaceForms.value.find((f) => f.name === name)
  if (!form) return
  workspacesSaving.value = name
  workspacesResult.value = ''
  try {
    await projectStore.updateWorkspace(name, {
      vault_root: form.vault_root,
      default_provider: form.default_provider,
      default_model: form.default_model,
      gws_profile: form.gws_profile,
      model_bucket: form.model_bucket,
      disallowed_tools: disallowedToolsPayload(form.disallowed_tools),
      claude_ai_mcps: claudeAiMcpsPayload(form.claude_ai_mcps),
    })
    notifySaved(`Workspace "${name}" saved.`, 'Workspaces')
    await fetchWorkspacesList()
  } catch (e: any) {
    workspacesResult.value = `Error: ${e?.message || e}`
  } finally {
    workspacesSaving.value = null
  }
}

async function createNewWorkspace() {
  const form = newWorkspaceForm.value
  if (!form.name.trim()) {
    workspacesResult.value = 'Enter a workspace name.'
    return
  }
  workspacesSaving.value = 'new'
  workspacesResult.value = ''
  try {
    await projectStore.createWorkspace({
      name: form.name.trim(),
      vault_root: form.vault_root,
      default_provider: form.default_provider,
      default_model: form.default_model,
      gws_profile: form.gws_profile,
      model_bucket: form.model_bucket,
      disallowed_tools: disallowedToolsPayload(form.disallowed_tools),
      claude_ai_mcps: claudeAiMcpsPayload(form.claude_ai_mcps),
    })
    notifySaved(`Workspace "${form.name.trim()}" created.`, 'Workspaces')
    showNewWorkspace.value = false
    newWorkspaceForm.value = blankWorkspaceForm()
    await fetchWorkspacesList()
  } catch (e: any) {
    workspacesResult.value = `Error: ${e?.message || e}`
  } finally {
    workspacesSaving.value = null
  }
}

async function removeWorkspace(name: string) {
  if (!window.confirm(`Delete workspace "${name}"? Chats keep their history but lose workspace routing.`)) return
  workspacesSaving.value = name
  workspacesResult.value = ''
  try {
    await projectStore.deleteWorkspace(name)
    notifySaved(`Workspace "${name}" deleted.`, 'Workspaces')
    await fetchWorkspacesList()
  } catch (e: any) {
    workspacesResult.value = `Error: ${e?.message || e}`
  } finally {
    workspacesSaving.value = null
  }
}

onMounted(async () => {
  loadAppearanceSettings()
  fetchSkills()
  fetchCommands()
  fetchAgentAssets()
  fetchLocalStatus().then(() => {
    if (localStatus.value?.dev_mode) refreshDebugIssues()
  })
  fetchRoutines()
  fetchAutomation()
  fetchPackageStatus()
  fetchProviderKeys()
  fetchGwsIntegration()
  fetchWorkspacesList()
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

const restarting = ref(false)
const restartMessage = ref('')

// Show the full-screen restart overlay, then wait for the server to come back
// before reloading. Used by any action that triggers a server restart (model
// installs, provider key changes) so the UI never lands on a half-booted
// server and shows a "Failed to fetch" error.
async function restartAndReload(message: string) {
  restartMessage.value = message
  restarting.value = true
  await reloadWhenServerReady()
}

async function reloadWhenServerReady(timeoutMs = 120000) {
  // The deploy endpoint returns ok immediately while the restart is only
  // scheduled (~2s later). Reloading on a fixed timer races the server
  // coming back up and lands on a dead/half-booted process -> grey screen.
  // Instead, poll /api/startup-status (the same signal App.vue's boot overlay
  // uses): wait for the server to go down, then reload once it reports
  // overall_ready again. Fallback to a forced reload on timeout.
  const start = Date.now()
  let sawDown = false
  while (true) {
    try {
      const res = await fetch('/api/startup-status')
      if (res.ok) {
        const data = await res.json()
        if (!data.overall_ready) {
          sawDown = true
        } else if (sawDown) {
          location.reload()
          return
        }
      } else {
        // server returned non-ok status (e.g. 502 Bad Gateway during restart)
        sawDown = true
      }
    } catch {
      // server is down mid-restart (network error / connection refused)
      sawDown = true
    }
    if (Date.now() - start > timeoutMs) {
      location.reload()
      return
    }
    await new Promise(r => setTimeout(r, 1000))
  }
}

async function doDeploy(confirmWarnings = false) {
  if (!confirmWarnings && !confirm('Restart? This will pull latest, rebuild, and restart.')) return
  actionPending.value = 'deploy'
  actionResult.value = ''
  deploySteps.value = []
  try {
    const r = await api.post<DeployResult>('/api/admin/deploy', { confirm_warnings: confirmWarnings })
    deploySteps.value = r.steps
    if (r.ok) {
      actionResult.value = 'Restart complete. Waiting for server to come back, then reloading...'
      reloadWhenServerReady()
    } else {
      actionResult.value = 'Restart failed. See steps above.'
    }
  } catch (e: any) {
    const payload = e?.payload
    if (Array.isArray(payload?.steps)) deploySteps.value = payload.steps
    if (payload?.blockers) {
      alert(`Restart blocked by secrets:\n\n${payload.blockers.join('\n')}`)
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

async function fixDeployErrorInChat() {
  let errorMsg = ''
  if (deploySteps.value.some(s => !s.ok)) {
    errorMsg = deploySteps.value
      .filter(s => !s.ok)
      .map(s => `Step: ${s.step}\nOutput:\n${s.output || 'No output'}`)
      .join('\n\n')
  } else {
    errorMsg = actionResult.value
  }

  const defaultWorkspace = projectStore.workspaceOptions[0]?.name || 'personal'

  if (projectStore.activeWorkspace !== defaultWorkspace) {
    await projectStore.switchWorkspace(defaultWorkspace)
  }

  let project = projectStore.projects.find(p => p.workspace === defaultWorkspace)
  if (!project) {
    try {
      project = await projectStore.createProject('General')
    } catch (e: any) {
      alert(`Failed to create project: ${e?.message || e}`)
      return
    }
  }

  try {
    const chat = await projectStore.createChat(project.project_id, 'Deploy Fix')
    if (chat) {
      const prompt = `I encountered an error during deployment:\n\n${errorMsg}\n\nPlease help me fix this.`
      projectStore.sendMessage(chat.chat_id, prompt)
      const { router } = await import('../router')
      router.push(`/chat/${chat.chat_id}`)
    }
  } catch (e: any) {
    alert(`Failed to start chat: ${e?.message || e}`)
  }
}

// ── Debug: runtime issue log → self-fix chat (dev mode only) ─────────────
const debugPending = ref(false)
const debugIssues = ref<DebugIssueReport | null>(null)
const debugSummary = computed(() => {
  const r = debugIssues.value
  if (!r) return ''
  if (!r.has_issues) return 'No runtime issues logged.'
  return `${r.failed_jobs.length} failed job run(s), ${r.error_log_lines} error-log line(s).`
})

async function refreshDebugIssues() {
  try {
    debugIssues.value = await api.get<DebugIssueReport>('/api/debug/issues')
  } catch {
    /* endpoint is 404 unless dev mode; leave null */
  }
}

async function fixIssuesInChat() {
  debugPending.value = true
  try {
    await refreshDebugIssues()
    const report = debugIssues.value
    if (!report?.has_issues) return

    const defaultWorkspace = projectStore.workspaceOptions[0]?.name || 'personal'
    if (projectStore.activeWorkspace !== defaultWorkspace) {
      await projectStore.switchWorkspace(defaultWorkspace)
    }
    let project = projectStore.projects.find(p => p.workspace === defaultWorkspace)
    if (!project) {
      project = await projectStore.createProject('General')
    }
    const chat = await projectStore.createChat(project.project_id, 'Issue Triage')
    if (chat) {
      const prompt = `Here is the current runtime issue report from this Ciaobot instance (server error log tail plus failed background jobs):\n\n${report.report_text}\n\nPlease triage these issues: group them by root cause, note frequency and impact, investigate the top causes in the app and workspace, and apply low-risk fixes directly. Report anything riskier that needs my approval.`
      projectStore.sendMessage(chat.chat_id, prompt)
      const { router } = await import('../router')
      router.push(`/chat/${chat.chat_id}`)
    }
  } catch (e: any) {
    alert(`Failed to start issue-triage chat: ${e?.message || e}`)
  } finally {
    debugPending.value = false
  }
}

async function doLogout() {
  // Clears the HttpOnly session cookie via /api/auth/logout (which JS can't
  // delete from document.cookie). After success the auth store routes back
  // to /login and the next login re-issues the cookie with the wider
  // Host-only cookie: no Domain attribute, scoped to the exact host.
  if (!confirm('Log out of Ciaobot?')) return
  actionPending.value = 'logout'
  actionResult.value = ''
  try {
    await useAuthStore().logout()
  } catch (e: any) {
    actionResult.value = `Error: ${e.message || 'logout failed'}`
  }
  actionPending.value = null
}

// ── Workspace git sync (current branch) ──────────────────────────────────
const localStatus = ref<LocalStatus | null>(null)

async function fetchLocalStatus() {
  try {
    localStatus.value = await api.get<LocalStatus>('/api/local/status')
  } catch {
    /* leave null on failure */
  }
}

async function localHandback(confirmWarnings = false) {
  if (!confirmWarnings && !confirm('Sync changes with the remote repository?')) return

  actionPending.value = 'snapshot'
  actionResult.value = ''

  try {
    const r = await api.post<any>('/api/local/handback', { confirm_warnings: confirmWarnings })
    if (r?.ok === false) {
      actionResult.value = `${r.step}: ${r.error}`
    } else if (r?.merged === true) {
      actionResult.value = 'Synced with remote repository.'
    } else if (r?.conflict === true) {
      actionResult.value = 'Sync conflict — opened a chat to resolve it. Answer it, then Sync again.'
    }
    await fetchLocalStatus()
  } catch (e: any) {
    const payload = e?.payload
    if (payload?.blockers) {
      alert(`Sync blocked by secrets:\n\n${payload.blockers.join('\n')}`)
      actionResult.value = 'Blocked by secrets.'
    } else if (payload?.warnings) {
      if (confirm(`Warnings found:\n\n${payload.warnings.join('\n')}\n\nDo you want to proceed anyway?`)) {
        actionPending.value = null
        return localHandback(true)
      }
      actionResult.value = 'Cancelled by user due to warnings.'
    } else {
      actionResult.value = `Error: ${e.message || 'sync failed'}`
    }
  }
  actionPending.value = null
}

// ── Package update ────────────────────────────────────────────────────────
const packageStatus = ref<any>(null)
const packageLoading = ref(false)
const packageUpdating = ref(false)
const packageResult = ref('')
const showUpdatePanel = ref(false)
const changelogLoading = ref(false)
const changelog = ref<any>({ commits: [], compare_url: '', error: '' })

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

async function openUpdatePanel() {
  showUpdatePanel.value = true
  changelogLoading.value = true
  changelog.value = { commits: [], compare_url: '', error: '' }
  try {
    changelog.value = await api.get<any>('/api/package/changelog')
  } catch (e: any) {
    changelog.value = { commits: [], compare_url: '', error: e?.message || 'unknown error' }
  } finally {
    changelogLoading.value = false
  }
}

async function doPackageUpdate() {
  packageUpdating.value = true
  packageResult.value = 'Updating Ciaobot and restarting...'
  try {
    const res = await api.post<any>('/api/package/update')
    if (res.ok) {
      showUpdatePanel.value = false
      packageResult.value = ''
      await restartAndReload('Update complete. Restarting Ciaobot with the latest version…')
    } else {
      packageResult.value = `Update failed: ${res.error || 'unknown error'}`
      await fetchPackageStatus()
    }
  } catch (e: any) {
    packageResult.value = `Update failed: ${e.message || 'unknown error'}`
    await fetchPackageStatus()
  } finally {
    packageUpdating.value = false
  }
}

</script>

<style scoped>
.settings-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  container-type: inline-size;
}
.pane-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  align-items: center;
}
.card {
  width: min(100%, 1040px);
  margin: 0 auto;
  gap: var(--space-4);
  border-color: var(--border);
  box-shadow: 0 1px 0 color-mix(in srgb, var(--fg) 4%, transparent);
}
.section-title {
  letter-spacing: 0.08em;
}
.settings-card-header {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-bottom: var(--space-3);
  border-bottom: 1px solid var(--border);
}
.settings-card-header--split {
  flex-direction: row;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
}
.settings-card-header--split > div {
  min-width: 0;
}
.settings-card-header .hint {
  margin: var(--space-2) 0 0;
  max-width: 76ch;
}
.settings-card-header-actions {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
  justify-content: flex-end;
  flex: 0 0 auto;
}
.hint--compact {
  margin: 0;
}
.skill-scope-note {
  margin-top: var(--space-2);
  margin-bottom: var(--space-3);
}
.hint--spaced {
  margin-top: var(--space-2);
}
.hint--section-empty {
  margin: var(--space-1) 0 var(--space-3);
}
.inline-hint {
  margin-left: var(--space-2);
}
.muted-text {
  color: var(--fg2);
  font-size: var(--text-xs);
  font-weight: 400;
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
.action-row--spaced {
  margin-top: var(--space-3);
}
.action-row > button {
  flex: 1 1 0;
}
.settings-actions {
  justify-content: flex-end;
  margin-top: var(--space-2);
}
.settings-actions > button {
  flex: 0 0 auto;
  min-width: 150px;
}

.action-result {
  font-size: var(--text-sm);
  color: var(--fg2);
  padding: 4px 0;
}
.action-result--error {
  color: var(--error);
}
.action-result.--error {
  color: var(--error);
}
.action-result--prewrap {
  white-space: pre-wrap;
}

.health-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.health-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
}

.health-row--warn {
  border-color: color-mix(in srgb, var(--warning) 42%, var(--border));
}

.health-row--error {
  border-color: color-mix(in srgb, var(--error) 48%, var(--border));
}

.health-dot {
  width: 9px;
  height: 9px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--success);
  flex: 0 0 auto;
}

.health-row--warn .health-dot {
  background: var(--warning);
}

.health-row--error .health-dot {
  background: var(--error);
}

.health-main {
  min-width: 0;
  flex: 1 1 auto;
}

.health-title-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.health-title {
  font-weight: 700;
  color: var(--fg);
}

.health-path {
  min-width: 0;
  color: var(--fg2);
  font-family: var(--font-mono, var(--font));
  font-size: var(--text-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.deploy-steps {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 4px;
}

.deploy-step {
  display: flex;
  align-items: flex-start;
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

.deploy-step-error-output {
  margin: 6px 0 0 0;
  padding: 8px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-family: monospace;
  font-size: var(--text-xs);
  color: var(--fg);
  white-space: pre-wrap;
  word-break: break-all;
  max-width: 100%;
  overflow-x: auto;
}

.instance-toggle {
  display: flex;
  gap: 0;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--border);
  margin-top: 0;
  width: 100%;
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
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(380px, 490px);
  align-items: start;
  gap: var(--space-4);
  padding: 14px 0;
  border-top: 1px solid var(--border);
  margin-top: 0;
}
.routine-row--flush {
  border-top: 0;
  padding-top: 0;
}
.routine-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  max-width: 62ch;
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
.routine-telemetry {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  font-size: var(--text-xs);
  color: var(--fg2);
  flex-wrap: wrap;
}
.telemetry-meta {
  color: var(--fg3, var(--fg2));
}
.telemetry-error {
  color: var(--error);
  font-weight: 500;
  max-width: 250px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.routine-select,
.routine-input {
  max-width: none;
  min-width: 0;
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg);
  color: var(--fg);
  font-size: var(--text-sm);
  /* 44px min tap target height on mobile is handled by padding + font */
  min-height: 38px;
}
.routine-input::placeholder {
  color: var(--fg3);
}
.routine-select {
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  padding-right: 30px;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M2.5 4.5L6 8l3.5-3.5' fill='none' stroke='%23888' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/></svg>");
  background-repeat: no-repeat;
  background-position: right 10px center;
  background-size: 12px 12px;
}
.routine-select::-ms-expand {
  display: none;
}
.routine-context {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: var(--space-3) 0;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: color-mix(in srgb, var(--bg) 76%, transparent);
  font-size: var(--text-sm);
}
.routine-context > div {
  display: flex;
  flex-direction: column;
  gap: 0.35em;
  align-items: flex-start;
}
.routine-context code {
  overflow-wrap: anywhere;
}
.routine-model-controls {
  width: 100%;
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 136px;
  gap: 8px;
  align-items: start;
}
.routine-model-controls--single {
  grid-template-columns: 1fr;
}
.routine-model-controls .routine-select {
  max-width: none;
  min-width: 0;
  width: 100%;
}
.routine-model-hint {
  grid-column: 1 / -1;
  min-width: 0;
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.routine-model-hint code {
  font-size: var(--text-xs);
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--bg);
  color: var(--fg);
}
.routine-model-hint a {
  color: var(--accent);
  text-decoration: underline;
}
.routine-model-hint a:hover {
  color: var(--accent2);
}
.setting-row,
.credential-row {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) 0;
  border-top: 1px solid var(--border);
}
.setting-row--inline {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
}
.setting-row--flush {
  border-top: 0;
  padding-top: 0;
}
.setting-row--stack {
  margin-top: 0;
}
.setting-row-main {
  min-width: 0;
}
.setting-row-main--inline {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  width: 100%;
}
.credential-row .routine-input {
  max-width: none;
  min-width: 0;
  width: 100%;
}
.settings-control {
  width: min(100%, 430px);
  min-width: 320px;
  flex: 0 0 auto;
}
.settings-checkbox {
  width: 20px;
  height: 20px;
  flex: 0 0 auto;
  cursor: pointer;
  accent-color: var(--accent);
}
.voice-warning {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
.voice-install-btn {
  flex: 0 0 auto;
  margin-top: 1px;
  padding: 4px 10px;
  font-size: var(--text-xs);
}
.critique-model-picker {
  width: 100%;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.critique-picker-header {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 112px;
  align-items: center;
  gap: 8px;
}
.critique-picker-summary {
  min-width: 0;
  min-height: 32px;
  display: flex;
  align-items: center;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg);
  color: var(--fg2);
  font-size: var(--text-sm);
}
.critique-picker-header .btn-small {
  width: 100%;
  min-height: 32px;
}
.critique-chip-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.critique-chip {
  max-width: 100%;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg3);
  color: var(--fg);
  font-size: var(--text-xs);
  cursor: pointer;
}
.critique-chip span:first-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.critique-chip:disabled {
  cursor: default;
  opacity: 0.65;
}
.critique-option-groups {
  max-height: 230px;
  overflow-y: auto;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg);
}
.critique-option-group + .critique-option-group {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}
.critique-group-label {
  margin-bottom: 4px;
  color: var(--fg2);
  font-size: var(--text-xs);
  font-weight: 600;
}
.critique-option {
  min-height: 32px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  color: var(--fg);
  font-size: var(--text-xs);
  line-height: 1.3;
}
.critique-option input {
  flex: 0 0 auto;
  width: 16px;
  height: 16px;
}
.critique-option span {
  min-width: 0;
  overflow-wrap: anywhere;
}
.tier-provider-section {
  padding-top: var(--space-3);
  margin-top: var(--space-3);
  border-top: 1px solid var(--border);
}
.tier-provider-header {
  margin-bottom: var(--space-2);
}
.alias-provider-bar {
  display: flex;
  align-items: flex-end;
  margin-top: var(--space-3);
}
.alias-provider-field {
  width: min(360px, 100%);
}
.tier-provider-note {
  margin-top: var(--space-2);
}
.integration-warning {
  margin-top: var(--space-3);
}
.gws-profile-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
  margin-top: var(--space-3);
}
.gws-profile-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--bg) 72%, transparent);
}
.gws-profile-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.gws-profile-heading {
  min-width: 0;
}
.gws-profile-title {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-sm);
  font-weight: 700;
}
.gws-profile-purpose {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.35;
}
.gws-example-row,
.gws-workspace-chips {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  min-width: 0;
}
.gws-chip {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  padding: 2px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg3);
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.35;
}
.gws-chip--workspace {
  color: var(--fg);
}
.gws-profile-meta {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: var(--space-2);
  border-top: 1px solid var(--border);
  color: var(--fg2);
  font-size: var(--text-xs);
}
.gws-profile-meta > div {
  display: grid;
  grid-template-columns: 92px minmax(0, 1fr);
  align-items: start;
  gap: var(--space-2);
  min-width: 0;
}
.gws-profile-meta .dev-label {
  min-width: 0;
}
.gws-profile-meta code,
.gws-command {
  min-width: 0;
  overflow-wrap: anywhere;
}
.gws-command {
  display: inline-block;
}
.gws-boundary-note {
  margin-top: var(--space-3);
}
.status-text--ok {
  color: var(--success);
}
.status-text--warn {
  color: var(--warning, #b7791f);
}
.gws-profile-email {
  margin: 0;
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: 500;
}
.gws-profile-actions {
  margin-top: var(--space-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.gws-action-hint {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-xs);
}
.file-upload-btn {
  display: inline-block;
  text-align: center;
  cursor: pointer;
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 6px 12px;
  font-size: var(--text-xs);
  color: var(--fg);
  font-weight: 500;
  width: fit-content;
}
.file-upload-btn:hover {
  background: var(--bg2);
  border-color: var(--fg3);
}
.gws-btn-group {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.gws-auth-flow-box {
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.gws-flow-step {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.4;
}
.gws-auth-link {
  color: var(--accent);
  text-decoration: underline;
  font-weight: 500;
}
.gws-auth-link:hover {
  color: var(--accent2);
}
.gws-auth-input {
  font-size: var(--text-xs) !important;
  padding: 4px 8px !important;
}
.gws-flow-buttons {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-1);
}
.btn-outline-danger {
  background: transparent;
  border: 1px solid var(--error);
  color: var(--error);
}
.btn-outline-danger:hover {
  background: color-mix(in srgb, var(--error) 10%, transparent);
}
@container (max-width: 720px) {
  .pane-body {
    padding: var(--space-3);
  }
  .settings-card-header--split,
  .setting-row--inline,
  .setting-row-main--inline {
    flex-direction: column;
    align-items: stretch;
  }
  .settings-card-header-actions {
    justify-content: stretch;
  }
  .settings-card-header-actions .btn-small {
    flex: 1 1 auto;
  }
  .settings-actions > button {
    flex: 1 1 auto;
  }
  .settings-control {
    min-width: 0;
    width: 100%;
  }
  .routine-row {
    grid-template-columns: 1fr;
    gap: var(--space-3);
  }
  .routine-select,
  .routine-input {
    max-width: none;
    min-height: 44px;
  }
  .routine-model-controls {
    max-width: none;
    min-width: 0;
    width: 100%;
    grid-template-columns: 1fr;
  }
  .routine-model-hint {
    grid-column: 1;
  }
  .critique-model-picker {
    max-width: none;
    min-width: 0;
    width: 100%;
  }
  .gws-profile-list {
    grid-template-columns: 1fr;
  }
  .gws-profile-header {
    flex-direction: column;
    align-items: stretch;
  }
  .critique-picker-summary {
    min-height: 44px;
  }
  .critique-option {
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
.skill-list--section {
  margin-bottom: var(--space-4);
}
.settings-form-panel {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-2);
  margin: var(--space-3) 0 var(--space-4);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--bg) 72%, transparent);
}
.changelog-list {
  list-style: none;
  margin: var(--space-2) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  max-height: 260px;
  overflow-y: auto;
}
.changelog-list li {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  line-height: 1.4;
}
.changelog-sha {
  flex: 0 0 auto;
  font-size: 0.85em;
  opacity: 0.7;
}
.changelog-subject {
  min-width: 0;
  word-break: break-word;
}
.asset-actions {
  display: flex;
  gap: 8px;
  margin-top: var(--space-2);
  flex-wrap: wrap;
}
.asset-actions .btn-small {
  flex: 0 0 auto;
}
.asset-edit-panel {
  margin-bottom: 0;
}
.routine-textarea {
  width: 100%;
  max-width: none;
  min-width: 0;
  resize: vertical;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg);
  font: inherit;
  font-size: var(--text-sm);
  line-height: 1.45;
  padding: 9px 10px;
}
.routine-textarea:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 22%, transparent);
}
.inline-path-button {
  min-width: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
  text-align: left;
  text-decoration: underline;
  text-underline-offset: 2px;
}
.inline-path-button:hover {
  opacity: 0.85;
}
.asset-code-preview {
  max-height: 360px;
  margin: var(--space-2) 0 0;
  padding: var(--space-3);
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg);
  font-size: var(--text-xs);
  line-height: 1.45;
  white-space: pre-wrap;
}
.subsection-title--spaced {
  margin-bottom: var(--space-2);
}
.skill-section--spaced {
  margin-top: var(--space-5);
}

.workspace-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.workspace-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: color-mix(in srgb, var(--bg) 72%, transparent);
}
.workspace-card--new {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
  background: color-mix(in srgb, var(--accent) 6%, var(--bg));
}
.workspace-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.workspace-title {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-base);
  font-weight: 700;
}
.workspace-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 0 0 auto;
}
.workspace-actions .btn-small {
  flex: 0 0 auto;
}
.settings-field-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
}
.settings-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.settings-label-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.field-info {
  position: relative;
  display: inline-flex;
  flex: 0 0 auto;
}
.field-info summary {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--bg);
  color: var(--fg2);
  font-size: var(--text-xs);
  font-weight: 700;
  cursor: pointer;
  line-height: 1;
  user-select: none;
}
.field-info summary::-webkit-details-marker {
  display: none;
}
.field-info[open] summary,
.field-info summary:hover {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 8%, var(--bg));
}
.field-info-panel {
  position: absolute;
  z-index: 30;
  top: calc(100% + 6px);
  left: 0;
  width: min(380px, calc(100vw - 48px));
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elev);
  box-shadow: 0 12px 30px color-mix(in srgb, #000 24%, transparent);
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.45;
}
.field-info-panel p {
  margin: 0;
}
.field-info-panel p + p {
  margin-top: var(--space-2);
}
.field-info-panel ol.field-info-steps {
  margin: var(--space-2) 0 0;
  padding-left: 1.2em;
}
.field-info-panel ol.field-info-steps li + li {
  margin-top: var(--space-1);
}
.field-info-panel a {
  color: var(--accent);
}
.settings-field--wide {
  grid-column: 1 / -1;
}
.settings-field .routine-input {
  max-width: none;
  min-width: 0;
  width: 100%;
}
.settings-advanced {
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--bg) 72%, transparent);
}
.settings-advanced summary {
  cursor: pointer;
  color: var(--fg2);
  font-size: var(--text-sm);
  font-weight: 600;
}
.settings-advanced[open] summary {
  margin-bottom: var(--space-2);
}
@container (max-width: 720px) {
  .voice-warning {
    align-items: stretch;
    flex-direction: column;
  }
  .settings-field-grid {
    grid-template-columns: 1fr;
  }
  .workspace-card-header {
    flex-direction: column;
    align-items: stretch;
  }
  .workspace-actions {
    width: 100%;
  }
  .workspace-actions .btn-small {
    flex: 1 1 auto;
  }
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
  flex: 1 1 auto;
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
.skill-row.expanded .skill-description {
  display: block;
  -webkit-line-clamp: unset;
  overflow: visible;
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
.skill-link {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.skill-link:hover {
  opacity: 0.85;
}

.command-title-row {
  flex-wrap: wrap;
}
.command-name {
  color: var(--fg);
  font-size: var(--text-sm);
  font-weight: 700;
  white-space: nowrap;
}
.command-args {
  min-width: 0;
  color: var(--fg2);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}
.skill-badges {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
  flex: 0 0 auto;
  flex-wrap: wrap;
}
.command-source {
  flex: 0 0 auto;
  text-transform: capitalize;
}
.command-path {
  min-width: 0;
  color: var(--fg);
  overflow-wrap: anywhere;
  word-break: break-word;
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
  margin-top: 0;
  width: 100%;
}
.font-scale-row .btn-small {
  flex: 1 1 0;
  min-width: 0;
}
.font-scale-display {
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--fg);
  flex: 0 0 56px;
  text-align: center;
}
.ws-label {
  font-size: var(--text-sm);
  color: var(--fg2);
}
</style>
