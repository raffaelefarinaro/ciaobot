<template>
  <div class="settings-pane">
    <!-- One overlay at a time. The package-update path owns it while it runs;
         otherwise an in-flight engine update does, driven by its record rather
         than by a timer, so it never claims progress nobody made. A settled
         record is the card's business, not a full-screen takeover.

         Only the applying half of an engine update gets it: that run takes the
         engine down, so the overlay is the whole window and is modal — this is
         its inert boundary, and `useModalFocus` below moves focus into the
         overlay's status region, so Tab can no longer reach Restart or Deploy
         underneath it, then hands focus back to the card once the record
         settles. Staging is the card's own inline progress instead, because
         nothing goes down while it runs. -->
    <UpdateProgressView
      v-if="packageUpdating"
      :version="packageStatus?.latest_version"
    />
    <div
      v-else-if="engineUpdateOverlayOpen"
      ref="engineUpdateOverlay"
      class="engine-update-overlay"
    >
      <UpdateProgressView
        ref="engineUpdateOverlayView"
        :version="engineUpdateVersion"
        :phase="engineUpdateOperation?.phase"
        :error="engineUpdateOperation?.error"
      />
    </div>
    <PaneHeader :page-tag="pageTag" @open-sidebar="emit('open-sidebar')" />
    <div ref="bodyEl" class="pane-body" @scroll.passive="onBodyScroll">
      <div class="page-grid settings-grid">
      <div ref="mainEl" class="page-main settings-main">

      <!-- HOME TAB -->
      <template v-if="currentTab === 'home'">
        <!-- Package update. The first section of General. Up to date with nothing
             in progress it is only its title and a status in the corner; every
             other state is the full card, with its copy and its controls. -->
        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">Updates</p>
              <p v-if="!updatesCompact" class="hint">
                <template v-if="packageStatus?.mode === 'bundled_app'">
                  This app updates by re-running the one-line installer.
                  <code>curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh</code>
                </template>
                <template v-else-if="packageStatus?.mode === 'installer' && engineUpdateEnabled">
                  Installed with the Ciaobot engine installer. Updating downloads and verifies the release
                  while Ciaobot keeps running, then restarts it once.
                </template>
                <template v-else-if="packageStatus?.mode === 'installer'">
                  Installed with the Ciaobot engine installer. To update, run it again:
                  <code>curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install-engine.sh | sh</code>
                </template>
                <template v-else>
                  Check the installed package version and upgrade this local app.
                </template>
              </p>
            </div>
            <div v-if="packageStatus && !['bundled_app', 'installer'].includes(packageStatus.mode ?? '') && packageStatus.update_available" class="settings-card-header-actions">
              <button
                class="btn-primary btn-small"
                @click="openUpdatePanel"
                :disabled="packageUpdating || showUpdatePanel"
              >
                {{ `Update to ${packageStatus.latest_version}` }}
              </button>
            </div>
            <!-- Nothing to do: the status sits in the corner as a badge. -->
            <span v-else-if="updatesCompact" class="badge badge--success">Up to date<template v-if="packageStatus?.current_version"> · {{ packageStatus.current_version }}</template></span>
          </div>
          <SkeletonLoader v-if="packageLoading && !packageStatus" label="Checking package status" :count="2" />
          <div v-else-if="packageStatus">
            <div v-if="packageStatus.error" class="hint hint--warn hint--spaced">
              Update check failed: {{ packageStatus.error }}
            </div>

            <div v-if="showUpdatePanel && !['bundled_app', 'installer'].includes(packageStatus.mode ?? '')" class="settings-form-panel">
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

          <!-- The engine update job (#608). One distinct panel per record state,
               every word of it read off the persisted operation; nothing here
               invents progress. A sibling of the package block because it is
               driven by a different route, and either may fail alone. -->
          <div v-if="engineUpdateVisible" ref="engineUpdatePanel" class="settings-form-panel engine-update-panel" tabindex="-1">
            <p class="section-title">Engine update</p>

            <!-- A coordinator refusal that wrote no record of its own: a release
                 lookup, a lock, an engine already on the target. Nothing will
                 ever arrive on a poll for a run that left no record, so the
                 card says why and offers the action the record allows. Text
                 alone would be a dead end until the page reloaded. A refusal
                 parked against an existing record answers here too: the served
                 reason is always the newer one, because the parked reason is
                 only served while the record it was parked against is
                 unchanged. -->
            <template v-if="updateStatus?.error">
              <p class="hint hint--warn hint--spaced">{{ updateStatus.error }}</p>
              <!-- Parking a reason changes nothing about the record it was
                   parked against, so a rollback is still a rollback and the
                   version it went back to is still the one now running. -->
              <p v-if="engineUpdateRolledBack && engineUpdateOperation?.from_version" class="hint hint--spaced">
                Back on v{{ engineUpdateOperation.from_version }}.
              </p>
              <div class="action-row settings-actions">
                <!-- One action whatever the record is: `/api/update/start` applies
                     a `staged` record itself and stages a terminal or absent one,
                     so the panel never has to pick the half. The parked reason
                     lives on the server, so a reload keeps it; the button is the
                     way out of it. -->
                <button
                  class="btn-primary"
                  @click="doEngineUpdate"
                  :disabled="updateActionPending || updatePolling"
                >
                  {{ updateActionPending ? 'Updating…' : (packageStatus?.update_available || engineUpdateStage === 'staged' ? 'Update & restart' : 'Try again') }}
                </button>
              </div>
            </template>

            <!-- Staging runs while the engine keeps serving, so it is not a
                 takeover: the same progress rows the overlay would carry, in
                 the card, with the rest of the app usable and reachable. The
                 poll keeps running, so this moves on its own. -->
            <UpdateProgressView
              v-else-if="engineUpdateStagingNow"
              ref="engineUpdateInlineView"
              inline
              :version="engineUpdateVersion"
              :phase="engineUpdateOperation?.phase"
              :error="engineUpdateOperation?.error"
            />

            <!-- Applying: the engine is replacing itself and the connection is
                 about to drop, so the full-window overlay above carries the rows
                 and this only names the phase it is in. -->
            <p v-else-if="engineUpdateBusy" class="hint hint--spaced">
              {{ engineUpdatePhaseText }}<template v-if="engineUpdateOperation"> · v{{ engineUpdateOperation.to_version }}</template>
            </p>

            <!-- Staged is not a failure: a release left downloaded and verified
                 (by an older engine, or a run whose apply refused). The same
                 button applies it. -->
            <template v-else-if="engineUpdateStage === 'staged'">
              <p class="hint hint--spaced">
                v{{ engineUpdateOperation?.to_version || packageStatus?.latest_version }} is downloaded and verified.
                Updating restarts Ciaobot.
              </p>
              <div class="action-row settings-actions">
                <button class="btn-primary" @click="doEngineUpdate" :disabled="updateActionPending || updatePolling">
                  {{ updateActionPending ? 'Updating…' : 'Update & restart' }}
                </button>
              </div>
            </template>

            <!-- Applied, and still the newest release: the record stays on disk
                 after a successful apply, so an engine that has updated once
                 would answer every later release with "up to date" and never
                 offer an update again. -->
            <template v-else-if="engineUpdateStage === 'done' && !engineUpdateStaleApplied">
              <p class="hint hint--spaced">ciaobot is up to date.</p>
            </template>

            <template v-else-if="engineUpdateFailed">
              <p class="hint hint--warn hint--spaced">{{ engineUpdateFailureText }}</p>
              <p v-if="engineUpdateRolledBack && engineUpdateOperation?.from_version" class="hint hint--spaced">
                Back on v{{ engineUpdateOperation.from_version }}.
              </p>
              <div class="action-row settings-actions">
                <button class="btn-primary" @click="doEngineUpdate" :disabled="updateActionPending || updatePolling">
                  {{ updateActionPending ? 'Updating…' : (engineUpdateStage === 'rolled_back' ? 'Try again' : 'Retry') }}
                </button>
              </div>
            </template>

            <template v-else-if="packageStatus?.update_available">
              <p class="hint hint--spaced">
                v{{ packageStatus.latest_version }} is available. Ciaobot keeps running while it downloads,
                then restarts once.
              </p>
              <div class="action-row settings-actions">
                <button class="btn-primary" @click="doEngineUpdate" :disabled="updateActionPending || updatePolling">
                  {{ updateActionPending ? 'Updating…' : 'Update & restart' }}
                </button>
              </div>
            </template>
          </div>
          <div v-if="packageResult" class="action-result">{{ packageResult }}</div>
        </div>

        <div class="card">
          <div class="settings-card-header">
            <p class="section-title">What can Ciaobot do?</p>
            <p class="hint">
              Ask Ciaobot “What can you do?” in any chat for an answer you can follow up on.
            </p>
            <a class="settings-guide-link" href="https://www.raffaelefarinaro.com/ciaobot/features.html" target="_blank" rel="noopener noreferrer">Explore the feature guide</a>
            <p class="hint">
              Dictation, keeping this computer awake, a browser for the agent and usage tracking are left to tools you choose.
            </p>
            <a class="settings-guide-link" href="https://www.raffaelefarinaro.com/ciaobot/companions.html" target="_blank" rel="noopener noreferrer">Tools to pair with Ciaobot</a>
          </div>
        </div>

        <!-- Installing is optional and the guidance is permanent: the Home setup
             reminder can be closed for good, so Settings keeps the steps. -->
        <SettingsAppInstall :expand-steps="route.hash === '#install-app'" />

        <!-- Notifications. -->
        <SettingsNotifications />

        <SettingsKeyboardShortcuts />

        <!-- Start at sign-in. It changes whether the engine service starts at
             the *next* sign-in, never the engine running now, and never the
             optional app window SettingsAppInstall owns. -->
        <SettingsEngineLogin />

        <!-- Open source. The face image on the right appears only while the
             GitHub-star ask is still live (housekeeping detects it via the
             `starred` / snoozed receipt): once the user has starred or
             dismissed, the card quiets back to prose. -->
        <div class="card open-source-card" :class="{ 'open-source-card--with-face': showStarNudge }">
          <div class="settings-card-header">
            <p class="section-title">Open source</p>
            <p class="hint">
              Ciaobot is an open-source project. Support and contributions are welcome:
              report issues, suggest features, or open a pull request on
              <a href="https://github.com/raffaelefarinaro/ciaobot" target="_blank" rel="noopener">GitHub</a>.
              You can browse without an account; submitting an issue or pull request requires a free GitHub account, and GitHub will prompt you to sign in or create one.
            </p>
            <div v-if="showStarNudge" class="open-source-actions">
              <button
                class="btn-primary btn-small"
                :disabled="starPending"
                @click="starOnGitHub"
              >{{ starPending ? 'Starring…' : '★ Star on GitHub' }}</button>
              <button class="btn-small" @click="dismissStarNudge">Later</button>
            </div>
          </div>
          <img
            v-if="showStarNudge"
            class="open-source-face"
            src="/face.png"
            alt=""
            draggable="false"
          />
        </div>
      </template>

      <!-- APPEARANCE TAB -->
      <template v-if="currentTab === 'appearance'">
        <!-- Appearance -->
        <div class="card">
          <div class="settings-card-header">
            <p class="section-title">Appearance</p>
            <p class="hint">Control the visual theme, text and type used across Ciaobot.</p>
          </div>
          <div class="setting-row setting-row--inline setting-row--flush">
            <div class="routine-info">
              <span class="routine-name">Theme</span>
              <span class="routine-detail">Choose light, dark, or match the device appearance.</span>
            </div>
            <div class="settings-control">
              <SettingsRadioGroup
                label="Theme"
                :options="THEME_OPTIONS"
                :model-value="activeTheme"
                @update:model-value="setTheme"
              />
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
          <SettingsAppearance />
          <p v-if="appearanceError" class="action-result" role="alert">{{ appearanceError }}</p>
        </div>
      </template>

      <!-- ACCESS & DEVICES TAB -->
      <template v-if="currentTab === 'access'">
        <!-- PWA password -->
        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">PWA password</p>
              <p class="hint">
                Ciaobot is always password-protected — this is the password you type to open it,
                and the one another device needs to sign in.
                <a
                  class="settings-help-link"
                  href="https://www.raffaelefarinaro.com/ciaobot/remote.html#browser"
                  target="_blank"
                  rel="noopener noreferrer"
                >Keeping it private</a>
              </p>
            </div>
          </div>
          <SkeletonLoader v-if="!authSettings" label="Loading access settings" :count="2" />
          <template v-else>
            <div class="settings-form-panel">
              <label v-if="!authSettings.on_host" class="settings-field">
                <span class="ws-label">Current password</span>
                <input
                  id="auth-current-password"
                  v-model="authCurrentPassword"
                  type="password"
                  class="routine-input"
                  autocomplete="current-password"
                  :aria-invalid="authCurrentPasswordError ? 'true' : undefined"
                  :aria-describedby="authCurrentPasswordError ? 'auth-current-password-error' : undefined"
                  :disabled="authSettingsSaving"
                />
                <span v-if="authCurrentPasswordError" id="auth-current-password-error" class="hint hint--warn" role="alert">{{ authCurrentPasswordError }}</span>
              </label>
              <p v-if="authSettings.on_host" class="hint">You're on the computer running Ciaobot, so you don't need the current password here.</p>
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

        <!-- Other devices — where to open Ciaobot from a phone or another
             computer. The one place to set the trusted HTTPS address. -->
        <SettingsDevices />

        <!-- Network access: server settings that used to live in the workspace .env. -->
        <SettingsServer
          section="network"
          :routines="routines"
          :routines-saving="routinesSaving"
          :save-routines="saveRoutines"
        />
      </template>

      <!-- ADVANCED TAB -->
      <template v-if="currentTab === 'advanced'">
        <!-- Actions. This card is about the Ciaobot *program*: the checkout it
             runs from, and whether it is up to date. It shares a repository and
             a serialization with the memory backup below, but never the
             meaning, so the two never share a label: this one is an explicit,
             bidirectional hand-off of the program, the other is an unattended
             one-way copy of the memory. -->
        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">This host</p>
              <p class="hint">
                The Ciaobot program on this computer: send pending changes to its repository, or
                restart to update it. Your memory and notes are backed up separately, below.
              </p>
            </div>
            <div class="settings-card-header-actions">
              <button
                class="btn-secondary btn-small"
                @click="() => localStatus?.git_repo ? localHandback() : doSnapshot()"
                :disabled="!!actionPending"
                :title="localStatus?.git_repo
                  ? 'Send pending changes in the Ciaobot program checkout to its repository, and pull what is there'
                  : 'Save a local snapshot of the Ciaobot program checkout, with no remote configured'"
              >
                {{ actionPending === 'snapshot' ? (localStatus?.git_repo ? 'Syncing...' : 'Snapshotting...') : (localStatus?.git_repo ? 'Sync with Remote' : 'Git Snapshot') }}
              </button>
              <button class="btn-caution btn-small" @click="() => doDeploy()" :disabled="!!actionPending" :title="localStatus?.restart_only ? 'Wait for active chats, then restart the installed server' : 'Pull latest, reinstall deps, rebuild the frontend, and restart with the latest code'">
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

        <!-- Memory backup. The unattended, one-way copy of the user's memory,
             right below the program's own repository controls so the two are
             read together and never confused. The panel owns its data and its
             actions; the only thing it cannot do alone is navigate to a chat,
             so it emits the setup chat's id for this view to open. -->
        <SettingsMemoryBackup @open-chat="openBackupSetupChat" />

        <!-- Ciaobot folder -->
        <div v-if="routines && routines.workspace_context" class="card">
          <div class="settings-card-header">
            <p class="section-title">Ciaobot folder</p>
            <p class="hint">
              The folder Ciaobot runs from: routines, skills, scripts, runtime state and the <code>.env</code> file.
              Chat workspaces (the sidebar switcher) are managed from the workspace switcher: Manage workspaces.
            </p>
          </div>
          <code class="workspace-root-path">{{ routines.workspace_context.workspace_root }}</code>
          <p v-if="workspaceMove?.cloud_sync_warning" class="hint hint--warn workspace-cloud-warning" role="note">
            {{ workspaceMove.cloud_sync_warning }}
          </p>
          <p v-if="workspaceMoveOutcome" class="hint workspace-move-outcome" :class="{ 'workspace-move-outcome--error': workspaceMoveOutcome.error }">
            {{ workspaceMoveOutcome.text }}
          </p>
          <p v-if="workspaceMove?.admin_only" class="hint">
            On Linux the administrator moves it: run
            <code>sudo /opt/ciaobot/venv/bin/ciao workspace-move &lt;new folder&gt; --apply</code>
            on the server.
          </p>
          <div v-else-if="workspaceMove?.local" class="workspace-move-row">
            <button class="btn-small" type="button" @click="workspaceMoveOpen = true">Move&hellip;</button>
          </div>
          <p v-else-if="workspaceMove" class="hint">
            To move it, open Settings on the computer running Ciaobot, or run
            <code>ciao workspace-move &lt;new folder&gt;</code> there.
          </p>
          <WorkspaceMoveDialog
            v-if="workspaceMove?.local && !workspaceMove.admin_only"
            :open="workspaceMoveOpen"
            :workspace-root="workspaceMove.workspace_root"
            @close="workspaceMoveOpen = false"
            @started="onWorkspaceMoveStarted"
          />
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
          <SkeletonLoader v-if="!agentAssetsLoaded" label="Scanning workspace health" :count="3" />
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

        <!-- Developer switches: server settings that used to live in the workspace .env. -->
        <SettingsServer
          section="developer"
          :routines="routines"
          :routines-saving="routinesSaving"
          :save-routines="saveRoutines"
        />

        <!-- Debug (dev mode only) -->
        <div v-if="localStatus?.dev_mode" class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">Debug</p>
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
      </template>

      <!-- PROVIDERS TAB: chat providers. The old models tab was renamed;
           /settings/models redirects here (router.ts). -->
      <template v-if="currentTab === 'providers'">
        <!-- Chat providers -->
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
          <div id="chat-providers" class="card">
            <div class="settings-card-header">
              <div>
                <p class="section-title">Chat providers</p>
                <p class="hint">
                  Each provider CLI manages its own login and credentials. Ciaobot verifies every connection.
                  The defaults below apply to new chats; any chat can override them from the picker.
                  Session insights is the model that reads chats into memory and names them. On Automatic it
                  is the chat's own model: a closed project uses its last-active chat, and a task resolution or
                  a project closed with no chats uses the workspace's default model.
                  <a
                    class="settings-help-link"
                    href="https://www.raffaelefarinaro.com/ciaobot/models.html"
                    target="_blank"
                    rel="noopener noreferrer"
                  >How models and providers work</a>
                </p>
              </div>
            </div>

            <p v-if="workspaceModelsError" class="hint hint--warn" role="alert">{{ workspaceModelsError }}</p>
            <div v-if="providerKeys.connections" class="provider-connections set-list">
              <div v-for="(conn, connKey) in providerKeys.connections" :key="connKey" class="credential-row set-row">
                <div class="set-row-head provider-row-head">
                  <div class="routine-info set-row-main">
                    <span class="routine-name set-row-title">{{ conn.label || connKey }}</span>
                    <p class="set-row-sub provider-connection-detail">
                      <span v-if="conn.version">{{ conn.version }}</span>
                      <span v-if="conn.account">{{ conn.account }}</span>
                      <span v-if="!conn.version && conn.detail">{{ conn.detail }}</span>
                    </p>
                    <p v-if="conn.auth === 'not_installed'" class="set-row-sub">
                      {{ conn.detail }}
                      Install it with <code>{{ conn.command }}</code>
                      <template v-if="conn.path_command">
                        , then put it on your PATH with
                        <code>{{ conn.path_command }}</code>
                      </template>&nbsp;<a
                        v-if="conn.install_url"
                        :href="conn.install_url"
                        target="_blank"
                        rel="noopener noreferrer"
                      >installation guide ↗</a>
                    </p>
                    <p v-if="(conn.auth === 'not_installed' || conn.auth === 'unsupported_version') && conn.terminal_action" class="set-row-sub">
                      <HostTerminalButton
                        :action="conn.terminal_action"
                        variant="small"
                        :aria-label="`Install ${conn.label || connKey} in Terminal`"
                        label="Install in Terminal"
                      />
                    </p>
                    <p
                      v-if="conn.auth !== 'not_installed' && conn.path_command"
                      class="set-row-sub"
                    >
                      Your terminal cannot find this CLI yet — put it on your PATH with
                      <code>{{ conn.path_command }}</code>
                    </p>
                  </div>
                  <div class="set-row-actions provider-connection-actions">
                    <span class="settings-conn-state" :class="conn.ok ? 'settings-conn-state--ok' : 'settings-conn-state--error'">
                      {{ conn.ok ? 'Connected' : 'Not connected' }}
                    </span>
                    <!-- Connect is the row's one primary only while it is needed;
                         once connected, the rare Reconnect / Log out live in the menu. -->
                    <button
                      v-if="!conn.ok"
                      type="button"
                      class="btn-primary btn-small"
                      :disabled="providerConnectionPending === connKey"
                      @click="providerConnectionAction(String(connKey), 'connect')"
                    >Connect</button>
                    <button
                      type="button"
                      class="set-link"
                      :disabled="providerConnectionPending === connKey"
                      @click="providerConnectionAction(String(connKey), 'verify')"
                    >Verify</button>
                    <DropdownMenuRoot
                      v-if="conn.ok"
                      :open="settingsMenu === `provider:${connKey}`"
                      :modal="false"
                      @update:open="setSettingsMenu(`provider:${connKey}`, $event)"
                    >
                      <DropdownMenuTrigger as-child>
                        <button
                          type="button"
                          class="set-menu-btn"
                          :aria-label="`More actions for ${conn.label || connKey}`"
                          :disabled="providerConnectionPending === connKey"
                        >&middot;&middot;&middot;</button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent as-child align="end" :side-offset="4" :collision-padding="8">
                        <div class="settings-menu" @keydown.esc="closeSettingsMenuOnEscape">
                          <DropdownMenuItem as-child @select="providerConnectionAction(String(connKey), 'connect')">
                            <button type="button" class="settings-menu-item">Reconnect</button>
                          </DropdownMenuItem>
                          <DropdownMenuItem as-child @select="providerConnectionAction(String(connKey), 'logout')">
                            <button type="button" class="settings-menu-item settings-menu-item--danger">Log out</button>
                          </DropdownMenuItem>
                        </div>
                      </DropdownMenuContent>
                    </DropdownMenuRoot>
                  </div>
                </div>
                <!-- Full width, after the head: inside .set-row-main it shares a line with Connected / Verify and clips at phone width. -->
                <div v-if="conn.release_check?.state === 'below_tested'" class="set-row-sub provider-release-check">
                  <p class="provider-release-note">Older than {{ conn.release_check.tested }}, the version this Ciaobot release was tested with. Update with:</p>
                  <code class="provider-release-command">{{ conn.release_check.command }}</code>
                  <div class="provider-release-actions">
                    <button
                      type="button"
                      class="btn-small btn-chip"
                      @click="providerCommandCopy.copy(String(connKey), conn.release_check.command || '')"
                    >{{ providerCommandCopy.stateOf(String(connKey)) === 'copied' ? 'Copied' : 'Copy' }}</button>
                    <HostTerminalButton
                      v-if="conn.release_check.terminal_action"
                      :action="conn.release_check.terminal_action"
                      variant="small"
                      label="Update in Terminal"
                      :aria-label="`Update ${conn.label || connKey} in Terminal`"
                      @not-applicable="fetchProviderKeys()"
                    />
                    <span v-if="providerCommandCopy.stateOf(String(connKey)) === 'failed'" class="provider-release-failed" role="status">Copy failed; select the command above.</span>
                  </div>
                </div>
                <p v-else-if="conn.release_check?.state === 'current'" class="set-row-sub">Tested with {{ conn.release_check.tested }}</p>
                <p v-else-if="conn.release_check?.state === 'bundled'" class="set-row-sub">Bundled with Ciaobot; updates with Ciaobot</p>
                <div v-if="getProviderSection(String(connKey))" class="provider-inline-defaults">
                  <div class="set-subrow">
                    <span :id="`default-model-label-${connKey}`" class="ws-label set-subrow-label">Default model</span>
                    <div class="set-subrow-control">
                      <ModelSelector
                        v-if="getProviderSection(String(connKey))?.configurable"
                        :model-value="providerDefaultModelSelectorValue(String(connKey) as AliasProviderKey)"
                        :sections="providerDefaultModelSectionsFor(String(connKey) as AliasProviderKey)"
                        :labelled-by="`default-model-label-${connKey}`"
                        :disabled="routinesSaving || !getProviderSection(String(connKey))?.available"
                        @update:model-value="saveProviderDefaultModel(String(connKey) as AliasProviderKey, $event)"
                      />
                      <span v-else class="hint hint--compact">Automatic — {{ (conn.label || connKey) }} picks its own default.</span>
                    </div>
                  </div>
                  <div class="set-subrow">
                    <span
                      :id="`insights-model-label-${connKey}`"
                      class="ws-label set-subrow-label"
                      title="Reads archived chats, closed projects and task resolutions into memory, names new chats, and checks schedule results for this provider. Automatic uses the chat's own model; Claude schedule checks use Haiku."
                    >Session insights</span>
                    <div class="set-subrow-control">
                      <ModelSelector
                        v-if="getProviderSection(String(connKey))?.configurable"
                        :model-value="providerInsightsModelSelectorValue(String(connKey) as AliasProviderKey)"
                        :sections="providerInsightsModelSectionsFor(String(connKey) as AliasProviderKey)"
                        :labelled-by="`insights-model-label-${connKey}`"
                        :disabled="routinesSaving || !getProviderSection(String(connKey))?.available"
                        @update:model-value="saveProviderInsightsModel(String(connKey) as AliasProviderKey, $event)"
                      />
                      <span v-else class="hint hint--compact">Automatic — same as the chat model.</span>
                    </div>
                  </div>
                  <label class="set-subrow">
                    <span class="ws-label set-subrow-label">Permission mode</span>
                    <select
                      class="routine-select set-subrow-control"
                      :value="providerDefaultModeValue(String(connKey) as AliasProviderKey)"
                      :disabled="routinesSaving || !getProviderSection(String(connKey))?.available"
                      @change="saveProviderDefaultMode(String(connKey) as AliasProviderKey, ($event.target as HTMLSelectElement).value)"
                    >
                      <option v-for="option in PROVIDER_MODE_OPTIONS" :key="option.value" :value="option.value">
                        {{ option.label }}
                      </option>
                    </select>
                  </label>
                  <div class="set-subrow">
                    <span :id="`thinking-label-${connKey}`" class="ws-label set-subrow-label">Default thinking</span>
                    <div class="set-subrow-control">
                      <!-- A short closed set reads best as a segmented control; a
                           long provider list falls back to a select. -->
                      <div
                        v-if="!workspaceModels"
                        class="hint hint--compact"
                      >{{ workspaceModelsError ? 'Effort options unavailable.' : 'Loading effort options…' }}</div>
                      <div
                        v-else-if="providerThinkingOptions(String(connKey) as AliasProviderKey).length <= 5"
                        class="set-seg"
                        role="radiogroup"
                        :aria-labelledby="`thinking-label-${connKey}`"
                      >
                        <button
                          v-for="option in providerThinkingOptions(String(connKey) as AliasProviderKey)"
                          :key="option.value"
                          type="button"
                          role="radio"
                          :aria-checked="providerDefaultThinkingValue(String(connKey) as AliasProviderKey) === option.value"
                          :disabled="routinesSaving || !getProviderSection(String(connKey))?.available"
                          @click="saveProviderDefaultThinking(String(connKey) as AliasProviderKey, option.value)"
                        >{{ thinkingOptionLabel(option) }}</button>
                      </div>
                      <select
                        v-else
                        class="routine-select"
                        :aria-labelledby="`thinking-label-${connKey}`"
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
                    </div>
                  </div>
                </div>
                <!-- What the CLI brings on its own. Long chip lists, so they sit
                     behind a disclosure, collapsed by default. -->
                <details class="provider-brings">
                  <summary>
                    What this CLI brings ({{ providerBringsCounts(String(connKey), conn) }})
                  </summary>
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
                          {{ !conn.ok ? 'Connect to discover' : conn.skills ? 'None reported by this CLI' : 'Could not read this list. Press Verify to try again.' }}
                        </span>
                      </template>
                    </div>

                    <!-- Skills the CLI ships itself, not installed by the user -->
                    <div class="ws-connectors-header" style="margin-top: 10px;">
                      <span class="ws-label">Built into {{ conn.label || connKey }} ({{ conn.bundled_skills?.length ?? 0 }})</span>
                    </div>
                    <div class="workspace-connector-pills">
                      <template v-if="conn.bundled_skills && conn.bundled_skills.length">
                        <span v-for="skill in conn.bundled_skills" :key="skill" class="connector-pill connector-pill--enabled" :title="`Ships with ${conn.label || connKey}: ${skill}`">
                          <span class="pill-dot"></span> {{ skill }}
                        </span>
                      </template>
                      <span v-else class="hint hint--compact">
                        {{ !conn.ok ? 'Connect to discover' : conn.bundled_skills ? 'None reported by this CLI' : conn.skills ? 'Listed after the next chat with this CLI.' : 'Could not read this list. Press Verify to try again.' }}
                      </span>
                    </div>
                  </div>
                </details>
              </div>
              <div v-if="providerConnectionResult" class="action-result">{{ providerConnectionResult }}</div>
            </div>
          </div>
        </template>

        <div v-if="routinesError" class="card"><p class="hint hint--warn">{{ routinesError }}</p></div>
      </template>

      <!-- WORKSPACE OVERVIEW TAB (the active workspace; /settings/workspace) -->
      <template v-if="currentTab === 'workspace'"><SettingsWorkspaceOverview /></template>

      <!-- WORKSPACES TAB (the list of all of them; /settings/workspaces) -->
      <template v-if="currentTab === 'workspaces'">
        <SettingsManageWorkspaces />
      </template>


      <!-- SKILLS TAB -->
      <template v-if="currentTab === 'skills'">
        <div class="card">
          <div class="settings-card-header settings-card-header--split">
            <div>
              <p class="section-title">Skills</p>
              <p class="hint">
                These are the skills of the <strong>{{ assetWorkspaceLabel }}</strong> workspace, saved in its own <code>skills/</code> folder or included with the app. Ask for one in chat, or invoke it by name with <code>/name</code> when your provider supports skill commands. Workspace git sync carries your own skills to other machines.
              </p>
              <p class="hint">Skills installed directly in Claude Code or opencode can also be used in chats run by that provider, even if they are not listed here. <a class="settings-help-link" href="https://www.raffaelefarinaro.com/ciaobot/builtin.html#skills" target="_blank" rel="noopener noreferrer">How skills work</a> <HostTerminalButton action="skills-update" label="Update skills in Terminal" /></p>
            </div>
            <div class="settings-card-header-actions">
              <button class="set-link set-link--quiet" type="button" @click="createSkillViaChat">Ask Ciao to write one</button>
              <button class="btn-small" type="button" :aria-expanded="showAddSkill" @click="toggleAddSkill">
                {{ showAddSkill ? 'Cancel' : 'New skill' }}
              </button>
            </div>
          </div>

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
                <p class="hint hint--compact">Single zip with one top-level folder containing <code>SKILL.md</code>. Frontmatter <code>name</code> must match folder. Over 15 KB imports with a warning — that file loads into context on every trigger.</p>
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
              <li>Validation: zip-slip, one <code>SKILL.md</code>, frontmatter <code>name/description</code> (15 KB is a budget, not a gate)</li>
              <li>Discoverable immediately: <code>.claude/skills</code> symlink</li>
              <li>Sync between operators = workspace git (no npx)</li>
            </ul>
            <div v-if="addSkillResult" class="action-result" :class="{ '--error': addSkillError }" role="alert">{{ addSkillResult }}</div>
          </div>

          <SkeletonLoader v-if="!skillsLoaded" label="Loading skills" :count="4" />
          <template v-else-if="skillsError">
            <p class="hint hint--warn">{{ skillsError }}</p>
          </template>
          <template v-else-if="skillsInventory">
            <!-- Custom Skills Section -->
            <div class="skill-section">
              <p class="subsection-title subsection-title--spaced">Custom skills</p>
              <p v-if="!customSkills.length" class="set-empty">
                <strong>No custom skills yet.</strong>
                A skill teaches Ciao a repeatable task, such as a review checklist or a report format.
                <button class="set-link" type="button" @click="createSkillViaChat">Ask Ciao to write one</button>
              </p>
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
                      <span class="skill-badges"><span class="set-tag set-tag--custom">Custom</span></span>
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
              <p class="subsection-title subsection-title--spaced">Built-in skills</p>
              <p v-if="!stockSkills.length" class="set-empty">
                <strong>No built-in skills installed.</strong>
                They come with the app and are restored by <code>ciao sync-skills</code>.
              </p>
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
                      <span class="skill-badges"><span class="set-tag">Built in</span></span>
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
              <p class="section-title">Subagents</p>
              <p class="hint">
                These are the subagents of the <strong>{{ assetWorkspaceLabel }}</strong> workspace, saved in its own <code>subagents/</code> folder and shared with Claude Code and opencode. Ask an agent in chat to delegate a task; subagents are not slash commands.
              </p>
              <p class="hint">Subagents installed directly in a provider may also be available in its chats. <a class="settings-help-link" href="https://www.raffaelefarinaro.com/ciaobot/builtin.html#extend" target="_blank" rel="noopener noreferrer">About extensions</a></p>
            </div>
            <div class="settings-card-header-actions">
              <button class="set-link set-link--quiet" type="button" @click="createAssetViaChat('subagent')">Ask Ciao to write one</button>
              <button class="btn-small" type="button" :aria-expanded="showAddSubagent" @click="toggleAddSubagent">
                {{ showAddSubagent ? 'Cancel' : 'New subagent' }}
              </button>
            </div>
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

          <SkeletonLoader v-if="!agentAssetsLoaded" label="Loading subagents" :count="3" />
          <template v-else-if="agentAssetsError">
            <p class="hint hint--warn">{{ agentAssetsError }}</p>
          </template>
          <template v-else>
            <p v-if="!subagentAssets.length" class="set-empty">
              <strong>No subagents yet.</strong>
              A subagent is a specialist Ciao hands part of a task to, with its own instructions.
              <button class="set-link" type="button" @click="toggleAddSubagent">Create one</button>
            </p>
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
                      <button
                        v-if="agent.editable"
                        type="button"
                        class="set-link"
                        :aria-label="`Edit subagent ${agent.name}`"
                        @click.stop="openSubagentEditor(agent)"
                      >Edit</button>
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
              <p class="section-title">Commands</p>
              <p class="hint">
                These are the slash commands of the <strong>{{ assetWorkspaceLabel }}</strong> workspace, saved in its own <code>commands/</code> folder and shared with Claude Code and opencode. Type <code>/name</code> in a chat to run one, for example <code>/interrogation</code>.
              </p>
              <p class="hint">Commands installed directly in a provider can also work in its chats, even if they are not shown here. <a class="settings-help-link" href="https://www.raffaelefarinaro.com/ciaobot/builtin.html#commands" target="_blank" rel="noopener noreferrer">How commands work</a></p>
            </div>
            <div class="settings-card-header-actions">
              <button class="set-link set-link--quiet" type="button" @click="createAssetViaChat('command')">Ask Ciao to write one</button>
              <button class="btn-small" type="button" :aria-expanded="showAddCommand" @click="toggleAddCommand">
                {{ showAddCommand ? 'Cancel' : 'New command' }}
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

          <SkeletonLoader v-if="!agentAssetsLoaded" label="Loading commands" :count="3" />
          <template v-else-if="agentAssetsError">
            <p class="hint hint--warn">{{ agentAssetsError }}</p>
          </template>
          <template v-else>
            <p v-if="!commandAssets.length" class="set-empty">
              <strong>No slash commands yet.</strong>
              A command is a saved prompt you run by typing <code>/name</code> in a chat.
              <button class="set-link" type="button" @click="toggleAddCommand">Create one</button>
            </p>
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
                      <button
                        v-if="command.editable"
                        type="button"
                        class="set-link"
                        :aria-label="`Edit command /${command.name}`"
                        @click.stop="openCommandEditor(command)"
                      >Edit</button>
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

      <!-- Native provider MCP inventory, scoped to the selected workspace. -->
      <template v-if="currentTab === 'mcp'">
        <SettingsMcpServers :mcp="mcp" :default-provider="mcpDefaultProvider" @ask-ciao="createAssetViaChat('mcp')" />
      </template>
      </div>

      <!-- On this page: built from the rendered sections of the current tab,
           so it follows whatever the tab actually shows (conditional sections
           included) instead of a second hand-kept list. -->
      <aside v-if="tocItems.length > 1 || assetRail || providersStatusRail" class="page-rail settings-rail" aria-label="About this page">
        <section v-if="tocItems.length > 1" class="rail-section">
          <h2 id="settings-toc-title" class="rail-title">On this page</h2>
          <nav class="settings-toc" aria-label="Sections on this page">
            <button
              v-for="item in tocItems"
              :key="item.id"
              type="button"
              class="settings-toc-item"
              :class="{ active: item.id === activeTocId }"
              :aria-current="item.id === activeTocId ? 'location' : undefined"
              @click="scrollToSection(item.id)"
            >{{ item.label }}</button>
          </nav>
        </section>
        <!-- Providers: a glance at whether each CLI is working. -->
        <section
          v-if="providersStatusRail"
          class="rail-section settings-rail-status"
          aria-labelledby="settings-rail-status-title"
        >
          <h2 id="settings-rail-status-title" class="rail-title">Status</h2>
          <div class="rail-kvs">
            <div v-for="(conn, connKey) in providerKeys?.connections" :key="`rail-${connKey}`" class="rail-kv">
              <span>{{ conn.label || connKey }}</span>
              <strong :class="{ 'rail-attention': !conn.ok }">{{ conn.ok ? 'Connected' : 'Not connected' }}</strong>
            </div>
          </div>
        </section>
        <!-- Skills, subagents, commands: how many exist and where they live. -->
        <section v-if="assetRail" class="rail-section settings-rail-assets" aria-labelledby="settings-rail-assets-title">
          <h2 id="settings-rail-assets-title" class="rail-title">{{ assetRail.title }}</h2>
          <div class="rail-kvs">
            <div class="rail-kv"><span>Custom</span><strong>{{ assetRail.custom }}</strong></div>
            <div class="rail-kv"><span>Built in</span><strong>{{ assetRail.builtin }}</strong></div>
            <div v-if="assetRail.other" class="rail-kv"><span>Other</span><strong>{{ assetRail.other }}</strong></div>
          </div>
          <p class="rail-note">
            Saved in <code>{{ assetRail.folder }}</code>.
            <template v-if="currentTab === 'skills'">
              Skills you install directly in a CLI stay available too; see
              <RouterLink to="/settings/providers#chat-providers">Providers</RouterLink>.
            </template>
          </p>
        </section>
      </aside>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import SkeletonLoader from './SkeletonLoader.vue'
import SettingsManageWorkspaces from './settings/SettingsManageWorkspaces.vue'
import { formatConnectorLabel } from '../lib/mcpLabels'
import { ref, computed, nextTick, onMounted, onUnmounted, watch, provide } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../lib/api'
import { describeInterruptedCards, type RestartBlocker } from '../lib/restartBlockers'
import { errorMessage, apiErrorMessage, errorPayload, errorPayloadList } from '../lib/errorMessage'
import {
  DEFAULT_FONT_SCALE,
  FONT_SCALE_STEP,
  MAX_FONT_SCALE,
  MIN_FONT_SCALE,
  useFontScale,
} from '../composables/useFontScale'
import { useModalFocus } from '../composables/useModalFocus'
import { useAppearanceSettings } from '../composables/useAppearanceSettings'
import { readLocalTheme, type Theme } from '../lib/appearance'
import type {
  AgentAssetsResponse,
  CommandAsset,
  CommandsResponse,
  CreatedAgentAssetResponse,
  DebugIssueReport,
  DeployResult,
  LocalStatus,
  ModelsResponse,
  ProviderConfigSettings,
  ProviderConnection,
  RoutineSettings,
  SkillInventory,
  SlashCommand,
  SubagentAsset,
  WorkspaceHealthResponse,
  PackageStatus,
  PackageChangelog,
  PackageUpdateResult,
  EngineUpdateStatus,
  ProviderActionResult,
  LocalHandbackResult,
} from '../lib/types'
import {
  updateFailed,
  updateFailureText,
  updateInFlight,
  updatePhaseLabel,
  updateStage,
  updateStageInFlight,
} from '../lib/engineUpdate'
import { askConfirm } from '../lib/confirm'
import { GWS_ACCOUNTS_KEY, useGwsAccounts } from '../composables/useGwsAccounts'
import {
  WORKSPACE_ADMIN_KEY,
  useWorkspaceAdmin,
} from '../composables/useWorkspaceAdmin'
import { useFileViewerStore } from '../stores/fileViewer'
import { useProjectStore } from '../stores/projects'
import { useHousekeepingStore } from '../stores/housekeeping'
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRoot,
  DropdownMenuTrigger,
} from 'reka-ui'
import PaneHeader from './PaneHeader.vue'
import HostTerminalButton from './HostTerminalButton.vue'
import { useCommandCopy } from '../composables/useCommandCopy'
import UpdateProgressView from './UpdateProgressView.vue'
import WorkspaceMoveDialog from './WorkspaceMoveDialog.vue'
import ModelSelector from './ModelSelector.vue'
import {
  DEFAULT_INSIGHTS_SELECTION,
  DEFAULT_MODE_SELECTION,
  DEFAULT_MODEL_SELECTION,
  DEFAULT_THINKING_SELECTION,
  PROVIDER_MODE_OPTIONS,
  useProviderChoices,
  type AliasProviderKey,
} from '../composables/useProviderChoices'
import SettingsDevices from './settings/SettingsDevices.vue'
import SettingsServer from './settings/SettingsServer.vue'
import SettingsAppInstall from './settings/SettingsAppInstall.vue'
import SettingsNotifications from './settings/SettingsNotifications.vue'
import SettingsKeyboardShortcuts from './settings/SettingsKeyboardShortcuts.vue'
import SettingsEngineLogin from './settings/SettingsEngineLogin.vue'
import SettingsMcpServers from './settings/SettingsMcpServers.vue'
import SettingsAppearance from './settings/SettingsAppearance.vue'
import SettingsRadioGroup from './settings/SettingsRadioGroup.vue'
import SettingsMemoryBackup from './settings/SettingsMemoryBackup.vue'
import SettingsWorkspaceOverview from './settings/SettingsWorkspaceOverview.vue'
import type { ModelSection } from '../lib/modelSections'
import { modelDisplayName } from '../lib/modelLabels'
import { settingsTabLabel } from '../lib/settingsNav'
import { workspaceLabel } from '../lib/workspaceLabel'
import { parseWorkspaceFileAccessHref } from '../lib/appLinks'
import { useMcpServers } from '../composables/useMcpServers'
import { assetOriginClass, assetOriginLabel, commandOrigin, subagentOrigin } from '../lib/assetOrigin'


