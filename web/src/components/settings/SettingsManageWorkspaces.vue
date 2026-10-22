<template>
  <div v-if="!workspacesLoaded" class="card"><SkeletonLoader label="Loading workspaces" :count="3" /></div>
  <div v-else-if="workspacesError" class="card"><p class="hint hint--warn">{{ workspacesError }}</p></div>
  <div v-else class="card">
    <div class="sr-only" role="status" aria-live="polite">{{ announcement }}</div>
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
        v-for="(form, index) in orderedForms"
        :key="form.name"
        :id="`workspace-row-${form.name}`"
        class="workspace-card workspace-row"
        :class="{
          'workspace-row--drag-over': dragOverName === form.name,
          'workspace-row--dragging': dragName === form.name,
        }"
        @dragover="onRowDragOver(form.name, $event)"
        @drop="onRowDrop(form.name, $event)"
      >
        <span v-if="form.name === pinnedFirst" class="workspace-row-handle workspace-row-handle--spacer" aria-hidden="true"></span>
        <button
          v-else
          type="button"
          class="workspace-row-handle"
          draggable="true"
          :data-workspace-handle="form.name"
          :aria-label="`Reorder ${workspaceLabel(form.name)}. Alt plus arrow up or down moves it.`"
          @keydown="onHandleKeydown(form.name, $event)"
          @dragstart="onRowDragStart(form.name, $event)"
          @dragend="onRowDragEnd"
        ><span aria-hidden="true">&#8942;&#8942;</span></button>
        <WorkspaceTile :name="form.name" />
        <div class="workspace-row-main">
          <div class="workspace-row-heading">
            <p class="workspace-title">{{ form.name }}</p>
            <span v-if="form.name === primaryWorkspace" class="set-tag">Main workspace</span>
            <span v-if="index < 9" class="workspace-row-key">Key {{ index + 1 }}</span>
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
            <DropdownMenuContent
              as-child
              align="end"
              :side-offset="4"
              :collision-padding="8"
              @close-auto-focus="onMenuCloseAutoFocus"
            >
              <div class="settings-menu" @keydown.esc="closeSettingsMenuOnEscape">
                <DropdownMenuItem as-child @select="openWorkspaceSettings(form.name, '#workspace-name')">
                  <button
                    type="button"
                    class="settings-menu-item"
                    :aria-label="`Rename workspace ${form.name}`"
                  >Rename</button>
                </DropdownMenuItem>
                <DropdownMenuItem
                  v-if="form.name !== pinnedFirst"
                  as-child
                  :disabled="index === 0 || (pinnedFirst !== null && index === 1)"
                  @select="moveBy(form.name, -1, true)"
                >
                  <button
                    type="button"
                    class="settings-menu-item"
                    :aria-label="`Move workspace ${form.name} up`"
                  >Move up</button>
                </DropdownMenuItem>
                <DropdownMenuItem
                  v-if="form.name !== pinnedFirst"
                  as-child
                  :disabled="index === orderedForms.length - 1"
                  @select="moveBy(form.name, 1, true)"
                >
                  <button
                    type="button"
                    class="settings-menu-item"
                    :aria-label="`Move workspace ${form.name} down`"
                  >Move down</button>
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
import { computed, inject, nextTick, ref } from 'vue'
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRoot,
  DropdownMenuTrigger,
} from 'reka-ui'
import SkeletonLoader from '../SkeletonLoader.vue'
import WorkspaceTile from '../WorkspaceTile.vue'
import { WORKSPACE_COLOR_PRESETS } from '../../lib/workspaceColors'
import { workspaceLabel } from '../../lib/workspaceLabel'
import { moveAnnouncement, moveName } from '../../lib/workspaceOrder'
import { useProjectStore } from '../../stores/projects'
import {
  WORKSPACE_ADMIN_KEY,
  WORKSPACE_FS_SANDBOX_NOTE,
  WORKSPACE_FS_SCOPE_COPY,
} from '../../composables/useWorkspaceAdmin'

// Manage workspaces (/settings/workspaces, reached from the workspace
// switcher): every workspace as a boxed row, the short New workspace form and
// the Archived list. Rows reorder by drag, by Alt+Arrow on the row handle or
// from the row menu; the order is engine-wide and is the switcher order and
// the 1-9 shortcut mapping. Editing happens on a workspace's Overview. The state
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

const projectStore = useProjectStore()

// The rows follow the store's order (the persisted one); each row's draft form
// stays the same object when it moves.
const orderedForms = computed(() => {
  const position = new Map(projectStore.workspaceOptions.map((w, i) => [w.name, i]))
  const rank = (name: string) => position.get(name) ?? Number.MAX_SAFE_INTEGER
  return workspaceForms.value
    .map((form, i) => ({ form, i }))
    .sort((a, b) => rank(a.form.name) - rank(b.form.name) || a.i - b.i)
    .map(({ form }) => form)
})

