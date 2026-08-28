<template>
  <div class="settings-pane">
    <UpdateProgressView
      v-if="packageUpdating"
      :version="packageStatus?.latest_version"
    />
    <PaneHeader page-tag="settings" @open-sidebar="emit('open-sidebar')" />
    <div class="pane-body">

      <!-- HOME TAB -->
      <template v-if="currentTab === 'home'">
        <!-- Whose settings am I looking at? In client mode every card below is
             the host's, because the API calls behind them are tunneled. Say so
             once, at the top, and point at the one screen that is local. -->
        <div v-if="isNodeClient" class="card scope-card">
          <div class="settings-card-header">
            <p class="section-title">you are viewing {{ hostScopeLabel }}</p>
            <p class="hint">
              This is the host's Settings, exactly as it looks on that machine. Changes here apply
              there, including the password and restarts.
              <router-link to="/device">This device</router-link>
              has its own panel for role, host connection and its local app version.
            </p>
          </div>
        </div>

        <!-- Actions -->
        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">app actions</p>
              <p class="hint">
                Snapshot, sync, or restart
                {{ isNodeClient ? `the host (${hostScopeLabel})` : 'this local Ciaobot instance' }}.
              </p>
            </div>
            <div class="settings-card-header-actions">
              <button class="btn-primary btn-small" @click="() => localStatus?.git_repo ? localHandback() : doSnapshot()" :disabled="!!actionPending">
                {{ actionPending === 'snapshot' ? (localStatus?.git_repo ? 'Syncing...' : 'Snapshotting...') : (localStatus?.git_repo ? 'Sync with Remote' : 'Git Snapshot') }}
              </button>
              <button class="btn-caution btn-small" @click="() => doDeploy()" :disabled="!!actionPending" title="Pull latest, reinstall deps, rebuild the frontend, and restart with the latest code">
                {{ actionPending === 'deploy' ? 'Restarting...' : 'Restart' }}
              </button>
            </div>
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
            <div class="action-row action-row--spaced action-row--compact">
              <button class="btn-primary" @click="fixDeployErrorInChat">
                Fix in Chat
              </button>
            </div>
          </div>
        </div>

        <!-- Keyboard shortcuts -->
        <div class="card">
          <div class="settings-card-header">
            <p class="section-title">keyboard shortcuts</p>
            <p class="hint">Global shortcuts. Text fields keep their normal meaning: number keys stay typeable, Cmd+A/Alt+A still selects all, and Esc inside the composer closes the slash-command picker instead of the chat.</p>
          </div>
          <ul class="shortcut-list">
            <li>
              <kbd v-if="inDesktopApp">&#8984;T</kbd>
              <kbd v-else>&#8224;N</kbd>
              <span>Open a new chat in the default General project</span>
            </li>
            <li>
              <kbd v-if="inDesktopApp">&#8984;D</kbd>
              <kbd v-else>&#8224;D</kbd>
              <span>Toggle voice dictation (start / stop)</span>
            </li>
            <li>
              <kbd v-if="inDesktopApp">&#8984;A</kbd>
              <kbd v-else>&#8224;A</kbd>
              <span>Archive the open chat (asks to confirm)</span>
            </li>
            <li>
              <kbd v-if="inDesktopApp">&#8984;S</kbd>
              <kbd v-else>&#8224;S</kbd>
              <span>Show or hide the sidebar</span>
            </li>
            <li>
              <kbd v-if="inDesktopApp">&#8984;&#8679;M</kbd>
              <kbd v-else>&#8224;M</kbd>
              <span>Open the model picker</span>
            </li>
            <li><kbd>1–9</kbd><span>Switch to the first through ninth workspace in the sidebar</span></li>
            <li>
              <kbd v-if="inDesktopApp">&#8984;&#8679;=</kbd>
              <kbd v-else>&#8224;=</kbd>
              <span>Increase the font size</span>
            </li>
            <li>
              <kbd v-if="inDesktopApp">&#8984;&#8679;-</kbd>
              <kbd v-else>&#8224;-</kbd>
              <span>Decrease the font size</span>
            </li>
            <li><kbd>Esc</kbd><span>Close the open chat (when not typing)</span></li>
            <li><kbd>&#8593;&#8595;&#8592;&#8594;</kbd><span>On the home screen: move between recent chats; stacked workspaces use up/down between lanes</span></li>
            <li><kbd>&#8629;</kbd><span>On the home screen: open the highlighted chat</span></li>
          </ul>
        </div>

        <!-- PWA password -->
        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">PWA password</p>
              <p class="hint">
                <template v-if="isNodeClient">
                  The password on {{ hostScopeLabel }} — the one you typed to open this client.
                  Changing it here keeps this device connected; other clients have to log in again.
                </template>
                <template v-else>
                  Ciaobot is always password-protected — this is the password you type to open it,
                  and the one another device needs to connect as a client.
                </template>
              </p>
            </div>
            <span
              v-if="authSettings"
              class="badge"
              :class="authSettings.auth_required ? 'badge--success' : 'badge--warn'"
            >
              {{ authSettings.auth_required ? 'on' : 'off' }}
            </span>
          </div>
          <div v-if="!authSettings" class="action-row"><span class="loading">Loading&hellip;</span></div>
          <template v-else>
            <div class="settings-form-panel node-peer-form">
              <p v-if="!authSettings.auth_required" class="hint hint--warn">
                This instance is running unprotected because PWA_AUTH_REQUIRED=false is set in the
                workspace .env. Setting a password here turns protection back on.
              </p>
              <label v-if="authSettings.auth_required" class="settings-field">
                <span class="ws-label">Current password</span>
                <input
                  v-model="authCurrentPassword"
                  type="password"
                  class="routine-input"
                  autocomplete="current-password"
                  :disabled="authSettingsSaving"
                />
              </label>
              <label class="settings-field">
                <span class="ws-label">New password</span>
                <input
                  v-model="authNewPassword"
                  type="password"
                  class="routine-input"
                  placeholder="at least 4 characters"
                  autocomplete="new-password"
                  :disabled="authSettingsSaving"
                />
              </label>
              <div class="action-row settings-actions">
                <button
                  class="btn-primary btn-small"
                  @click="saveAuthSettings"
                  :disabled="authSettingsSaving || !canSaveAuthSettings"
                >
                  {{ authSettingsSaving ? 'Saving…' : 'Save password' }}
                </button>
              </div>
            </div>
            <div v-if="authSettingsResult" class="action-result" :class="{ 'action-result--error': authSettingsError }">
              {{ authSettingsResult }}
            </div>
          </template>
        </div>

        <!-- Workspace health -->
        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">workspace health</p>
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

        <!-- Main workspace -->
        <div v-if="routines && routines.workspace_context" class="card">
          <div class="settings-card-header">
            <p class="section-title">main workspace</p>
            <p class="hint">
              The server filesystem root for routines, skills, scripts, and runtime state.
              Set <code>CIAO_WORKSPACE</code> in your <code>.env</code> file, then restart Ciaobot.
              Logical chat workspaces (sidebar switcher) are managed separately under Settings &rarr; Workspaces.
            </p>
          </div>
          <code class="workspace-root-path">{{ routines.workspace_context.workspace_root }}</code>
        </div>

        <!-- Package update — the desktop app drives this from the tray. -->
        <div v-if="!inDesktopApp" class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">package update</p>
              <p class="hint">
                <template v-if="isNodeClient && packageStatus?.mode !== 'bundled_app'">
                  The version installed on {{ hostScopeLabel }}. Updating restarts the host.
                  To upgrade this computer, open <router-link to="/device">this device</router-link>.
                </template>
                <template v-else-if="packageStatus?.mode === 'bundled_app'">
                  This bundled app updates through the Ciaobot menu-bar icon. Choose
                  <strong>Update</strong> there, or run the one-line installer again.
                </template>
                <template v-else>
                  Check the installed package version and upgrade this local app.
                </template>
              </p>
            </div>
            <div v-if="packageStatus && packageStatus.mode !== 'bundled_app'" class="settings-card-header-actions">
              <button
                :class="packageStatus.update_available ? 'btn-primary btn-small' : 'btn-secondary btn-small'"
                @click="openUpdatePanel"
                :disabled="!packageStatus.update_available || packageUpdating || showUpdatePanel"
              >
                {{ packageStatus.update_available
                    ? `Update to ${packageStatus.latest_version}`
                    : 'Up to date' }}
              </button>
            </div>
          </div>
          <div v-if="packageLoading && !packageStatus" class="loading">
            Checking package status...
          </div>
          <div v-else-if="packageStatus">
            <div v-if="packageStatus.error" class="hint hint--warn hint--spaced">
              Update check failed: {{ packageStatus.error }}
            </div>

            <div v-if="showUpdatePanel && packageStatus.mode !== 'bundled_app'" class="settings-form-panel">
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
                <p v-if="packageStatus.source" class="hint hint--spaced">
                  <a :href="packageStatus.source" target="_blank" rel="noopener">Release notes on GitHub</a>
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

        <!-- Notifications — the desktop app owns this in the tray, so the
             card drops out there rather than showing web-push controls the
             tray already supersedes. Same card as the Notifications tab. -->
        <SettingsNotifications hide-in-desktop-app />

        <!-- Appearance -->
        <div class="card">
          <div class="settings-card-header">
            <p class="section-title">appearance</p>
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
                <button class="btn-small" @click="adjustFontScale(-FONT_SCALE_STEP)" :disabled="fontScale <= MIN_FONT_SCALE">Decrease</button>
                <span class="font-scale-display">{{ fontScalePercent }}%</span>
                <button class="btn-small" @click="adjustFontScale(FONT_SCALE_STEP)" :disabled="fontScale >= MAX_FONT_SCALE">Increase</button>
                <button class="btn-small font-reset" @click="resetFontScale" :disabled="fontScale === DEFAULT_FONT_SCALE">Reset</button>
              </div>
            </div>
          </div>
          <div v-if="appleModelAvailable" class="setting-row setting-row--inline setting-row--toggle">
            <div class="routine-info">
              <span class="routine-name">Re-entry summary</span>
              <span class="routine-detail">Show a one-line Apple Intelligence orientation note when you reopen a quiet chat. The summary stays visible while you scroll and clears the moment you send a new message.</span>
            </div>
            <label class="settings-checkbox-hit">
              <input
                type="checkbox"
                class="settings-checkbox"
                v-model="reentrySummaryEnabled"
                @change="onReentrySummaryToggle"
              />
            </label>
          </div>
          <div v-else class="setting-row setting-row--inline">
            <div class="routine-info">
              <span class="routine-name">Re-entry summary</span>
              <span class="routine-detail">Show a one-line Apple Intelligence orientation note when you reopen a quiet chat.</span>
              <span class="hint hint--warn">Unavailable: {{ routines?.apple_model_unavailable_reason || 'Apple Intelligence is not supported on this machine' }}</span>
            </div>
          </div>
        </div>

        
        <!-- Debug (dev mode only) -->
        <div v-if="localStatus?.dev_mode" class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">debug</p>
              <p class="hint">Runtime issue log: server errors and failed background jobs. Send it to a chat so the agent can self-fix.</p>
            </div>
            <div class="settings-card-header-actions">
              <button class="btn-primary btn-small" @click="fixIssuesInChat" :disabled="debugPending">
                {{ debugPending ? 'Collecting issues...' : 'Fix issues in chat' }}
              </button>
              <button class="btn-small" @click="refreshDebugIssues" :disabled="debugPending">Refresh</button>
            </div>
          </div>
          <div v-if="debugSummary" class="action-result">{{ debugSummary }}</div>
        </div>

        <!-- This device (role, host connection, local app). Rendered inline so it
             behaves like every other tile here, instead of navigating to /device. -->
        <div class="device-tile">
          <DevicePanel />
        </div>

        <!-- Open source -->
        <div class="card">
          <div class="settings-card-header">
            <p class="section-title">open source</p>
            <p class="hint">
              Ciaobot is an open-source project. Support and contributions are welcome:
              report issues, suggest features, or open a pull request on
              <a href="https://github.com/raffaelefarinaro/ciaobot" target="_blank" rel="noopener">GitHub</a>.
              You can browse without an account; submitting an issue or pull request requires a free GitHub account, and GitHub will prompt you to sign in or create one.
            </p>
          </div>
        </div>
      </template>

      <!-- NOTIFICATIONS TAB -->
      <template v-if="currentTab === 'notifications'">
        <SettingsNotifications />
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
              <p class="section-title">internal models</p>
              <p class="hint">
                These tasks use their own model setting, separate from the active chat model.
                "Automatic" keeps the built-in default.
                System automations without a model picker are tracked on the Automations page.
              </p>
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
              <div
                class="routine-model-controls"
                :class="{ 'routine-model-controls--single': routineProviderValue('insights_model') === 'apple' }"
              >
                <select
                  class="routine-select routine-select--provider"
                  :value="routineProviderValue('insights_model')"
                  :disabled="routinesSaving"
                  @change="saveRoutineProvider('insights_model', ($event.target as HTMLSelectElement).value)"
                >
                  <option value="automatic">Automatic</option>
                  <option v-if="appleModelAvailable" value="apple">Local (free)</option>
                  <option v-for="provider in aliasProviderSections" :key="provider.key" :value="provider.key">
                    {{ provider.label }}
                  </option>
                </select>
                <ModelSelector
                  v-if="routineProviderValue('insights_model') !== 'apple' && routineProviderValue('insights_model') !== 'automatic'"
                  :model-value="routineModelValue('insights_model')"
                  :sections="routineModelSectionsFor('insights_model')"
                  :disabled="routinesSaving"
                  @update:model-value="saveRoutineModel('insights_model', $event)"
                />
                <span class="routine-model-hint">
                  <template v-if="routineProviderValue('insights_model') === 'apple'">
                    Runs on-device for free using Apple Intelligence. Nothing to install.
                  </template>
                  <template v-else>{{ routineModelSummary('insights_model') }}</template>
                </span>
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
                    <span v-else>Automatic default ({{ routines?.critique_models_effective || '' }})</span>
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
                  :empty-placeholder="`Automatic default (${routines?.critique_models_effective || ''})`"
                  :disabled="routinesSaving"
                  @update:model-value="setCritiqueModels"
                />
              </div>
            </div>
          </div>

          <!-- Voice: hear (dictation) and speak (read aloud) -->
          <div class="card">
            <div class="settings-card-header">
              <p class="section-title">voice</p>
              <p class="hint">Choose the engines used to hear you (dictation) and to speak messages aloud.</p>
            </div>
            <!-- No engine picker: voice is on-device only now. Both engines are
                 free and need no key, so the only thing worth saying is whether
                 this machine can run them and, if not, why. -->
            <div class="routine-row routine-row--flush">
              <div class="routine-info">
                <span class="routine-name">
                  <svg class="routine-voice-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><line x1="12" y1="19" x2="12" y2="22"/></svg>
                  Hear
                </span>
              </div>
              <div class="routine-model-controls routine-model-controls--single">
                <span class="routine-model-hint">
                  <template v-if="routines.transcription.available">
                    Dictation runs on-device using macOS speech recognition
                    (<code>{{ routines.transcription.locale }}</code>). Free, nothing to download.
                  </template>
                  <template v-else>
                    <span class="hint--warn">
                      Dictation is unavailable: {{ routines.transcription.unavailable_reason }}
                    </span>
                  </template>
                </span>
              </div>
            </div>
            <div class="routine-row routine-row--flush">
              <div class="routine-info">
                <span class="routine-name">
                  <svg class="routine-voice-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>
                  Speak
                </span>
              </div>
              <div class="routine-model-controls routine-model-controls--single">
                <span class="routine-model-hint">
                  <template v-if="routines.speech.available">
                    Read-aloud uses the macOS system voice. Free, nothing to download.
                  </template>
                  <template v-else>
                    <span class="hint--warn">
                      Read-aloud is unavailable. Install Ciaobot with the
                      one-line installer from the release page.
                    </span>
                  </template>
                </span>
              </div>
            </div>
            <!-- The installed voice list differs per machine, so it is served by
                 the engine rather than hardcoded, best quality first. Empty
                 means "let macOS pick the best one for the language". -->
            <div
              v-if="routines.speech.available"
              class="routine-row routine-row--flush"
            >
              <div class="routine-info">
                <span class="routine-name routine-name--sub">Voice</span>
              </div>
              <div class="routine-model-controls routine-model-controls--single">
                <select
                  class="routine-select"
                  :value="routines.speech.local_voice"
                  :disabled="routinesSaving"
                  @change="saveRoutines({ tts_local_voice: ($event.target as HTMLSelectElement).value })"
                >
                  <option value="">Best available for the language</option>
                  <option v-for="voice in routines.speech.local_voices || []" :key="voice.id" :value="voice.id">
                    {{ voice.name }} ({{ voice.locale }}{{ voice.quality === 'default' ? '' : ', ' + voice.quality }})
                  </option>
                </select>
                <span class="routine-model-hint">
                  The stock voices are the basic tier. Look for ones marked
                  <strong>Premium</strong> (then Enhanced) &mdash; they are a free download under
                  System Settings &rsaquo; Accessibility &rsaquo; Read &amp; Speak &rsaquo;
                  System voice &rsaquo; Manage Voices, and Ciaobot picks the best installed one
                  automatically.
                  <a
                    href="https://support.apple.com/guide/mac-help/mchlp2290/mac"
                    target="_blank"
                    rel="noopener"
                  >How to add a voice</a>.
                </span>
              </div>
            </div>
          </div>
          <div v-if="routinesResult" class="action-result">{{ routinesResult }}</div>
        </template>
      </template>

      <!-- PROVIDERS TAB -->
      <template v-if="currentTab === 'providers'">
        <div v-if="!providerKeysLoaded" class="card" role="status" aria-live="polite" aria-label="Loading providers">
          <div class="mm-loading-heading"><span class="history-loading-spinner" aria-hidden="true"></span><span>Loading providers…</span></div>
          <div class="mm-skeleton-block" aria-hidden="true">
            <span class="mm-shimmer-line" style="width: 100%; height: 72px; margin-bottom: 12px;"></span>
            <span class="mm-shimmer-line" style="width: 100%; height: 72px; margin-bottom: 12px;"></span>
            <span class="mm-shimmer-line" style="width: 100%; height: 72px; margin-bottom: 12px;"></span>
            <span class="mm-shimmer-line" style="width: 88%; height: 56px;"></span>
          </div>
        </div>
        <template v-else-if="providerKeysError">
          <div class="card"><p class="hint hint--warn">{{ providerKeysError }}</p></div>
        </template>
        <template v-else-if="providerKeys">
          <div class="card">
            <div class="settings-card-header">
              <div>
                <p class="section-title">providers</p>
                <p class="hint">
                  Each provider CLI manages its own login and credentials. Ciaobot verifies every connection.
                </p>
              </div>
            </div>

            <div v-if="providerKeys.connections" class="provider-connections">
              <div v-for="(conn, connKey) in providerKeys.connections" :key="connKey" class="credential-row">
                <div class="setting-row-main setting-row-main--inline">
                  <div class="routine-info">
                    <span class="routine-name">{{ conn.label || connKey }}</span>
                    <p class="hint hint--compact provider-connection-detail">
                      <span v-if="conn.version">{{ conn.version }}</span>
                      <span v-if="conn.account">{{ conn.account }}</span>
                      <span v-if="!conn.version && conn.detail">{{ conn.detail }}</span>
                    </p>
                    <p v-if="conn.auth === 'not_installed'" class="hint hint--compact">
                      {{ conn.detail }}
                      Install it with <code>{{ conn.command }}</code>
                      <a
                        v-if="conn.install_url"
                        :href="conn.install_url"
                        target="_blank"
                        rel="noopener noreferrer"
                      >installation guide ↗</a>
                    </p>
                  </div>
                  <span class="badge" :class="conn.ok ? 'badge--success' : 'badge--error'">
                    {{ conn.ok ? `Connected · ${conn.auth}` : 'Not connected' }}
                  </span>
                </div>
                <div class="provider-mcps-preview">
                  <div class="ws-connectors-header">
                    <span class="ws-label">Configured MCP Servers &amp; Connectors ({{ connectionMcps(String(connKey)).length }})</span>
                  </div>
                  <div class="workspace-connector-pills">
                    <template v-if="connectionMcps(String(connKey)).length">
                      <span
                        v-for="mcpName in connectionMcps(String(connKey))"
                        :key="mcpName"
                        class="connector-pill connector-pill--enabled"
                        :title="`${mcpName} configured for ${conn.label || connKey}`"
                      >
                        <span class="pill-dot"></span> {{ mcpName }}
                      </span>
                    </template>
                    <span v-else class="hint hint--compact">No MCP servers enabled</span>
                  </div>

                  <!-- Platform System Skills -->
                  <div class="ws-connectors-header" style="margin-top: 10px;">
                    <span class="ws-label">Platform System Skills &amp; Plugins ({{ (conn.skills && conn.skills.length) ? conn.skills.length : 0 }})</span>
                  </div>
                  <div class="workspace-connector-pills">
                    <template v-if="conn.skills && conn.skills.length">
                      <span v-for="skill in conn.skills" :key="skill" class="connector-pill connector-pill--enabled" :title="`Installed CLI plugin/skill: ${skill}`">
                        <span class="pill-dot"></span> {{ skill }}
                      </span>
                    </template>
                    <template v-else>
                      <span class="hint hint--compact">
                        {{ conn.ok ? 'None reported by this CLI' : 'Connect to discover' }}
                      </span>
                    </template>
                  </div>
                </div>
                <div v-if="getProviderSection(String(connKey))" class="provider-inline-defaults">
                  <label class="settings-field">
                    <span class="ws-label">Default model</span>
                    <ModelSelector
                      v-if="getProviderSection(String(connKey))?.configurable"
                      :model-value="providerDefaultModelSelectorValue(String(connKey) as AliasProviderKey)"
                      :sections="providerDefaultModelSectionsFor(String(connKey) as AliasProviderKey)"
                      :disabled="routinesSaving || !getProviderSection(String(connKey))?.available"
                      @update:model-value="saveProviderDefaultModel(String(connKey) as AliasProviderKey, $event)"
                    />
                    <span v-else class="hint hint--compact">Automatic — {{ (conn.label || connKey) }} picks its own default.</span>
                  </label>
                  <label class="settings-field">
                    <span class="ws-label">Default thinking</span>
                    <select
                      class="routine-select"
                      :value="providerDefaultThinkingValue(String(connKey) as AliasProviderKey)"
                      :disabled="routinesSaving || !getProviderSection(String(connKey))?.available"
                      @change="saveProviderDefaultThinking(String(connKey) as AliasProviderKey, ($event.target as HTMLSelectElement).value)"
                    >
                      <option
                        v-for="option in providerThinkingOptions(String(connKey) as AliasProviderKey)"
                        :key="option.value"
                        :value="option.value"
                      >
                        {{ option.label }}
                      </option>
                    </select>
                  </label>
                </div>
                <div class="action-row provider-connection-actions">
                  <button class="btn-primary btn-small" :disabled="providerConnectionPending === connKey" @click="providerConnectionAction(String(connKey), 'connect')">
                    {{ conn.ok ? 'Reconnect' : 'Connect' }}
                  </button>
                  <button class="btn-small" :disabled="providerConnectionPending === connKey" @click="providerConnectionAction(String(connKey), 'verify')">Verify</button>
                  <button v-if="conn.ok" class="btn-small" :disabled="providerConnectionPending === connKey" @click="providerConnectionAction(String(connKey), 'logout')">Log out</button>
                </div>
              </div>
              <div v-if="providerConnectionResult" class="action-result">{{ providerConnectionResult }}</div>
            </div>

            <p
              v-if="providerKeys.connections?.opencode"
              class="hint provider-auto-mode-hint"
            >
              To get a Claude-Code-style automatic approval classifier in opencode chats,
              install the <code>opencode-auto-permissions</code> plugin from a terminal:
              <code>opencode plugin -g opencode-auto-permissions</code>. It reviews each
              permission request with your session's model instead of showing an approval
              card. See <a
                href="https://opencode.ai/docs/permissions/#auto-mode"
                target="_blank"
                rel="noopener noreferrer"
              >opencode permissions docs ↗</a>.
            </p>

           </div>
         </template>
      </template>

      <!-- AUTOMATIONS TAB -->
      <template v-if="currentTab === 'automations'">
        <SettingsAutomation
          :automation-items="automationItems"
          :automation-loaded="automationLoaded"
          :automation-error="automationError"
          :fetch-automation="fetchAutomation"
          :proposal-outcomes="proposalOutcomes"
          :notify-saved="notifySaved"
          :notify-failed="notifyFailed"
          :routines="routines"
          :provider-models="workspaceModels?.provider_models"
          :provider-labels="aliasProviderLabels"
        />
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
                <p class="section-title">workspaces</p>
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
                <div class="settings-field settings-field--wide">
                  <span class="ws-label" id="new-workspace-color-label">Accent color</span>
                  <div
                    class="workspace-color-swatches"
                    role="radiogroup"
                    aria-labelledby="new-workspace-color-label"
                  >
                    <button
                      v-for="preset in WORKSPACE_COLOR_PRESETS"
                      :key="`new-color-${preset.id}`"
                      type="button"
                      class="workspace-color-swatch"
                      role="radio"
                      :aria-checked="newWorkspaceForm.color === preset.id"
                      :aria-label="preset.label"
                      :title="preset.label"
                      :disabled="workspacesSaving === 'new'"
                      :class="{ active: newWorkspaceForm.color === preset.id }"
                      :style="{ '--swatch': preset.swatch }"
                      @click="newWorkspaceForm.color = preset.id"
                    />
                  </div>
                </div>
                <label class="settings-field"><span class="ws-label">{{ workspaceProviderFieldLabel }}</span>
                  <select class="routine-input workspace-select" v-model="newWorkspaceForm.default_provider" :disabled="workspacesSaving === 'new'">
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
                          Selects which Google account this workspace uses. Accounts are added and connected in the Google Workspace card below.
                        </p>
                      </div>
                    </details>
                  </div>
                  <select class="routine-input workspace-select" v-model="newWorkspaceForm.gws_profile" :disabled="workspacesSaving === 'new'">
                    <option value="">{{ gwsUnlinkedOptionLabel }}</option>
                    <option v-for="profile in gwsProfileOptions" :key="`new-gws-${profile.name}`" :value="profile.name">
                      {{ profile.label }} ({{ profile.email || profile.name }})
                    </option>
                    <option v-if="workspaceCustomGwsProfile(newWorkspaceForm.gws_profile)" :value="newWorkspaceForm.gws_profile">
                      Custom: {{ newWorkspaceForm.gws_profile }}
                    </option>
                  </select>
                </label>
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
                  <div class="settings-field settings-field--wide">
                    <span class="ws-label" :id="`workspace-color-${form.name}`">Accent color</span>
                    <div
                      class="workspace-color-swatches"
                      role="radiogroup"
                      :aria-labelledby="`workspace-color-${form.name}`"
                    >
                      <button
                        v-for="preset in WORKSPACE_COLOR_PRESETS"
                        :key="`${form.name}-color-${preset.id}`"
                        type="button"
                        class="workspace-color-swatch"
                        role="radio"
                        :aria-checked="form.color === preset.id"
                        :aria-label="preset.label"
                        :title="preset.label"
                        :disabled="workspacesSaving === form.name"
                        :class="{ active: form.color === preset.id }"
                        :style="{ '--swatch': preset.swatch }"
                        @click="form.color = preset.id"
                      />
                    </div>
                  </div>
                  <label class="settings-field"><span class="ws-label">{{ workspaceProviderFieldLabel }}</span>
                    <select class="routine-input workspace-select" v-model="form.default_provider" :disabled="workspacesSaving === form.name">
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
                            Selects which Google account this workspace uses. Accounts are added and connected in the Google Workspace card below.
                          </p>
                        </div>
                      </details>
                    </div>
                    <select class="routine-input workspace-select" v-model="form.gws_profile" :disabled="workspacesSaving === form.name">
                      <option value="">{{ gwsUnlinkedOptionLabel }}</option>
                      <option v-for="profile in gwsProfileOptions" :key="`${form.name}-gws-${profile.name}`" :value="profile.name">
                        {{ profile.label }} ({{ profile.email || profile.name }})
                      </option>
                      <option v-if="workspaceCustomGwsProfile(form.gws_profile)" :value="form.gws_profile">
                        Custom: {{ form.gws_profile }}
                      </option>
                    </select>
                  </label>
                </div>
              </div>
            </div>

            <div v-if="workspacesResult" class="action-result action-result--error" role="alert">{{ workspacesResult }}</div>
          </div>

          <!-- Google Workspace integration -->
          <div class="card">
            <div class="settings-card-header settings-card-header--split">
              <div>
                <div class="settings-label-row">
                  <p class="section-title">google workspace</p>
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
                        Add one account per Google login you use, and keep them separate so a
                        personal chat never inherits work Drive or calendar access. Each
                        workspace picks which account it uses.
                      </p>
                      <p><strong>One-time setup per account</strong></p>
                      <ol class="field-info-steps">
                        <li>Install <code>gws</code> (button below or <code>npm install -g @googleworkspace/cli</code>).</li>
                        <li>Add the account below and give it a short name.</li>
                        <li>
                          Click <strong>Sign in with Google</strong> on the account card and approve access in the browser
                          tab that opens. It connects automatically (no copy-paste).
                        </li>
                      </ol>
                      <p><strong>Alternative setups</strong></p>
                      <ul class="field-info-steps">
                        <li>
                          <strong>gcloud (no console needed):</strong> install the
                          <a href="https://cloud.google.com/sdk/docs/install" target="_blank" rel="noopener noreferrer">gcloud CLI</a>,
                          authenticate once, then run
                          <code>ciao gws &lt;profile&gt; auth setup</code>. Ciaobot auto-detects the account.
                        </li>
                        <li>
                          <strong>Bring your own client:</strong> in
                          <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener noreferrer">Google Cloud Console &rarr; Credentials</a>
                          create an OAuth client (Desktop app), download the JSON, and upload it as <code>client_secret.json</code>.
                        </li>
                      </ul>
                      <p>
                        Enable the APIs you need in your GCP project (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks).
                      </p>
                    </div>
                  </details>
                </div>
                <p class="hint">
                  Add one account per Google login you use, then link each workspace to the
                  account it should use (the <strong>Google profile</strong> field above).
                  Accounts are local <code>gws</code> profiles: Gmail, Calendar, Drive, Docs,
                  Sheets, Slides, and Tasks.
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
                  <button class="btn-primary btn-small" @click="installGwsInChat">Install in Chat</button>
                </div>
                <p class="hint hint--compact">Opens a chat that walks through <code>npm install -g @googleworkspace/cli</code> with you — Ciaobot does not run package installs on its own.</p>
              </div>
              <div class="gws-account-add">
                <span class="ws-label">Add a Google account</span>
                <div class="gws-account-add-row">
                  <input
                    class="routine-input"
                    v-model="newGwsProfileName"
                    placeholder="short name (e.g. personal, acme)"
                    :disabled="gwsProfileAdding"
                    @keyup.enter="addGwsProfile"
                  />
                  <input
                    class="routine-input"
                    v-model="newGwsProfileLabel"
                    placeholder="display name (optional)"
                    :disabled="gwsProfileAdding"
                    @keyup.enter="addGwsProfile"
                  />
                  <button
                    class="btn-primary btn-small"
                    :disabled="!newGwsProfileName.trim() || gwsProfileAdding"
                    @click="addGwsProfile"
                  >
                    {{ gwsProfileAdding ? 'Adding…' : 'Add account' }}
                  </button>
                </div>
                <p class="hint hint--compact">
                  The short name identifies the account in chats and in
                  <code>secrets/gws-&lt;name&gt;/</code>. You connect the Google login in the
                  card that appears.
                </p>
                <p v-if="gwsProfileActionError" class="hint hint--warn">{{ gwsProfileActionError }}</p>
              </div>

              <p v-if="!gwsIntegration.profiles?.length" class="hint hint--compact">
                No Google accounts yet. Workspaces run without Google access until you add one.
              </p>

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
                    <div class="gws-profile-header-actions">
                      <span class="badge" :class="gwsProfileBadgeClass(profile)">
                        {{ gwsProfileStatus(profile) }}
                      </span>
                      <button
                        class="btn-small btn-danger"
                        :disabled="gwsSavingProfile === profile.name"
                        @click="removeGwsProfile(profile)"
                      >Remove account</button>
                    </div>
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
                        First, create an OAuth client. Easiest: run
                        <code>ciao gws {{ profile.name }} auth setup</code>
                        in a terminal with the
                        <a :href="gwsReloginHelpUrl()" target="_blank" rel="noopener noreferrer">gcloud CLI</a>
                        installed — it creates the client for you. Or upload one you made in
                        <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener noreferrer">Google Cloud Console</a>.
                      </p>
                      <label class="btn-small file-upload-btn">
                        Upload client_secret.json
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
                      <div v-if="!gwsAuthUrls[profile.name] && !gwsReloginPending[profile.name]" class="gws-btn-group">
                        <button
                          v-if="profile.loopback_eligible"
                          class="btn-primary btn-small"
                          :disabled="gwsSavingProfile === profile.name"
                          @click="gwsReloginStart(profile.name)"
                        >
                          Sign in with Google
                        </button>
                        <button
                          class="btn-small"
                          :disabled="gwsSavingProfile === profile.name"
                          @click="startGwsAuth(profile.name)"
                        >
                          Manual connect (paste code)
                        </button>
                        <button
                          class="btn-small btn-danger"
                          @click="disconnectGwsProfile(profile.name, true)"
                          :disabled="gwsSavingProfile === profile.name"
                        >
                          Remove OAuth Client
                        </button>
                      </div>
                      <p v-if="gwsReloginPending[profile.name]" class="gws-action-hint">
                        <template v-if="gwsAuthUrls[profile.name]">
                          The sign-in tab was blocked. Open this link, then finish in the browser — it connects automatically:
                          <a :href="gwsAuthUrls[profile.name]" target="_blank" class="gws-auth-link">Open authorization page</a>
                        </template>
                        <template v-else>
                          Sign-in in progress: complete it in the browser tab that opened. It will connect automatically.
                        </template>
                        <button class="btn-small" @click="gwsReloginCancel(profile.name)">Cancel</button>
                      </p>
                      <p v-else-if="gwsReloginError[profile.name]" class="gws-action-hint hint--warn">{{ gwsReloginError[profile.name] }}</p>
                      <div v-else-if="gwsAuthUrls[profile.name]" class="gws-auth-flow-box">
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
                      <p v-if="profile.needs_relogin" class="gws-action-hint hint--warn">
                        This login has expired or been revoked<span v-if="profile.token_error"> ({{ profile.token_error }})</span>.
                        Sign back in to restore Gmail, Calendar, Drive, and scheduled Google tasks.
                      </p>
                      <div v-if="!gwsAuthUrls[profile.name] && !gwsReloginPending[profile.name]" class="gws-btn-group">
                        <button
                          v-if="profile.needs_relogin && profile.loopback_eligible"
                          class="btn-primary btn-small"
                          :disabled="gwsSavingProfile === profile.name"
                          @click="gwsReloginStart(profile.name)"
                        >
                          Re-authenticate
                        </button>
                        <button
                          v-if="profile.needs_relogin"
                          class="btn-small"
                          :disabled="gwsSavingProfile === profile.name"
                          @click="startGwsAuth(profile.name)"
                        >
                          Manual connect (paste code)
                        </button>
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
                      <p v-if="gwsReloginPending[profile.name]" class="gws-action-hint">
                        <template v-if="gwsAuthUrls[profile.name]">
                          The re-auth tab was blocked. Open this link, then finish in the browser — it connects automatically:
                          <a :href="gwsAuthUrls[profile.name]" target="_blank" class="gws-auth-link">Open authorization page</a>
                        </template>
                        <template v-else>
                          Re-authentication in progress: complete it in the browser tab that opened. It will connect automatically.
                        </template>
                        <button class="btn-small" @click="gwsReloginCancel(profile.name)">Cancel</button>
                      </p>
                      <p v-else-if="gwsReloginError[profile.name]" class="gws-action-hint hint--warn">{{ gwsReloginError[profile.name] }}</p>
                      <div v-else-if="gwsAuthUrls[profile.name]" class="gws-auth-flow-box">
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
                  </div>
                </div>
              </div>
            </template>
          </div>
        </template>
      </template>


      <!-- SKILLS TAB -->
      <template v-if="currentTab === 'skills'">
        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">skills</p>
              <p class="hint">
                Skills are local folders: place <code>skills/&lt;name&gt;/SKILL.md</code> (or upload a validated zip) then <code>ciao sync-skills</code>. Workspace git sync propagates to other operators. No GitHub fetch.
              </p>
            </div>
            <div class="settings-card-header-actions">
              <button class="btn-small" @click="createSkillViaChat">Add via chat</button>
              <button class="btn-primary btn-small" @click="toggleAddSkill">
                {{ showAddSkill ? 'Cancel' : '+ Add skill' }}
              </button>
            </div>
          </div>

          <p class="hint hint--info skill-scope-note">
             Ciaobot runs chats through Claude Code or opencode. Ciaobot-managed skills are synchronized into both runtimes where supported. Skills, plugins, and MCP servers you install directly in a CLI also remain available to Ciaobot when that provider runs the chat; provider-specific assets stay with that provider. This page lists only the shared, Ciaobot-managed stock and custom skills — see
            <RouterLink to="/settings/providers">Providers</RouterLink> for what each CLI brings on its own.
          </p>

          <!-- Add Skill Form: folder note + zip upload -->
          <div v-if="showAddSkill" class="settings-form-panel">
            <div class="gws-account-add" style="margin-bottom: 12px;">
              <p class="ws-label">Add a skill</p>
              <p class="hint hint--compact">
                Place a folder <code>skills/&lt;name&gt;/SKILL.md</code> in the workspace, then run <code>ciao sync-skills</code>. Or upload a zip containing exactly one top-level folder with <code>SKILL.md</code>. Validated before extract.
              </p>
            </div>
            <div class="settings-field-grid">
              <div class="settings-field settings-field--wide">
                <span class="ws-label">Upload zip</span>
                <p class="hint hint--compact">Single zip with one top-level folder containing <code>SKILL.md</code>. Max 15 KB SKILL.md, frontmatter <code>name</code> must match folder.</p>
                <input
                  ref="skillZipInput"
                  type="file"
                  accept=".zip"
                  style="display: none;"
                  @change="onSkillZipSelected"
                  :disabled="addingSkill"
                />
                <button class="btn-small" @click="triggerSkillZipPicker" :disabled="addingSkill">
                  {{ addingSkill ? 'Uploading…' : 'Choose zip' }}
                </button>
                <span v-if="skillZipFileName" class="hint hint--compact" style="margin-left: 8px;">{{ skillZipFileName }}</span>
              </div>
            </div>
            <div class="action-row settings-actions">
              <button class="btn-primary" @click="uploadSkillZip" :disabled="addingSkill || !skillZipFile">
                {{ addingSkill ? 'Uploading…' : 'Upload zip' }}
              </button>
            </div>
            <ul class="check" style="margin-top: 8px; padding-left: 16px; font-size: 12px; color: var(--fg3);">
              <li>Validation: zip-slip, one <code>SKILL.md</code>, frontmatter <code>name/description</code>, ≤15 KB</li>
              <li>Discoverable immediately: <code>.claude/skills</code> + <code>.agents/skills</code> symlink</li>
              <li>Sync between operators = workspace git (no npx)</li>
            </ul>
            <div v-if="addSkillResult" class="action-result" :class="{ '--error': addSkillError }" role="alert">{{ addSkillResult }}</div>
          </div>

          <div v-if="!skillsLoaded" class="action-row"><span class="loading">Loading&hellip;</span></div>
          <template v-else-if="skillsError">
            <p class="hint hint--warn">{{ skillsError }}</p>
          </template>
          <template v-else-if="skillsInventory">
            <!-- Custom Skills Section -->
            <div class="skill-section">
              <p class="subsection-title subsection-title--spaced">custom skills</p>
              <p v-if="!customSkills.length" class="hint hint--section-empty">No custom skills yet. Add a folder <code>skills/&lt;name&gt;/SKILL.md</code> or upload a zip.</p>
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
                      <p v-if="skill.path" class="skill-meta">
                        <span class="skill-meta-label">Path</span>
                        <button class="inline-path-button" @click.stop="openAssetPath(skill.path)">{{ skill.path }}</button>
                      </p>
                      <pre v-if="skill.content" class="asset-code-preview"><code>{{ skill.content }}</code></pre>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <!-- Stock Skills Section -->
            <div class="skill-section skill-section--spaced">
              <p class="subsection-title subsection-title--spaced">stock skills</p>
              <p v-if="!stockSkills.length" class="hint hint--section-empty">No stock skills installed.</p>
              <div v-else class="skill-list skill-list--section">
                <div
                  v-for="skill in stockSkills"
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
                      <p v-if="skill.path" class="skill-meta">
                        <span class="skill-meta-label">Path</span>
                        <button class="inline-path-button" @click.stop="openAssetPath(skill.path)">{{ skill.path }}</button>
                      </p>
                      <pre v-if="skill.content" class="asset-code-preview"><code>{{ skill.content }}</code></pre>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>
      </template>

      <!-- SUBAGENTS TAB -->
      <template v-if="currentTab === 'subagents'">
        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">subagents</p>
              <p class="hint">
                 Shared subagents available to Claude Code and opencode. Custom definitions are saved in <code>subagents/</code>, mirrored into the vault, and synchronized into each runtime's native format.
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
                      <span :class="assetOriginClass(subagentOrigin(agent))">{{ assetOriginLabel(subagentOrigin(agent)) }}</span>
                      <span v-if="agent.scope && agent.scope !== 'custom' && agent.scope !== 'built-in'" class="badge badge--muted command-source">{{ agent.scope }}</span>
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
      </template>

      <!-- COMMANDS TAB -->
      <template v-if="currentTab === 'commands'">
        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">commands</p>
              <p class="hint">
                 Shared commands available to Claude Code and opencode. Custom commands are saved in <code>commands/</code>, mirrored into the vault, and exposed through each runtime's native format.
              </p>
            </div>
            <div class="settings-card-header-actions">
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
                      <span :class="assetOriginClass(commandOrigin(command))">{{ assetOriginLabel(commandOrigin(command)) }}</span>
                      <span v-if="command.scope && command.scope !== 'custom' && command.scope !== 'built-in'" class="badge badge--muted command-source">{{ command.scope }}</span>
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

      <!-- MCP TAB -->
      <template v-if="currentTab === 'mcp'">
        <div class="card" id="mcp-servers">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">mcp servers</p>
              <p class="hint">
                Model Context Protocol (MCP) servers and tools available to Ciaobot agents.
              </p>
            </div>
            <div class="settings-card-header-actions">
              <button class="btn-small" @click="createMcpViaChat">Add via chat</button>
              <button class="btn-small" @click="toggleAddMcpServer">
                {{ showAddMcpServer ? 'Cancel' : '+ New MCP server' }}
              </button>
            </div>
          </div>

          <!-- Add MCP Server Form -->
          <div v-if="showAddMcpServer" class="settings-form-panel">
            <div class="settings-field-grid">
              <label class="settings-field">
                <span class="ws-label">Server Name</span>
                <input class="routine-input" v-model="newMcpName" :disabled="addingMcpServer" placeholder="e.g. postgres-db" />
              </label>
              <label class="settings-field">
                <span class="ws-label">Transport Type</span>
                <select class="routine-select" v-model="newMcpTransport" :disabled="addingMcpServer">
                  <option value="http">HTTP / SSE</option>
                  <option value="stdio">stdio (Command)</option>
                </select>
              </label>
              <label v-if="newMcpTransport === 'http'" class="settings-field settings-field--wide">
                <span class="ws-label">Server URL</span>
                <input class="routine-input" v-model="newMcpUrl" :disabled="addingMcpServer" placeholder="https://mcp.example.com/http" />
              </label>
              <label v-else class="settings-field settings-field--wide">
                <span class="ws-label">Command Line</span>
                <input class="routine-input" v-model="newMcpCommand" :disabled="addingMcpServer" placeholder="npx -y @modelcontextprotocol/server-postgres postgresql://..." />
              </label>
            </div>
            <div class="action-row settings-actions">
              <button class="btn-primary" @click="addCustomMcpServer" :disabled="addingMcpServer || !newMcpName.trim() || (newMcpTransport === 'http' ? !newMcpUrl.trim() : !newMcpCommand.trim())">
                {{ addingMcpServer ? 'Adding...' : 'Add MCP server' }}
              </button>
            </div>
            <div v-if="addMcpServerResult" class="action-result" :class="{ '--error': addMcpServerError }">{{ addMcpServerResult }}</div>
          </div>

          <!-- List of MCP Servers (exact skill-list / skill-row UI) -->
          <div class="skill-list">
            <!-- 1. Built-in Ciaobot FastMCP Server -->
            <div
              class="skill-row"
              :class="{ expanded: isMcpExpanded('ciaobot-fastmcp') }"
              @click="toggleMcp('ciaobot-fastmcp')"
            >
              <div class="skill-main">
                <div class="skill-title-row command-title-row">
                  <span class="skill-chevron">{{ isMcpExpanded('ciaobot-fastmcp') ? '&#9662;' : '&#9656;' }}</span>
                  <span class="skill-name">ciaobot</span>
                  <span class="skill-badges">
                    <span :class="assetOriginClass('builtin')">{{ assetOriginLabel('builtin') }}</span>
                    <span class="badge" :class="fastMcpEnabled ? 'badge--success' : 'badge--muted'">
                      {{ fastMcpEnabled ? 'enabled' : 'disabled' }}
                    </span>
                  </span>
                </div>
                <p class="skill-description">Vault, chats, projects, and schedules.</p>
                <div v-if="isMcpExpanded('ciaobot-fastmcp')" class="skill-detail" @click.stop>
                  <p class="skill-meta"><span class="skill-meta-label">Endpoint</span><code>http://127.0.0.1:8443/mcp/</code></p>
                  <div class="setting-row setting-row--inline setting-row--toggle" style="margin-top: 8px;">
                    <span class="routine-name">FastMCP Control Plane Active</span>
                    <label class="settings-checkbox-hit">
                      <input type="checkbox" class="settings-checkbox" v-model="fastMcpEnabled" @change="saveFastMcpToggle" />
                    </label>
                  </div>
                  <p class="skill-meta" style="margin-top: 8px;"><span class="skill-meta-label">Embedded Tools ({{ inspectorEmbeddedTools.length }})</span></p>
                  <div class="mcp-tag-grid mcp-tag-grid--wide">
                    <span v-for="tool in inspectorEmbeddedTools" :key="tool" class="mcp-tag mcp-tag--embedded">{{ tool }}</span>
                  </div>
                </div>
              </div>
            </div>

            <!-- 2. Custom & Project .mcp.json Servers -->
            <template v-if="mcpStatus?.project_servers && mcpStatus.project_servers.length">
              <div
                v-for="srv in mcpStatus.project_servers"
                :key="srv.name"
                class="skill-row"
                :class="{ expanded: isMcpExpanded(srv.name) }"
                @click="toggleMcp(srv.name)"
              >
                <div class="skill-main">
                  <div class="skill-title-row command-title-row">
                    <span class="skill-chevron">{{ isMcpExpanded(srv.name) ? '&#9662;' : '&#9656;' }}</span>
                    <span class="skill-name">{{ srv.name }}</span>
                    <span class="skill-badges">
                      <span :class="assetOriginClass(mcpServerOrigin(srv))">{{ assetOriginLabel(mcpServerOrigin(srv)) }}</span>
                      <span
                        class="badge"
                        :class="srv.ready === false ? 'badge--warn' : 'badge--success'"
                      >
                        {{ srv.ready === false ? 'needs .env' : 'ready' }}
                      </span>
                    </span>
                  </div>
                  <p v-if="srv.url" class="skill-description">URL: {{ srv.url }}</p>
                  <p v-else-if="srv.command" class="skill-description">
                    Command: {{ srv.command }}<template v-if="srv.args?.length"> {{ srv.args.join(' ') }}</template>
                  </p>
                  <div v-if="isMcpExpanded(srv.name)" class="skill-detail" @click.stop>
                    <div class="settings-field-grid mcp-edit-grid">
                      <label v-if="(mcpEditDraft(srv).transport || srv.transport) === 'http'" class="settings-field settings-field--wide">
                        <span class="ws-label">URL</span>
                        <input
                          class="routine-input"
                          :value="mcpEditDraft(srv).url"
                          :disabled="mcpServerSaving === srv.name"
                          aria-label="MCP server URL"
                          @input="setMcpEditField(srv.name, 'url', ($event.target as HTMLInputElement).value)"
                        />
                      </label>
                      <template v-else>
                        <label class="settings-field">
                          <span class="ws-label">Command</span>
                          <input
                            class="routine-input"
                            :value="mcpEditDraft(srv).command"
                            :disabled="mcpServerSaving === srv.name"
                            aria-label="MCP server command"
                            @input="setMcpEditField(srv.name, 'command', ($event.target as HTMLInputElement).value)"
                          />
                        </label>
                        <label class="settings-field settings-field--wide">
                          <span class="ws-label">Args</span>
                          <input
                            class="routine-input"
                            :value="mcpEditDraft(srv).argsText"
                            :disabled="mcpServerSaving === srv.name"
                            placeholder="e.g. -y @notionhq/notion-mcp-server"
                            aria-label="MCP server args"
                            @input="setMcpEditField(srv.name, 'argsText', ($event.target as HTMLInputElement).value)"
                          />
                        </label>
                      </template>
                      <p v-if="srv.env_path || mcpStatus?.env_path" class="settings-field settings-field--wide hint hint--compact">
                        Secrets are saved to <code>{{ srv.env_path || mcpStatus?.env_path }}</code>. Connection config stays in <code>.mcp.json</code>.
                      </p>
                    </div>

                    <div class="mcp-env-block">
                      <p class="skill-meta">
                        <span class="skill-meta-label">Secrets for this server</span>
                      </p>
                      <p class="hint hint--compact">
                        Paste the token into the field below. It is saved to the workspace <code>.env</code> (not into <code>.mcp.json</code>).
                      </p>
                      <div
                        v-for="envKey in mcpEnvKeysFor(srv)"
                        :key="`${srv.name}:${envKey.key}`"
                        class="credential-row mcp-env-row"
                      >
                        <div class="setting-row-main setting-row-main--inline">
                          <div class="routine-info">
                            <span class="routine-name">{{ envKey.key }}</span>
                            <p v-if="envKey.hint" class="hint hint--compact">{{ envKey.hint }}</p>
                          </div>
                          <span class="badge" :class="envKey.configured ? 'badge--success' : 'badge--error'">
                            {{ envKey.configured ? 'Configured' : 'Missing' }}
                          </span>
                        </div>
                        <input
                          type="password"
                          class="routine-input"
                          :value="mcpEnvInputs[envKey.key] || ''"
                          :placeholder="envKey.configured ? '•••••••••••• (leave blank to keep)' : `Paste ${envKey.key}`"
                          :disabled="mcpEnvSaving"
                          :aria-label="envKey.key"
                          @input="mcpEnvInputs[envKey.key] = ($event.target as HTMLInputElement).value"
                        />
                      </div>
                      <p v-if="!mcpEnvKeysFor(srv).length" class="hint hint--compact">
                        No secrets referenced by this server's <code>.mcp.json</code> config.
                      </p>
                      <div class="action-row settings-actions">
                        <button
                          class="btn-small"
                          :disabled="mcpEnvSaving || !hasMcpEnvEdits(srv)"
                          @click="saveMcpEnvKeys(srv)"
                        >
                          {{ mcpEnvSaving ? 'Saving...' : 'Save secrets' }}
                        </button>
                        <button
                          class="btn-small"
                          :disabled="mcpServerSaving === srv.name || !mcpEditDirty(srv)"
                          @click="saveMcpServer(srv)"
                        >
                          {{ mcpServerSaving === srv.name ? 'Saving...' : 'Save connection' }}
                        </button>
                      </div>
                      <div
                        v-if="(mcpEnvResult && mcpEnvResultServer === srv.name) || (mcpServerResult && mcpServerResultName === srv.name)"
                        class="action-result"
                        :class="{ '--error': (mcpEnvResultServer === srv.name && mcpEnvError) || (mcpServerResultName === srv.name && mcpServerError) }"
                      >{{ (mcpEnvResultServer === srv.name && mcpEnvResult) || (mcpServerResultName === srv.name && mcpServerResult) }}</div>
                    </div>

                    <div class="mcp-tools-block">
                      <div class="setting-row setting-row--inline" style="margin-top: 8px;">
                        <p class="skill-meta" style="margin: 0;">
                          <span class="skill-meta-label">
                            Tools ({{ (mcpServerTools[srv.name] || srv.tools || []).length }})
                            <template v-if="srv.tools_source && srv.tools_source !== 'none'">
                              · {{ srv.tools_source }}
                            </template>
                          </span>
                        </p>
                        <button
                          class="btn-small"
                          :disabled="mcpToolsLoading[srv.name]"
                          @click="refreshMcpServerTools(srv)"
                        >
                          {{ mcpToolsLoading[srv.name] ? 'Loading...' : (srv.transport === 'http' ? 'Probe tools' : 'Refresh') }}
                        </button>
                      </div>
                      <p
                        v-if="mcpToolsError[srv.name] || (!(mcpServerTools[srv.name] || srv.tools || []).length && srv.tools_note)"
                        class="hint hint--compact"
                        :class="{ 'hint--warn': !!mcpToolsError[srv.name] }"
                      >
                        {{ mcpToolsError[srv.name] || srv.tools_note }}
                      </p>
                      <div
                        v-if="(mcpServerTools[srv.name] || srv.tools || []).length"
                        class="mcp-tag-grid mcp-tag-grid--wide"
                      >
                        <span
                          v-for="tool in (mcpServerTools[srv.name] || srv.tools || [])"
                          :key="tool"
                          class="mcp-tag mcp-tag--embedded"
                        >{{ tool }}</span>
                      </div>
                    </div>

                    <div class="asset-actions">
                      <button class="btn-small btn-danger" @click.stop="deleteCustomMcpServer(srv.name)">Delete</button>
                    </div>
                  </div>
                </div>
              </div>
            </template>
          </div>
        </div>
      </template>




    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../lib/api'
