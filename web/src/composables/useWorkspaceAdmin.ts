import { computed, onScopeDispose, ref, watch, type ComputedRef, type InjectionKey } from 'vue'
import { apiErrorMessage, errorMessage } from '../lib/errorMessage'
import { askConfirm } from '../lib/confirm'
import { archiveConfirmMessage, restoreConfirmMessage, restoredMessage } from '../lib/workspaceArchive'
import { useProjectStore } from '../stores/projects'
import {
  DEFAULT_WORKSPACE_COLOR,
  WORKSPACE_COLOR_PRESETS,
  normalizeWorkspaceColor,
  type WorkspaceColorId,
} from '../lib/workspaceColors'
import type { ArchivedWorkspace, WorkspaceInfo, WorkspaceProvider } from '../lib/types'

// The per-workspace admin state behind Settings: the workspace registry as
// editable forms, the save/archive/restore actions, and the dirty tracking.
// SettingsView calls this once and provides it; the workspace pages inject it,
// so one load and one set of drafts serves every page that edits a workspace.

export type WorkspaceForm = {
  name: string
  vault_root: string
  default_provider: WorkspaceProvider
  gws_profile: string
  disallowed_tools: string
  // Whether the workspace's Claude chats load the claude.ai connectors.
  claude_ai_connectors: boolean
  color: WorkspaceColorId
  // '' is "no choice": the key is left out of a create, and an update sends
  // null, so the engine keeps or returns the workspace to its default.
  agent_fs_scope: FsScopeChoice
}

export type FsScopeChoice = 'workspace' | 'machine' | ''

// Copy for the File access select. The default's name comes from the engine's
// payload (default_agent_fs_scope), so the PWA never restates the platform rule.
export const WORKSPACE_FS_SCOPE_LABEL: Record<'workspace' | 'machine', string> = {
  workspace: 'Workspace only',
  machine: 'Whole machine',
}
export const WORKSPACE_FS_SCOPE_COPY: Record<FsScopeChoice, string> = {
  '': 'Uses the default this engine applies to workspaces with no File access choice.',
  workspace: 'Workspace only. The agent and its shell cannot read outside this workspace.',
  machine: 'Whole machine. The agent can read any file this user can read.',
}
export const WORKSPACE_FS_SANDBOX_NOTE = 'If the engine cannot sandbox, a workspace-only chat refuses to start until you choose whole machine.'

export type GwsProfileOption = { name: string; label: string; email: string }

