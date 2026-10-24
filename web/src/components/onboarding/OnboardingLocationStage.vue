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
const validName = computed(() => /^[\p{L}\p{N}][\p{L}\p{N} _-]{0,63}$/u.test(name.value.trim()))
const canContinue = computed(() => Boolean((props.context.provisioned ? provisionedPath.value : folder.value.trim()) && validName.value))

watch([folder, name, canContinue], () => emit('state', { dirty: folder.value !== props.context.workspace || !!name.value, pending: false, canContinue: canContinue.value, error: folderError.value || null }), { immediate: true })
watch(() => props.context.workspace, value => { folder.value = value })

async function inspect() {
  folderError.value = ''
  if (props.context.provisioned) return
  if (!folder.value.trim()) { existingNotes.value = false; return }
  try {
    const result = await api.get<{ mode?: string }>(`/api/setup/inspect-folder?path=${encodeURIComponent(folder.value.trim())}`)
    existingNotes.value = result.mode === 'existing' || result.mode === 'notes'
  } catch {
    folderError.value = 'Could not inspect this folder. Check the path and try again.'
  }
}
onMounted(async () => {
  if (props.context.provisioned) {
    try {
      const status = await api.get<SetupStatus>('/api/setup-status')
      provisionedPath.value = status.workspace_root
      folder.value = status.workspace_root
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
      <input id="onboarding-name" v-model="name" name="name" autocomplete="off" aria-describedby="name-help" @blur="name = name.trim()">
      <p id="name-help" class="help">You can add more workspaces later.</p>
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
