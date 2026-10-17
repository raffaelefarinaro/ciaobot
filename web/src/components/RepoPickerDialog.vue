<template>
  <DialogRoot :open="open" modal @update:open="value => { if (!value) emit('close') }">
    <DialogPortal>
      <DialogOverlay class="repo-backdrop" />
      <DialogContent class="repo-card" aria-modal="true" @open-auto-focus="onOpenAutoFocus">
        <DialogTitle as="p" class="repo-title">{{ currentPath ? 'Change repository' : 'Link a repository' }}</DialogTitle>
        <DialogDescription as="p" class="repo-hint">
          Chats in this project keep working in the workspace and also get the repository's AGENTS.md and skills.
        </DialogDescription>

        <p id="repo-picker-found" class="repo-label">Found on this computer</p>
        <p v-if="candidatesLoading" class="repo-note" role="status">Looking for repositories…</p>
        <p v-else-if="candidatesError" class="repo-note repo-note--error" role="alert">
          {{ candidatesError }}
          <button type="button" class="repo-retry" @click="loadCandidates">Retry</button>
        </p>
        <ul v-else-if="candidates.length" class="repo-candidates" aria-labelledby="repo-picker-found">
          <li v-for="c in candidates" :key="c.path">
            <button
              type="button"
              class="repo-candidate"
              :aria-pressed="pathDraft.trim() === c.path"
              @click="pick(c.path)"
            >
              <span class="repo-candidate-name">{{ c.name }}</span>
              <code class="repo-candidate-path">{{ c.path }}</code>
            </button>
          </li>
        </ul>
        <p v-else class="repo-note">
          No git repositories found in ~/repos, ~/code, ~/src, ~/dev, ~/Developer or ~/projects. Type the folder below.
        </p>

        <form novalidate @submit.prevent="submit">
          <label class="repo-label" for="repo-picker-path">Repository folder</label>
          <input
            id="repo-picker-path"
            ref="pathInput"
            v-model="pathDraft"
            class="repo-input"
            type="text"
            autocomplete="off"
            autocapitalize="off"
            spellcheck="false"
            placeholder="/Users/you/repos/my-app"
            :aria-invalid="saveError ? 'true' : undefined"
            :aria-describedby="saveError ? 'repo-picker-error' : undefined"
          />
          <p v-if="saveError" id="repo-picker-error" class="repo-note repo-note--error" role="alert">{{ saveError }}</p>
          <div class="repo-actions">
            <DialogClose as-child>
              <button type="button" class="repo-action repo-action--cancel">Cancel</button>
            </DialogClose>
            <button type="submit" class="repo-action repo-action--primary" :disabled="!canSubmit">
              {{ saving ? 'Saving…' : (currentPath ? 'Change' : 'Link') }}
            </button>
          </div>
        </form>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>

<script setup lang="ts">
import {
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogRoot,
  DialogTitle,
} from 'reka-ui'
import { computed, nextTick, ref, watch } from 'vue'
import { api } from '../lib/api'
import { useProjectStore } from '../stores/projects'
import type { RepoCandidate } from '../lib/types'

const props = defineProps<{ open: boolean; projectId: string; currentPath: string | null }>()
const emit = defineEmits<{ close: []; linked: [] }>()

const store = useProjectStore()
const candidates = ref<RepoCandidate[]>([])
const candidatesLoading = ref(false)
const candidatesError = ref('')
const pathDraft = ref('')
const saving = ref(false)
const saveError = ref('')
const pathInput = ref<HTMLInputElement | null>(null)
let loadSeq = 0

async function loadCandidates() {
  const seq = ++loadSeq
  candidatesLoading.value = true
  candidatesError.value = ''
  try {
    const data = await api.get<{ roots?: string[]; candidates?: unknown }>('/api/repos/candidates')
    if (seq !== loadSeq) return
    candidates.value = Array.isArray(data?.candidates)
      ? (data.candidates as RepoCandidate[]).filter(c => c && typeof c.path === 'string' && c.path !== '')
      : []
  } catch (e) {
    if (seq !== loadSeq) return
    candidates.value = []
    candidatesError.value = e instanceof Error ? e.message : 'Could not look for repositories.'
  } finally {
    if (seq === loadSeq) candidatesLoading.value = false
  }
}

