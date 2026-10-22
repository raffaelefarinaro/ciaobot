<template>
  <div v-if="!workspacesLoaded" class="card"><SkeletonLoader label="Loading workspaces" :count="3" /></div>
  <div v-else-if="workspacesError" class="card"><p class="hint hint--warn">{{ workspacesError }}</p></div>
  <div v-else class="card">
    <div class="settings-card-header settings-card-header--split">
      <div>
        <p class="section-title">Workspaces</p>
        <p class="hint">
          Each workspace keeps its own projects, chats, memory, model defaults and Google account.
        </p>
      </div>
      <button class="btn-small" :aria-expanded="showNewWorkspace" @click="showNewWorkspace = !showNewWorkspace">
        {{ showNewWorkspace ? 'Cancel' : 'New workspace' }}
      </button>
    </div>

    <div v-if="showNewWorkspace" id="new-workspace" class="workspace-card workspace-card--new settings-form-panel">
      <div class="workspace-card-header">
        <div>
          <p class="workspace-title">New workspace</p>
          <p class="hint hint--compact">
            Saved to <code>.runtime/workspaces.json</code> and applied immediately. Google and claude.ai connectors are set afterwards in the new workspace's Overview.
          </p>
        </div>
      </div>
      <div class="settings-field-grid">
        <label class="settings-field"><span class="ws-label">Name</span>
          <input class="routine-input" v-model="newWorkspaceForm.name" :disabled="workspacesSaving === 'new'" placeholder="letters, numbers, dashes, underscores" />
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
        <label class="settings-field"><span class="ws-label">File access</span>
          <select class="routine-input workspace-select" v-model="newWorkspaceForm.agent_fs_scope" :disabled="workspacesSaving === 'new'">
            <option value="">{{ fsScopeDefaultOptionLabel }}</option>
            <option value="workspace">Workspace only</option>
            <option value="machine">Whole machine</option>
          </select>
          <span class="hint hint--compact">{{ WORKSPACE_FS_SCOPE_COPY[newWorkspaceForm.agent_fs_scope] }} {{ WORKSPACE_FS_SANDBOX_NOTE }}</span>
        </label>
      </div>
      <div class="action-row settings-actions">
        <button class="set-link set-link--quiet" type="button" :disabled="workspacesSaving === 'new'" @click="showNewWorkspace = false">Cancel</button>
        <button class="btn-primary" @click="createNewWorkspace" :disabled="workspacesSaving === 'new'">
          {{ workspacesSaving === 'new' ? 'Creating...' : 'Create and open' }}
        </button>
      </div>
    </div>

    <p v-if="workspaceLinkStatus" class="hint hint--warn workspace-link-status" role="status">
      {{ workspaceLinkStatus }}
    </p>

    <div class="workspace-list">
      <div
        v-for="form in workspaceForms"
        :key="form.name"
        :id="`workspace-row-${form.name}`"
        class="workspace-card workspace-row"
      >
        <WorkspaceTile :name="form.name" />
        <div class="workspace-row-main">
          <div class="workspace-row-heading">
            <p class="workspace-title">{{ form.name }}</p>
            <span v-if="form.name === primaryWorkspace" class="set-tag">Main workspace</span>
          </div>
          <p class="set-row-sub">{{ workspaceSummary(form) }}</p>
        </div>
        <div class="workspace-actions set-row-actions">
          <button
            type="button"
            class="set-link"
            :aria-label="`Settings for workspace ${form.name}`"
            @click="openWorkspaceSettings(form.name)"
          >Settings &rarr;</button>
          <DropdownMenuRoot
            :open="settingsMenu === `ws:${form.name}`"
            :modal="false"
            @update:open="setSettingsMenu(`ws:${form.name}`, $event)"
          >
            <DropdownMenuTrigger as-child>
              <button
                type="button"
                class="set-menu-btn"
                :aria-label="`Actions for workspace ${form.name}`"
                :disabled="archivingWorkspace === form.name"
              >&middot;&middot;&middot;</button>
            </DropdownMenuTrigger>
            <DropdownMenuContent as-child align="end" :side-offset="4" :collision-padding="8">
              <div class="settings-menu" @keydown.esc="closeSettingsMenuOnEscape">
                <DropdownMenuItem as-child @select="openWorkspaceSettings(form.name, '#workspace-name')">
                  <button
                    type="button"
                    class="settings-menu-item"
                    :aria-label="`Rename workspace ${form.name}`"
                  >Rename</button>
                </DropdownMenuItem>
                <DropdownMenuItem v-if="workspaceArchivable(form.name)" as-child @select="archiveWorkspace(form.name)">
                  <button
                    type="button"
                    class="settings-menu-item settings-menu-item--caution"
                    :aria-label="`Archive workspace ${form.name}`"
                  >{{ archivingWorkspace === form.name ? 'Archiving...' : 'Archive' }}</button>
                </DropdownMenuItem>
              </div>
            </DropdownMenuContent>
          </DropdownMenuRoot>
        </div>
      </div>
    </div>

    <div v-if="workspacesResult" class="action-result action-result--error" role="alert">{{ workspacesResult }}</div>

    <section class="archived-workspaces" aria-labelledby="archived-workspaces-title">
      <p id="archived-workspaces-title" class="subsection-title">Archived workspaces</p>
      <p class="hint hint--compact">
        Archived workspaces keep every file in <code>.archived-workspaces/</code> in the Ciaobot folder. Ciaobot does not read their notes or memory until you restore them.
      </p>
      <p v-if="archivedLoading && !archivedWorkspaces.length" class="hint">Loading&hellip;</p>
      <div v-else-if="archivedLoadError" class="action-result action-result--error" role="alert">
        {{ archivedLoadError }}
        <button class="btn-small" type="button" @click="fetchArchivedWorkspaces">Retry</button>
      </div>
      <p v-else-if="!archivedWorkspaces.length" class="hint archived-empty">No archived workspaces.</p>
      <ul v-if="archivedWorkspaces.length" class="archived-list">
        <li
          v-for="item in archivedWorkspaces"
          :key="item.id"
          class="archived-item"
        >
          <div class="archived-item-text">
            <p class="workspace-title">{{ item.name }}</p>
            <p class="hint hint--compact">
              Archived {{ formatArchivedAt(item.archived_at) }} &middot; <code>{{ item.path }}</code>
            </p>
            <p v-if="!item.restorable && item.blocked_reason" class="hint hint--warn hint--compact">
              {{ item.blocked_reason }}
            </p>
          </div>
          <button
            class="btn-small"
            type="button"
            :aria-label="`Restore workspace ${item.name}`"
            :disabled="!item.restorable || restoringArchiveId === item.id"
            @click="restoreWorkspace(item)"
          >{{ restoringArchiveId === item.id ? 'Restoring...' : 'Restore' }}</button>
        </li>
      </ul>
    </section>
  </div>
