<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import type { OnboardingStageContext, StageState } from '../../lib/onboarding'
import type { SetupStatus } from '../../lib/types'
import { api } from '../../lib/api'
import { isHostOrigin } from '../../lib/hostOrigin'

const props = defineProps<{ context: OnboardingStageContext; busy: boolean }>()
const emit = defineEmits<{ state: [state: StageState]; continue: []; back: []; refresh: [] }>()
const folder = ref(props.context.workspace)
const name = ref('')
const folderError = ref('')
const provisionedPath = ref('')
const existingNotes = ref(false)
const onHost = isHostOrigin()
// The engine's own contract (ciao/workspaces.py): ASCII alphanumerics, then up
// to 63 more of the same plus underscore and hyphen. A name that passes here and
// fails there would strand the workspace after the user has moved on.
const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/
const nameTouched = ref(false)
const validName = computed(() => NAME_PATTERN.test(name.value.trim()))
const nameError = computed(() =>
  nameTouched.value && name.value.trim() && !validName.value
    ? 'Use letters, numbers, dashes or underscores; start with a letter or number.'
    : '',
)
// A path is only usable once the engine has inspected it, so a typo or an
// unreadable folder cannot be carried into setup.
const inspected = ref(false)
const inspecting = ref(false)
const chosenFolder = computed(() => (props.context.provisioned ? provisionedPath.value : folder.value.trim()))
const canContinue = computed(() =>
  Boolean(chosenFolder.value && validName.value && inspected.value && !folderError.value && !inspecting.value),
)

watch(
  [folder, name, canContinue, folderError, nameError, inspecting, () => props.busy],
  () => emit('state', {
    dirty: folder.value !== props.context.workspace || !!name.value,
    pending: inspecting.value,
    canContinue: canContinue.value,
    error: folderError.value || nameError.value || null,
  }),
  { immediate: true },
)
// Editing an editable path invalidates the previous inspection. A provisioned
// install sets `folder` from the authoritative path, which is not an edit.
watch(folder, () => {
  if (props.context.provisioned) return
  inspected.value = false
  existingNotes.value = false
  folderError.value = ''
})
watch(() => props.context.workspace, value => { folder.value = value })

let inspectorId = 0
async function inspect() {
  folderError.value = ''
  inspected.value = false
  if (props.context.provisioned) return
  const requested = folder.value.trim()
  if (!requested) { existingNotes.value = false; return }
  const id = ++inspectorId
  inspecting.value = true
  try {
    const result = await api.get<{ mode?: string; ok?: boolean; error?: string }>(
      `/api/setup/inspect-folder?path=${encodeURIComponent(requested)}`,
    )
    // The path may have changed while the request was out; a stale answer must
    // not validate the newer value.
    if (id !== inspectorId || folder.value.trim() !== requested) return
    if (result?.ok === false) {
      folderError.value = result.error || 'That folder cannot be used. Check the path and try again.'
      existingNotes.value = false
      return
    }
    existingNotes.value = result.mode === 'existing' || result.mode === 'notes'
    inspected.value = true
  } catch {
    if (id !== inspectorId || folder.value.trim() !== requested) return
    folderError.value = 'Could not inspect this folder. Check the path and try again.'
  } finally {
    if (id === inspectorId) inspecting.value = false
  }
}
onMounted(async () => {
  if (props.context.provisioned) {
    try {
      const status = await api.get<SetupStatus>('/api/setup-status')
      provisionedPath.value = status.workspace_root
      folder.value = status.workspace_root
      // The engine reported this path: it needs no inspection, and the watcher
      // deliberately leaves provisioned installs alone.
      inspected.value = true
    } catch { folderError.value = 'Could not load the current Ciaobot folder.' }
  }
})
function submit() { if (canContinue.value && !props.busy) emit('continue') }
</script>

<template>
  <form class="location-stage" @submit.prevent="submit">
    <div class="field">
      <label for="onboarding-folder">Main Ciaobot folder</label>
      <input id="onboarding-folder" v-model="folder" name="folder" autocomplete="off" :readonly="context.provisioned" :aria-describedby="folderError ? 'folder-help folder-error' : 'folder-help'" @change="inspect">
      <p id="folder-help" class="help">Holds application data, workspaces, notes, chats and settings. Choose an empty folder or one with existing notes.</p>
      <p v-if="context.provisioned" class="help">This folder is already in use. To change it, use the <a href="/settings/advanced#workspace-move">guarded move flow</a>.</p>
      <p v-if="!onHost" class="help">The Ciaobot folder lives on the computer running Ciaobot. This browser cannot access folders on that computer.</p>
      <p v-if="existingNotes" class="help">Existing notes found: Ciaobot keeps files where they are and adapts around them. It will not reorganise or upload anything.</p>
      <p v-if="folderError" id="folder-error" class="error" role="alert">{{ folderError }}</p>
    </div>
    <div class="field">
      <label for="onboarding-name">Workspace name</label>
      <input
        id="onboarding-name"
        v-model="name"
        name="name"
        autocomplete="off"
        :aria-invalid="!!nameError"
        :aria-describedby="nameError ? 'name-help name-error' : 'name-help'"
        @blur="nameTouched = true; name = name.trim()"
      >
      <p id="name-help" class="help">You can add more workspaces later.</p>
      <p v-if="nameError" id="name-error" class="error" role="alert">{{ nameError }}</p>
    </div>
    <button class="continue" type="submit" :disabled="busy || !canContinue">Continue</button>
  </form>
</template>

<style scoped>
.location-stage { display: grid; gap: 20px; }
.field { display: grid; gap: 8px; }
label { font-weight: 600; }
input { width: 100%; min-height: 44px; padding: 10px 12px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm, 6px); background: var(--bg); color: var(--fg); font: inherit; }
input:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.help { margin: 0; color: var(--fg2); line-height: 1.5; }
.help a { color: var(--accent); }
.error { margin: 0; color: var(--error); }
.continue { min-height: 44px; justify-self: end; padding: 8px 16px; border: 1px solid var(--accent); border-radius: var(--radius-sm, 6px); background: var(--accent); color: var(--on-accent); font: inherit; cursor: pointer; }
.continue:disabled { opacity: .55; cursor: not-allowed; }
</style>