import { errorMessage, apiErrorMessage, errorPayload, errorPayloadList } from '../lib/errorMessage'
import { formatTime, formatDuration } from '../lib/time'
import { isDesktopApp } from '../lib/desktop'
import {
  DEFAULT_FONT_SCALE,
  FONT_SCALE_STEP,
  MAX_FONT_SCALE,
  MIN_FONT_SCALE,
  useFontScale,
} from '../composables/useFontScale'
import type {
  AgentAssetsResponse,
  AutomationPayload,
  AutomationProcess,
  CommandAsset,
  CommandsResponse,
  CreatedAgentAssetResponse,
  DebugIssueReport,
  DeployResult,
  GwsIntegrationSettings,
  LocalStatus,
  ModelsResponse,
  NodeStatus,
  McpStatus,
  McpUsage,
  McpProjectServer,
  McpEnvKey,
  ProviderConfigSettings,
  ProposalOutcomes,
  RoutineSettings,
  SkillInventory,
  SlashCommand,
  SubagentAsset,
  WorkspaceInfo,
  RuntimeProvider,
  WorkspaceHealthResponse,
  WorkspaceProvider,
  PackageStatus,
  PackageChangelog,
  PackageUpdateResult,
  ProviderActionResult,
  LocalHandbackResult,
} from '../lib/types'
import { askConfirm } from '../lib/confirm'
import { useFileViewerStore } from '../stores/fileViewer'
import { useProjectStore } from '../stores/projects'
import PaneHeader from './PaneHeader.vue'
import UpdateProgressView from './UpdateProgressView.vue'
import ModelSelector from './ModelSelector.vue'
import SettingsAutomation from './settings/SettingsAutomation.vue'
import SettingsNotifications from './settings/SettingsNotifications.vue'
import DevicePanel from './DevicePanel.vue'
import { sectionsFromModelsResponse, type ModelSection } from '../lib/modelSections'
import { isGwsEngineHostEligible } from '../lib/gwsEngineHost'
import { useReentrySummaryPreference } from '../composables/useReentrySummaryPreference'