export function useWorkspaceAdmin(deps: {
  notifySaved: (body: string, title?: string) => void
  notifyFailed: (title: string, detail: string) => void
  gwsProfileOptions: ComputedRef<GwsProfileOption[]>
  workspaceCustomGwsProfile: (profile: string) => boolean
  fetchGwsIntegration: () => Promise<void>
}) {
  const { notifySaved, notifyFailed, gwsProfileOptions, workspaceCustomGwsProfile, fetchGwsIntegration } = deps
  const projectStore = useProjectStore()
  const workspaceProviderFieldLabel = 'Agent CLI/Runtime'

  const workspacesLoaded = ref(false)
  const workspacesError = ref('')
  const workspacesSaving = ref<string | null>(null)
  const workspacesResult = ref('')
  const archivingWorkspace = ref<string | null>(null)
  const primaryWorkspace = ref<string | null>(null)
  const archivedWorkspaces = ref<ArchivedWorkspace[]>([])
  const archivedLoading = ref(false)
  const archivedLoadError = ref('')
  const restoringArchiveId = ref<string | null>(null)
  const showNewWorkspace = ref(false)

  // The engine's default for File access, from /api/workspaces. Null until the
  // list has loaded, when the default option carries no name yet.
  const fsScopeEngineDefault = ref<'workspace' | 'machine' | null>(null)
  const fsScopeDefaultOptionLabel = computed(() => {
    const label = fsScopeEngineDefault.value ? WORKSPACE_FS_SCOPE_LABEL[fsScopeEngineDefault.value] : ''
    return label ? `Default (${label})` : 'Default'
  })

  function defaultWorkspaceProvider(): WorkspaceProvider {
    return projectStore.workspaceProviderOptions[0]?.value || 'claude'
  }

  function blankWorkspaceForm(): WorkspaceForm {
    return {
      name: '',
      vault_root: '',
      default_provider: defaultWorkspaceProvider(),
      gws_profile: '',
      disallowed_tools: '',
      claude_ai_connectors: true,
      color: DEFAULT_WORKSPACE_COLOR,
      agent_fs_scope: '',
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

  // A workspace that never chose has no stored scope, so it reads as '' (the default).
  function fsScopeChoice(raw: unknown): FsScopeChoice {
    return raw === 'workspace' || raw === 'machine' ? raw : ''
  }

  function workspaceToForm(ws: WorkspaceInfo): WorkspaceForm {
    return {
      name: ws.name,
      vault_root: ws.vault_root || '',
      default_provider: normalizeWorkspaceProvider(ws.default_provider),
      gws_profile: ws.gws_profile || '',
      disallowed_tools: Array.isArray(ws.disallowed_tools) ? ws.disallowed_tools.join(', ') : '',
      claude_ai_connectors: ws.claude_ai_connectors !== false,
      color: normalizeWorkspaceColor(ws.color),
      agent_fs_scope: fsScopeChoice(ws.agent_fs_scope),
    }
  }

  const workspaceForms = ref<WorkspaceForm[]>([])
  const newWorkspaceForm = ref<WorkspaceForm>(blankWorkspaceForm())

  // The baseline is the saved form, so Save appears only once something
  // actually changed.
  const workspaceBaseline = ref<Record<string, string>>({})
  // The Name field of the active workspace. It is a draft kept beside
  // `form.name`, not bound to it: `form.name` is the form's identity (baseline,
  // key, PATCH path), and changing it mid-edit would re-key the card and lose focus.
  const workspaceNameDraft = ref('')

  // A File access link (`/settings/workspaces?workspace=NAME#file-access`)
  // switches the active workspace to NAME and focuses its File access control;
  // it never selects a value and never saves. This is the status shown when it
  // cannot: an unknown workspace, or unsaved changes the user chose to keep.
  const workspaceLinkStatus = ref('')
  // The row being pointed at, so the CSS animation has one source of truth.
  const workspaceLinkTarget = ref<string | null>(null)

  function clearWorkspaceLinkHighlight(name: string): void {
    if (workspaceLinkTarget.value === name) workspaceLinkTarget.value = null
  }

  function workspaceDirty(form: WorkspaceForm): boolean {
    const saved = workspaceBaseline.value[form.name]
    if (saved === undefined) return false
    if (form.name === projectStore.activeWorkspace && workspaceNameDraft.value.trim() !== form.name) return true
    return saved !== JSON.stringify(form)
  }

  function discardWorkspace(name: string): void {
    const saved = workspaceBaseline.value[name]
    const index = workspaceForms.value.findIndex((f) => f.name === name)
    if (saved !== undefined && index >= 0) {
      workspaceForms.value[index] = JSON.parse(saved) as WorkspaceForm
    }
    workspaceNameDraft.value = name
  }

  function workspaceSwatch(color: WorkspaceColorId): string {
    return WORKSPACE_COLOR_PRESETS.find((preset) => preset.id === color)?.swatch || 'var(--accent)'
  }

  // The File access a workspace actually gets: its own choice, else the engine's
  // default. Empty until the list has loaded, when the default is not yet known.
  function workspaceFsScopeBadge(choice: FsScopeChoice): string {
    const effective = choice || fsScopeEngineDefault.value
    return effective ? WORKSPACE_FS_SCOPE_LABEL[effective] : ''
  }

  function workspaceSummary(form: WorkspaceForm): string {
    const provider = workspaceProviderOptions.value.find((option) => option.value === form.default_provider)?.label
      || form.default_provider
    const profile = form.gws_profile
      ? gwsProfileOptions.value.find((option) => option.name === form.gws_profile)?.label || form.gws_profile
      : ''
    const parts = [provider, profile ? `Google: ${profile}` : 'No Google account']
    if (form.name === primaryWorkspace.value) parts.push('Main workspace')
    return parts.join(' · ')
  }

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
      const res = await projectStore.fetchWorkspaces()
      primaryWorkspace.value = res?.primary ?? null
      fsScopeEngineDefault.value = res?.default_agent_fs_scope ?? null
      workspaceForms.value = projectStore.workspaces.map(workspaceToForm)
      workspaceBaseline.value = Object.fromEntries(
        workspaceForms.value.map((form) => [form.name, JSON.stringify(form)]),
      )
      newWorkspaceForm.value.default_provider = normalizeWorkspaceProvider(newWorkspaceForm.value.default_provider)
    } catch (e) {
      workspacesError.value = `Failed to load workspaces: ${errorMessage(e)}`
    } finally {
      workspacesLoaded.value = true
    }
  }

  function renameConfirmMessage(from: string, to: string): string {
    const message = `Rename "${from}" to "${to}". Projects and automations keep their ids and follow the new name.`
    // Without a workspace named `personal`, the first one in the list is the
    // main workspace, and a rename keeps its place in the list (#1188).
    const first = workspaceForms.value[0]?.name
    if (from === 'personal' && first !== from) return `${message} "${first}" becomes the main workspace.`
    // A workspace named `personal` is the main one whatever its place in the list.
    if (to === 'personal' && (primaryWorkspace.value ?? first) !== from) return `${message} It becomes the main workspace.`
    return message
  }

  async function saveWorkspace(name: string) {
    const form = workspaceForms.value.find((f) => f.name === name)
    if (!form) return
    const target = workspaceNameDraft.value.trim()
    const renaming = target !== name
    // An empty name would otherwise be read by the server as "no rename" and
    // dropped silently, so refuse it here.
    if (renaming && !target) {
      workspacesResult.value = 'Enter a workspace name.'
      return
    }
    if (renaming && !await askConfirm(renameConfirmMessage(name, target), {
      title: 'Rename workspace',
      confirmLabel: 'Rename',
      destructive: false,
    })) return
    workspacesSaving.value = name
    workspacesResult.value = ''
    try {
      await projectStore.updateWorkspace(name, {
        // Only a changed name is sent; an unchanged save is the plain patch.
        ...(renaming ? { name: target } : {}),
        // Send what the form holds. Overwriting it with the workspace name
        // silently discarded any vault path the user had set, and reset an
        // adopted workspace's `memory-vault/<name>` root to a bare name.
        vault_root: form.vault_root.trim() || target,
        default_provider: form.default_provider,
        gws_profile: form.gws_profile,
        disallowed_tools: disallowedToolsPayload(form.disallowed_tools),
        claude_ai_connectors: form.claude_ai_connectors,
        color: form.color,
        // Sent only when File access was changed. Picking the default again goes
        // as null, which clears an earlier explicit choice.
        ...(form.agent_fs_scope !== fsScopeChoice(projectStore.workspaces.find(w => w.name === name)?.agent_fs_scope)
          ? { agent_fs_scope: form.agent_fs_scope || null }
          : {}),
      })
      notifySaved(renaming ? `Workspace "${name}" renamed to "${target}".` : `Workspace "${name}" saved.`, 'Workspaces')
      await fetchWorkspacesList()
      void fetchGwsIntegration()
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
        gws_profile: form.gws_profile,
        disallowed_tools: disallowedToolsPayload(form.disallowed_tools),
        color: form.color,
        // Sent only when the user picked a value, so the server applies its default.
        ...(form.agent_fs_scope ? { agent_fs_scope: form.agent_fs_scope } : {}),
      })
      notifySaved(`Workspace "${form.name.trim()}" created.`, 'Workspaces')
      showNewWorkspace.value = false
      newWorkspaceForm.value = blankWorkspaceForm()
      await fetchWorkspacesList()
      void fetchGwsIntegration()
    } catch (e) {
      const detail = apiErrorMessage(e, 'The workspace could not be created.')
      workspacesResult.value = `Error: ${detail}`
      notifyFailed(`Workspace "${form.name.trim()}" not created`, detail)
    } finally {
      workspacesSaving.value = null
    }
  }

  // The primary workspace and the last one cannot be archived; the server
  // refuses both, so the button is not offered for them.
  function workspaceArchivable(name: string): boolean {
    return workspaceForms.value.length > 1 && name !== primaryWorkspace.value
  }

  async function archiveWorkspace(name: string) {
    if (!await askConfirm(archiveConfirmMessage(name), {
      title: 'Archive workspace',
      confirmLabel: 'Archive workspace',
      // Recoverable, so not styled as a destructive delete (DESIGN.md).
      destructive: false,
    })) return
    archivingWorkspace.value = name
    workspacesResult.value = ''
    try {
      const res = await projectStore.archiveWorkspace(name)
      const where = res.archived?.path ? ` Files are in ${res.archived.path}.` : ''
      notifySaved(`Workspace "${name}" archived.${where}`, 'Workspaces')
      await Promise.all([fetchWorkspacesList(), fetchArchivedWorkspaces()])
    } catch (e) {
      const detail = apiErrorMessage(e, 'The workspace could not be archived.')
      workspacesResult.value = `Error: ${detail}`
      notifyFailed(`Workspace "${name}" not archived`, detail)
    } finally {
      archivingWorkspace.value = null
    }
  }

  async function fetchArchivedWorkspaces() {
    archivedLoading.value = true
    archivedLoadError.value = ''
    try {
      archivedWorkspaces.value = await projectStore.fetchArchivedWorkspaces()
    } catch (e) {
      archivedLoadError.value = `Could not load archived workspaces: ${errorMessage(e)}`
    } finally {
      archivedLoading.value = false
    }
  }

  function formatArchivedAt(iso: string): string {
    const date = new Date(iso)
    if (Number.isNaN(date.getTime())) return iso
    return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
  }

  async function restoreWorkspace(item: ArchivedWorkspace) {
    // The archive's metadata syncs through git like any note, so show what the
    // restore will apply (validated server-side) before applying it.
    if (!await askConfirm(restoreConfirmMessage(item), {
      title: 'Restore workspace',
      confirmLabel: 'Restore workspace',
      destructive: false,
    })) return
    restoringArchiveId.value = item.id
    workspacesResult.value = ''
    try {
      const res = await projectStore.restoreArchivedWorkspace(item.id)
      notifySaved(restoredMessage(item.name, res.restored?.schedules_paused ?? 0), 'Workspaces')
      await Promise.all([fetchWorkspacesList(), fetchArchivedWorkspaces(), projectStore.fetchAll()])
    } catch (e) {
      const detail = apiErrorMessage(e, 'The workspace could not be restored.')
      workspacesResult.value = `Error: ${detail}`
      notifyFailed(`Workspace "${item.name}" not restored`, detail)
    } finally {
      restoringArchiveId.value = null
    }
  }

  // The editor is always open for the active workspace: one form, one name
  // draft. A switch drops the previous workspace's draft so nothing hidden
  // reappears later. Rename follows through the store, so the draft resets to
  // the new name after a saved rename.
  const activeWorkspaceForm = computed(
    () => workspaceForms.value.find((f) => f.name === projectStore.activeWorkspace) ?? null,
  )
  watch(
    () => projectStore.activeWorkspace,
    (next, prev) => {
      if (prev && prev !== next) discardWorkspace(prev)
      workspaceNameDraft.value = next || ''
    },
    { immediate: true },
  )

  // A switch the user starts while the active workspace has unsaved edits asks
  // first and stays put when declined, instead of dropping the draft silently.
  async function confirmWorkspaceSwitch(from: string): Promise<boolean> {
    const form = workspaceForms.value.find((f) => f.name === from)
    if (!form || !workspaceDirty(form)) return true
    return askConfirm(`Discard unsaved changes to ${from}?`, {
      title: 'Unsaved workspace changes',
      confirmLabel: 'Discard',
      cancelLabel: 'Keep editing',
      destructive: true,
    })
  }
  projectStore.setWorkspaceSwitchGuard(confirmWorkspaceSwitch)
  // Clear only our own guard: a newer instance may already have replaced it.
  onScopeDispose(() => projectStore.clearWorkspaceSwitchGuard(confirmWorkspaceSwitch))

  return {
    workspaceProviderFieldLabel,
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
    fsScopeEngineDefault,
    workspaceForms,
    newWorkspaceForm,
    workspaceBaseline,
    workspaceNameDraft,
    workspaceLinkStatus,
    workspaceLinkTarget,
    fsScopeDefaultOptionLabel,
    defaultWorkspaceProvider,
    blankWorkspaceForm,
    normalizeWorkspaceProvider,
    fsScopeChoice,
    workspaceToForm,
    activeWorkspaceForm,
    clearWorkspaceLinkHighlight,
    workspaceDirty,
    discardWorkspace,
    workspaceSwatch,
    workspaceFsScopeBadge,
    workspaceSummary,
    workspaceProviderOptions,
    disallowedToolsPayload,
    fetchWorkspacesList,
    renameConfirmMessage,
    saveWorkspace,
    createNewWorkspace,
    workspaceArchivable,
    archiveWorkspace,
    fetchArchivedWorkspaces,
    formatArchivedAt,
    restoreWorkspace,
    gwsProfileOptions,
    workspaceCustomGwsProfile,
  }
}

export type WorkspaceAdmin = ReturnType<typeof useWorkspaceAdmin>
export const WORKSPACE_ADMIN_KEY: InjectionKey<WorkspaceAdmin> = Symbol('workspace-admin')