const emit = defineEmits<{ 'open-sidebar': [] }>()

const route = useRoute()
const router = useRouter()
const fileViewer = useFileViewerStore()
const projectStore = useProjectStore()
const housekeeping = useHousekeepingStore()

// Native MCP inventory follows the sidebar's selected workspace.
const mcp = useMcpServers({ api, workspace: () => projectStore.activeWorkspace })
const mcpDefaultProvider = computed(() => projectStore.workspaceOptions.find(w => w.name === projectStore.activeWorkspace)?.default_provider)
const currentTab = computed(() => {
  const tab = (route.params.tab as string) || 'home'
  return tab
})
// The Overview page is about one workspace, so its header names it.
const pageTag = computed(() => (
  currentTab.value === 'workspace'
    ? `${workspaceLabel(projectStore.activeWorkspace)} · Overview`
    : settingsTabLabel(route.path)
))

// ── Keyboard shortcuts: the common four, the rest behind a disclosure ────

// ── On this page ─────────────────────────────────────────────────────────
// Read from the rendered tab, not a hand-kept list: a section is any
// top-level .card with a .section-title, so conditional sections (debug,
// client scope, loading states) appear and disappear with the page itself.
type TocItem = { id: string; label: string }
const bodyEl = ref<HTMLElement | null>(null)
const mainEl = ref<HTMLElement | null>(null)
const tocItems = ref<TocItem[]>([])
const activeTocId = ref('')
let tocObserver: MutationObserver | null = null