// The tray owns package updates and native notifications in the desktop app.
const inDesktopApp = isDesktopApp()
import {
  DEFAULT_WORKSPACE_COLOR,
  WORKSPACE_COLOR_PRESETS,
  normalizeWorkspaceColor,
  type WorkspaceColorId,
} from '../lib/workspaceColors'

const emit = defineEmits<{ 'open-sidebar': [] }>()

const route = useRoute()
const router = useRouter()
const fileViewer = useFileViewerStore()
const projectStore = useProjectStore()
const currentTab = computed(() => {
  const tab = (route.params.tab as string) || 'home'
  return tab
})

const expandedSkills = ref<Record<string, boolean>>({})
const expandedCommands = ref<Record<string, boolean>>({})
const expandedSubagents = ref<Record<string, boolean>>({})

// MCP Server management state
const showAddMcpServer = ref(false)
const addingMcpServer = ref(false)
const addMcpServerResult = ref('')
const addMcpServerError = ref(false)
const newMcpName = ref('')
const newMcpTransport = ref<'http' | 'stdio'>('http')
const newMcpUrl = ref('')
const newMcpCommand = ref('')
const fastMcpEnabled = ref(true)
const { reentrySummaryEnabled, setReentrySummaryEnabled } = useReentrySummaryPreference()
const expandedMcp = ref<Record<string, boolean>>({})
const mcpEnvInputs = ref<Record<string, string>>({})
const mcpEnvSaving = ref(false)
const mcpEnvResult = ref('')
const mcpEnvError = ref(false)
const mcpEnvResultServer = ref('')
const mcpEditDrafts = ref<Record<string, { transport: string; url: string; command: string; argsText: string }>>({})
const mcpServerSaving = ref('')
const mcpServerResult = ref('')
const mcpServerError = ref(false)
const mcpServerResultName = ref('')
const mcpServerTools = ref<Record<string, string[]>>({})
const mcpToolsLoading = ref<Record<string, boolean>>({})
const mcpToolsError = ref<Record<string, string>>({})

