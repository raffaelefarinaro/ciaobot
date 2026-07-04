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
            <button class="btn-primary" @click="() => doDeploy()" :disabled="!!actionPending" title="Pull latest, reinstall deps, rebuild the frontend, and restart with the latest code">
              {{ actionPending === 'deploy' ? 'Restarting...' : 'Restart' }}
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
              <div v-if="packageStatus.error" class="hint hint--warn hint--spaced">
                Update check failed: {{ packageStatus.error }}
              </div>
            </div>

            <div v-if="packageStatus.update_available" class="action-row action-row--spaced">
              <button class="btn-primary" @click="doPackageUpdate" :disabled="packageUpdating">
                {{ packageUpdating ? 'Upgrading...' : 'Update Package' }}
              </button>
            </div>
            <p v-else class="hint hint--spaced">
              Ciaobot is up to date.
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
            <button class="btn-small font-reset" @click="resetFontScale" :disabled="fontScale === 1.0">Reset</button>
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
              <span v-if="localStatus?.dirty" class="hint inline-hint">(uncommitted changes)</span>
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
          <div v-if="localResult" class="action-result action-result--prewrap">{{ localResult }}</div>
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
            <div v-if="routines.workspace_context" class="routine-context">
              <div>
                <span class="dev-label">Main workspace</span>
                <code>{{ routines.workspace_context.workspace_root }}</code>
              </div>
              <div>
                <span class="dev-label">Vault root</span>
                <code>{{ routines.workspace_context.vault_root }}</code>
              </div>
              <p class="hint hint--compact">
                Change the main workspace by starting Ciaobot with a different <code>CIAO_WORKSPACE</code> and restarting.
                Settings &rarr; Workspaces are logical chat spaces; they do not move the server workspace root.
              </p>
            </div>

            <div class="routine-row">
              <div class="routine-info">
                <span class="routine-name">Chat titles</span>
                <span class="routine-detail">Names a new chat after the first message; runs from the main workspace.</span>
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
              <div class="routine-model-controls">
                <select
                  class="routine-select routine-select--provider"
                  :value="routineProviderValue('title_model')"
                  :disabled="routinesSaving"
                  @change="saveRoutineProvider('title_model', ($event.target as HTMLSelectElement).value)"
                >
                  <option value="automatic">Automatic</option>
                  <option value="apple">Apple Intelligence</option>
                  <option v-for="provider in aliasProviderSections" :key="provider.key" :value="provider.key">
                    {{ provider.label }}
                  </option>
                  <option v-if="routineProviderValue('title_model') === 'custom'" value="custom">Custom model</option>
                </select>
                <select
                  class="routine-select routine-select--tier"
                  :value="routineTierValue('title_model')"
                  :disabled="routinesSaving || !routineTierSelectable('title_model')"
                  @change="saveRoutineTier('title_model', ($event.target as HTMLSelectElement).value)"
                >
                  <option v-for="tier in modelTiers" :key="`title-${tier.key}`" :value="tier.key">
                    {{ tier.label }}
                  </option>
                </select>
                <span class="routine-model-hint">{{ routineModelSummary('title_model') }}</span>
              </div>
            </div>

            <div class="routine-row">
              <div class="routine-info">
                <span class="routine-name">Session insights</span>
                <span class="routine-detail">Extracts learnings when a chat is archived; follows that chat's logical workspace.</span>
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
                <span class="routine-name">Skill evolution</span>
                <span class="routine-detail">Weekly main-workspace pass; proposals go under personal/Workspace/Skill-Proposals. Run model is set at the schedule level.</span>
                <div v-if="getJobTelemetry('skill_evolution')" class="routine-telemetry">
                  <span class="badge" :class="getJobBadgeClass('skill_evolution')">
                    {{ getJobStatus('skill_evolution') }}
                  </span>
                  <span v-if="hasJobLastRun('skill_evolution')" class="telemetry-meta">
                    Last run: {{ getJobLastRunLabel('skill_evolution') }} ({{ getJobDuration('skill_evolution') }})
                  </span>
                  <span v-if="getJobStatus('skill_evolution') === 'error' && getJobLastError('skill_evolution')" class="telemetry-error" :title="getJobLastError('skill_evolution')">
                    &middot; {{ getJobLastError('skill_evolution') }}
                  </span>
                </div>
              </div>
            </div>

            <div class="routine-row routine-row--top">
              <div class="routine-info">
                <span class="routine-name">Critique models</span>
                <span class="routine-detail">Select one or more models for adversarial review.</span>
              </div>
              <div class="critique-model-picker">
                <div class="critique-picker-header">
                  <span class="critique-picker-summary">
                    {{ selectedCritiqueModels.length ? `${selectedCritiqueModels.length} selected` : 'Automatic default' }}
                  </span>
                  <button
                    type="button"
                    class="btn-small"
                    :disabled="routinesSaving || selectedCritiqueModels.length === 0"
                    @click="setCritiqueModels([])"
                  >
                    Reset
                  </button>
                </div>
                <div
                  v-if="selectedCritiqueModels.length"
                  class="critique-chip-list"
                  aria-label="Selected critique models"
                >
                  <button
                    v-for="model in selectedCritiqueModels"
                    :key="model"
                    type="button"
                    class="critique-chip"
                    :disabled="routinesSaving"
                    :title="`Remove ${model}`"
                    @click="toggleCritiqueModel(model, false)"
                  >
                    <span>{{ model }}</span>
                    <span aria-hidden="true">&times;</span>
                  </button>
                </div>
                <div class="critique-option-groups" role="group" aria-label="Critique model choices">
                  <div v-for="group in critiqueModelGroups" :key="group.key" class="critique-option-group">
                    <div class="critique-group-label">{{ group.label }}</div>
                    <label v-for="model in group.models" :key="model" class="critique-option">
                      <input
                        type="checkbox"
                        :checked="isCritiqueModelSelected(model)"
                        :disabled="routinesSaving"
                        @change="toggleCritiqueModel(model, ($event.target as HTMLInputElement).checked)"
                      />
                      <span>{{ model }}</span>
                    </label>
                  </div>
                  <div v-if="customCritiqueModels.length" class="critique-option-group">
                    <div class="critique-group-label">Saved custom</div>
                    <label v-for="model in customCritiqueModels" :key="model" class="critique-option">
                      <input
                        type="checkbox"
                        checked
                        :disabled="routinesSaving"
                        @change="toggleCritiqueModel(model, ($event.target as HTMLInputElement).checked)"
                      />
                      <span>{{ model }}</span>
                    </label>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- Voice transcription -->
          <div class="card">
            <p class="section-title">Voice transcription</p>
            <div class="routine-row routine-row--flush">
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

          <!-- Provider alias tiers -->
          <div v-if="tierProviderSections.length" class="card">
            <p class="section-title">Provider alias models</p>
            <p class="hint">
              Pick a provider, then set the model behind <code>opus</code>, <code>sonnet</code>, and <code>haiku</code>.
            </p>
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
                  <select
                    v-if="selectedTierProviderSection.configurable"
                    class="routine-input"
                    :value="tierOverrideValue(selectedTierProviderSection.key as TierProviderKey, tier.key)"
                    :disabled="routinesSaving || !selectedTierProviderSection.available"
                    @change="saveTierModel(selectedTierProviderSection.key as TierProviderKey, tier.key, ($event.target as HTMLSelectElement).value)"
                  >
                    <option value="">Default ({{ tierEffectiveValue(selectedTierProviderSection.key as TierProviderKey, tier.key) || 'automatic' }})</option>
                    <option v-for="model in selectedTierProviderSection.options" :key="model" :value="model">{{ model }}</option>
                  </select>
                  <input
                    v-else
                    class="routine-input"
                    :value="tier.key"
                    disabled
                  />
                </label>
              </div>
              <p v-if="!selectedTierProviderSection.configurable" class="hint hint--compact tier-provider-note">
                Claude uses the native tier aliases directly.
              </p>
              <p v-else-if="!selectedTierProviderSection.available" class="hint hint--compact tier-provider-note">
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
          <div class="card"><p class="hint">{{ workspacesError }}</p></div>
        </template>
        <template v-else>
          <div class="card">
            <div class="settings-card-header settings-card-header--split">
              <div>
                <p class="section-title">Workspaces</p>
                <p class="hint">
                  Logical chat spaces that route projects, chats, vault roots, model defaults, and integration profiles.
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
                <label class="settings-field"><span class="ws-label">Vault root</span>
                  <input class="routine-input" v-model="newWorkspaceForm.vault_root" :disabled="workspacesSaving === 'new'" placeholder="(defaults to name)" />
                </label>
                <label class="settings-field"><span class="ws-label">Provider</span>
                  <select class="routine-input" v-model="newWorkspaceForm.default_provider" :disabled="workspacesSaving === 'new'">
                    <option v-for="provider in workspaceProviderOptions" :key="provider.value" :value="provider.value">
                      {{ provider.label }}
                    </option>
                  </select>
                </label>
                <label class="settings-field"><span class="ws-label">Default tier</span>
                  <select class="routine-input" v-model="newWorkspaceForm.default_model" :disabled="workspacesSaving === 'new'">
                    <option value="">Inherit default tier</option>
                    <option v-for="tier in modelTiers" :key="`new-${tier.key}`" :value="tier.key">
                      {{ tier.label }}
                    </option>
                    <option v-if="workspaceCustomDefaultModel(newWorkspaceForm.default_model)" :value="newWorkspaceForm.default_model">
                      Custom: {{ newWorkspaceForm.default_model }}
                    </option>
                  </select>
                </label>
                <label class="settings-field"><span class="ws-label">GWS profile</span>
                  <input class="routine-input" v-model="newWorkspaceForm.gws_profile" :disabled="workspacesSaving === 'new'" placeholder="(none)" />
                </label>
                <label class="settings-field settings-field--wide"><span class="ws-label">Claude.ai MCPs</span>
                  <select class="routine-input" v-model="newWorkspaceForm.claude_ai_mcps" :disabled="workspacesSaving === 'new'">
                    <option value="default">Default (off for personal, on for work)</option>
                    <option value="on">On (connectors allowed)</option>
                    <option value="off">Off (connectors blocked)</option>
                  </select>
                  <p class="hint hint--compact">Toggle the claude.ai connector MCPs: {{ claudeAiMcpsLabel }}.</p>
                </label>
                <details class="settings-advanced settings-field settings-field--wide">
                  <summary>Advanced routing</summary>
                  <label class="settings-field">
                    <span class="ws-label">Model bucket</span>
                    <input class="routine-input" v-model="newWorkspaceForm.model_bucket" :disabled="workspacesSaving === 'new'" placeholder="(automatic from provider)" />
                  </label>
                  <label class="settings-field settings-field--wide"><span class="ws-label">Extra disallowed tools (advanced)</span>
                    <input class="routine-input" v-model="newWorkspaceForm.disallowed_tools" :disabled="workspacesSaving === 'new'" placeholder="comma-separated, e.g. mcp__n8n_mcp" />
                  </label>
                </details>
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
                    <p class="hint hint--compact">{{ form.vault_root || form.name }} vault root</p>
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
                  <label class="settings-field"><span class="ws-label">Vault root</span>
                    <input class="routine-input" v-model="form.vault_root" :disabled="workspacesSaving === form.name" placeholder="(defaults to workspace name)" />
                  </label>
                  <label class="settings-field"><span class="ws-label">Provider</span>
                    <select class="routine-input" v-model="form.default_provider" :disabled="workspacesSaving === form.name">
                      <option v-for="provider in workspaceProviderOptions" :key="provider.value" :value="provider.value">
                        {{ provider.label }}
                      </option>
                    </select>
                  </label>
                  <label class="settings-field"><span class="ws-label">Default tier</span>
                    <select class="routine-input" v-model="form.default_model" :disabled="workspacesSaving === form.name">
                      <option value="">Inherit default tier</option>
                      <option v-for="tier in modelTiers" :key="`${form.name}-${tier.key}`" :value="tier.key">
                        {{ tier.label }}
                      </option>
                      <option v-if="workspaceCustomDefaultModel(form.default_model)" :value="form.default_model">
                        Custom: {{ form.default_model }}
                      </option>
                    </select>
                  </label>
                  <label class="settings-field"><span class="ws-label">GWS profile</span>
                    <input class="routine-input" v-model="form.gws_profile" :disabled="workspacesSaving === form.name" placeholder="(none)" />
                  </label>
                  <label class="settings-field settings-field--wide"><span class="ws-label">Claude.ai MCPs</span>
                    <select class="routine-input" v-model="form.claude_ai_mcps" :disabled="workspacesSaving === form.name">
                      <option value="default">Default (off for personal, on for work)</option>
                      <option value="on">On (connectors allowed)</option>
                      <option value="off">Off (connectors blocked)</option>
                    </select>
                    <p class="hint hint--compact">Toggle the claude.ai connector MCPs: {{ claudeAiMcpsLabel }}.</p>
                  </label>
                  <details class="settings-advanced settings-field settings-field--wide">
                    <summary>Advanced routing</summary>
                    <label class="settings-field">
                      <span class="ws-label">Model bucket</span>
                      <input class="routine-input" v-model="form.model_bucket" :disabled="workspacesSaving === form.name" placeholder="(automatic from provider)" />
                    </label>
                    <label class="settings-field settings-field--wide"><span class="ws-label">Extra disallowed tools (advanced)</span>
                      <input class="routine-input" v-model="form.disallowed_tools" :disabled="workspacesSaving === form.name" placeholder="comma-separated, e.g. mcp__n8n_mcp" />
                    </label>
                  </details>
                </div>
              </div>
            </div>

            <div v-if="workspacesResult" class="action-result">{{ workspacesResult }}</div>
          </div>
        </template>
      </template>

      <!-- SKILLS TAB -->
      <template v-if="currentTab === 'skills'">
        <div class="card">
          <div class="settings-toolbar">
            <p class="section-title">Skills</p>
            <div class="settings-toolbar-actions">
              <button class="btn-small" @click="createSkillViaChat">Add via Chat</button>
              <button class="btn-small" @click="toggleAddGithubSkill">
                {{ showAddGithubSkill ? 'Cancel' : '+ Add from GitHub' }}
              </button>
            </div>
          </div>

          <p class="hint hint--compact skill-scope-note">
            Ciaobot runs on Claude Code, so any plugins and skills you install globally in Claude Code are also loaded and available to Ciaobot. This page only lists the skills managed here, the Ciaobot-specific ones (custom and GitHub/package skills).
          </p>

          <!-- Auto-Update GitHub Skills -->
          <div class="setting-row setting-row--inline">
            <div class="routine-info">
              <span class="routine-name">Auto-Update GitHub Skills</span>
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
            <label class="ws-field"><span class="ws-label">GitHub URL / owner/repo</span>
              <input class="routine-input" v-model="githubSource" :disabled="addingGithubSkill" placeholder="e.g. owner/repo or github URL" />
            </label>
            <label class="ws-field"><span class="ws-label">Skill Name (optional)</span>
              <input class="routine-input" v-model="githubSkillName" :disabled="addingGithubSkill" placeholder="(inferred from URL if omitted)" />
            </label>
            <div class="action-row settings-actions">
              <button class="btn-primary" @click="addGithubSkill" :disabled="addingGithubSkill || !githubSource.trim()">
                {{ addingGithubSkill ? 'Adding...' : 'Add Skill' }}
              </button>
            </div>
            <div v-if="addGithubSkillResult" class="action-result" :class="{ '--error': addGithubSkillError }">{{ addGithubSkillResult }}</div>
          </div>

          <div v-if="!skillsLoaded" class="action-row"><span class="loading">Loading&hellip;</span></div>
          <template v-else-if="skillsError">
            <p class="hint">{{ skillsError }}</p>
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
                      <p v-if="skill.source_type" class="skill-meta"><span class="skill-meta-label">Type</span> {{ skill.source_type }}</p>
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
                      <p v-if="skill.source_type" class="skill-meta"><span class="skill-meta-label">Type</span> {{ skill.source_type }}</p>
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
import type { AutomationProcess, DeployResult, LocalStatus, RoutineSettings, SkillInventory, ProviderConfigSettings, WorkspaceInfo, WorkspaceProvider } from '../lib/types'
import { currentSubscription, disablePush, enablePush, isPushEnabled, pushSupported } from '../lib/push'
import { useAuthStore } from '../stores/auth'
import { useProjectStore } from '../stores/projects'
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