function slugify(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'section'
}

function tocSections(): HTMLElement[] {
  const main = mainEl.value
  if (!main) return []
  return Array.from(main.querySelectorAll<HTMLElement>('.card')).filter(card => (
    card.parentElement?.closest('.card') == null
    && card.querySelector('.section-title')
  ))
}

function rebuildToc(): void {
  const used = new Set<string>()
  const items: TocItem[] = []
  for (const card of tocSections()) {
    const label = card.querySelector('.section-title')?.textContent?.trim() || ''
    if (!label) continue
    let id = card.id
    if (!id || used.has(id)) {
      const base = `settings-${slugify(label)}`
      id = base
      for (let n = 2; used.has(id); n += 1) id = `${base}-${n}`
      card.id = id
    }
    used.add(id)
    items.push({ id, label })
  }
  const same = items.length === tocItems.value.length
    && items.every((item, i) => item.id === tocItems.value[i].id && item.label === tocItems.value[i].label)
  if (!same) tocItems.value = items
  updateActiveToc()
}

// The active entry is the last section whose top has scrolled past a line a
// little below the pane's top edge.
function updateActiveToc(): void {
  const body = bodyEl.value
  if (!body || !tocItems.value.length) return
  const line = body.getBoundingClientRect().top + 96
  let active = tocItems.value[0].id
  for (const item of tocItems.value) {
    const el = document.getElementById(item.id)
    if (el && el.getBoundingClientRect().top <= line) active = item.id
  }
  activeTocId.value = active
}

