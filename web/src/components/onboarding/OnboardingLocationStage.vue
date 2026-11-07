<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import type { OnboardingStageContext, StageState } from '../../lib/onboarding'
import type { SetupStatus } from '../../lib/types'
import { api } from '../../lib/api'
import { isHostOrigin } from '../../lib/hostOrigin'
import OnboardingStack from './OnboardingStack.vue'
import OnboardingField from './OnboardingField.vue'
import OnboardingButton from './OnboardingButton.vue'

const props = defineProps<{ context: OnboardingStageContext; busy: boolean }>()
const emit = defineEmits<{ state: [state: StageState] }>()
const folder = ref(props.context.workspace)
// The installer already registered the workspace; it is shown as set and only
// asked again when the person chooses to change it.
const initialName = props.context.provisioned ? props.context.workspace : ''
const name = ref(initialName)
const editingName = ref(false)
const nameSet = computed(() => !!initialName && !editingName.value)
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
    dirty: folder.value !== props.context.workspace || name.value !== initialName,
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
</script>

<template>
  <OnboardingStack as="form" gap="xl" class="location-stage" @submit.prevent>
    <div v-if="context.provisioned" class="set-row">
      <div class="set-text">
        <span class="set-label">Ciaobot folder</span>
        <span class="set-value">{{ chosenFolder || 'Loading…' }}</span>
        <span v-if="folderError" class="error" role="alert">{{ folderError }}</span>
      </div>
      <a class="change" href="/settings/advanced#workspace-move">Change</a>
    </div>
    <OnboardingField v-else id="onboarding-folder" label="Main Ciaobot folder" :error="folderError">
      <template #default="{ describedby }">
        <input id="onboarding-folder" v-model="folder" name="folder" autocomplete="off" :aria-describedby="describedby" @change="inspect">
      </template>
      <template #help>
        <p>Holds application data, workspaces, notes, chats and settings. Choose an empty folder or one with existing notes.</p>
        <p v-if="!onHost">The Ciaobot folder lives on the computer running Ciaobot. This browser cannot access folders on that computer.</p>
        <p v-if="existingNotes">Existing notes found: Ciaobot keeps files where they are and adapts around them. It will not reorganise or upload anything.</p>
      </template>
    </OnboardingField>

    <div v-if="nameSet" class="set-row">
      <div class="set-text">
        <span class="set-label">Workspace</span>
        <span class="set-value">{{ name }}</span>
      </div>
      <OnboardingButton variant="link" class="change-name" @click="editingName = true">Change</OnboardingButton>
    </div>
    <OnboardingField v-else id="onboarding-name" label="Workspace name" help="You can add more workspaces later." :error="nameError">
      <template #default="{ describedby, invalid }">
        <input
          id="onboarding-name"
          v-model="name"
          name="name"
          autocomplete="off"
          :aria-invalid="invalid"
          :aria-describedby="describedby"
          @blur="nameTouched = true; name = name.trim()"
        >
      </template>
    </OnboardingField>
  </OnboardingStack>
</template>

<style scoped>
.set-row { display: flex; flex-wrap: nowrap; align-items: center; justify-content: space-between; gap: var(--space-3, 12px); padding: var(--space-3, 12px) var(--space-4, 16px); border: 1px solid var(--border); border-radius: var(--radius-sm, 6px); background: var(--bg); }
.set-text { flex: 1; display: grid; gap: var(--space-1, 4px); min-width: 0; }
.set-label { color: var(--fg2); }
.set-value { overflow-wrap: anywhere; }
.change { display: inline-flex; align-items: center; min-height: var(--touch, 44px); padding: 0 var(--space-4, 16px); border: 1px solid transparent; color: var(--accent); text-decoration: none; }
.change:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.error { color: var(--error); }
</style>