</template>

<script setup lang="ts">
import { inject } from 'vue'
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRoot,
  DropdownMenuTrigger,
} from 'reka-ui'
import SkeletonLoader from '../SkeletonLoader.vue'
import WorkspaceTile from '../WorkspaceTile.vue'
import { WORKSPACE_COLOR_PRESETS } from '../../lib/workspaceColors'
import {
  WORKSPACE_ADMIN_KEY,
  WORKSPACE_FS_SANDBOX_NOTE,
  WORKSPACE_FS_SCOPE_COPY,
} from '../../composables/useWorkspaceAdmin'

// Manage workspaces (/settings/workspaces, reached from the workspace
// switcher): every workspace as a boxed row, the short New workspace form and
// the Archived list. Editing happens on a workspace's Overview. The state
// belongs to SettingsView's useWorkspaceAdmin, which also provides the shared
// row-menu helpers.
const admin = inject(WORKSPACE_ADMIN_KEY)
if (!admin) throw new Error('SettingsManageWorkspaces needs the workspace admin SettingsView provides')

const {
  workspacesLoaded,
  workspacesError,
  workspacesSaving,
  workspacesResult,
  archivingWorkspace,
  primaryWorkspace,
  archivedWorkspaces,
  archivedLoading,
  archivedLoadError,
  restoringArchiveId,
  showNewWorkspace,
  workspaceForms,
  newWorkspaceForm,
  workspaceLinkStatus,
  fsScopeDefaultOptionLabel,
  workspaceProviderFieldLabel,
  workspaceProviderOptions,
  settingsMenu,
  setSettingsMenu,
  closeSettingsMenuOnEscape,
  workspaceSummary,
  workspaceArchivable,
  openWorkspaceSettings,
  createNewWorkspace,
  archiveWorkspace,
  fetchArchivedWorkspaces,
  formatArchivedAt,
  restoreWorkspace,
} = admin
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.workspace-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
/* One workspace = one boxed row: tile, name and summary, then its actions. */
.workspace-row {
  flex-direction: row;
  align-items: center;
  gap: var(--space-3);
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 10px;
}
.workspace-row-main {
  flex: 1 1 auto;
  min-width: 0;
}
.workspace-row-heading {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.workspace-row .set-row-sub {
  overflow-wrap: anywhere;
}
.workspace-card {
  display: flex;
}
.workspace-link-status {
  margin: var(--space-2) 0;
}
/* The unsaved new workspace keeps a light frame: it is a draft form, and it
   has to read as not-yet-real next to the saved rows. */
.workspace-card--new {
  flex-direction: column;
  gap: var(--space-3);
}
.workspace-card-header {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
}
.workspace-title {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-base);
  font-weight: 600;
}
.workspace-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 0 0 auto;
}
.archived-workspaces {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-4);
  padding-top: var(--space-3);
  border-top: 1px solid var(--border);
}
.archived-workspaces .subsection-title {
  margin: 0;
}
.archived-list {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
  border-top: 1px solid var(--border);
}
.archived-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  min-height: 52px;
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--border);
}
.archived-item-text {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}
.archived-item-text p {
  margin: 0;
}
.archived-item-text code {
  overflow-wrap: anywhere;
}
.archived-item .btn-small {
  flex: 0 0 auto;
}
/* The reason is printed beside it; the dimming only confirms it. */
.archived-item .btn-small:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  transform: none;
}
.action-result--error { color: var(--error); }
@media (max-width: 520px) {
  .workspace-row { flex-wrap: wrap; }
  .workspace-row-main { flex-basis: calc(100% - 22px - var(--space-3)); }
  .workspace-actions { margin-left: calc(22px + var(--space-3)); }
}
@container (max-width: 720px) {
  .archived-item {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