function onBodyScroll(): void {
  updateActiveToc()
}

function scrollToSection(id: string): void {
  const el = document.getElementById(id)
  if (!el) return
  const reduce = typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  el.scrollIntoView?.({ block: 'start', behavior: reduce ? 'auto' : 'smooth' })
  // Move focus with the view so keyboard users land in the section too.
  if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '-1')
  el.focus({ preventScroll: true })
  activeTocId.value = id
}

watch(currentTab, () => {
  void nextTick(rebuildToc)
})

onMounted(() => {
  void nextTick(rebuildToc)
  if (mainEl.value && typeof MutationObserver !== 'undefined') {
    tocObserver = new MutationObserver(rebuildToc)
    tocObserver.observe(mainEl.value, { childList: true, subtree: true, characterData: true })
  }
})

onUnmounted(() => {
  tocObserver?.disconnect()
  tocObserver = null
})

// ── GitHub star nudge, mirrored onto the open-source card ─────────────────
// The home strip's ask-style action is the source of truth: the backend gates
// it on a `starred` receipt (permanent) or a "Later" snooze, so this simply
// reflects whether the ask is currently live. The strip's own toast is the
// success feedback when the backend button runs; here the button disappears,
// which is feedback enough.
const starPending = ref(false)
const showStarNudge = computed(() =>
  housekeeping.actions.some(action => action.id === 'github-star'),
)

async function starOnGitHub(): Promise<void> {
  if (starPending.value) return
  starPending.value = true
  try {
    const { ok, summary } = await housekeeping.run('github-star')
    if (ok) {
      projectStore.pushToast({
        chat_id: '',
        title: '★ Starred — thank you!',
        body: summary || 'It genuinely helps other developers discover Ciaobot.',
      })
    } else if (summary) {
      projectStore.pushErrorToast('Could not star the repository', summary)
    }
  } catch (e) {
    projectStore.pushErrorToast('Could not star the repository', e instanceof Error ? e.message : String(e))
  } finally {
    starPending.value = false
  }
}

async function dismissStarNudge(): Promise<void> {
  await housekeeping.dismiss('github-star')
}

const expandedSkills = ref<Record<string, boolean>>({})
const expandedCommands = ref<Record<string, boolean>>({})
const expandedSubagents = ref<Record<string, boolean>>({})

// ── Appearance settings ────────────────────────────────────────────────────
const activeTheme = ref<Theme>('system')
// The scale itself, its bounds, its step and its persistence live in
// useFontScale, shared with the global zoom shortcuts. Only the displayed
// percentage is Settings' own: the scale is anchored to the pre-rescale UI, so
// DEFAULT_FONT_SCALE (1.2) reads as "100%".
const { fontScale, adjust: adjustFontScale, reset: resetFontScale } = useFontScale()
const appearance = useAppearanceSettings()
const fontScalePercent = computed(() => Math.round((fontScale.value / DEFAULT_FONT_SCALE) * 100))

function loadAppearanceSettings() {
  activeTheme.value = appearance.settings.value.theme ?? readLocalTheme()
}

// Theme and font size are engine-wide: apply here at once, then store on the
// engine so every device follows. A failed save reloads the engine value.
const THEME_OPTIONS: { value: Theme; label: string }[] = [
  { value: 'dark', label: 'Dark' },
  { value: 'light', label: 'Light' },
  { value: 'system', label: 'System' },
]
const appearanceError = ref('')
// Another device (or the first load) can move the engine theme while Settings is open.
// While a choice is being saved the control shows that choice, not the engine's
// answer to an earlier one.
watch([() => appearance.settings.value.theme, appearance.themeSaves], ([theme, saves]) => {
  if (theme && saves === 0) activeTheme.value = theme
})