// ── Reorder ──────────────────────────────────────────────────────────────
const announcement = ref('')
const dragName = ref<string | null>(null)
const dragOverName = ref<string | null>(null)
// Set while a menu move is in flight, so closing the menu does not take focus
// back to the ··· trigger instead of the moved row's handle.
const menuMoveName = ref<string | null>(null)
// Moves run one after another so a held Alt+Arrow computes each step from the
// order the previous one produced.
let moveQueue: Promise<void> = Promise.resolve()

function currentOrder(): string[] {
  return projectStore.workspaceOptions.map((w) => w.name)
}

function focusHandle(name: string): void {
  void nextTick(() => {
    document.querySelector<HTMLElement>(`[data-workspace-handle="${CSS.escape(name)}"]`)?.focus()
  })
}

// The main workspace is the first one unless a workspace is named `personal`;
// the server refuses an order that would change it, so that row is pinned.
const pinnedFirst = computed<string | null>(() => {
  const primary = primaryWorkspace.value
  if (!primary || primary === 'personal') return null
  return currentOrder()[0] === primary ? primary : null
})

const ORDER_ERROR_PREFIX = 'Could not save the workspace order'

// Queued so a held Alt+Arrow computes each step from the order the previous
// one left behind (or restored, when its save failed).
function enqueueMove(name: string, nextOrder: (names: string[]) => string[]): void {
  moveQueue = moveQueue.then(async () => {
    const names = currentOrder()
    const next = nextOrder(names)
    if (next.join('\u0000') === names.join('\u0000')) return
    try {
      const res = await projectStore.reorderWorkspaces(next)
      if (!res) return
      primaryWorkspace.value = res.primary ?? null
      if (workspacesResult.value.startsWith(ORDER_ERROR_PREFIX)) workspacesResult.value = ''
      announcement.value = moveAnnouncement(workspaceLabel(name), next.indexOf(name) + 1, next.length)
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e)
      workspacesResult.value = `${ORDER_ERROR_PREFIX}: ${detail}`
    }
  })
}

function moveBy(name: string, delta: -1 | 1, fromMenu = false): void {
  if (fromMenu) menuMoveName.value = name
  enqueueMove(name, (names) => {
    const from = names.indexOf(name)
    return moveName(names, from, from + delta, pinnedFirst.value)
  })
  void moveQueue.then(() => {
    focusHandle(name)
    menuMoveName.value = null
  })
}

function onMenuCloseAutoFocus(event: Event): void {
  if (menuMoveName.value) event.preventDefault()
}

function onHandleKeydown(name: string, event: KeyboardEvent): void {
  if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
  if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return
  event.preventDefault()
  moveBy(name, event.key === 'ArrowUp' ? -1 : 1)
}

function onRowDragStart(name: string, event: DragEvent): void {
  dragName.value = name
  const transfer = event.dataTransfer
  if (!transfer) return
  transfer.effectAllowed = 'move'
  // Firefox requires data to be set for the drag to start.
  transfer.setData('text/plain', name)
  const row = document.getElementById(`workspace-row-${name}`)
  if (row) transfer.setDragImage(row, 16, 16)
}

function onRowDragOver(name: string, event: DragEvent): void {
  if (!dragName.value) return
  event.preventDefault()
  dragOverName.value = dragName.value === name || name === pinnedFirst.value ? null : name
}

function onRowDrop(target: string, event: DragEvent): void {
  const dragged = dragName.value
  if (!dragged) return
  event.preventDefault()
  onRowDragEnd()
  if (dragged === target) return
  enqueueMove(dragged, (names) => {
    const from = names.indexOf(dragged)
    const to = names.indexOf(target)
    return moveName(names, from, to, pinnedFirst.value)
  })
}

function onRowDragEnd(): void {
  dragName.value = null
  dragOverName.value = null
}
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
.workspace-row { --handle: 34px; }
.workspace-row-main {
  flex: 1 1 auto;
  min-width: 0;
}
.workspace-row-handle {
  flex: 0 0 auto;
  width: var(--handle);
  height: var(--handle);
  display: inline-grid;
  place-items: center;
  margin: 0 calc(var(--space-2) * -1) 0 -8px;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1;
  letter-spacing: -2px;
  cursor: grab;
}
.workspace-row-handle--spacer { cursor: default; }
.workspace-row-handle:hover { background: var(--bg3); color: var(--fg); }
.workspace-row-handle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
  color: var(--fg);
}
.workspace-row--dragging { opacity: 0.4; }
.workspace-row--drag-over {
  box-shadow: inset 0 2px 0 0 var(--accent);
  background: var(--bg3);
}
.workspace-row-key {
  color: var(--fg3);
  font-size: var(--text-xs);
}
@media (pointer: coarse) {
  .workspace-row { --handle: var(--touch); }
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
  .workspace-row-main { flex-basis: calc(100% - var(--handle) - 22px - 2 * var(--space-3) + var(--space-2)); }
  .workspace-actions { margin-left: calc(var(--handle) + 22px + 2 * var(--space-3) - var(--space-2)); }
}
@container (max-width: 720px) {
  .archived-item {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
