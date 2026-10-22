<template>
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
          Name, colour and file access for this workspace.
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
          <div
            class="workspace-color-swatches set-subrow-control"
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
        <label
          class="settings-field set-subrow"
          :class="{ 'workspace-card--target': workspaceLinkTarget === form.name }"
          :id="`workspace-file-access-row-${form.name}`"
          @animationend.self="clearWorkspaceLinkHighlight(form.name)"
        >
          <span class="ws-label set-subrow-label">File access</span>
          <select
            :id="`workspace-file-access-${form.name}`"
            class="routine-input routine-select workspace-select set-subrow-control"
            v-model="form.agent_fs_scope"
            :disabled="workspacesSaving === form.name"
          >
            <option value="">{{ fsScopeDefaultOptionLabel }}</option>
            <option value="workspace">Workspace only</option>
            <option value="machine">Whole machine</option>
          </select>
        </label>
        <p class="hint hint--compact set-subrow-hint">{{ WORKSPACE_FS_SCOPE_COPY[form.agent_fs_scope] }} {{ WORKSPACE_FS_SANDBOX_NOTE }}</p>
        <div class="settings-switch-row set-subrow">
          <span class="ws-label set-subrow-label">claude.ai connectors</span>
          <button
            class="settings-switch"
            type="button"
            role="switch"
            :aria-checked="form.claude_ai_connectors"
            aria-label="claude.ai connectors"
            :disabled="workspacesSaving === form.name"
            @click="form.claude_ai_connectors = !form.claude_ai_connectors"
          >
            <span class="switch-word">{{ form.claude_ai_connectors ? 'On' : 'Off' }}</span>
            <span class="switch-track" aria-hidden="true"></span>
          </button>
        </div>
        <p class="hint hint--compact set-subrow-hint">Load your Claude account's connectors (Slack, Jira, …) in this workspace's Claude chats.</p>
      </div>
    </div>

    <SettingsWorkspaceModels />

    <div class="card">
      <div class="settings-card-header">
        <p class="section-title">Google account</p>
        <p :id="`workspace-gws-hint-${form.name}`" class="hint">
          Which Google account this workspace uses. Accounts are added on Manage workspaces under Google Workspace.
          Save changes applies to every card above it.
        </p>
      </div>
      <div class="overview-body">
        <label class="settings-field set-subrow">
          <span class="ws-label set-subrow-label">Google profile</span>
          <select
            class="routine-input routine-select workspace-select set-subrow-control"
            v-model="form.gws_profile"
            :disabled="workspacesSaving === form.name"
            :aria-describedby="`workspace-gws-hint-${form.name}`"
          >
            <option value="">No Google account</option>
            <option v-for="profile in gwsProfileOptions" :key="`${form.name}-gws-${profile.name}`" :value="profile.name">
              {{ profile.label }} ({{ profile.email || profile.name }})
            </option>
            <option v-if="workspaceCustomGwsProfile(form.gws_profile)" :value="form.gws_profile">
              Custom: {{ form.gws_profile }}
            </option>
          </select>
        </label>
        <div v-if="workspaceDirty(form)" class="workspace-edit-actions">
          <button
            type="button"
            class="btn-primary btn-small workspace-save"
            :disabled="workspacesSaving === form.name || archivingWorkspace === form.name"
            @click="saveWorkspace(form.name)"
          >{{ workspacesSaving === form.name ? 'Saving...' : 'Save changes' }}</button>
          <button
            type="button"
            class="btn-small workspace-discard"
            :disabled="workspacesSaving === form.name"
            @click="discardWorkspace(form.name)"
          >Discard</button>
        </div>
      </div>
    </div>

    <div v-if="workspacesResult" class="action-result action-result--error" role="alert">{{ workspacesResult }}</div>

    <SettingsMemoryHandover />
    <SettingsUpdateTasks />

    <div v-if="workspaceArchivable(form.name)" class="card">
      <div class="settings-card-header">
        <p class="section-title">Archive this workspace</p>
        <p class="hint">
          Archiving removes it from the sidebar; nothing is deleted and you can restore it from Manage workspaces.
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
import SettingsWorkspaceModels from './SettingsWorkspaceModels.vue'
import SettingsMemoryHandover from './SettingsMemoryHandover.vue'
import SettingsUpdateTasks from './SettingsUpdateTasks.vue'
import { WORKSPACE_COLOR_PRESETS } from '../../lib/workspaceColors'
import {
  WORKSPACE_ADMIN_KEY,
  WORKSPACE_FS_SANDBOX_NOTE,
  WORKSPACE_FS_SCOPE_COPY,
} from '../../composables/useWorkspaceAdmin'

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
  archivingWorkspace,
  workspaceNameDraft,
  workspaceLinkTarget,
  fsScopeDefaultOptionLabel,
  gwsProfileOptions,
  workspaceCustomGwsProfile,
  activeWorkspaceForm: form,
  clearWorkspaceLinkHighlight,
  workspaceDirty,
  discardWorkspace,
  saveWorkspace,
  workspaceArchivable,
  archiveWorkspace,
} = admin
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.overview-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.overview-title .section-title { margin: 0; }
.overview-body .set-subrow,
.overview-body .set-subrow-hint { padding-left: 0; }
.action-result--error { color: var(--error); }
</style>