function setTheme(theme: Theme) {
  activeTheme.value = theme
  appearanceError.value = ''
  void appearance.saveTheme(theme).catch((cause: unknown) => {
    appearanceError.value = cause instanceof Error ? cause.message : 'Could not save appearance settings.'
  })
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
let skillsLoadSequence = 0
let commandsLoadSequence = 0
let agentAssetsLoadSequence = 0
const skillsLoaded = ref(false)
const skillsError = ref('')
const commands = ref<SlashCommand[]>([])
const commandsLoaded = ref(false)
const commandsError = ref('')
const agentAssets = ref<AgentAssetsResponse | null>(null)
const agentAssetsLoaded = ref(false)
const agentAssetsError = ref('')

// ── Routine settings (Providers tab) ─────────────────────────────────────────
const routines = ref<RoutineSettings | null>(null)
const routinesError = ref('')
const routinesSaving = ref(false)
const workspaceModels = ref<ModelsResponse | null>(null)

const {
  getProviderSection,
  providerSectionsWithDefault,
  providerDefaultModelOverride,
  providerThinkingOptions,
} = useProviderChoices({ routines, workspaceModels })

interface WorkspaceMoveOperation {
  phase: string
  source: string
  target: string
  error: string
  updated_at: string
  denied?: boolean
}

interface WorkspaceMoveStatus {
  workspace_root: string
  local: boolean
  admin_only?: boolean
  cloud_sync_warning?: string
  operation: WorkspaceMoveOperation | null
}

const workspaceMove = ref<WorkspaceMoveStatus | null>(null)
const workspaceMoveOpen = ref(false)

async function fetchWorkspaceMove() {
  try {
    workspaceMove.value = await api.get<WorkspaceMoveStatus>('/api/workspace-move')
  } catch {
    workspaceMove.value = null
  }
}

// The last move's outcome, for a day after it: the page reloads while the
// engine restarts, so this is where a move that finished — or was undone —
// says so.
const workspaceMoveOutcome = computed(() => {
  const op = workspaceMove.value?.operation
  if (!op) return null
  const age = Date.now() - Date.parse(op.updated_at)
  if (!(age < 24 * 60 * 60 * 1000)) return null
  switch (op.phase) {
    case 'done':
      return { text: `Moved here from ${op.source}.`, error: false }
    case 'failed':
      return { text: `The move to ${op.target} did not start: ${op.error}`, error: true }
    case 'rolled_back':
      return { text: `The move to ${op.target} did not finish and was undone: ${op.error}`, error: true }
    case 'rollback_failed':
      return { text: `The move to ${op.target} failed and could not be undone: ${op.error}`, error: true }
    default:
      return null
  }
})

function onWorkspaceMoveStarted() {
  workspaceMoveOpen.value = false
  restartAndReload('Moving the workspace. Ciaobot will restart from the new folder…')
}

async function fetchRoutines() {
  try {
    routines.value = await api.get<RoutineSettings>('/api/settings/routines')
  } catch (e) {
    routinesError.value = `Failed to load model settings: ${errorMessage(e)}`
  }
}

async function saveRoutines(patch: Record<string, unknown>) {
  routinesSaving.value = true
  try {
    routines.value = await api.patch<RoutineSettings>('/api/settings/routines', patch)
    notifySaved('Model settings saved.')
  } catch (e) {
    const detail = errorMessage(e)
    notifyFailed('Could not save model settings', detail)
  } finally {
    routinesSaving.value = false
  }
}

// ── Per-provider default model / thinking (Providers tab) ─────────────────
function providerDefaultModelSectionsFor(provider: AliasProviderKey): ModelSection[] {
  return providerSectionsWithDefault(
    provider,
    DEFAULT_MODEL_SELECTION,
    (effective) => (effective ? `Automatic (${modelDisplayName(effective)})` : 'Automatic'),
    true, // the entry names the model Automatic resolves to
  )
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

function providerDefaultThinkingOverride(provider: AliasProviderKey): string {
  return routines.value?.provider_default_thinking?.[provider] || ''
}

function providerDefaultThinkingValue(provider: AliasProviderKey): string {
  return providerDefaultThinkingOverride(provider) || DEFAULT_THINKING_SELECTION
}

/** Segment label: the automatic entry is just "Auto"; levels are capitalised. */
function thinkingOptionLabel(option: { value: string; label: string }): string {
  if (option.value === DEFAULT_THINKING_SELECTION) return 'Auto'
  return option.label.charAt(0).toUpperCase() + option.label.slice(1)
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

// ── Per-provider default permission mode (Providers tab) ────────────────
function providerDefaultModeValue(provider: AliasProviderKey): string {
  return routines.value?.provider_default_modes?.[provider] || DEFAULT_MODE_SELECTION
}

async function saveProviderDefaultMode(provider: AliasProviderKey, value: string) {
  const modes = JSON.parse(
    JSON.stringify(routines.value?.provider_default_modes || {}),
  ) as Record<string, string>
  if (value === DEFAULT_MODE_SELECTION) delete modes[provider]
  else modes[provider] = value
  await saveRoutines({ provider_default_modes: modes })
}

// ── Per-provider Session insights model (Providers tab) ────────────────
// Automatic reads the session with that chat's own model; a closed project
// uses its last-active chat (ciao/insights.py::resolve_insights_model).
function providerInsightsModelSelectorValue(provider: AliasProviderKey): string {
  return routines.value?.provider_insights_models?.[provider] || DEFAULT_INSIGHTS_SELECTION
}

function providerInsightsModelSectionsFor(provider: AliasProviderKey): ModelSection[] {
  return providerSectionsWithDefault(provider, DEFAULT_INSIGHTS_SELECTION, () =>
    'Automatic — same as chat model',
  )
}

async function saveProviderInsightsModel(provider: AliasProviderKey, value: string | string[]) {
  const selected = Array.isArray(value) ? value[0] || '' : value
  const model = selected === DEFAULT_INSIGHTS_SELECTION ? '' : selected.trim()
  const models = { ...(routines.value?.provider_insights_models || {}) }
  if (model) models[provider] = model
  else delete models[provider]
  await saveRoutines({ provider_insights_models: models })
}

// ── Provider connections (Providers tab, chat providers card) ───────────────────
const providerKeys = ref<ProviderConfigSettings | null>(null)
const providerKeysLoaded = ref(false)
const providerKeysError = ref('')
const providersStatusRail = computed(() => currentTab.value === 'providers' && !!providerKeys.value?.connections)
const providerConnectionPending = ref('')
const providerConnectionResult = ref('')
const gwsAccounts = useGwsAccounts({ notifySaved, notifyFailed })
const { gwsProfileOptions, fetchGwsIntegration } = gwsAccounts
provide(GWS_ACCOUNTS_KEY, gwsAccounts)

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

// /settings/providers#chat-providers lands on the Chat providers card. The anchor
// only exists once the connections have loaded, so the browser's own hash
// jump misses it, and the router has no scrollBehavior for in-app links (this
// view stays mounted across tabs). Scroll on the first load and on each
// navigation to the anchor — not after every refetch, or Verify/Connect would
// yank the page back to the top of the card.
function scrollToChatProvidersIfLinked(): void {
  if (route.hash !== '#chat-providers') return
  void nextTick(() => {
    document.getElementById('chat-providers')?.scrollIntoView?.({ block: 'start' })
  })
}
watch(() => route.fullPath, scrollToChatProvidersIfLinked)

// Home links land on these cards (#install-app on General from the setup
// card's "How to install", #other-devices on Access & devices from "Phone
// setup"). A link mounts the tab and sets the hash in one navigation and the
// cards mount after the tab flips, so the scroll re-runs on either change.
const TAB_ANCHORS: Record<string, string[]> = {
  home: ['#install-app'],
  access: ['#other-devices'],
}
watch(
  [() => route.hash, currentTab],
  ([hash, tab]) => {
    if (!TAB_ANCHORS[tab]?.includes(hash)) return
    void nextTick(() => {
      document.getElementById(hash.slice(1))?.scrollIntoView?.({ block: 'start' })
    })
  },
  { immediate: true },
)

/** Summary line for the collapsed "What this CLI brings" disclosure. */
function providerBringsCounts(providerId: string, conn: ProviderConnection): string {
  const mcps = connectionMcps(providerId).length
  const skills = conn.skills?.length ?? 0
  const mcpLabel = `${mcps} MCP ${mcps === 1 ? 'server' : 'servers'}`
  const skillLabel = conn.ok && !conn.skills
    ? 'skills not read'
    : `${skills} ${skills === 1 ? 'skill or plugin' : 'skills & plugins'}`
  const bundled = conn.bundled_skills?.length
  return bundled ? `${mcpLabel}, ${skillLabel}, ${bundled} built in` : `${mcpLabel}, ${skillLabel}`
}



const providerCommandCopy = useCommandCopy()

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
      const providerName = provider === 'opencode' ? 'opencode' : 'Claude Code'
      providerConnectionResult.value = `Opened the ${providerName} sign-in in ${result.launched || 'a terminal'}.`
    } else if (action === 'logout') {
      providerConnectionResult.value = 'Logged out.'
    } else {
      providerConnectionResult.value = result.ok ? `Connection verified (${result.auth}).` : result.detail || 'Not connected.'
    }
    await fetchProviderKeys()
  } catch (e) {
    // Connect answers a browser that is not on the engine's computer (or a host that
    // cannot open a terminal) with the command to run there: guidance, not an error.
    const command = errorPayload(e)?.command
    providerConnectionResult.value = typeof command === 'string' && command
      ? errorMessage(e)
      : `Error: ${errorMessage(e)}`
  } finally {
    providerConnectionPending.value = ''
  }
}

// The skills, commands, subagents and MCP servers below belong to ONE
// workspace: the one selected in the sidebar. Every request carries that name
// so the list and the writes that edit it resolve the same agent root — without
// it, an edit would land in a root the list is not showing.
const assetScope = computed(() => {
  const name = projectStore.activeWorkspace || ''
  return name ? `?workspace=${encodeURIComponent(name)}` : ''
})

// Names the workspace these tabs describe, so the copy can say which one
// rather than implying the list covers the whole install.
const assetWorkspaceLabel = computed(() => projectStore.activeWorkspace || 'selected')

async function fetchSkills() {
  const sequence = ++skillsLoadSequence
  const scope = assetScope.value
  skillsLoaded.value = false
  skillsError.value = ''
  try {
    const inventory = await api.get<SkillInventory>(`/api/admin/skills${scope}`)
    if (sequence === skillsLoadSequence && scope === assetScope.value) skillsInventory.value = inventory
  } catch (e) {
    if (sequence === skillsLoadSequence && scope === assetScope.value) {
      skillsError.value = `Failed to load skills: ${errorMessage(e)}`
    }
  } finally {
    if (sequence === skillsLoadSequence && scope === assetScope.value) skillsLoaded.value = true
  }
}

async function fetchCommands() {
  const sequence = ++commandsLoadSequence
  const scope = assetScope.value
  commandsLoaded.value = false
  commandsError.value = ''
  try {
    const res = await api.get<CommandsResponse>(`/api/commands${scope}`)
    if (sequence === commandsLoadSequence && scope === assetScope.value) {
      commands.value = Array.isArray(res.commands) ? res.commands : []
    }
  } catch (e) {
    if (sequence === commandsLoadSequence && scope === assetScope.value) {
      commandsError.value = `Failed to load commands: ${errorMessage(e)}`
    }
  } finally {
    if (sequence === commandsLoadSequence && scope === assetScope.value) commandsLoaded.value = true
  }
}