type CritiqueModelGroup = {
  key: string
  label: string
  models: string[]
}

type AliasProviderKey = 'claude' | 'ollama' | 'openrouter'
type TierProviderKey = Exclude<AliasProviderKey, 'claude'>
type TierKey = 'haiku' | 'sonnet' | 'opus'
type RoutineModelKey = 'title_model' | 'insights_model'
type RoutineProviderValue = 'automatic' | 'apple' | 'custom' | AliasProviderKey
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
  insights_model: 'haiku',
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
    routinesResult.value = 'Saved.'
    setTimeout(() => { routinesResult.value = '' }, 2000)
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

const critiqueModelGroups = computed<CritiqueModelGroup[]>(() => {
  const options = routines.value?.model_options
  if (!options) return []
  return [
    { key: 'ollama_local', label: 'Ollama (local, free)', models: options.ollama_local || [] },
    { key: 'ollama_cloud', label: 'Ollama cloud', models: options.ollama_cloud || [] },
    { key: 'openrouter', label: 'OpenRouter', models: options.openrouter || [] },
    { key: 'anthropic', label: 'Anthropic', models: options.anthropic || [] },
  ].filter((group) => group.models.length > 0)
})

const knownCritiqueModels = computed(() => new Set(critiqueModelGroups.value.flatMap((group) => group.models)))