function toggleAddMcpServer() {
  showAddMcpServer.value = !showAddMcpServer.value
  addMcpServerResult.value = ''
}

function isMcpExpanded(name: string) {
  return !!expandedMcp.value[name]
}

function ensureMcpEditDraft(srv: McpProjectServer) {
  if (!mcpEditDrafts.value[srv.name]) {
    mcpEditDrafts.value[srv.name] = {
      transport: srv.transport || (srv.url ? 'http' : 'stdio'),
      url: srv.url || '',
      command: srv.command || '',
      argsText: (srv.args || []).join(' '),
    }
  }
}

function mcpEditDraft(srv: McpProjectServer) {
  ensureMcpEditDraft(srv)
  return mcpEditDrafts.value[srv.name]
}

function setMcpEditField(name: string, field: 'url' | 'command' | 'argsText', value: string) {
  const draft = mcpEditDrafts.value[name]
  if (!draft) return
  draft[field] = value
}

function mcpEditDirty(srv: McpProjectServer) {
  const draft = mcpEditDraft(srv)
  const args = (srv.args || []).join(' ')
  if ((draft.transport || srv.transport) === 'http') {
    return draft.url.trim() !== (srv.url || '').trim()
  }
  return draft.command.trim() !== (srv.command || '').trim() || draft.argsText.trim() !== args.trim()
}

function toggleMcp(name: string) {
  const next = !expandedMcp.value[name]
  expandedMcp.value[name] = next
  if (next) {
    const srv = mcpStatus.value?.project_servers?.find((s) => s.name === name)
    if (srv) {
      ensureMcpEditDraft(srv)
      if (!(mcpServerTools.value[name]?.length) && !(srv.tools?.length)) {
        void refreshMcpServerTools(srv)
      }
    }
  }
}

function hasMcpEnvEdits(srv: McpProjectServer) {
  return mcpEnvKeysFor(srv).some((entry) => (mcpEnvInputs.value[entry.key] || '').length > 0)
}

/** Well-known secrets when the status API has not returned env_keys yet. */
const MCP_DEFAULT_ENV_KEYS: Record<string, { key: string; hint: string }> = {
  n8n_mcp: {
    key: 'N8N_MCP_TOKEN',
    hint: 'Bearer token for your n8n MCP HTTP endpoint.',
  },
  notion: {
    key: 'NOTION_TOKEN',
    hint: 'Notion internal integration secret.',
  },
}

type McpEnvKeyView = McpEnvKey & { hint?: string }

function mcpEnvKeysFor(srv: McpProjectServer): McpEnvKeyView[] {
  if (srv.env_keys?.length) {
    return srv.env_keys.map((entry) => {
      const fallback = MCP_DEFAULT_ENV_KEYS[srv.name]
      return {
        ...entry,
        hint: fallback?.key === entry.key ? fallback.hint : undefined,
      }
    })
  }
  const fallback = MCP_DEFAULT_ENV_KEYS[srv.name]
  if (!fallback) return []
  return [{
    key: fallback.key,
    configured: false,
    source: 'suggested',
    hint: fallback.hint,
  }]
}

function splitMcpArgs(text: string): string[] {
  return text.trim().split(/\s+/).filter(Boolean)
}

async function saveMcpEnvKeys(srv: McpProjectServer) {
  const keys: Record<string, string> = {}
  for (const entry of mcpEnvKeysFor(srv)) {
    const value = mcpEnvInputs.value[entry.key]
    if (value != null && value.length > 0) {
      keys[entry.key] = value
    }
  }
  if (!Object.keys(keys).length) return
  mcpEnvSaving.value = true
  mcpEnvResult.value = ''
  mcpEnvError.value = false
  mcpEnvResultServer.value = srv.name
  try {
    const res = await api.post<McpStatus>('/api/mcp/env-keys', { keys, server: srv.name })
    mcpStatus.value = res
    for (const key of Object.keys(keys)) {
      mcpEnvInputs.value[key] = ''
    }
    const updated = res.project_servers?.find((s) => s.name === srv.name)
    if (updated) {
      mcpEditDrafts.value[srv.name] = {
        transport: updated.transport || (updated.url ? 'http' : 'stdio'),
        url: updated.url || '',
        command: updated.command || '',
        argsText: (updated.args || []).join(' '),
      }
    }
    mcpEnvResult.value = 'Saved to workspace .env. New chats will pick up the keys.'
    notifySaved(`Saved MCP secrets for ${srv.name}.`)
    setTimeout(() => {
      if (mcpEnvResultServer.value === srv.name) mcpEnvResult.value = ''
    }, 3000)
  } catch (e) {
    mcpEnvError.value = true
    mcpEnvResult.value = errorMessage(e, 'Failed to save MCP secrets.')
  } finally {
    mcpEnvSaving.value = false
  }
}

async function saveMcpServer(srv: McpProjectServer) {
  const draft = mcpEditDraft(srv)
  mcpServerSaving.value = srv.name
  mcpServerResult.value = ''
  mcpServerError.value = false
  mcpServerResultName.value = srv.name
  try {
    const body: Record<string, unknown> = {}
    if ((draft.transport || srv.transport) === 'http') {
      body.url = draft.url.trim()
      body.command = ''
      body.args = []
    } else {
      body.command = draft.command.trim()
      body.args = splitMcpArgs(draft.argsText)
      body.url = ''
    }
    const res = await api.patch<McpStatus>(`/api/mcp/servers/${encodeURIComponent(srv.name)}`, body)
    mcpStatus.value = res
    const updated = res.project_servers?.find((s) => s.name === srv.name)
    if (updated) {
      mcpEditDrafts.value[srv.name] = {
        transport: updated.transport || (updated.url ? 'http' : 'stdio'),
        url: updated.url || '',
        command: updated.command || '',
        argsText: (updated.args || []).join(' '),
      }
    }
    mcpServerResult.value = 'Connection saved to .mcp.json.'
    notifySaved(`Updated MCP server ${srv.name}.`)
    setTimeout(() => {
      if (mcpServerResultName.value === srv.name) mcpServerResult.value = ''
    }, 3000)
  } catch (e) {
    mcpServerError.value = true
    mcpServerResult.value = errorMessage(e, 'Failed to save MCP server.')
  } finally {
    mcpServerSaving.value = ''
  }
}

async function refreshMcpServerTools(srv: McpProjectServer) {
  mcpToolsLoading.value[srv.name] = true
  mcpToolsError.value[srv.name] = ''
  try {
    const res = await api.get<{
      ok: boolean
      tools?: string[]
      error?: string
      tools_note?: string
      tools_source?: string
    }>(`/api/mcp/servers/${encodeURIComponent(srv.name)}/tools`)
    const tools = res.tools || []
    mcpServerTools.value[srv.name] = tools
    if (mcpStatus.value?.project_servers) {
      const target = mcpStatus.value.project_servers.find((s) => s.name === srv.name)
      if (target) {
        target.tools = tools
        target.tools_source = res.tools_source || (tools.length ? 'probed' : 'none')
        if (res.tools_note) target.tools_note = res.tools_note
      }
    }
    if (!res.ok && res.error) {
      mcpToolsError.value[srv.name] = res.error
    }
  } catch (e) {
    const message = errorMessage(e, 'Could not load tools.')
    mcpToolsError.value[srv.name] = /not available on the running server|Unexpected token|<!DOCTYPE|not valid JSON/i.test(message)
      ? 'MCP tools endpoint not available on the running server yet. Use Settings → Deploy, then restart Ciaobot.'
      : message
  } finally {
    mcpToolsLoading.value[srv.name] = false
  }
}

function saveFastMcpToggle() {
  notifySaved(fastMcpEnabled.value ? 'Ciaobot FastMCP enabled.' : 'Ciaobot FastMCP disabled.')
}

function onReentrySummaryToggle() {
  setReentrySummaryEnabled(reentrySummaryEnabled.value)
  // The store action already evicts cached summaries when the toggle goes
  // off. Persist to localStorage too so the choice survives reload.
  if (reentrySummaryEnabled.value) {
    notifySaved('Re-entry summary enabled.')
  } else {
    notifySaved('Re-entry summary disabled.')
  }
}

async function createMcpViaChat() {
  const activeProj = projectStore.activeProject
  let projectId = activeProj?.project_id
  if (!projectId) {
    projectId = projectStore.workspaceProjects[0]?.project_id
  }
  if (!projectId) {
    projectId = projectStore.projects[0]?.project_id
  }
  if (!projectId) {
    notifySaved('Create a project first, then start the chat.', 'No project yet')
    return
  }

  try {
    const chat = await projectStore.createChat(projectId, 'New MCP Server')
    if (chat) {
      router.push({
        path: `/chat/${chat.chat_id}`,
        query: { initialPrompt: 'Help me set up and configure a new MCP server for this project.' }
      })
    }
  } catch (e) {
    notifyFailed('Could not create chat', errorMessage(e))
  }
}