async function fetchAgentAssets() {
  const sequence = ++agentAssetsLoadSequence
  const scope = assetScope.value
  agentAssetsLoaded.value = false
  agentAssetsError.value = ''
  try {
    const assets = await api.get<AgentAssetsResponse>(`/api/agent-assets${scope}`)
    if (sequence === agentAssetsLoadSequence && scope === assetScope.value) agentAssets.value = assets
  } catch (e) {
    if (sequence === agentAssetsLoadSequence && scope === assetScope.value) {
      agentAssetsError.value = `Failed to load workspace assets: ${errorMessage(e)}`
    }
  } finally {
    if (sequence === agentAssetsLoadSequence && scope === assetScope.value) agentAssetsLoaded.value = true
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

// `skills` is optional on the wire in practice: a partial or malformed
// response ({} from a stub or an older engine) must render the empty state,
// not throw in render and blank the whole Settings pane.
const stockSkills = computed(() => {
  return skillsInventory.value?.skills?.filter(s => s.label === 'stock') || []
})

const customSkills = computed(() => {
  return skillsInventory.value?.skills?.filter(s => s.label === 'custom') || []
})

const subagentAssets = computed(() => agentAssets.value?.subagents || [])
const commandAssets = computed(() => agentAssets.value?.commands || [])

type AssetRail = { title: string; custom: number; builtin: number; other: number; folder: string }

function originCounts(items: { editable?: boolean; scope?: string }[]): Pick<AssetRail, 'custom' | 'builtin' | 'other'> {
  const counts = { custom: 0, builtin: 0, other: 0 }
  for (const item of items) {
    const origin = commandOrigin(item)
    if (origin === 'custom') counts.custom += 1
    else if (origin === 'builtin') counts.builtin += 1
    else counts.other += 1
  }
  return counts
}

// Rail content for the asset list tabs; null elsewhere or while loading.
const assetRail = computed<AssetRail | null>(() => {
  if (currentTab.value === 'skills' && skillsLoaded.value && !skillsError.value) {
    return { title: 'Skills', custom: customSkills.value.length, builtin: stockSkills.value.length, other: 0, folder: 'skills/' }
  }
  if (!agentAssetsLoaded.value || agentAssetsError.value) return null
  if (currentTab.value === 'subagents') {
    return { title: 'Subagents', ...originCounts(subagentAssets.value), folder: 'subagents/' }
  }
  if (currentTab.value === 'commands') {
    return { title: 'Commands', ...originCounts(commandAssets.value), folder: 'commands/' }
  }
  return null
})
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
      workspace: projectStore.activeWorkspace,
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
      workspace: projectStore.activeWorkspace,
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

// Row-level Edit: open the row and its editor in one step.
function openSubagentEditor(agent: SubagentAsset) {
  expandedSubagents.value[`${agent.source}:${agent.name}:${agent.path}`] = true
  startEditSubagent(agent)
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
      workspace: projectStore.activeWorkspace,
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
    await api.del(`/api/agent-assets/subagents/${encodeURIComponent(agent.name)}${assetScope.value}`)
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

function openCommandEditor(command: CommandAsset) {
  expandedCommands.value[commandKey(command)] = true
  startEditCommand(command)
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
      workspace: projectStore.activeWorkspace,
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
    await api.del(`/api/agent-assets/commands/${encodeURIComponent(command.name)}${assetScope.value}`)
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

const ASSET_CHAT_PROMPTS = {
  skill: {
    title: 'New Custom Skill',
    prompt: 'I want to create a new custom skill. Please guide me through writing a new skill (creating the SKILL.md under the skills/ directory).',
  },
  subagent: {
    title: 'New Subagent',
    prompt: 'I want to create a new shared subagent. Please guide me through writing its definition under the subagents/ directory.',
  },
  command: {
    title: 'New Slash Command',
    prompt: 'I want to create a new shared slash command. Please guide me through writing it under the commands/ directory.',
  },
  mcp: {
    title: 'Add an MCP server',
    prompt: 'Add an MCP server to this workspace. Ask me which server and its URL, run the CLI command for the provider I use, help me sign in, then reload this chat.',
  },
} as const

function createSkillViaChat() {
  return createAssetViaChat('skill')
}

async function createAssetViaChat(kind: keyof typeof ASSET_CHAT_PROMPTS) {
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
    const chat = await projectStore.createChat(projectId, ASSET_CHAT_PROMPTS[kind].title)
    if (chat) {
      projectStore.sendMessage(chat.chat_id, ASSET_CHAT_PROMPTS[kind].prompt)
    }
  } catch (e) {
    notifyFailed('Could not start chat', errorMessage(e))
  }
}


// ── Workspaces settings (Overview and Manage workspaces) ───────────────────────────────────
// Transient success feedback. Routes through the app-wide in-app toast (the
// same auto-dismissing popup used for routine/chat notifications) instead of
// leaving persistent inline text under the form.
function notifySaved(body: string, title = 'settings') {
  projectStore.pushToast({ chat_id: '', title, body })
}

// The failure sibling of notifySaved. Error toasts persist until dismissed and
// can seed a fix chat from `detail`.
function notifyFailed(title: string, detail: string) {
  projectStore.pushErrorToast(title, detail)
}

// The workspace registry, its editor state and actions live in a composable so
// the workspace pages share one load and one set of drafts.
const workspaceAdmin = useWorkspaceAdmin({
  notifySaved,
  notifyFailed,
  gwsProfileOptions,
  fetchGwsIntegration,
})
const {
  workspacesLoaded,
  workspacesError,
  showNewWorkspace,
  workspaceForms,
  workspaceLinkStatus,
  workspaceLinkTarget,
  fetchWorkspacesList,
  fetchArchivedWorkspaces,
} = workspaceAdmin
// The sidebar's "New workspace" links to /settings/workspaces#new-workspace:
// open the form and bring it into view instead of landing on the list.
watch(
  () => [route.hash, currentTab.value, workspacesLoaded.value] as const,
  ([hash, tab, loaded]) => {
    if (hash !== '#new-workspace') return
    showNewWorkspace.value = true
    // The form renders once the list has loaded, so wait for that to focus it.
    if (tab !== 'workspaces' || !loaded) return
    void nextTick(() => {
      document.querySelector('.workspace-card--new')?.scrollIntoView?.({ block: 'start' })
      document.querySelector<HTMLInputElement>('.workspace-card--new input')?.focus()
    })
  },
  { immediate: true },
)
const workspaceModelsError = ref('')

// One row "..." menu open at a time, keyed by row. Esc closes it here and
// marks the press handled: ChatLayout's window Esc handler leaves Settings
// unless the event was already handled, and Reka's own window listener is
// registered later, too late to mark it.
const settingsMenu = ref<string | null>(null)

function setSettingsMenu(key: string, open: boolean): void {
  if (open) settingsMenu.value = key
  else if (settingsMenu.value === key) settingsMenu.value = null
}

function closeSettingsMenuOnEscape(event: KeyboardEvent): void {
  event.preventDefault()
  settingsMenu.value = null
}
provide(WORKSPACE_ADMIN_KEY, { ...workspaceAdmin, settingsMenu, setSettingsMenu, closeSettingsMenuOnEscape })

// Bumped on every route resolution so a delayed list load from a superseded
// link cannot open, scroll to or focus the wrong row.
let workspaceLinkRequest = 0
const WORKSPACE_LINK_OFFSET = 16

function scrollToWorkspaceFileAccess(name: string): void {
  const body = bodyEl.value
  if (!body) return
  // Scope both lookups to the owning pane, and measure the field row itself:
  // the card header can be in view while the File access control is still
  // below the fold.
  const row = body.querySelector<HTMLElement>(`#workspace-file-access-row-${name}`)
  const control = body.querySelector<HTMLElement>(`#workspace-file-access-${name}`)
  if (!row || !control) return
  // Only the settings pane scrolls. Its scrollTop is the truth; a measured
  // offset keeps this correct whatever else has rendered above the row.
  // scrollIntoView is deliberately not used: on a nested pane iOS Safari can
  // scroll the wrong ancestor.
  const offset = row.getBoundingClientRect().top - body.getBoundingClientRect().top
  const top = Math.max(0, body.scrollTop + offset - WORKSPACE_LINK_OFFSET)
  const reduce = typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  body.scrollTo({ top, behavior: reduce ? 'auto' : 'smooth' })
  control.focus({ preventScroll: true })
  workspaceLinkTarget.value = name
}

async function applyWorkspaceFileAccessLink(fullPath: string): Promise<void> {
  const request = ++workspaceLinkRequest
  // A navigation clears what an earlier link said; a missing target and the
  // row it pointed at must not outlive the address that produced them.
  workspaceLinkStatus.value = ''
  workspaceLinkTarget.value = null
  const name = parseWorkspaceFileAccessHref(fullPath)
  if (!name || currentTab.value !== 'workspaces') return
  await loadSettingsTab('workspaces')
  if (request !== workspaceLinkRequest) return
  await nextTick()
  if (request !== workspaceLinkRequest) return
  // A failed load is an error, not a claim that the workspace is missing.
  if (workspacesError.value) return
  const form = workspaceForms.value.find(f => f.name === name)
  if (!form) {
    workspaceLinkStatus.value = `No workspace named "${name}" exists. No settings changed.`
    return
  }
  // The page shows one workspace, so the link makes the named one active. It
  // never selects a value or saves. Leaving a draft with unsaved edits asks
  // first (the store's switch guard); declining keeps the user where they are.
  await projectStore.switchWorkspace(name, { transition: false })
  if (request !== workspaceLinkRequest) return
  if (projectStore.activeWorkspace !== name) {
    workspaceLinkStatus.value =
      `File access for "${name}" was not opened: your unsaved changes to "${projectStore.activeWorkspace}" were kept.`
    return
  }
  await router.replace({ path: '/settings/workspace', hash: '#file-access' })
}

// The Overview page is the active workspace's editor. A File access link lands
// here with #file-access: scroll to and focus the selector once the list is in.
watch(
  () => [route.hash, currentTab.value, workspacesLoaded.value] as const,
  async ([hash, tab, loaded]) => {
    if (tab !== 'workspace' || !loaded) return
    await nextTick()
    if (hash === '#file-access') scrollToWorkspaceFileAccess(projectStore.activeWorkspace)
    else if (hash === '#workspace-name') document.getElementById('workspace-name-input')?.focus()
  },
  { immediate: true },
)

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

// `force` bypasses the provider catalog caches, which costs a provider process
// spawn. Opening Settings is exactly the moment a user has just connected a
// provider elsewhere, so the tab does refresh -- but it renders from cache first
// and lets the forced fetch land after, rather than holding the page on it.
async function fetchWorkspaceModels(force = false) {
  try {
    workspaceModels.value = await api.get<ModelsResponse>(
      force ? '/api/models?refresh=1' : '/api/models',
    )
    workspaceModelsError.value = ''
  } catch (e) {
    workspaceModelsError.value = `Could not load model and effort options: ${errorMessage(e)}`
  }
}


// Each tab loads only the data its visible controls use. A completed or
// in-flight load is reused while this view stays mounted; workspace-scoped
// tabs get a separate load after switching roots.
const tabLoads = new Map<string, Promise<void>>()
// The workspace list feeds both workspace pages. It loads once per registry
// revision, not once per page: a second load would rebuild the forms and drop
// the unsaved edits a user left on Overview when they open Workspaces.
let workspaceListLoad: Promise<void> | null = null
function ensureWorkspaceList(): Promise<void> {
  workspaceListLoad ??= fetchWorkspacesList()
  return workspaceListLoad
}
function loadSettingsTab(tab: string): Promise<void> {
  const scoped = ['providers', 'skills', 'subagents', 'commands', 'mcp'].includes(tab)
  const key = scoped ? `${tab}:${projectStore.activeWorkspace}` : tab
  const prior = tabLoads.get(key)
  if (prior) return prior
  let reads: Promise<unknown>[]
  switch (tab) {
    case 'home':
      // The Updates card reads the package and update status; Session
      // insights reads the routines.
      reads = [fetchPackageStatus(), fetchUpdateStatus(), fetchRoutines()]
      break
    case 'appearance':
      // The card is local: loadAppearanceSettings() runs on mount.
      reads = []
      break
    case 'access':
      // PWA password; Network access reads the routines. SettingsDevices
      // owns its own loads.
      reads = [fetchAuthSettings(), fetchRoutines()]
      break
    case 'advanced':
      // The health card waits on the agent assets, so this tab has to read
      // them: without this it stays on its loader until another tab happens
      // to fetch the same data.
      reads = [fetchLocalStatus().then(() => {
        if (localStatus.value?.dev_mode) return refreshDebugIssues()
      }), fetchRoutines(), fetchWorkspaceMove(), fetchAgentAssets()]
      break
    case 'providers':
      reads = [fetchRoutines(), fetchProviderKeys().then(scrollToChatProvidersIfLinked), mcp.fetchStatus(), fetchWorkspaceModels()]
      break
    case 'workspace':
      // Not scoped: the list covers every workspace. Handover and Update tasks
      // reload themselves when the active workspace changes.
      reads = [fetchGwsIntegration(), ensureWorkspaceList()]
      break
    case 'workspaces':
      reads = [fetchGwsIntegration(), ensureWorkspaceList(), fetchArchivedWorkspaces()]
      break
    case 'skills':
      reads = [fetchSkills()]
      break
    case 'commands':
      reads = [fetchCommands(), fetchAgentAssets()]
      break
    case 'subagents':
      reads = [fetchAgentAssets()]
      break
    case 'mcp':
      reads = [mcp.fetchStatus()]
      break
    default:
      reads = []
  }
  const request = Promise.all(reads).then(() => {})
  tabLoads.set(key, request)
  return request
}

// File access links are checked on every navigation, including the direct load
// of one. `loadSettingsTab` is cached, so a link that arrives with the tab
// costs no second fetch, and a link that arrives before the first list does
// waits for that same load instead of opening a row that is not there yet.
watch(
  () => route.fullPath,
  path => { void applyWorkspaceFileAccessLink(path) },
  { immediate: true },
)

onMounted(() => {
  loadAppearanceSettings()
  housekeeping.init()
  void loadSettingsTab(currentTab.value)
})
watch(currentTab, tab => { void loadSettingsTab(tab) })

watch(() => projectStore.workspaceRegistryRevision, () => {
  tabLoads.delete('workspaces')
  tabLoads.delete('workspace')
  workspaceListLoad = null
  if (currentTab.value === 'workspaces') void loadSettingsTab('workspaces')
  else if (currentTab.value === 'workspace') void loadSettingsTab('workspace')
})

watch(() => projectStore.activeWorkspace, () => {
  // Asset refs hold one workspace's data, so invalidate cached scope keys:
  // returning to an earlier root must never show a different root's rows.
  for (const key of tabLoads.keys()) {
    if (key.includes(':')) tabLoads.delete(key)
  }
  if (['providers', 'skills', 'subagents', 'commands', 'mcp'].includes(currentTab.value)) {
    void loadSettingsTab(currentTab.value)
  } else if (currentTab.value === 'advanced') {
    // The advanced tab's health card reads the same workspace-scoped agent
    // assets, so a switch has to refresh them too. The advanced tab itself stays
    // unscoped: its other reads do not belong to a workspace.
    void fetchAgentAssets()
  }
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
  // Fail closed on unknown host state: with no status, restart_only reads as
  // false, and guessing deploy on a production Linux host would
  // snapshot/pull/rebuild an administrator-managed checkout. Re-check once;
  // if the host still cannot be identified, stop before touching either
  // endpoint.
  if (localStatus.value === null) {
    await fetchLocalStatus()
    if (localStatus.value === null) {
      actionResult.value = 'Could not determine the server type. Reload Settings and try again.'
      return
    }
  }
  const restartOnly = !!localStatus.value?.restart_only
  const confirmation = restartOnly
    ? 'Restart the installed server? Active chats will finish before it restarts.'
    : 'Restart? This will pull latest, rebuild, and restart.'
  let cardNote = ''
  try {
    cardNote = describeInterruptedCards((await api.get<{ restart_blockers: RestartBlocker[] }>('/api/admin/restart-blockers')).restart_blockers || [])
  } catch { /* best effort: confirmation text falls back */ }
  if (!confirmWarnings && !await askConfirm(cardNote ? `${confirmation}\n\n${cardNote}` : confirmation, {
    title: restartOnly ? 'Restart server' : 'Restart and redeploy',
    confirmLabel: 'Restart',
  })) return
  actionPending.value = 'deploy'
  actionResult.value = ''
  deploySteps.value = []
  try {
    const endpoint = restartOnly ? '/api/admin/restart' : '/api/admin/deploy'
    const r = await api.post<DeployResult>(endpoint, { confirm_warnings: confirmWarnings })
    deploySteps.value = r.steps
    if (r.ok) {
      actionResult.value = 'Restart complete. Waiting for server to come back, then reloading...'
      projectStore.beginServerRestart(restartOnly ? 'Waiting for active chats, then restarting Ciaobot…' : 'Deploy complete. Restarting Ciaobot…')
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
    if ((await projectStore.switchWorkspace(defaultWorkspace)) === false) return
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
      if ((await projectStore.switchWorkspace(defaultWorkspace)) === false) return
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

// Developer mode is a setting now; the Debug card reads it from the local
// status, so a toggle refreshes that rather than waiting for a reload.
watch(
  () => routines.value?.dev_mode,
  (current, previous) => {
    if (previous !== undefined && current !== previous) void fetchLocalStatus()
  },
)

async function fetchLocalStatus() {
  try {
    localStatus.value = await api.get<LocalStatus>('/api/local/status')
  } catch {
    /* leave null on failure */
  }
}

// ── PWA password (Settings → home) ─────────────────────────────────────
interface AuthSettings {
  password_configured: boolean
  on_host?: boolean
}

const authSettings = ref<AuthSettings | null>(null)
const authCurrentPassword = ref('')
const authCurrentPasswordError = ref('')
watch(authCurrentPassword, () => { authCurrentPasswordError.value = '' })
const authNewPassword = ref('')
const authSettingsSaving = ref(false)
const authSettingsResult = ref('')
const authSettingsError = ref(false)

// Protection is always on, so this card only changes the password. The server
// asks for the current one except from a browser on the computer running
// Ciaobot, which reports `on_host`.
const canSaveAuthSettings = computed(() => {
  if (!authSettings.value) return false
  if (!authNewPassword.value.trim()) return false
  if (!authSettings.value.on_host && !authCurrentPassword.value) return false
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
  authCurrentPasswordError.value = ''
  try {
    const body: Record<string, string> = { password: authNewPassword.value }
    if (!authSettings.value.on_host) body.current_password = authCurrentPassword.value
    const res = await api.post<AuthSettings & { ok?: boolean }>('/api/auth/settings', body)
    authSettings.value = {
      ...authSettings.value,
      password_configured: res.password_configured,
    }
    authCurrentPassword.value = ''
    authNewPassword.value = ''
    authSettingsResult.value = 'Password saved. Other devices have to log in again.'
  } catch (e) {
    const code = (e as { payload?: { code?: unknown } } | null)?.payload?.code
    if (code === 'wrong_current_password' || code === 'current_password_required' || code === 'rate_limited') {
      authCurrentPasswordError.value = code === 'wrong_current_password'
        ? 'Current password is incorrect.'
        : code === 'rate_limited'
          ? apiErrorMessage(e, 'Too many wrong attempts. Wait a few minutes and try again.')
          : 'Enter your current password.'
      authSettingsSaving.value = false
      await nextTick()
      document.getElementById('auth-current-password')?.focus()
      return
    }
    authSettingsError.value = true
    authSettingsResult.value = apiErrorMessage(e, 'Could not save password settings')
  }
  authSettingsSaving.value = false
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

// The memory-backup panel holds no router, so this view opens its setup chat.
function openBackupSetupChat(chatId: string) {
  if (chatId) router.push(`/chat/${chatId}`)
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

// ── Engine update job (#608) ───────────────────────────────────────────────────
// The persisted operation record is the only source of truth here. The card
// keeps no local guess about how far a run got, so an engine restarted
// mid-apply — or a Settings tab opened after the fact — shows what the record
// says rather than what this session remembers doing.
const updateStatus = ref<EngineUpdateStatus | null>(null)
const updatePolling = ref(false)
const updateActionPending = ref(false)
// 2s. Staging is a download plus a wheel build, the apply tens of seconds more;
// faster buys nothing a reader can use, only request volume.
const UPDATE_POLL_MS = 2000
let updatePollTimer: number | null = null
// A 202 says the coordinator owns the run; only the record says how far it got,
// and `stage_update` writes nothing until it has taken the lock, resolved the
// release over the network and removed any previous staged environment. For the
// first seconds of a run, then, the status route can only serve the record the
// card was already holding — the previous run's. `updateRunStarted` says this
// session asked for a run, `updateRunSeenInFlight` says it has since seen that
// run's own record in flight: together they are the difference between the
// previous run's outcome and this one's, and a poll that cannot tell them apart
// ends a run that is still going.
let updateRunStarted = false
// The restart overlay is requested once per run, when the record first shows
// the applying half. The server chains both halves, so the click that started
// the run is long past by then.
let updateRestartRequested = false
let updateRunSeenInFlight = false
let updateRunStartedAt = 0
// The refusal the card was already holding when it asked for this run. A parked
// reason lives on the server and is served for as long as the record it was
// parked against is unchanged, so the previous run's refusal is still on the
// wire during this run's pre-record window and is not this run's answer. Empty
// when the card held no refusal, which makes any served error this run's own.
let updateRunErrorAtStart = ''
// A ceiling on the pre-record window, never the end condition: a run that lands
// no record and refuses nothing must not leave the card polling forever, but a
// slow link, a cold DNS or a re-stage of a version whose environment is still on
// disk all have to be waited out rather than given up on.
const UPDATE_RUN_GRACE_MS = 60_000

const engineUpdateOperation = computed(() => updateStatus.value?.operation ?? null)
const engineUpdateStage = computed(() => updateStage(engineUpdateOperation.value))
// `applied` is the last run's outcome, not a job: nothing ever unlinks the
// record, so a machine that has updated once would answer every later release
// with "up to date" and never offer an update again. The release page and the
// installed version are read independently of it, so a newer release is
// exactly the signal that the record is history rather than the answer.
const engineUpdateStaleApplied = computed(
  () => engineUpdateStage.value === 'done' && !!packageStatus.value?.update_available,
)
const engineUpdateBusy = computed(() => updateInFlight(engineUpdateOperation.value))
const engineUpdateFailed = computed(() => updateFailed(engineUpdateOperation.value))
const engineUpdateRolledBack = computed(() => engineUpdateStage.value === 'rolled_back')
const engineUpdatePhaseText = computed(() => updatePhaseLabel(engineUpdateOperation.value))
const engineUpdateFailureText = computed(() => updateFailureText(engineUpdateOperation.value))
// Only an installer engine has an install to swap, so only it gets the job
// surface. Every other mode keeps the guidance the card has always shown.
const engineUpdateEnabled = computed(
  () => !!updateStatus.value && updateStatus.value.install_mode === 'installer' && updateStatus.value.can_update,
)
// The panel is for the states that need an action or an answer. Idle and up to
// date is a status, and the header already says so.
const engineUpdateVisible = computed(
  () => engineUpdateEnabled.value
    && (engineUpdateStage.value !== 'idle'
      || !!updateStatus.value?.error
      || !!packageStatus.value?.update_available),
)
const engineUpdateVersion = computed(
  () => engineUpdateOperation.value?.to_version || packageStatus.value?.latest_version,
)
// The compact Updates state: the installed version is known to be the newest,
// and nothing is staged, running, done or failed, so the section is only its
// title with an "Up to date" status. A version check that never answered, or
// that answered with a rate limit, has said nothing about the installed version,
// so it is never compact: "Up to date" over "Update check failed" would be the
// card asserting what it does not know. A `done` record is not compact either,
// so the applied outcome keeps its own line in the panel. Staging before its
// first record is written reads as idle, which is why an in-flight poll or
// action also keeps the full card.
const updatesCompact = computed(() => {
  const status = packageStatus.value
  if (!status || status.error || status.update_available) return false
  if (packageUpdating.value || updatePolling.value || updateActionPending.value) return false
  if (updateStatus.value?.error) return false
  if (engineUpdateEnabled.value) return engineUpdateStage.value === 'idle'
  return true
})

async function fetchUpdateStatus() {
  try {
    updateStatus.value = await api.get<EngineUpdateStatus>('/api/update/status')
    reconcileUpdatePoll()
  } catch {
    // best-effort, exactly like the package status: an engine too old to have
    // the route keeps the guidance it always had.
  }
}

/**
 * The poll's own end condition, checked after every read.
 *
 * What ends a poll is "nothing this run can still do", and a record the card
 * was already holding when it asked for the run is not that: the coordinator
 * writes its first record only after the lock, the release lookup and the
 * removal of any previous staged environment, so for the first seconds the
 * status route can only serve the previous run's outcome. Reading that as the
 * end of this run stopped the poll while the run went on staging invisibly,
 * leaving a settled-looking panel with a re-enabled button and nothing left
 * watching. So the record is only believed once this session has seen one in
 * flight.
 */
function reconcileUpdatePoll() {
  // A record in flight is a run that still moves on its own, and the first time
  // one is seen the card can trust the record from then on.
  if (engineUpdateBusy.value) {
    updateRunSeenInFlight = true
    return
  }
  const status = updateStatus.value
  if (!status) return
  // The job is between its halves (the record sits on the non-in-flight
  // `staged` phase) or before its first record: it is not over, so keep polling.
  if (status.running) return
  // A served error is this run's own answer only if it is not the one the card
  // was already holding when it asked: a parked reason is served for as long as
  // the record it was parked against is unchanged, so the previous run's refusal
  // is still on the wire during this run's pre-record window. Reading that as
  // the end of this run stopped the poll while it staged invisibly, behind a
  // panel showing the previous run's reason with a re-enabled button — and that
  // button's second click is a 409, because by then the run really is in flight.
  if (status.error && status.error !== updateRunErrorAtStart) {
    updateRunStarted = false
    updateRunSeenInFlight = false
    updateRunErrorAtStart = ''
    stopUpdatePoll()
    return
  }
  // Once the record has been seen in flight, a record that is no longer in
  // flight is that run's outcome and nothing will move it.
  if (updateRunSeenInFlight) {
    updateRunStarted = false
    updateRunSeenInFlight = false
    updateRunErrorAtStart = ''
    stopUpdatePoll()
    return
  }
  // Otherwise this session asked for a run whose record may not be on disk yet,
  // so keep polling. The grace bound is a ceiling on that window and nothing
  // more: past it, a run that has produced neither a record nor a refusal is
  // not going to, and the card would otherwise hold every button disabled until
  // the page reloaded.
  if (updateRunStarted && Date.now() - updateRunStartedAt >= UPDATE_RUN_GRACE_MS) {
    updateRunStarted = false
    updateRunErrorAtStart = ''
    stopUpdatePoll()
  }
}

/**
 * Record that a 202 has started a run the card is now watching.
 *
 * Only the two 202 paths call this, never `startUpdatePoll` — the tab watcher
 * starts polls too, and it is not the card asking for anything. The
 * seen-in-flight flag resets with it: a new run has to look for its own record,
 * so a flag left over from an earlier run must not end this one on its first
 * read. The refusal the card is holding is captured for the same reason: it is
 * the previous run's until a different one arrives. Neither flag is cleared by
 * `stopUpdatePoll`, so a Settings tab that is left and reopened during the
 * pre-record window keeps watching the run.
 */
function beginUpdateRun() {
  updateRunStarted = true
  updateRunStartedAt = Date.now()
  updateRunSeenInFlight = false
  updateRestartRequested = false
  updateRunErrorAtStart = updateStatus.value?.error || ''
}

function stopUpdatePoll() {
  if (updatePollTimer !== null) {
    window.clearInterval(updatePollTimer)
    updatePollTimer = null
  }
  updatePolling.value = false
}

function startUpdatePoll() {
  if (updatePollTimer !== null) return
  updatePolling.value = true
  updatePollTimer = window.setInterval(() => { void fetchUpdateStatus() }, UPDATE_POLL_MS)
}

async function doEngineUpdate() {
  // A run the coordinator already owns: the 202 has not become a record yet, so
  // the record alone cannot say the job is over. The backend refuses a second
  // run with a 409, and the button is already disabled; both say the same thing.
  if (updatePolling.value) return
  updateActionPending.value = true
  packageResult.value = ''
  try {
    await api.post('/api/update/start')
    // 202: the server owns both halves from here and the record is its progress.
    // Its first record is written only after the lock, the release lookup and
    // the removal of any previous staged environment, so the reads that follow
    // can still be serving the last run's outcome; `beginUpdateRun` is what
    // keeps the poll alive across them.
    startUpdatePoll()
    beginUpdateRun()
    packageResult.value = 'Updating. Ciaobot keeps running while the release downloads, then restarts once.'
  } catch (e) {
    // One action, one line. The 400/409 the route answers with is a reason to
    // read, not a stream of toasts.
    packageResult.value = `Could not update: ${apiErrorMessage(e, 'unknown error')}`
    await fetchUpdateStatus()
  } finally {
    updateActionPending.value = false
  }
}

// The apply drains and reboots the engine, so the restart overlay is the one a
// Settings restart already uses: it covers the downtime and reloads the tab onto
// the new version. A drain the engine gives up on cancels it through
// `server_restart_cancelled`, and the card re-reads the record. Only a run this
// session started asks for it, so a run begun on the CLI or another device does
// not reload this tab.
watch(engineUpdateStage, (stage) => {
  if (stage !== 'applying' || !updateRunStarted || updateRestartRequested) return
  updateRestartRequested = true
  restartAndReload('Updating Ciaobot… the engine will restart')
})

// Poll only while a run is in flight, and only on the tab that shows the card:
// a Settings tab left open on an idle engine must not poll forever. A run this
// session asked for counts as in flight for this purpose too: staging is not a
// takeover, so leaving the Updates tab mid-download and coming back has to find
// the run still being watched rather than a card frozen on the last record.
watch([currentTab, engineUpdateBusy], ([tab, busy]) => {
  if (tab === 'home' && (busy || updateRunStarted)) startUpdatePoll()
  else stopUpdatePoll()
}, { immediate: true })

// The overlay is the whole window while it is up, so it is modal: focus moves
// into its status region, the Settings behind it go inert so Tab cannot reach
// Restart or Deploy under the overlay, and the card takes focus back when the
// record settles.
const engineUpdateOverlay = ref<HTMLElement | null>(null)
const engineUpdateOverlayView = ref<InstanceType<typeof UpdateProgressView> | null>(null)
const engineUpdatePanel = ref<HTMLElement | null>(null)
const engineUpdateInlineView = ref<InstanceType<typeof UpdateProgressView> | null>(null)
// The two halves of a run are not the same thing to the app, and `useModalFocus`
// makes the background inert up to `document.body`, so which one is up decides
// whether the whole app is locked while it runs.
//
// Staging (`resolving|downloading|verifying|staging`) is a release lookup, a
// download and a wheel build, and the engine keeps serving through all of it —
// the card's own copy says so. Taking the window over for minutes of that is
// the plan contradicting itself, so staging is the card's business: the same
// progress rows rendered inline below, with nothing inert anywhere.
//
// Applying (`draining|applying|stopping|swapping|starting|verifying_start`, and
// the `rolling_back` that answers a failed swap) is the engine replacing itself.
// It goes down, the connection drops, and nothing comes back until the new one
// is up, so there the takeover is exactly right and the copy stays.
const engineUpdateStagingNow = computed(() => engineUpdateStage.value === 'staging')
const engineUpdateOverlayOpen = computed(
  () => engineUpdateBusy.value && !engineUpdateStagingNow.value && engineUpdateEnabled.value,
)
// The overlay's own status region, not the inert boundary: a focus target with
// a visible ring (DESIGN) beats an invisible full-window box.
const engineUpdateStatusRegion = computed(() => engineUpdateOverlayView.value?.statusRegion ?? null)
// The in-card region's own focus target, for the same reason on the staging half.
const engineUpdateInlineStatusRegion = computed(() => engineUpdateInlineView.value?.statusRegion ?? null)

useModalFocus(engineUpdateOverlay, engineUpdateOverlayOpen, {
  initialFocus: engineUpdateStatusRegion,
  // The card owns the way back, below: the control that started the run is
  // usually replaced by its outcome, so the captured opener is often gone.
  restoreFocus: false,
  // Escape is claimed and does nothing on purpose: the run under this overlay is
  // the engine replacing itself, and no key leaves it running. Letting the key
  // through would hand it to a global shortcut, which is worse than swallowing
  // it.
  onEscape: () => {
    // Intentionally inert.
  },
})

// Staging takes over nothing, but the control that started it is replaced by the
// progress that replaced it, and focus left on nothing at all is worse than
// focus on the region that now describes the run. Nothing is inert here: this is
// a move into the card, not a claim on the window, and Tab leaves it for the
// rest of the app like any other. Only a run this session asked for counts — a
// run that started on the CLI or on another device must not pull focus out of
// wherever this tab was left — and only when that control's focus is what the
// replacement took with it.
watch(engineUpdateStagingNow, async (staging) => {
  if (!staging || !updateRunStarted) return
  await nextTick()
  const active = document.activeElement
  if (active && active !== document.body && !engineUpdatePanel.value?.contains(active)) return
  engineUpdateInlineStatusRegion.value?.focus()
})

// Focus comes back to the card when the overlay closes. What the run produced is
// what the card now offers — Update & restart for a staged release, Retry for a failure —
// so that is where focus goes: never onto a control the record has replaced,
// and never onto nothing at all. An `applied` record offers no control, so the
// panel itself is the target — it carries `tabindex="-1"` and states its own
// focus ring below, because a region has no ring of its own.
watch(engineUpdateOverlayOpen, async (open) => {
  if (open) return
  await nextTick()
  focusEngineUpdatePanel()
})

function focusEngineUpdatePanel() {
  const panel = engineUpdatePanel.value
  if (!panel) return
  const control = panel.querySelector<HTMLElement>('button:not([disabled])')
  const target = control ?? panel
  target.focus()
}

/**
 * Focus that fell on the document is not focus anyone put there.
 *
 * A re-render that removes the focused control drops focus on `body`, and the
 * card can do that to itself: a record that settles on `applied` re-reads the
 * version check, and a machine now on the release that check named has no
 * control left in the panel — so the control the focus return just landed on is
 * gone by the next answer. Focus the user moved themselves is left alone.
 */
function reclaimEngineUpdatePanelFocus() {
  const active = document.activeElement
  if (active && active !== document.body) return
  focusEngineUpdatePanel()
}

// Applied: the record says the new version is running, so the package status is
// re-read too and its "Up to date" is the engine's own answer.
watch(() => engineUpdateStage.value, (stage, previous) => {
  // "Updating…" describes a run that is in flight. Once the record
  // settles it is the last thing that happened rather than the state, and would
  // otherwise sit under the panel that replaced it.
  if (updateStageInFlight(previous) && !updateStageInFlight(stage)) packageResult.value = ''
  if (stage !== 'done' || previous === 'done') return
  void (async () => {
    await Promise.all([fetchPackageStatus(), fetchUpdateStatus()])
    // Those reads can replace the control the focus return landed on with a
    // panel that has none, which is focus falling on the document rather than
    // anywhere. The card takes it back; see `reclaimEngineUpdatePanelFocus`.
    reclaimEngineUpdatePanelFocus()
  })()
})

onUnmounted(stopUpdatePoll)

</script>

<!-- Styles the extracted Settings panels share with this view. Scoped rules
     do not cross into a child component, so anything a child needs lives in
     the shared sheet and both sides pull it in. It loads first, which keeps
     every rule that used to follow one of these still following it. -->
<style scoped src="./settings/settingsPanels.css"></style>

<style scoped>
.settings-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  container-type: inline-size;
}

/* The engine update's inert boundary: an empty full-window layer the overlay
   paints into, so the Settings behind it can be made inert while it is up. */
.engine-update-overlay {
  position: fixed;
  inset: 0;
}

/* The card's own focus target, for the run that leaves it with nothing to
   click: the app's focus ring (DESIGN: 2px accent) does not reach a region, so
   the panel states one exactly as the overlay's status region does. */
.engine-update-panel:focus {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.shortcut-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}

.shortcut-list li {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 40px;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
  color: var(--fg);
}

.shortcut-list kbd {
  flex: 0 0 150px;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--fg2);
}

@media (max-width: 700px) {
  .shortcut-list kbd { flex-basis: 72px; }
}
.pane-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-6) 0 48px;
}

/* Sections stack in the page grid's main column with generous separation;
   the rail holds the tab's table of contents. */
.settings-main {
  display: flex;
  flex-direction: column;
  gap: 36px;
}

.settings-toc {
  display: grid;
  border-left: 1px solid var(--border);
}

.settings-toc-item {
  min-height: 32px;
  margin-left: -1px;
  padding: 4px 0 4px 12px;
  border: 0;
  border-left: 2px solid transparent;
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
}

.settings-toc-item:hover {
  color: var(--fg);
}

.settings-toc-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.settings-guide-link {
  display: inline-flex;
  align-items: center;
  min-height: var(--touch);
  font-size: var(--text-sm);
}

.settings-toc-item.active {
  border-left-color: var(--accent);
  color: var(--fg);
}

@media (pointer: coarse) {
  .settings-toc-item { min-height: var(--touch); }
}

/* One button size across every settings section: the standard secondary
   control. Header actions used to mix 44px primaries with compact chips,
   which is what made each card read as its own little app. */
.settings-main :deep(:is(.btn-primary, .btn-secondary, .btn-caution, .btn-small)) {
  min-height: 34px;
  padding: 0 12px;
  border-radius: 8px;
  font-size: var(--text-sm);
  font-weight: 600;
  line-height: 1.2;
}

@media (pointer: coarse) {
  .settings-main :deep(:is(.btn-primary, .btn-secondary, .btn-caution, .btn-small)) {
    min-height: var(--touch);
  }
}

/* Connection state as text with a leading dot, not a mono pill. */
.settings-conn-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: none;
  color: var(--fg2);
  font-size: var(--text-sm);
  white-space: nowrap;
}
.settings-conn-state::before {
  content: '';
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}
.settings-conn-state--ok::before { background: var(--success); }
.settings-conn-state--error { color: var(--error); }

.settings-main :deep(:is(.btn-primary, .btn-secondary, .btn-caution, .btn-small)) {
  white-space: nowrap;
}

.settings-status {
  color: var(--fg2);
  font-size: var(--text-sm);
  white-space: nowrap;
}

/* The rail here is navigation, not context, so it outlasts the shared
   page grid's 940px collapse: a compact column down to a 760px pane (a
   1280px window with the widest sidebar is 780px), then gone — a table of
   contents under the page it indexes would only be something to scroll
   past. */
@container chat-pane (max-width: 940px) {
  .settings-grid { grid-template-columns: minmax(0, 1fr) 200px; gap: var(--space-6); }
  .settings-rail { position: sticky; }
  .settings-toc-item { white-space: normal; }
}
@container chat-pane (max-width: 760px) {
  .settings-grid { grid-template-columns: minmax(0, 1fr); }
  .settings-rail { display: none; }
}
/* Open-source card with the star ask still live: the pixel face sits on the
   right, out of the prose's flow. Without the nudge the card has no class and
   no image renders, so plain cards are untouched. */
.open-source-card {
  position: relative;
}

.open-source-face {
  position: absolute;
  right: var(--space-4);
  top: var(--space-4);
  width: 64px;
  height: 64px;
  image-rendering: pixelated;
  -webkit-user-drag: none;
  opacity: 0.9;
  pointer-events: none;
}

/* Keep the title clear of the face on narrow cards too. */
.open-source-card--with-face .settings-card-header {
  padding-right: 84px;
}

.open-source-actions {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-top: var(--space-2);
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

.action-row--spaced {
  margin-top: var(--space-3);
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
/* The global mobile rule gives .btn-small a 44px target; the compact caution
   variant above resets min-height and would otherwise drop below it. */
@media (max-width: 768px) {
  .btn-caution.btn-small { min-height: var(--touch); }
}
.btn-caution:hover { background: color-mix(in srgb, var(--warning) 15%, var(--bg3)); }
.btn-secondary:active,
.btn-caution:active { transform: scale(0.98); }
.btn-secondary:disabled,
.btn-caution:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

/* Router links used as buttons in card headers (e.g. "Open device settings"). */
a.btn-secondary {
  display: inline-flex;
  align-items: center;
  text-decoration: none;
}
.action-result--error {
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

.workspace-move-row {
  margin-top: var(--space-3);
}
.workspace-move-outcome {
  margin-top: var(--space-3);
}
.workspace-move-outcome--error {
  color: var(--error);
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
.setting-row--flush {
  border-top: 0;
  padding-top: 0;
}
.setting-row--stack {
  margin-top: 0;
}
.provider-connections {
  display: flex;
  flex-direction: column;
}
.provider-connection-actions {
  justify-content: flex-start;
  gap: var(--space-2);
}
.provider-release-check { display: flex; flex-direction: column; gap: var(--space-2); }
.provider-release-note { margin: 0; }
.provider-release-command {
  display: block;
  min-width: 0;
  overflow-wrap: anywhere;
  font-family: var(--font-mono);
}
.provider-release-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.provider-release-failed { color: var(--fg3); }
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
.gws-account-add {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) 0;
  border-bottom: 1px dashed var(--border);
  margin-bottom: var(--space-3);
}
@container (max-width: 720px) {
  .pane-body {
    padding: var(--space-3);
  }
  .action-row--compact > button {
    flex: 1 1 100%;
    width: 100%;
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

.skill-list--section {
  margin-bottom: var(--space-4);
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
/* The provider's defaults are indented hairline rows (.set-subrow) under its
   head, not a boxed grid of selects. */
.provider-inline-defaults {
  display: block;
}
@media (max-width: 720px) {
  .provider-defaults {
    grid-template-columns: 1fr;
  }
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
/* Touch: the 20px glyph stays, inside a 44px hit area. */
@media (pointer: coarse) {
  .field-info summary {
    width: var(--touch);
    height: var(--touch);
    border: 0;
    background: none;
    position: relative;
    isolation: isolate;
  }
  .field-info[open] summary,
  .field-info summary:hover {
    border: 0;
    background: none;
  }
  /* The panel anchors to its label row, not to the glyph, so it opens inside the
     viewport wherever the glyph sits. Doubled class: the base .field-info-panel
     rule comes later in the file. */
  .settings-label-row { position: relative; }
  .field-info { position: static; }
  .field-info .field-info-panel {
    top: calc(100% - 6px);
    width: min(360px, 100%);
  }
  .field-info summary::before {
    content: '';
    position: absolute;
    inset: 12px;
    z-index: -1;
    border: 1px solid var(--border);
    border-radius: 50%;
    background: var(--bg);
  }
  .field-info[open] summary::before,
  .field-info summary:hover::before {
    border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
    background: color-mix(in srgb, var(--accent) 8%, var(--bg));
  }
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
.skill-source {
  color: var(--fg2);
  opacity: 0.7;
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
.font-scale-row {
  justify-content: flex-end;
}
.font-scale-row .btn-small {
  flex: 0 0 auto;
}
.font-scale-display {
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--fg);
  flex: 0 0 56px;
  text-align: center;
}

/* "What this CLI brings": the per-provider MCP and skill chip lists, folded
   away by default so the providers tab stays scannable. The summary is the whole
   touch target, so it keeps the 44px minimum. */
.provider-brings > summary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: var(--touch);
  color: var(--fg2);
  font-size: var(--text-sm);
  cursor: pointer;
  list-style: none;
}
.provider-brings > summary::-webkit-details-marker { display: none; }
/* inline-flex drops the native marker, so draw the disclosure caret. */
.provider-brings > summary::before {
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
.provider-brings[open] > summary::before { transform: rotate(45deg); }
@media (prefers-reduced-motion: reduce) {
  .provider-brings > summary::before { transition: none; }
}
/* /settings/providers scrolls here; keep the card clear of the pane header. */
#chat-providers, #install-app, #other-devices { scroll-margin-top: var(--space-4); }
.provider-brings > summary:hover { color: var(--fg); }
.provider-brings > summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
.provider-brings[open] > .provider-mcps-preview { margin-top: var(--space-1); }

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


</style>