const selectedCritiqueModels = computed(() => parseModelList(routines.value?.critique_models || ''))

const customCritiqueModels = computed(() =>
  selectedCritiqueModels.value.filter((model) => !knownCritiqueModels.value.has(model))
)

function isCritiqueModelSelected(model: string): boolean {
  return selectedCritiqueModels.value.includes(model)
}

async function setCritiqueModels(models: string[]) {
  await saveRoutines({ critique_models: serializeModelList(models) })
}

async function toggleCritiqueModel(model: string, checked: boolean) {
  const current = selectedCritiqueModels.value
  const next = checked
    ? [...current, model]
    : current.filter((selected) => selected !== model)
  await setCritiqueModels(next)
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

async function saveTierModel(provider: TierProviderKey, tier: TierKey, model: string) {
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
  if (provider === 'apple') return 'Apple Intelligence'
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
const autoUpdateGithubSkills = ref(true)
const autoUpdateSaving = ref(false)
const autoUpdateResult = ref('')

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
    autoUpdateResult.value = 'Saved.'
    setTimeout(() => { autoUpdateResult.value = '' }, 2000)
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

const customSkills = computed(() => {
  return skillsInventory.value?.skills.filter(s => s.label === 'custom') || []
})

const githubSkills = computed(() => {
  return skillsInventory.value?.skills.filter(s => s.label === 'github') || []
})

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
      addGithubSkillResult.value = res.message || 'Skill added successfully.'
      githubSource.value = ''
      githubSkillName.value = ''
      await fetchSkills()
      setTimeout(() => {
        showAddGithubSkill.value = false
        addGithubSkillResult.value = ''
      }, 2000)
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
  // 'default' = per-workspace default (personal off, else on); on/off are explicit.
  claude_ai_mcps: 'default' | 'on' | 'off'
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
    claude_ai_mcps: 'default',
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
    claude_ai_mcps: mcps === true ? 'on' : mcps === false ? 'off' : 'default',
  }
}

