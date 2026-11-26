<template>
  <p v-if="workspaceLinkStatus" class="hint hint--warn workspace-link-status" role="status">{{ workspaceLinkStatus }}</p>
  <div v-if="!workspacesLoaded" class="card"><SkeletonLoader label="Loading workspace" :count="3" /></div>
  <div v-else-if="workspacesError" class="card"><p class="hint hint--warn">{{ workspacesError }}</p></div>
  <template v-else-if="form">
    <div class="card">
      <div class="settings-card-header">
        <div class="overview-title">
          <WorkspaceTile :name="form.name" />
          <p class="section-title">Workspace</p>
        </div>
        <p class="hint">
          Name and colour for this workspace.
        </p>
      </div>
      <div class="overview-body">
        <label class="settings-field set-subrow">
          <span class="ws-label set-subrow-label">Name</span>
          <input
            id="workspace-name-input"
            class="routine-input set-subrow-control"
            v-model="workspaceNameDraft"
            :disabled="workspacesSaving === form.name"
            :aria-describedby="`workspace-name-hint-${form.name}`"
          />
        </label>
        <p :id="`workspace-name-hint-${form.name}`" class="hint hint--compact set-subrow-hint">Letters, numbers, dashes, underscores.</p>
        <div class="set-subrow">
          <span class="ws-label set-subrow-label" :id="`workspace-color-${form.name}`">Accent</span>
          <WorkspaceColorPicker
            v-model="form.color"
            class="set-subrow-control"
            :labelledby="`workspace-color-${form.name}`"
            :disabled="workspacesSaving === form.name"
          />
        </div>
      </div>
    </div>

    <SettingsClaudeTrim />

    <SettingsWorkspaceModels />

    <div v-if="workspacesResult" class="action-result action-result--error" role="alert">{{ workspacesResult }}</div>

    <SettingsSharedBrains />
    <SettingsUpdateTasks />

    <div v-if="workspaceArchivable(form.name)" class="card">
      <div class="settings-card-header">
        <p class="section-title">Archive this workspace</p>
        <p class="hint">
          Archiving removes it from the sidebar; nothing is deleted and you can restore it from Archived workspaces further down this page.
        </p>
      </div>
      <div class="action-row settings-actions">
        <button
          type="button"
          class="btn-small settings-menu-item--caution"
          :aria-label="`Archive workspace ${form.name}`"
          :disabled="archivingWorkspace === form.name"
          @click="archiveWorkspace(form.name)"
        >{{ archivingWorkspace === form.name ? 'Archiving...' : 'Archive workspace' }}</button>
      </div>
    </div>
  </template>
  <div v-else class="card"><p class="hint">This workspace is not in the registry.</p></div>
</template>

<script setup lang="ts">
import { inject } from 'vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import WorkspaceTile from '../WorkspaceTile.vue'
import SettingsClaudeTrim from './SettingsClaudeTrim.vue'
import SettingsWorkspaceModels from './SettingsWorkspaceModels.vue'
import SettingsSharedBrains from './SettingsSharedBrains.vue'
import SettingsUpdateTasks from './SettingsUpdateTasks.vue'
import WorkspaceColorPicker from './WorkspaceColorPicker.vue'
import { WORKSPACE_ADMIN_KEY } from '../../composables/useWorkspaceAdmin'

// Workspace > Overview: the page for the workspace the sidebar has active. The
// editor state belongs to SettingsView's useWorkspaceAdmin (one load, one set
// of drafts for every workspace page); this page only draws the active form.
const admin = inject(WORKSPACE_ADMIN_KEY)
if (!admin) throw new Error('SettingsWorkspaceOverview needs the workspace admin SettingsView provides')

const {
  workspacesLoaded,
  workspacesError,
  workspacesSaving,
  workspacesResult,
  workspaceLinkStatus,
  archivingWorkspace,
  workspaceNameDraft,
  activeWorkspaceForm: form,
  workspaceArchivable,
  archiveWorkspace,
} = admin
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.workspace-link-status { margin: var(--space-2) 0; }
.overview-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.overview-title .section-title { margin: 0; }
.overview-body .set-subrow,
.overview-body .set-subrow-hint { padding-left: 0; }
.action-result--error { color: var(--error-text); }
</style>