async function addCustomMcpServer() {
  if (!newMcpName.value.trim()) return
  addingMcpServer.value = true
  addMcpServerResult.value = ''
  addMcpServerError.value = false
  const name = newMcpName.value.trim()
  try {
    const body: Record<string, unknown> = { name }
    if (newMcpTransport.value === 'http') {
      body.url = newMcpUrl.value.trim()
    } else {
      const parts = splitMcpArgs(newMcpCommand.value)
      body.command = parts[0] || ''
      body.args = parts.slice(1)
    }
    const res = await api.post<McpStatus>('/api/mcp/servers', body)
    mcpStatus.value = res
    newMcpName.value = ''
    newMcpUrl.value = ''
    newMcpCommand.value = ''
    showAddMcpServer.value = false
    expandedMcp.value[name] = true
    const created = res.project_servers?.find((s) => s.name === name)
    if (created) {
      mcpEditDrafts.value[name] = {
        transport: created.transport || (created.url ? 'http' : 'stdio'),
        url: created.url || '',
        command: created.command || '',
        argsText: (created.args || []).join(' '),
      }
    }
    notifySaved(`Added MCP server ${name}.`)
  } catch (e) {
    addMcpServerError.value = true
    addMcpServerResult.value = errorMessage(e, `Failed to add MCP server`)
  } finally {
    addingMcpServer.value = false
  }
}

async function deleteCustomMcpServer(name: string) {
  if (!await askConfirm(`Are you sure you want to delete MCP server "${name}"?`, {
    title: 'Delete MCP server',
    confirmLabel: 'Delete server',
    destructive: true,
  })) return
  try {
    const res = await api.del<McpStatus>(`/api/mcp/servers/${encodeURIComponent(name)}`)
    mcpStatus.value = res
    delete mcpEditDrafts.value[name]
    delete mcpServerTools.value[name]
    delete mcpToolsError.value[name]
    notifySaved(`Removed MCP server ${name}.`)
  } catch (e) {
    notifyFailed(`Could not delete MCP server ${name}`, errorMessage(e, 'The request failed.'))
  }
}

// ── Appearance settings ────────────────────────────────────────────────────
const activeTheme = ref('system')
// The scale itself, its bounds, its step and its persistence live in
// useFontScale, shared with the global zoom shortcuts. Only the displayed
// percentage is Settings' own: the scale is anchored to the pre-rescale UI, so
// DEFAULT_FONT_SCALE (1.2) reads as "100%".
const { fontScale, adjust: adjustFontScale, reset: resetFontScale } = useFontScale()
const fontScalePercent = computed(() => Math.round((fontScale.value / DEFAULT_FONT_SCALE) * 100))

function loadAppearanceSettings() {
  try {
    activeTheme.value = localStorage.getItem('ciao-theme') || 'system'
  } catch {
    // Ignore localStorage block
  }
}

function setTheme(theme: 'dark' | 'light' | 'system') {
  activeTheme.value = theme
  try {
    localStorage.setItem('ciao-theme', theme)
  } catch { /* localStorage blocked */ }

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

// Every provider with models is a runtime provider now.
type AliasProviderKey = RuntimeProvider
type RoutineModelKey = 'insights_model'
// The routine pickers offer Automatic, Apple, and each available provider.
type RoutineProviderValue = 'automatic' | 'apple' | AliasProviderKey

type AliasProviderSection = {
  key: AliasProviderKey
  label: string
  options: string[]
  configurable: boolean
  // Whether the provider is signed in and reporting models. Routine selectors
  // filter to available sections.
  available: boolean
}

const routineEffectiveKeys: Record<RoutineModelKey, keyof RoutineSettings> = {
  insights_model: 'insights_model_effective',
}

async function fetchRoutines() {
  try {
    routines.value = await api.get<RoutineSettings>('/api/settings/routines')
  } catch (e) {
    routinesError.value = `Failed to load model settings: ${errorMessage(e)}`
  } finally {
    routinesLoaded.value = true
  }
}

async function saveRoutines(patch: Record<string, unknown>) {
  routinesSaving.value = true
  routinesResult.value = ''
  try {
    routines.value = await api.patch<RoutineSettings>('/api/settings/routines', patch)
    notifySaved('Model settings saved.')
  } catch (e) {
    routinesResult.value = `Error: ${errorMessage(e)}`
  } finally {
    routinesSaving.value = false
  }
}

// Apple's on-device model is hardware-gated: it shows only when this machine
// can run it. No app-side opt-in flag any more.
const appleModelAvailable = computed(() => routines.value?.apple_model_available === true)

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

// A panel entry may name the provider that runs it. Anthropic tiers are bare
// aliases; opencode entries carry a `<provider>:` prefix, matching
// what `ciao/critique.py::_split_provider` dispatches on.
const critiqueModelSections = computed<ModelSection[]>(() => {
  const options = routines.value?.model_options
  if (!options) return []
  const tiers = options.anthropic || []
  const prefixed = (provider: string, models: string[]) =>
    models.filter((m) => tiers.includes(m)).map((m) => `${provider}:${m}`)
  const opencodeModels = workspaceModels.value?.opencode_models || []
  return [
    { key: 'anthropic', label: 'Anthropic', models: tiers },
    {
      key: 'opencode',
      label: 'opencode',
      models: opencodeModels.length ? prefixed('opencode', tiers) : [],
    },
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
      label: 'Anthropic (via Claude Code)',
      options: settings.model_options.anthropic || [],
      configurable: false,
      available: true,
    },
  ]
  // opencode can serve routines once its catalog is non-empty.
  const opencodeModels = parseModelList((
    workspaceModels.value?.opencode_models
    || workspaceModels.value?.provider_models?.opencode
    || []
  ).join(','))
  if (opencodeModels.length) {
    sections.push({
      key: 'opencode',
      label: 'opencode',
      options: opencodeModels,
      configurable: true,
      available: true,
    })
  }
  return sections
})

// Provider key -> human label, for components that render model ids from the
// default-model table (Automations offers a one-off retry model).
const aliasProviderLabels = computed<Record<string, string>>(() => {
  const labels: Record<string, string> = { claude: 'Anthropic (via Claude Code)' }
  for (const section of aliasProviderSections.value) labels[section.key] = section.label
  return labels
})

function getProviderSection(provider: string): AliasProviderSection | undefined {
  return aliasProviderSections.value.find((s) => s.key === provider)
}

// ── Per-provider default model / thinking (Models tab) ─────────────────
const DEFAULT_MODEL_SELECTION = '__ciao_default_model__'

function providerDefaultModelOverride(provider: AliasProviderKey): string {
  return routines.value?.provider_default_models?.[provider] || ''
}

// The alias-provider section entry (models + disabled state) is identical
// wherever a picker offers one provider's catalog; only the surrounding
// sections (e.g. the "Default"/Automatic entry above) differ per caller.
function aliasSectionEntry(provider: string): ModelSection {
  const section = aliasProviderSections.value.find((s) => s.key === provider)
  return {
    key: provider,
    label: section?.label || provider,
    models: section?.options || [],
    disabled: !section?.available,
  }
}

function providerDefaultModelSectionsFor(provider: AliasProviderKey): ModelSection[] {
  const effective = providerDefaultModelEffective(provider)
  return [
    {
      key: 'default',
      label: 'Default',
      models: [DEFAULT_MODEL_SELECTION],
      modelLabels: {
        [DEFAULT_MODEL_SELECTION]: effective ? `Automatic (${effective})` : 'Automatic',
      },
    },
    aliasSectionEntry(provider),
  ]
}

function providerDefaultModelEffective(provider: AliasProviderKey): string {
  const explicit = workspaceModels.value?.provider_defaults?.[provider]
  if (explicit) return explicit
  // Fall back to the provider's first discovered model so a routine pick can
  // store a concrete id even before an operator default is configured.
  const section = aliasProviderSections.value.find((s) => s.key === provider)
  return section?.options?.[0] || ''
}

function providerDefaultModelSelectorValue(provider: AliasProviderKey): string {
  return providerDefaultModelOverride(provider) || DEFAULT_MODEL_SELECTION
}

async function saveProviderDefaultModel(provider: AliasProviderKey, value: string | string[]) {
  const selected = Array.isArray(value) ? value[0] || '' : value
  const model = selected === DEFAULT_MODEL_SELECTION ? '' : selected
  const defaults = JSON.parse(
    JSON.stringify(routines.value?.provider_default_models || {}),
  ) as Record<string, string>
  if (model.trim()) defaults[provider] = model.trim()
  else delete defaults[provider]
  await saveRoutines({ provider_default_models: defaults })
  await fetchWorkspaceModels()
}

const DEFAULT_THINKING_SELECTION = '__ciao_thinking_default__'

function providerDefaultThinkingOverride(provider: AliasProviderKey): string {
  return routines.value?.provider_default_thinking?.[provider] || ''
}

function providerDefaultThinkingValue(provider: AliasProviderKey): string {
  return providerDefaultThinkingOverride(provider) || DEFAULT_THINKING_SELECTION
}

function providerThinkingOptions(provider: AliasProviderKey): { value: string; label: string }[] {
  const levels = workspaceModels.value?.thinking_levels?.[provider] || []
  return [
    {
      value: DEFAULT_THINKING_SELECTION,
      label: 'Automatic (provider default)',
    },
    ...levels.map((level) => ({ value: level, label: level })),
  ]
}

async function saveProviderDefaultThinking(provider: AliasProviderKey, value: string) {
  const selected = value === DEFAULT_THINKING_SELECTION ? '' : value
  const defaults = JSON.parse(
    JSON.stringify(routines.value?.provider_default_thinking || {}),
  ) as Record<string, string>
  if (selected) defaults[provider] = selected
  else delete defaults[provider]
  await saveRoutines({ provider_default_thinking: defaults })
}

function serializeRoutineModel(provider: RoutineProviderValue, model: string): string {
  // Runtime-provider models need an explicit qualifier so the backend does not
  // send a global routine override through Claude by default.
  if (provider === 'opencode') {
    return `${provider}:${model}`
  }
  return model
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

function inferRoutineModel(model: string): { provider: RoutineProviderValue; model: string } {
  const raw = model.trim()
  if (!raw) return { provider: 'automatic', model: '' }
  // 'apfel' is the legacy id from when this shelled out to the apfel CLI.
  if (raw === 'apple' || raw === 'apfel') return { provider: 'apple', model: '' }
  for (const provider of ['opencode'] as const) {
    const prefix = `${provider}:`
    if (raw.startsWith(prefix)) {
      return { provider, model: raw.slice(prefix.length) }
    }
  }
  // A bare Claude tier alias is a real model id on Claude.
  if (['haiku', 'sonnet', 'opus', 'fable'].includes(raw)) {
    return { provider: 'claude', model: raw }
  }
  // A concrete model id on the default provider (Claude).
  return { provider: 'claude', model: raw }
}

function routineProviderValue(key: RoutineModelKey): RoutineProviderValue {
  return inferRoutineModel(routines.value?.[key] || '').provider
}

function routineModelValue(key: RoutineModelKey): string {
  const raw = routines.value?.[key] || ''
  if (raw.trim()) return inferRoutineModel(raw).model
  return routineEffectiveModel(key)
}

// The concrete-model sections for a routine once its provider is chosen.
function routineModelSectionsFor(key: RoutineModelKey): ModelSection[] {
  const provider = routineProviderValue(key)
  if (provider === 'automatic' || provider === 'apple') return []
  return [aliasSectionEntry(provider)]
}

async function saveRoutineProvider(key: RoutineModelKey, providerValue: string) {
  const provider = providerValue as RoutineProviderValue
  if (provider === 'automatic') {
    await saveRoutines({ [key]: '' })
    return
  }
  if (provider === 'apple') {
    await saveRoutines({ [key]: 'apple' })
    return
  }
  // Pick the provider's effective default model as the starting point.
  const model = providerDefaultModelEffective(provider) || routineEffectiveModel(key)
  await saveRoutines({ [key]: serializeRoutineModel(provider, model) })
}

async function saveRoutineModel(key: RoutineModelKey, value: string | string[]) {
  const selected = Array.isArray(value) ? value[0] || '' : value
  if (!selected) {
    await saveRoutines({ [key]: '' })
    return
  }
  await saveRoutines({ [key]: selected })
}

// Automatic does not pick one model: resolve_insights_model takes the chat's
// workspace and reads that workspace's tier. Naming a single model here read
// as a global choice and was wrong for every workspace but the primary one, so
// say what it follows and list the per-workspace answers.
function routineWorkspaceModels(key: RoutineModelKey): Array<[string, string]> {
  const settings = routines.value
  if (!settings) return []
  return Object.entries(settings.insights_model_by_workspace || {})
}

function routineModelSummary(key: RoutineModelKey): string {
  const provider = routineProviderValue(key)
  if (provider === 'automatic') {
    const perWorkspace = routineWorkspaceModels(key)
    const distinct = new Set(perWorkspace.map(([, model]) => model))
    if (distinct.size > 1) {
      const parts = perWorkspace.map(([ws, model]) => `${ws}: ${model || 'default'}`)
      return `Automatic — follows each chat's workspace (${parts.join(' · ')})`
    }
    if (distinct.size === 1) {
      return `Automatic — follows each chat's workspace (currently ${[...distinct][0] || 'default'} for all)`
    }
    return `Automatic: ${routineEffectiveModel(key) || 'default'}`
  }
  if (provider === 'apple') return 'Local (free)'
  const model = routineModelValue(key)
  if (provider === 'opencode') return `opencode: ${model || 'default'}`
  return `${aliasProviderLabel(provider)}: ${model || 'default'}`
}

// ── Provider API Key settings (Providers tab) ─────────────────────────────────
const providerKeys = ref<ProviderConfigSettings | null>(null)
const providerKeysLoaded = ref(false)
const providerKeysError = ref('')
const mcpStatus = ref<McpStatus | null>(null)
const mcpUsage = ref<McpUsage | null>(null)
const mcpUsageLoaded = ref(false)
const mcpUsageError = ref('')
const providerConnectionPending = ref('')
const providerConnectionResult = ref('')
const gwsIntegration = ref<GwsIntegrationSettings | null>(null)
const gwsIntegrationLoaded = ref(false)
const gwsIntegrationError = ref('')

type GwsProfile = GwsIntegrationSettings['profiles'][number]

function gwsProfileStatus(profile: GwsProfile): string {
  if (profile.configured && profile.needs_relogin) return 'Login expired'
  if (profile.configured) return 'Authenticated'
  if (profile.client_secret_present) return 'Ready to auth'
  return 'Needs OAuth client'
}

function gwsProfileBadgeClass(profile: GwsProfile): string {
  if (profile.configured && profile.needs_relogin) return 'badge--error'
  if (profile.configured) return 'badge--success'
  if (profile.client_secret_present) return 'badge--warn'
  return 'badge--error'
}

const defaultGwsProfileName = computed(() => gwsIntegration.value?.default_profile || '')

// Only the accounts this install actually has. No built-in personal/work pair:
// a fresh install has none until the user adds one below.
const gwsProfileOptions = computed(() =>
  (gwsIntegration.value?.profiles || []).map((profile) => ({
    name: profile.name,
    label: profile.label,
    email: profile.email,
  })),
)

const gwsUnlinkedOptionLabel = computed(() =>
  defaultGwsProfileName.value
    ? `Default (${defaultGwsProfileName.value})`
    : 'No Google account',
)

function workspaceCustomGwsProfile(profile: string): boolean {
  const name = profile.trim()
  return Boolean(name) && !gwsProfileOptions.value.some((option) => option.name === name)
}

async function fetchGwsIntegration() {
  gwsIntegrationError.value = ''
  try {
    gwsIntegration.value = await api.get<GwsIntegrationSettings>('/api/integrations/gws')
  } catch (e) {
    gwsIntegrationError.value = `Failed to load Google Workspace integration: ${errorMessage(e)}`
  } finally {
    gwsIntegrationLoaded.value = true
  }
}

const newGwsProfileName = ref('')
const newGwsProfileLabel = ref('')
const gwsProfileAdding = ref(false)
const gwsProfileActionError = ref('')

async function addGwsProfile() {
  const name = newGwsProfileName.value.trim()
  if (!name || gwsProfileAdding.value) return
  gwsProfileAdding.value = true
  gwsProfileActionError.value = ''
  try {
    gwsIntegration.value = await api.post<GwsIntegrationSettings>(
      '/api/integrations/gws/profiles/add',
      { name, label: newGwsProfileLabel.value.trim() },
    )
    newGwsProfileName.value = ''
    newGwsProfileLabel.value = ''
  } catch (e) {
    gwsProfileActionError.value = errorMessage(e, 'Failed to add the account')
  } finally {
    gwsProfileAdding.value = false
  }
}

async function removeGwsProfile(profile: GwsProfile) {
  const linked = profile.workspaces.length
    ? ` ${profile.workspaces.join(', ')} will lose their Google access until you link another account.`
    : ''
  if (!await askConfirm(
    `Remove ${profile.label} and delete its stored Google credentials from this machine?${linked}`,
    { title: 'Remove Google account?', confirmLabel: 'Remove account', destructive: true },
  )) {
    return
  }
  gwsSavingProfile.value = profile.name
  gwsProfileActionError.value = ''
  try {
    gwsIntegration.value = await api.post<GwsIntegrationSettings>(
      '/api/integrations/gws/profiles/remove',
      { profile: profile.name },
    )
    await fetchWorkspacesList()
  } catch (e) {
    gwsProfileActionError.value = errorMessage(e, 'Failed to remove the account')
  } finally {
    gwsSavingProfile.value = null
  }
}

async function installGwsInChat() {
  try {
    await projectStore.fixError({
      errorText:
        'The @googleworkspace/cli (gws) is not installed on this machine, so Google Workspace tools are unavailable.',
      context:
        'Installing @googleworkspace/cli from Settings → Google Workspace → Install in Chat. Run `npm install -g @googleworkspace/cli` (Node.js/npm required; npm exit code 243 / EACCES is common when /usr/local is root-owned after a .pkg Node install). Walk the operator through the install; Ciaobot never runs package installs on its own.',
      title: 'Install gws',
    })
  } catch (e) {
    notifyFailed('Could not open install chat', errorMessage(e))
  }
}

const gwsSavingProfile = ref<string | null>(null)
const gwsAuthUrls = ref<Record<string, string>>({})
const gwsAuthFlowIds = ref<Record<string, string>>({})
const gwsRedirectUrls = ref<Record<string, string>>({})

// One-click "Sign in with Google" loopback flow (issue #145). Tracks, per
// profile, whether a server-managed re-login is pending so the Settings card
// can poll `relogin/status` until it completes instead of copy-pasting a
// redirect URL. Mirrors the manual JSON-upload flow as the power-user path.
const gwsReloginPending = ref<Record<string, boolean>>({})
const gwsReloginError = ref<Record<string, string>>({})
let gwsReloginTimer: ReturnType<typeof setInterval> | null = null
// A tick can outlast its own interval: the completed branch awaits
// fetchGwsIntegration(), which runs a health check that shells out per profile
// with a 30s timeout. Without this, every 2s tick in that window starts another
// round of status polls and another concurrent health scan.
let gwsReloginPolling = false

function gwsReloginHelpUrl(): string {
  return 'https://cloud.google.com/sdk/docs/install'
}

// The loopback re-login listener binds to the *engine's* 127.0.0.1, so the
// consent redirect only reaches it when the browser is on the engine host
// (localhost) and the integration API is not proxied to a remote host. From a
// phone, LAN browser, or client-mode node the popup's redirect would target
// the client's own loopback and never arrive — those users must use the
// manual paste flow instead.
function gwsOnEngineHost(): boolean {
  return isGwsEngineHostEligible(window.location.hostname, inDesktopApp, {
    loaded: nodeStatusLoaded.value,
    error: nodeStatusError.value,
    isClient: isNodeClient.value,
  })
}

async function gwsReloginStart(profileName: string) {
  gwsReloginError.value[profileName] = ''
  if (!gwsOnEngineHost()) {
    gwsReloginError.value[profileName] =
      'One-click sign-in only works from a browser on this machine. Use Manual connect (paste code) instead.'
    return
  }
  gwsSavingProfile.value = profileName
  // Open a placeholder tab synchronously so the browser does not treat it as a
  // popup (blocked outside the click's user-activation window); navigate it to
  // the consent URL once the backend hands one back. If the popup is blocked,
  // surface the link so the flow can still proceed manually.
  const tab = window.open('', '_blank')
  try {
    const res = await api.post<{ auth_url: string }>('/api/integrations/gws/relogin/start', {
      profile: profileName,
    })
    gwsReloginPending.value[profileName] = true
    if (tab && tab.location) {
      tab.location.href = res.auth_url
    } else {
      gwsReloginError.value[profileName] =
        'The sign-in tab was blocked by the browser. Open this link, then finish here:'
      gwsAuthUrls.value[profileName] = res.auth_url
    }
    gwsPollRelogin(profileName)
  } catch (e) {
    if (tab) tab.close()
    gwsReloginError.value[profileName] = errorMessage(e, 'Could not start the sign-in flow.')
  } finally {
    gwsSavingProfile.value = null
  }
}

function gwsClearRelogin(profileName: string) {
  delete gwsReloginPending.value[profileName]
  delete gwsReloginError.value[profileName]
  // The fallback auth URL belongs to a now-finished loopback session; clearing
  // it keeps the card out of a stale manual-flow state and un-hides the
  // authenticated controls on success.
  delete gwsAuthUrls.value[profileName]
  delete gwsAuthFlowIds.value[profileName]
  delete gwsRedirectUrls.value[profileName]
}

function gwsPollRelogin(profileName: string) {
  if (gwsReloginTimer) return
  gwsReloginTimer = setInterval(async () => {
    if (gwsReloginPolling) return
    const pending = Object.entries(gwsReloginPending.value)
      .filter(([, p]) => p)
      .map(([name]) => name)
    if (!pending.length) {
      if (gwsReloginTimer) {
        clearInterval(gwsReloginTimer)
        gwsReloginTimer = null
      }
      return
    }
    gwsReloginPolling = true
    try {
      for (const name of pending) {
        try {
          const status = await api.get<{ status: string; email?: string; error?: string }>(
            `/api/integrations/gws/relogin/status?profile=${encodeURIComponent(name)}`,
          )
          if (status.status === 'completed') {
            gwsClearRelogin(name)
            await fetchGwsIntegration()
            notifySaved('Google account connected.')
          } else if (status.status === 'error' || status.status === 'none') {
            const msg =
              status.error ||
              (status.status === 'none'
                ? 'The sign-in timed out before you finished it. Try again.'
                : 'The sign-in failed.')
            gwsClearRelogin(name)
            gwsReloginError.value[name] = msg
            await fetchGwsIntegration()
          }
        } catch (e) {
          const msg = errorMessage(e, 'Could not check the sign-in status.')
          gwsClearRelogin(name)
          gwsReloginError.value[name] = msg
        }
      }
    } finally {
      gwsReloginPolling = false
    }
  }, 2000)
}

function gwsReloginCancel(profileName: string) {
  gwsClearRelogin(profileName)
  api.post('/api/integrations/gws/relogin/cancel', { profile: profileName }).catch(() => {})
}

onUnmounted(() => {
  if (gwsReloginTimer) {
    clearInterval(gwsReloginTimer)
    gwsReloginTimer = null
  }
})

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
  } catch (e) {
    notifyFailed('Could not upload the client secret', errorMessage(e, 'The upload failed.'))
  } finally {
    gwsSavingProfile.value = null
    target.value = ''
  }
}