const claudeAiConnectors = computed(() => projectStore.workspaceClaudeAiConnectors)

// Human-readable label for the claude.ai MCPs switch: lists the connectors the
// toggle controls (or a generic fallback if the payload hasn't loaded).
const claudeAiMcpsLabel = computed(() => {
  const names = claudeAiConnectors.value
  if (!names.length) return 'claude.ai connector MCPs'
  return names.map((n) => n.replace('mcp__claude_ai_', '').replace(/_/g, ' ')).join(', ')
})

function claudeAiMcpsPayload(value: 'default' | 'on' | 'off'): boolean | null {
  if (value === 'on') return true
  if (value === 'off') return false
  return null
}

function workspaceCustomDefaultModel(model: string): boolean {
  const value = model.trim()
  return Boolean(value) && !modelTiers.some((tier) => tier.key === value)
}

const workspaceForms = ref<WorkspaceForm[]>([])
const newWorkspaceForm = ref<WorkspaceForm>(blankWorkspaceForm())

const workspaceProviderOptions = computed(() =>
  projectStore.workspaceProviderOptions.length
    ? projectStore.workspaceProviderOptions
    : [{ value: 'claude' as WorkspaceProvider, label: 'Claude' }]
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
    workspacesResult.value = `Workspace "${name}" saved.`
    await fetchWorkspacesList()
  } catch (e: any) {
    workspacesResult.value = `Error: ${e?.message || e}`
  } finally {
    workspacesSaving.value = null
    setTimeout(() => { if (workspacesResult.value.startsWith('Workspace')) workspacesResult.value = '' }, 3000)
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
    workspacesResult.value = `Workspace "${form.name.trim()}" created.`
    showNewWorkspace.value = false
    newWorkspaceForm.value = blankWorkspaceForm()
    await fetchWorkspacesList()
  } catch (e: any) {
    workspacesResult.value = `Error: ${e?.message || e}`
  } finally {
    workspacesSaving.value = null
    setTimeout(() => { if (workspacesResult.value.startsWith('Workspace')) workspacesResult.value = '' }, 3000)
  }
}