const canSubmit = computed(() => {
  const p = pathDraft.value.trim()
  return !saving.value && p !== '' && p !== (props.currentPath ?? '')
})

function pick(path: string) {
  pathDraft.value = path
}

async function submit() {
  if (!canSubmit.value) return
  saving.value = true
  saveError.value = ''
  try {
    await store.updateProject(props.projectId, { repo_path: pathDraft.value.trim() })
    emit('linked')
    emit('close')
  } catch (e) {
    saveError.value = e instanceof Error ? e.message : 'Could not link the repository.'
  } finally {
    saving.value = false
  }
}

watch(pathDraft, () => { saveError.value = '' })

function onOpenAutoFocus(event: Event) {
  event.preventDefault()
  void nextTick(() => pathInput.value?.focus())
}

watch(
  () => props.open,
  value => {
    if (!value) return
    pathDraft.value = props.currentPath ?? ''
    saveError.value = ''
    saving.value = false
    void loadCandidates()
  },
  { immediate: true },
)
</script>

<style scoped>
.repo-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgb(0 0 0 / 55%);
}
.repo-card {
  position: fixed;
  top: 50%;
  left: 50%;
  z-index: 1001;
  transform: translate(-50%, -50%);
  width: min(32rem, calc(100vw - 2 * var(--space-4)));
  max-height: calc(100dvh - 2 * var(--space-4));
  overflow-y: auto;
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-elev);
  color: var(--fg);
  box-shadow: 0 1.5rem 3rem rgb(0 0 0 / 45%);
}
.repo-title {
  margin: 0 0 var(--space-2);
  font-weight: 600;
}
.repo-hint {
  margin: 0 0 var(--space-4);
  color: var(--fg2);
}
.repo-label {
  display: block;
  margin: 0 0 var(--space-1);
  color: var(--fg2);
  font-size: 0.875rem;
}
.repo-candidates {
  max-height: 12rem;
  margin: 0 0 var(--space-3);
  padding: 0;
  overflow-y: auto;
  list-style: none;
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.repo-candidate,
.repo-retry {
  min-height: 38px;
  border: 0;
  background: transparent;
  color: var(--fg);
  font: inherit;
  cursor: pointer;
}
.repo-candidate {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: 100%;
  padding: var(--space-1) var(--space-3);
  text-align: left;
  overflow-wrap: anywhere;
}
.repo-candidate-path {
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}
.repo-candidate[aria-pressed='true'] {
  background: var(--bg3);
  box-shadow: inset 2px 0 0 var(--accent);
}
.repo-retry {
  padding: 0 var(--space-2);
  border-radius: var(--radius-sm);
  color: var(--accent);
}
.repo-candidate:hover,
.repo-retry:hover {
  background: var(--bg3);
}
.repo-candidate:focus-visible,
.repo-retry:focus-visible,
.repo-action:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.repo-input {
  width: 100%;
  min-height: 2.75rem;
  padding: 0 var(--space-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg);
  font: inherit;
}
.repo-input:focus-visible {
  border-color: var(--accent);
  outline: none;
}
.repo-note {
  margin: 0 0 var(--space-2);
  color: var(--fg2);
  font-size: 0.875rem;
}
.repo-note--error {
  color: var(--error);
}
.repo-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
.repo-action {
  min-width: 6rem;
  min-height: 2.75rem;
  padding: 0 var(--space-3);
  border: 1px solid transparent;
  border-radius: var(--radius);
  color: var(--fg);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
.repo-action--cancel {
  border-color: var(--border-strong);
  background: var(--bg3);
}
.repo-action--primary {
  border-color: var(--accent);
  background: var(--accent);
  color: var(--on-accent);
}
.repo-action--primary:disabled {
  border-color: var(--border-strong);
  background: var(--bg3);
  color: var(--fg2);
  cursor: default;
}
@media (pointer: coarse) {
  .repo-candidate,
  .repo-retry {
    min-height: var(--touch);
  }
}
</style>