async function startGwsAuth(profileName: string) {
  gwsSavingProfile.value = profileName
  // A prior one-click failure (e.g. the remote-browser guard) leaves an error
  // that would otherwise win over the manual-flow box and hide it.
  delete gwsReloginError.value[profileName]
  delete gwsReloginPending.value[profileName]
  try {
    const res = await api.post<{ auth_url: string; flow_id: string }>('/api/integrations/gws/auth-url', {
      profile: profileName,
    })
    gwsAuthUrls.value[profileName] = res.auth_url
    gwsAuthFlowIds.value[profileName] = res.flow_id
    gwsRedirectUrls.value[profileName] = ''
    window.open(res.auth_url, '_blank')
  } catch (e) {
    notifyFailed('Could not generate the authorization URL', errorMessage(e, 'The request failed.'))
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
      flow_id: gwsAuthFlowIds.value[profileName],
    })
    gwsIntegration.value = updated
    delete gwsAuthUrls.value[profileName]
    delete gwsAuthFlowIds.value[profileName]
    delete gwsRedirectUrls.value[profileName]
  } catch (e) {
    notifyFailed('Could not complete the connection', errorMessage(e, 'The request failed.'))
  } finally {
    gwsSavingProfile.value = null
  }
}

function cancelGwsAuth(profileName: string) {
  delete gwsAuthUrls.value[profileName]
  delete gwsAuthFlowIds.value[profileName]
  delete gwsRedirectUrls.value[profileName]
}

async function disconnectGwsProfile(profileName: string, deleteClientSecret: boolean) {
  const message = deleteClientSecret
    ? `Are you sure you want to delete the OAuth Client Secret for the ${profileName} profile?`
    : `Are you sure you want to disconnect/sign out the ${profileName} Google account?`

  if (!await askConfirm(message, {
    title: deleteClientSecret ? 'Delete OAuth Client Secret' : 'Disconnect Google account',
    confirmLabel: deleteClientSecret ? 'Delete secret' : 'Disconnect',
    destructive: true,
  })) return

  gwsSavingProfile.value = profileName
  try {
    const updated = await api.post<GwsIntegrationSettings>('/api/integrations/gws/disconnect', {
      profile: profileName,
      delete_client_secret: deleteClientSecret,
    })
    gwsIntegration.value = updated
    cancelGwsAuth(profileName)
  } catch (e) {
    notifyFailed('Could not update the profile connection', errorMessage(e, 'The request failed.'))
  } finally {
    gwsSavingProfile.value = null
  }
}

async function fetchProviderKeys() {
  try {
    const res = await api.get<ProviderConfigSettings>('/api/settings/providers')
    providerKeys.value = res
  } catch (e) {
    providerKeysError.value = `Failed to load provider keys: ${errorMessage(e)}`
  } finally {
    providerKeysLoaded.value = true
  }
}

async function fetchMcpStatus() {
  try {
    mcpStatus.value = await api.get<McpStatus>('/api/mcp/status')
  } catch {
    mcpStatus.value = { enabled: false, bound: false, tool_count: 0 }
  }
}

async function fetchMcpUsage() {
  mcpUsageError.value = ''
  try {
    mcpUsage.value = await api.get<McpUsage>('/api/mcp/usage')
  } catch (err) {
    mcpUsage.value = null
    const message = err instanceof Error ? err.message : String(err)
    // A non-JSON body (the SPA index.html) means the /api/mcp/usage route
    // isn't served yet — the running backend predates it and needs a restart.
    mcpUsageError.value = /Unexpected token|not valid JSON|<!DOCTYPE/i.test(message)
      ? 'MCP usage endpoint not available on the running server yet. Restart the Ciaobot service (or ask the operator to Deploy) to enable it.'
      : message || 'Could not load MCP tool usage.'
  } finally {
    mcpUsageLoaded.value = true
  }
}



async function providerConnectionAction(provider: string, action: 'connect' | 'verify' | 'logout') {
  if (action === 'logout' && !await askConfirm(`Log out of ${provider === 'opencode' ? 'opencode' : 'Claude Code'} on this computer?`, {
    title: 'Log out',
    confirmLabel: 'Log out',
    destructive: true,
  })) {
    return
  }
  providerConnectionPending.value = provider
  providerConnectionResult.value = ''
  try {
    const result = await api.post<ProviderActionResult>(`/api/settings/providers/${provider}/${action}`)
    if (action === 'connect') {
      providerConnectionResult.value = result.opened
        ? `Opened ${provider === 'opencode' ? 'opencode' : 'Claude Code'} login in Terminal.`
        : `Run ${result.command} in Terminal.`
    } else if (action === 'logout') {
      providerConnectionResult.value = 'Logged out.'
    } else {
      providerConnectionResult.value = result.ok ? `Connection verified (${result.auth}).` : result.detail || 'Not connected.'
    }
    await fetchProviderKeys()
  } catch (e) {
    providerConnectionResult.value = `Error: ${errorMessage(e)}`
  } finally {
    providerConnectionPending.value = ''
  }
}

async function fetchSkills() {
  try {
    skillsInventory.value = await api.get<SkillInventory>('/api/admin/skills')
  } catch (e) {
    skillsError.value = `Failed to load skills: ${errorMessage(e)}`
  } finally {
    skillsLoaded.value = true
  }
}

async function fetchCommands() {
  commandsError.value = ''
  try {
    const res = await api.get<CommandsResponse>('/api/commands')
    commands.value = Array.isArray(res.commands) ? res.commands : []
  } catch (e) {
    commandsError.value = `Failed to load commands: ${errorMessage(e)}`
  } finally {
    commandsLoaded.value = true
  }
}

async function fetchAgentAssets() {
  agentAssetsError.value = ''
  try {
    agentAssets.value = await api.get<AgentAssetsResponse>('/api/agent-assets')
  } catch (e) {
    agentAssetsError.value = `Failed to load agent assets: ${errorMessage(e)}`
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
  } catch (e) {
    healthFixError.value = errorMessage(e, 'fix failed')
  } finally {
    healthFixPending.value = false
  }
}

const stockSkills = computed(() => {
  return skillsInventory.value?.skills.filter(s => s.label === 'stock') || []
})

const customSkills = computed(() => {
  return skillsInventory.value?.skills.filter(s => s.label === 'custom') || []
})

/** Shared origin labels: Ciaobot-shipped vs user-authored. */
type AssetOrigin = 'builtin' | 'custom' | 'installed' | 'global'

function assetOriginLabel(origin: AssetOrigin): string {
  if (origin === 'custom') return 'Custom'
  if (origin === 'installed') return 'Installed'
  if (origin === 'global') return 'Global'
  return 'Built-in'
}

function assetOriginClass(origin: AssetOrigin): string {
  if (origin === 'custom') return 'badge badge--success command-source'
  if (origin === 'builtin') return 'badge badge--builtin command-source'
  return 'badge badge--muted command-source'
}

function commandOrigin(command: { editable?: boolean; scope?: string }): AssetOrigin {
  // Scope is definitive when set: stock commands/subagents are seeded into
  // the same editable location as custom ones (so users can override them
  // in place), so `editable` alone can't distinguish "built-in" from
  // "custom" — it's only a fallback for the rare case scope is unset.
  if (command.scope === 'custom') return 'custom'
  if (command.scope === 'built-in') return 'builtin'
  if (command.scope === 'global') return 'global'
  if (command.scope === 'installed') return 'installed'
  return command.editable ? 'custom' : 'installed'
}

function subagentOrigin(agent: { editable?: boolean; scope?: string }): AssetOrigin {
  return commandOrigin(agent)
}

function mcpServerOrigin(_srv: { name?: string; source?: string }): AssetOrigin {
  return 'custom'
}

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
  } catch (e) {
    addSubagentError.value = true
    addSubagentResult.value = `Error: ${errorMessage(e)}`
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
  } catch (e) {
    addCommandError.value = true
    addCommandResult.value = `Error: ${errorMessage(e)}`
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
  } catch (e) {
    assetLifecycleError.value = true
    assetLifecycleResult.value = `Error: ${errorMessage(e)}`
  } finally {
    savingSubagent.value = null
  }
}

async function deleteSubagent(agent: SubagentAsset) {
  if (!agent.editable) return
  if (!await askConfirm(`Delete custom subagent "${agent.name}"?`, {
    title: 'Delete subagent',
    confirmLabel: 'Delete subagent',
    destructive: true,
  })) return
  savingSubagent.value = agent.name
  assetLifecycleResult.value = 'Deleting subagent...'
  assetLifecycleError.value = false
  try {
    await api.del(`/api/agent-assets/subagents/${encodeURIComponent(agent.name)}`)
    assetLifecycleResult.value = ''
    notifySaved(`Deleted ${agent.name}. Restart or sync Claude Code sessions to pick it up.`, 'Subagent')
    if (editingSubagent.value === agent.name) cancelEditSubagent()
    await fetchAgentAssets()
  } catch (e) {
    assetLifecycleError.value = true
    assetLifecycleResult.value = `Error: ${errorMessage(e)}`
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
  } catch (e) {
    assetLifecycleError.value = true
    assetLifecycleResult.value = `Error: ${errorMessage(e)}`
  } finally {
    savingCommand.value = null
  }
}

async function deleteCommand(command: CommandAsset) {
  if (!command.editable) return
  if (!await askConfirm(`Delete custom command "/${command.name}"?`, {
    title: 'Delete command',
    confirmLabel: 'Delete command',
    destructive: true,
  })) return
  savingCommand.value = command.name
  assetLifecycleResult.value = 'Deleting command...'
  assetLifecycleError.value = false
  try {
    await api.del(`/api/agent-assets/commands/${encodeURIComponent(command.name)}`)
    assetLifecycleResult.value = ''
    notifySaved(`Deleted /${command.name}. Restart or sync Claude Code sessions to pick it up.`, 'Command')
    if (editingCommand.value === command.name) cancelEditCommand()
    await Promise.all([fetchAgentAssets(), fetchCommands()])
  } catch (e) {
    assetLifecycleError.value = true
    assetLifecycleResult.value = `Error: ${errorMessage(e)}`
  } finally {
    savingCommand.value = null
  }
}

const showAddSkill = ref(false)
const addingSkill = ref(false)
const addSkillResult = ref('')
const addSkillError = ref(false)
const skillZipFile = ref<File | null>(null)
const skillZipFileName = ref('')
const skillZipInput = ref<HTMLInputElement | null>(null)

function toggleAddSkill() {
  showAddSkill.value = !showAddSkill.value
  addSkillResult.value = ''
  addSkillError.value = false
  skillZipFile.value = null
  skillZipFileName.value = ''
}

function triggerSkillZipPicker() {
  skillZipInput.value?.click()
}

function onSkillZipSelected(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] || null
  skillZipFile.value = file
  skillZipFileName.value = file ? file.name : ''
  addSkillResult.value = ''
  addSkillError.value = false
}