async function removeWorkspace(name: string) {
  if (!window.confirm(`Delete workspace "${name}"? Chats keep their history but lose workspace routing.`)) return
  workspacesSaving.value = name
  workspacesResult.value = ''
  try {
    await projectStore.deleteWorkspace(name)
    workspacesResult.value = `Workspace "${name}" deleted.`
    await fetchWorkspacesList()
  } catch (e: any) {
    workspacesResult.value = `Error: ${e?.message || e}`
  } finally {
    workspacesSaving.value = null
    setTimeout(() => { if (workspacesResult.value.startsWith('Workspace')) workspacesResult.value = '' }, 3000)
  }
}

onMounted(async () => {
  loadAppearanceSettings()
  fetchSkills()
  fetchLocalStatus()
  fetchRoutines()
  fetchAutomation()
  fetchPackageStatus()
  fetchProviderKeys()
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
        if (data.overall_ready && sawDown) {
          location.reload()
          return
        }
      }
    } catch {
      // server is down mid-restart
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
  if (!confirm('Upgrade Ciaobot package and restart?')) return
  packageUpdating.value = true
  packageResult.value = 'Upgrading Ciaobot package...'
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
.action-result--prewrap {
  white-space: pre-wrap;
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
  padding: 14px 0;
  border-top: 1px solid var(--border);
  margin-top: 0;
}
.routine-row--top {
  align-items: flex-start;
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
.routine-value-stub {
  flex-shrink: 0;
  max-width: 46%;
  padding: 6px 8px;
  color: var(--fg2);
  font-size: var(--text-xs);
  font-style: italic;
  min-height: 32px;
  display: flex;
  align-items: center;
}
.routine-select,
.routine-input {
  flex-shrink: 0;
  max-width: 46%;
  min-width: min(360px, 46%);
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
.routine-context code {
  overflow-wrap: anywhere;
}
.routine-model-controls {
  flex: 0 1 46%;
  max-width: 46%;
  min-width: 300px;
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(96px, 0.7fr);
  gap: 8px;
  align-items: start;
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
.settings-checkbox {
  width: 20px;
  height: 20px;
  flex: 0 0 auto;
  cursor: pointer;
  accent-color: var(--accent);
}
.voice-warning {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
.voice-install-btn {
  flex: 0 0 auto;
  padding: 4px 10px;
  font-size: var(--text-xs);
}
.critique-model-picker {
  flex: 0 1 46%;
  max-width: 46%;
  min-width: 280px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.critique-picker-header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.critique-picker-summary {
  flex: 1;
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
@media (max-width: 520px) {
  .pane-body {
    padding: var(--space-3);
  }
  .settings-card-header--split,
  .setting-row--inline,
  .setting-row-main--inline {
    flex-direction: column;
    align-items: stretch;
  }
  .settings-actions > button {
    flex: 1 1 auto;
  }
  .routine-row {
    flex-direction: column;
    align-items: stretch;
  }
  .routine-select,
  .routine-input,
  .routine-value-stub {
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
.settings-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}
.settings-toolbar-actions {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
  justify-content: flex-end;
}
.settings-form-panel {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-2);
  margin-bottom: var(--space-4);
  padding-bottom: var(--space-4);
  border-bottom: 1px solid var(--border);
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
@media (max-width: 720px) {
  .settings-toolbar {
    flex-direction: column;
    align-items: stretch;
  }
  .settings-toolbar-actions {
    justify-content: stretch;
  }
  .settings-toolbar-actions .btn-small {
    flex: 1 1 auto;
  }
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
.skill-link {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.skill-link:hover {
  opacity: 0.85;
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
.ws-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
}
.ws-label {
  font-size: var(--text-sm);
  color: var(--fg2);
}
</style>