async function uploadSkillZip() {
  if (!skillZipFile.value) return
  addingSkill.value = true
  addSkillResult.value = 'Uploading...'
  addSkillError.value = false
  try {
    const form = new FormData()
    form.append('file', skillZipFile.value)
    form.append('workspace', projectStore.activeWorkspace)
    const res = await api.postForm<{ ok: boolean; name?: string; message?: string; error?: string }>('/api/skills/import', form)
    if (res.ok) {
      addSkillResult.value = ''
      notifySaved(res.message || `Skill ${res.name} added — available to all operators after next sync.`, 'Skills')
      skillZipFile.value = null
      skillZipFileName.value = ''
      if (skillZipInput.value) skillZipInput.value.value = ''
      showAddSkill.value = false
      await fetchSkills()
    } else {
      addSkillError.value = true
      addSkillResult.value = res.error || 'Failed to add skill.'
    }
  } catch (e) {
    addSkillError.value = true
    addSkillResult.value = `Error: ${errorMessage(e)}`
  } finally {
    addingSkill.value = false
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
    notifySaved('Create a project first, then start the chat.', 'No project yet')
    return
  }

  try {
    const chat = await projectStore.createChat(projectId, 'New Custom Skill')
    if (chat) {
      const prompt = 'I want to create a new custom skill. Please guide me through writing a new skill (creating the SKILL.md under the skills/ directory).'
      projectStore.sendMessage(chat.chat_id, prompt)
    }
  } catch (e) {
    notifyFailed('Could not start chat', errorMessage(e))
  }
}


const automationItems = ref<AutomationProcess[]>([])
const automationLoaded = ref(false)
const automationError = ref('')
const proposalOutcomes = ref<ProposalOutcomes | null>(null)

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
  return formatDuration(dur) || 'unknown'
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
    const data = await api.get<AutomationPayload | AutomationProcess[]>('/api/automation?include=outcomes')
    if (Array.isArray(data)) {
      // An older server ignores the include hint and answers with the bare
      // job list; the outcomes line simply stays hidden.
      automationItems.value = data
    } else {
      automationItems.value = data.jobs
      proposalOutcomes.value = data.proposal_outcomes ?? null
    }
  } catch (e) {
    automationError.value = `Failed to load automation: ${errorMessage(e)}`
  } finally {
    automationLoaded.value = true
  }
}

// ── Workspaces settings (Workspaces tab) ───────────────────────────────────
// Transient success feedback. Routes through the app-wide in-app toast (the
// same auto-dismissing popup used for routine/chat notifications) instead of
// leaving persistent inline text under the form.
function notifySaved(body: string, title = 'settings') {
  projectStore.pushToast({ chat_id: '', title, body })
}

// The failure sibling of notifySaved. `alert` cannot be used for this: wry's
// WKUIDelegate implements no JS dialog panels, so inside the desktop app the
// native dialog never appears and every failure reported through it was
// completely invisible -- the action just seemed to do nothing. Error toasts
// persist until dismissed and can seed a fix chat from `detail`.
function notifyFailed(title: string, detail: string) {
  projectStore.pushErrorToast(title, detail)
}
const workspaceProviderFieldLabel = 'Agent CLI/Runtime'
const workspacesLoaded = ref(false)
const workspacesError = ref('')
const workspacesSaving = ref<string | null>(null)
const workspacesResult = ref('')
const showNewWorkspace = ref(false)
const workspaceModels = ref<ModelsResponse | null>(null)

type WorkspaceForm = {
  name: string
  vault_root: string
  default_provider: WorkspaceProvider
  default_model: string
  gws_profile: string
  disallowed_tools: string
  color: WorkspaceColorId
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
    disallowed_tools: '',
    color: DEFAULT_WORKSPACE_COLOR,
  }
}

function normalizeWorkspaceProvider(value: unknown): WorkspaceProvider {
  // A stored provider outside the current registry (for example the removed
  // pre-refactor "ollama") would render a native select with no selected
  // option. Keep the form on a registry value so it can be saved cleanly.
  const storedProvider = typeof value === 'string' ? value.trim() : ''
  return projectStore.workspaceProviderOptions.some((option) => option.value === storedProvider)
    ? storedProvider
    : defaultWorkspaceProvider()
}

function workspaceToForm(ws: WorkspaceInfo): WorkspaceForm {
  return {
    name: ws.name,
    vault_root: ws.vault_root || '',
    default_provider: normalizeWorkspaceProvider(ws.default_provider),
    default_model: ws.default_model || '',
    gws_profile: ws.gws_profile || '',
    disallowed_tools: Array.isArray(ws.disallowed_tools) ? ws.disallowed_tools.join(', ') : '',
    color: normalizeWorkspaceColor(ws.color),
  }
}

function workspaceModelSectionsForProvider(provider: WorkspaceProvider, currentModelValue: string): ModelSection[] {
  if (provider.startsWith('custom:')) {
    const section = sectionsFromModelsResponse(workspaceModels.value)
      .find((item) => item.key === provider)
    if (!section) return []
    const models = [...section.models]
    if (currentModelValue && !models.includes(currentModelValue)) models.push(currentModelValue)
    return [{ ...section, models }]
  }
  if (provider === 'opencode') {
    const section = sectionsFromModelsResponse(workspaceModels.value).find((item) => item.key === provider)
    if (!section) return []
    const models = [...section.models]
    const current = currentModelValue.trim()
    if (current && !models.includes(current)) models.push(current)
    return [{ ...section, models }]
  }
  // Claude's models are the tier aliases plus any configured concrete ids.
  const section = sectionsFromModelsResponse(workspaceModels.value)
    .find((item) => item.key === 'anthropic')
  const models = section ? [...section.models] : []
  const current = (currentModelValue || '').trim()
  if (current && !models.includes(current)) models.push(current)
  return [{
    key: provider,
    label: aliasProviderLabel(provider as AliasProviderKey),
    models,
  }]
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

function formatConnectorLabel(name: string): string {
  let clean = name.replace(/^mcp__claude_ai_/, '').replace(/^mcp__/, '')
  if (clean === 'Google_Cloud_BigQuery') return 'BigQuery'
  if (clean === 'incident_io') return 'incident.io'
  return clean
}



const inspectorEmbeddedTools = computed(() => {
  if (mcpStatus.value?.tools && mcpStatus.value.tools.length) {
    return mcpStatus.value.tools
  }
  return [
    'context_get', 'vault_search', 'projects_list', 'project_get', 'project',
    'chats_list', 'chat_get', 'chat_create', 'chat_send',
    'chat_continue', 'chat_retry', 'chat_handover', 'chat_archive', 'chat_delete',
    'schedules_list', 'schedule', 'schedule_action',
    'loops_list', 'loop', 'loop_action', 'file_surface',
    'project_action',
  ]
})

// Platform MCP list only — exclude Ciaobot project servers from .mcp.json
// (n8n_mcp, notion, ciaobot), which have their own MCP status section.
const EXCLUDED_PLATFORM_MCPS = new Set(['n8n_mcp', 'notion', 'ciaobot', 'ciaobot-fastmcp'])

/**
 * Platform MCP servers a provider's CLI reports.
 *
 * Keyed by provider id rather than written per provider, so a provider added
 * to the backend registry gets a correct card without another branch here.
 * Reports only what the CLI actually discovered — an empty result means
 * "none reported", not a guess, since a placeholder here would be
 * indistinguishable from a real connection.
 */
function connectionMcps(providerId: string): string[] {
  const discovered = providerKeys.value?.connections?.[providerId]?.mcps || []
  const result: string[] = []
  for (const mcpName of discovered) {
    if (EXCLUDED_PLATFORM_MCPS.has(mcpName)) continue
    const label = formatConnectorLabel(mcpName)
    if (!result.includes(label)) result.push(label)
  }
  return result
}


async function fetchWorkspacesList() {
  workspacesError.value = ''
  try {
    await projectStore.fetchWorkspaces()
    workspaceForms.value = projectStore.workspaces.map(workspaceToForm)
    newWorkspaceForm.value.default_provider = normalizeWorkspaceProvider(newWorkspaceForm.value.default_provider)
  } catch (e) {
    workspacesError.value = `Failed to load workspaces: ${errorMessage(e)}`
  } finally {
    workspacesLoaded.value = true
  }
}

// `force` bypasses the provider catalog caches, which costs a provider process
// spawn. Opening Settings is exactly the moment a user has just connected a
// provider elsewhere, so the tab does refresh -- but it renders from cache first
// and lets the forced fetch land after, rather than holding the page on it.
async function fetchWorkspaceModels(force = false) {
  try {
    workspaceModels.value = await api.get<ModelsResponse>(
      force ? '/api/models?refresh=1' : '/api/models',
    )
  } catch {
    workspaceModels.value = null
  }
}

async function saveWorkspace(name: string) {
  const form = workspaceForms.value.find((f) => f.name === name)
  if (!form) return
  workspacesSaving.value = name
  workspacesResult.value = ''
  try {
    await projectStore.updateWorkspace(name, {
      // Send what the form holds. Overwriting it with the workspace name
      // silently discarded any vault path the user had set, and reset an
      // adopted workspace's `memory-vault/<name>` root to a bare name.
      vault_root: form.vault_root.trim() || name,
      default_provider: form.default_provider,
      default_model: form.default_model,
      gws_profile: form.gws_profile,
      disallowed_tools: disallowedToolsPayload(form.disallowed_tools),
      color: form.color,
    })
    notifySaved(`Workspace "${name}" saved.`, 'Workspaces')
    await fetchWorkspacesList()
  } catch (e) {
    const detail = apiErrorMessage(e, 'The workspace could not be saved.')
    workspacesResult.value = `Error: ${detail}`
    notifyFailed(`Workspace "${name}" not saved`, detail)
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
      // The "Vault name" field is optional and defaults to the workspace name.
      vault_root: form.vault_root.trim() || form.name.trim(),
      default_provider: form.default_provider,
      default_model: form.default_model,
      gws_profile: form.gws_profile,
      disallowed_tools: disallowedToolsPayload(form.disallowed_tools),
      color: form.color,
    })
    notifySaved(`Workspace "${form.name.trim()}" created.`, 'Workspaces')
    showNewWorkspace.value = false
    newWorkspaceForm.value = blankWorkspaceForm()
    await fetchWorkspacesList()
  } catch (e) {
    const detail = apiErrorMessage(e, 'The workspace could not be created.')
    workspacesResult.value = `Error: ${detail}`
    notifyFailed(`Workspace "${form.name.trim()}" not created`, detail)
  } finally {
    workspacesSaving.value = null
  }
}

async function removeWorkspace(name: string) {
  if (!await askConfirm(`Delete workspace "${name}"? Chats keep their history but lose workspace routing.`, {
    title: 'Delete workspace',
    confirmLabel: 'Delete workspace',
    destructive: true,
  })) return
  workspacesSaving.value = name
  workspacesResult.value = ''
  try {
    await projectStore.deleteWorkspace(name)
    notifySaved(`Workspace "${name}" deleted.`, 'Workspaces')
    await fetchWorkspacesList()
  } catch (e) {
    workspacesResult.value = `Error: ${apiErrorMessage(e, 'The workspace could not be deleted.')}`
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
  fetchNodeStatus()
  fetchAuthSettings()
  fetchRoutines()
  fetchAutomation()
  fetchPackageStatus()
  fetchProviderKeys()
  fetchMcpStatus()
  fetchMcpUsage()
  // Render from cache immediately, then pick up anything connected elsewhere.
  fetchWorkspaceModels().then(() => fetchWorkspaceModels(true))
  fetchGwsIntegration()
  fetchWorkspacesList()
})


async function doSnapshot(confirmWarnings = false) {
  actionPending.value = 'snapshot'
  actionResult.value = ''
  deploySteps.value = []
  try {
    const r = await api.post<{ message: string }>('/api/admin/snapshot', { confirm_warnings: confirmWarnings })
    actionResult.value = r.message
  } catch (e) {
    const blockers = errorPayloadList(e, 'blockers')
    const warnings = errorPayloadList(e, 'warnings')
    if (blockers) {
      notifyFailed('Snapshot blocked by secrets', blockers.join('\n'))
      actionResult.value = 'Blocked by secrets.'
    } else if (warnings) {
      if (await askConfirm(`Warnings found:\n\n${warnings.join('\n')}\n\nDo you want to proceed anyway?`, {
        title: 'Snapshot warnings',
        confirmLabel: 'Proceed anyway',
      })) {
        actionPending.value = null
        return doSnapshot(true)
      }
      actionResult.value = 'Cancelled by user due to warnings.'
    } else {
      actionResult.value = `Error: ${errorMessage(e)}`
    }
  }
  actionPending.value = null
}

// Show the full-screen restart overlay (App.vue), then wait for the server to
// come back before reloading. Used by any action that triggers a server
// restart (model installs, provider key changes, deploy) so the UI never
// lands on a half-booted server and shows a "Failed to fetch" error.
function restartAndReload(message: string) {
  projectStore.beginServerRestart(message)
}

async function doDeploy(confirmWarnings = false) {
  // In dev mode the restart also rebuilds the Tauri shell when desktop/ changed,
  // which is a multi-minute Rust build that ends by quitting and relaunching the
  // app. Worth warning about before the window disappears.
  const devNote = localStatus.value?.dev_mode
    ? '\n\nDev mode: if desktop/ changed, this also rebuilds the desktop app (several minutes) and relaunches it.'
    : ''
  if (!confirmWarnings && !await askConfirm(`Restart? This will pull latest, rebuild, and restart.${devNote}`, {
    title: 'Restart and redeploy',
    confirmLabel: 'Restart',
  })) return
  actionPending.value = 'deploy'
  actionResult.value = ''
  deploySteps.value = []
  try {
    const r = await api.post<DeployResult>('/api/admin/deploy', { confirm_warnings: confirmWarnings })
    deploySteps.value = r.steps
    if (r.ok) {
      actionResult.value = 'Restart complete. Waiting for server to come back, then reloading...'
      projectStore.beginServerRestart('Deploy complete. Restarting Ciaobot…')
    } else {
      actionResult.value = 'Restart failed. See steps below.'
    }
  } catch (e) {
    const payload = errorPayload(e)
    const blockers = errorPayloadList(e, 'blockers')
    const warnings = errorPayloadList(e, 'warnings')
    if (Array.isArray(payload?.steps)) deploySteps.value = payload.steps
    if (blockers) {
      notifyFailed('Restart blocked by secrets', blockers.join('\n'))
      actionResult.value = 'Blocked by secrets.'
    } else if (warnings) {
      if (await askConfirm(`Warnings found:\n\n${warnings.join('\n')}\n\nDo you want to proceed anyway?`, {
        title: 'Deploy warnings',
        confirmLabel: 'Proceed anyway',
      })) {
        actionPending.value = null
        return doDeploy(true)
      }
      actionResult.value = 'Cancelled by user due to warnings.'
    } else if (deploySteps.value.some(s => !s.ok)) {
      // The failed-step cards below already show the step name and its full
      // output. Repeating the server's error string here rendered the same
      // failure twice: once as an unstyled truncated wall of red text, once in
      // the readable card. Keep the headline, drop the duplicate.
      actionResult.value = 'Restart failed. See steps below.'
    } else {
      actionResult.value = `Error: ${errorMessage(e, 'unknown error')}`
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
    } catch (e) {
      notifyFailed('Could not create project', errorMessage(e))
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
  } catch (e) {
    notifyFailed('Could not start chat', errorMessage(e))
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
  } catch (e) {
    notifyFailed('Could not start the issue-triage chat', errorMessage(e))
  } finally {
    debugPending.value = false
  }
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

// ── PWA password (Settings → home) ─────────────────────────────────────
interface AuthSettings {
  auth_required: boolean
  password_configured: boolean
}

const authSettings = ref<AuthSettings | null>(null)
const authCurrentPassword = ref('')
const authNewPassword = ref('')
const authSettingsSaving = ref(false)
const authSettingsResult = ref('')
const authSettingsError = ref(false)

// Protection is the default and cannot be switched off from here (the server
// rejects `auth_required: false`), so this card only changes the password.
const canSaveAuthSettings = computed(() => {
  if (!authSettings.value) return false
  if (!authNewPassword.value.trim()) return false
  if (authSettings.value.auth_required && !authCurrentPassword.value) return false
  return true
})

async function fetchAuthSettings() {
  try {
    authSettings.value = await api.get<AuthSettings>('/api/auth/settings')
  } catch {
    authSettings.value = null
  }
}

async function saveAuthSettings() {
  if (!authSettings.value || !canSaveAuthSettings.value) return
  authSettingsSaving.value = true
  authSettingsResult.value = ''
  authSettingsError.value = false
  try {
    const res = await api.post<AuthSettings & { ok?: boolean }>('/api/auth/settings', {
      password: authNewPassword.value,
      current_password: authCurrentPassword.value,
    })
    authSettings.value = {
      auth_required: res.auth_required,
      password_configured: res.password_configured,
    }
    authCurrentPassword.value = ''
    authNewPassword.value = ''
    authSettingsResult.value = 'Password saved. Other devices have to log in again.'
  } catch (e) {
    authSettingsError.value = true
    authSettingsResult.value = apiErrorMessage(e, 'Could not save password settings')
  }
  authSettingsSaving.value = false
}

// ── Host / client: labeling only ──────────────────────────────────────────
// The device-scoped controls live in DeviceView (/device). What is left here is
// just enough to answer "whose settings am I editing": in client mode every
// other card on this page is served by the host through the tunnel.
const nodeStatus = ref<NodeStatus | null>(null)
// Tri-state signal for gwsOnEngineHost (issue #351): nodeStatus reading null
// is ambiguous between "still loading" and "the fetch failed", and both must
// not be silently treated as a confirmed host role off a loopback hostname.
const nodeStatusLoaded = ref(false)
const nodeStatusError = ref(false)

const isNodeClient = computed(() => {
  const role = nodeStatus.value?.role
  return role === 'client' || role === 'standby'
})

const connectedHostUrl = computed(
  () => nodeStatus.value?.host_url || nodeStatus.value?.active_peer_url || '',
)

const hostScopeLabel = computed(() => {
  const named = nodeStatus.value?.host_node_id
  const url = connectedHostUrl.value
  if (named && url) return `${named} (${url})`
  return named || url || 'the host'
})

async function fetchNodeStatus() {
  try {
    nodeStatus.value = await api.get<NodeStatus>('/api/node/status')
    nodeStatusError.value = false
  } catch {
    /* leave null on failure: cards then read as host-mode, which is the default */
    nodeStatusError.value = true
  } finally {
    nodeStatusLoaded.value = true
  }
}

async function localHandback(confirmWarnings = false) {
  if (!confirmWarnings && !await askConfirm('Sync changes with the remote repository?', {
    title: 'Sync with remote',
    confirmLabel: 'Sync with remote',
  })) return

  actionPending.value = 'snapshot'
  actionResult.value = ''

  try {
    const r = await api.post<LocalHandbackResult>('/api/local/handback', { confirm_warnings: confirmWarnings })
    if (r?.ok === false) {
      actionResult.value = `${r.step}: ${r.error}`
    } else if (r?.merged === true) {
      actionResult.value = 'Synced with remote repository.'
    } else if (r?.conflict === true) {
      actionResult.value = 'Sync conflict — opened a chat to resolve it. Answer it, then Sync again.'
    }
    await fetchLocalStatus()
  } catch (e) {
    const blockers = errorPayloadList(e, 'blockers')
    const warnings = errorPayloadList(e, 'warnings')
    if (blockers) {
      notifyFailed('Sync blocked by secrets', blockers.join('\n'))
      actionResult.value = 'Blocked by secrets.'
    } else if (warnings) {
      if (await askConfirm(`Warnings found:\n\n${warnings.join('\n')}\n\nDo you want to proceed anyway?`, {
        title: 'Sync warnings',
        confirmLabel: 'Proceed anyway',
      })) {
        actionPending.value = null
        return localHandback(true)
      }
      actionResult.value = 'Cancelled by user due to warnings.'
    } else {
      actionResult.value = `Error: ${errorMessage(e, 'sync failed')}`
    }
  }
  actionPending.value = null
}

// ── Package update ────────────────────────────────────────────────────────
const packageStatus = ref<PackageStatus | null>(null)
const packageLoading = ref(false)
const packageUpdating = ref(false)
const packageResult = ref('')
const showUpdatePanel = ref(false)
const changelogLoading = ref(false)
const changelog = ref<PackageChangelog>({ commits: [], compare_url: '', error: '' })

async function fetchPackageStatus() {
  packageLoading.value = true
  try {
    packageStatus.value = await api.get<PackageStatus>('/api/package/status')
  } catch {
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
    changelog.value = await api.get<PackageChangelog>('/api/package/changelog')
  } catch (e) {
    changelog.value = { commits: [], compare_url: '', error: errorMessage(e, 'unknown error') }
  } finally {
    changelogLoading.value = false
  }
}

async function doPackageUpdate() {
  packageUpdating.value = true
  packageResult.value = 'Updating Ciaobot and restarting...'
  try {
    const res = await api.post<PackageUpdateResult>('/api/package/update')
    if (res.ok) {
      showUpdatePanel.value = false
      packageResult.value = ''
      await restartAndReload('Update complete. Restarting Ciaobot with the latest version…')
    } else {
      packageResult.value = `Update failed: ${res.error || 'unknown error'}`
      await fetchPackageStatus()
    }
  } catch (e) {
    packageResult.value = `Update failed: ${errorMessage(e, 'unknown error')}`
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

.shortcut-list {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.shortcut-list li {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  color: var(--fg2);
}

.shortcut-list kbd {
  font-family: var(--font);
  font-size: 12px;
  min-width: 44px;
  text-align: center;
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg);
  flex: 0 0 auto;
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
/* The inline device panel renders its own .card tiles; give the wrapper the
   same width as every other card here so they line up with the rest. */
.device-tile {
  width: min(100%, 1040px);
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
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
/* No divider when the header is the only element in the card (nothing below
   it to separate). v-if="false" siblings render as comment nodes, which
   :last-child ignores, so this also covers cards whose body is conditional. */
.settings-card-header:last-child {
  padding-bottom: 0;
  border-bottom: none;
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

.mm-loading-heading {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--fg2);
  font-size: var(--text-sm);
  margin-bottom: var(--space-3);
}
.mm-shimmer-line {
  display: block;
  height: 10px;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--bg2) 0%, var(--bg3) 50%, var(--bg2) 100%);
  background-size: 200% 100%;
  animation: title-shimmer-sweep 1.4s ease-in-out infinite;
}
.mm-skeleton-block { margin-top: var(--space-3); }
.history-loading-spinner {
  width: 9px;
  height: 9px;
  flex: 0 0 auto;
  border: 2px solid color-mix(in srgb, var(--accent) 28%, transparent);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: history-loading-spin 0.8s linear infinite;
}
@keyframes history-loading-spin { to { transform: rotate(360deg); } }
@keyframes title-shimmer-sweep {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}
@media (prefers-reduced-motion: reduce) {
  .mm-shimmer-line { animation: title-shimmer-pulse 1.8s ease-in-out infinite; }
  @keyframes title-shimmer-pulse {
    0%, 100% { opacity: 0.6; }
    50% { opacity: 1; }
  }
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
.action-row--compact > button {
  flex: 0 1 auto;
  min-width: 190px;
}
.btn-secondary,
.btn-caution {
  min-height: var(--touch);
  padding: 10px 20px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg3);
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  font-size: calc(14px * var(--font-scale));
  font-weight: 600;
  transition: background 120ms var(--ease), border-color 120ms var(--ease), transform 120ms var(--ease);
}
.btn-secondary:hover { background: var(--border-strong); }
.btn-caution {
  color: var(--warning);
  border-color: color-mix(in srgb, var(--warning) 65%, var(--border));
  background: color-mix(in srgb, var(--warning) 8%, var(--bg3));
}
/* When paired with .btn-small, keep the compact size (scoped .btn-caution
   would otherwise outrank the global .btn-small padding). */
.btn-caution.btn-small {
  min-height: 0;
  padding: 6px 12px;
  font-size: var(--text-sm);
  font-weight: 500;
}
.btn-caution:hover { background: color-mix(in srgb, var(--warning) 15%, var(--bg3)); }
.btn-secondary:active,
.btn-caution:active { transform: scale(0.98); }
.btn-secondary:disabled,
.btn-caution:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
.settings-actions {
  justify-content: flex-end;
  margin-top: var(--space-2);
}

/* Router links used as buttons in card headers (e.g. "Open device settings"). */
a.btn-secondary {
  display: inline-flex;
  align-items: center;
  text-decoration: none;
}
/* Client mode: names the machine whose settings the rest of the page edits. */
.scope-card {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
  background: color-mix(in srgb, var(--accent) 6%, var(--bg2));
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

.node-path {
  display: flex;
  align-items: stretch;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-top: var(--space-1);
}
.node-path-endpoint {
  flex: 1 1 140px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
}
.node-path-endpoint--host {
  border-color: color-mix(in srgb, var(--accent) 28%, var(--border));
  background: color-mix(in srgb, var(--accent) 5%, var(--bg));
}
.node-path-label {
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: var(--fg3, var(--fg2));
}
.node-path-value {
  color: var(--fg);
  font-size: var(--text-sm);
  font-family: var(--font);
  overflow-wrap: anywhere;
  word-break: break-word;
}
.node-path-link {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 0 2px;
  min-width: 72px;
}
.node-path-arrow {
  color: var(--accent);
  font-size: calc(18px * var(--font-scale));
  font-weight: 700;
  line-height: 1;
}
@container (max-width: 720px) {
  .node-path {
    flex-direction: column;
    align-items: stretch;
  }
  .node-path-link {
    flex-direction: row;
    justify-content: flex-start;
    min-width: 0;
    padding: 2px 0;
    gap: var(--space-2);
  }
  .node-path .node-path-arrow {
    transform: rotate(90deg);
  }
}

.node-peer-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}
.node-peer-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
}
.node-peer-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.node-peer-form {
  margin-top: 0;
  margin-bottom: 0;
}

.connected-clients-panel {
  margin-top: var(--space-4);
  padding-top: var(--space-3);
  border-top: 1px solid var(--border);
}
.connected-clients-panel .section-title {
  margin-bottom: var(--space-2);
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
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.routine-voice-icon {
  flex: none;
  color: var(--fg2);
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
.workspace-root-path {
  display: block;
  margin-top: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: color-mix(in srgb, var(--bg) 76%, transparent);
  font-size: var(--text-sm);
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
.provider-connections {
  display: flex;
  flex-direction: column;
}
.provider-connection-actions {
  justify-content: flex-start;
  gap: var(--space-2);
}
.provider-connection-detail {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
}
.provider-connection-detail > span + span::before {
  content: '·';
  margin-right: 10px;
  color: var(--fg3);
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
.settings-checkbox-hit {
  width: var(--touch);
  height: var(--touch);
  flex: 0 0 var(--touch);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}
.voice-warning {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  margin-top: var(--space-3);
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
  width: 100%;
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
.gws-profile-header-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
}
.gws-account-add {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) 0;
  border-bottom: 1px dashed var(--border);
  margin-bottom: var(--space-3);
}
.gws-account-add-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
}
.gws-account-add-row .routine-input {
  flex: 1 1 180px;
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
  .setting-row--inline:not(.setting-row--toggle),
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
  .action-row--compact > button {
    flex: 1 1 100%;
    width: 100%;
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
.provider-defaults {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--space-3);
}
.provider-defaults-row {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: color-mix(in srgb, var(--bg) 76%, transparent);
}
.provider-defaults-title {
  font-weight: 600;
  color: var(--fg);
}
.provider-inline-defaults {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 4px);
  background: color-mix(in srgb, var(--bg) 76%, transparent);
  margin-bottom: var(--space-3);
}
@media (max-width: 720px) {
  .provider-inline-defaults { grid-template-columns: 1fr; }
}
@media (max-width: 720px) {
  .provider-defaults {
    grid-template-columns: 1fr;
  }
}
.settings-field--wide {
  grid-column: 1 / -1;
}
.workspace-color-swatches {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
}
.workspace-color-swatch {
  width: var(--touch);
  height: var(--touch);
  padding: 0;
  border: 2px solid var(--border);
  border-radius: 50%;
  background:
    radial-gradient(circle at center, var(--swatch) 0 58%, transparent 60%),
    var(--bg);
  cursor: pointer;
  transition: border-color 120ms var(--ease), transform 120ms var(--ease);
}
.workspace-color-swatch:hover:not(:disabled) {
  border-color: var(--border-strong);
  transform: translateY(-1px);
}
.workspace-color-swatch.active {
  border-color: var(--swatch);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--swatch) 35%, transparent);
}
.workspace-color-swatch:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.workspace-color-swatch:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.settings-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.settings-field > .ws-label,
.settings-label-row {
  min-height: 20px;
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
  right: auto;
  width: min(360px, calc(100vw - 48px));
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
.settings-field .routine-input {
  max-width: none;
  min-width: 0;
  width: 100%;
}
.workspace-select {
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  min-height: 36px;
  padding: 7px 36px 7px 12px;
  border-radius: var(--radius);
  background-color: var(--bg-elev);
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M2.5 4.5L6 8l3.5-3.5' fill='none' stroke='%237a7a90' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/></svg>");
  background-repeat: no-repeat;
  background-position: right 12px center;
  background-size: 12px 12px;
  color: var(--fg);
  font: inherit;
  font-size: 14px;
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease);
}
.workspace-select:hover:not(:disabled) {
  background-color: var(--bg3);
}
.workspace-select:focus {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-color: var(--accent);
}
.workspace-select:disabled {
  opacity: 0.6;
  cursor: not-allowed;
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

/* MCP tool usage tab */
.usage-summary {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}
/* When docked into the split card header, sit on the right and drop the
   bottom margin (the header divider already provides the spacing). */
.usage-summary--header {
  margin-bottom: 0;
  gap: var(--space-2);
  flex: 0 0 auto;
  justify-content: flex-end;
}
.usage-stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 88px;
  padding: var(--space-3);
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.usage-stat-value {
  font-size: 1.5rem;
  font-weight: 600;
  color: var(--fg);
  font-variant-numeric: tabular-nums;
}
.usage-stat-value--warn {
  color: var(--warning);
}
.usage-stat-label {
  font-size: var(--text-xs);
  color: var(--fg3);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.usage-table-wrap {
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.usage-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}
.usage-th {
  text-align: left;
  padding: var(--space-2) var(--space-3);
  color: var(--fg2);
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
  position: sticky;
  top: 0;
}
.usage-th--num {
  text-align: right;
}
.usage-th--active {
  color: var(--fg);
}
.usage-th:hover {
  color: var(--fg);
}
.usage-sort {
  margin-left: 4px;
  font-size: var(--text-xs);
}
.usage-td {
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--border);
  color: var(--fg);
  font-variant-numeric: tabular-nums;
}
.usage-td--tool {
  font-family: var(--font-mono, ui-monospace, monospace);
  color: var(--fg);
  white-space: nowrap;
}
.usage-td--num {
  text-align: right;
}
.usage-td--warn {
  color: var(--warning);
  font-weight: 600;
}
.usage-td--providers {
  color: var(--fg2);
  font-size: var(--text-xs);
}
.usage-row--idle .usage-td {
  color: var(--fg3);
}
.usage-row--idle .usage-td--tool {
  color: var(--fg3);
}
.usage-table tbody tr:last-child .usage-td {
  border-bottom: none;
}
.usage-table tbody tr:hover .usage-td {
  background: var(--bg2);
}

/* Provider & Workspace MCP Connectors Bar */
.provider-mcps-preview {
  margin-top: var(--space-3);
  margin-bottom: var(--space-3);
  padding: var(--space-3);
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.workspace-connectors-preview {
  grid-column: 1 / -1;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--space-3);
  margin-top: var(--space-2);
}

.workspace-connectors-preview--disabled {
  background: var(--bg);
}

.ws-connectors-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-2);
}

.workspace-connector-pills {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.connector-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 12px;
  border: 1px solid var(--border);
  font-weight: 500;
}

.connector-pill--enabled {
  background: var(--bg);
  color: var(--fg);
  border-color: rgba(46, 160, 67, 0.4);
}

.connector-pill--enabled .pill-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: #2ea44f;
}

.connector-pill--blocked {
  background: var(--bg);
  color: var(--fg3);
  opacity: 0.6;
  text-decoration: line-through;
}

.connector-pill--blocked .pill-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: var(--fg3);
}

/* MCP Inspector */
.mcp-inspector-bar {
  display: flex;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}

.mcp-inspector-field {
  flex: 1;
  max-width: 250px;
}

.mcp-inspector-panels {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-4);
}

@media (max-width: 768px) {
  .mcp-inspector-panels {
    grid-template-columns: 1fr;
  }
}

.mcp-inspector-panel {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--space-3);
}

.panel-header {
  margin-bottom: var(--space-3);
}

.panel-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 2px;
}

.panel-title {
  font-weight: 600;
  font-size: var(--text-sm);
}

.mcp-env-block,
.mcp-tools-block {
  margin-top: 10px;
}

.mcp-env-row {
  margin-top: 8px;
}

.mcp-edit-grid {
  margin-top: 8px;
}

.mcp-tag-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.mcp-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  padding: 3px 8px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  font-family: var(--font-mono, monospace);
}

.mcp-tag--embedded {
  background: var(--bg);
  color: var(--fg);
}

.mcp-tag--active {
  background: rgba(46, 160, 67, 0.1);
  color: var(--fg);
  border-color: rgba(46, 160, 67, 0.4);
}

.mcp-tag--active .pill-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: #2ea44f;
}

.mcp-tag--blocked {
  background: var(--bg);
  color: var(--fg3);
  opacity: 0.5;
  text-decoration: line-through;
}

.badge--compact {
  font-size: 10px;
  padding: 1px 6px;
}

.mcp-server-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--space-3);
  margin-top: var(--space-2);
}

.mcp-server-card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--space-3);
}

.mcp-server-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-2);
}

.mcp-server-name {
  font-weight: 600;
  font-size: var(--text-sm);
  margin-right: var(--space-2);
}

.mcp-tag-grid--wide {
  gap: 8px;
}

</style>
