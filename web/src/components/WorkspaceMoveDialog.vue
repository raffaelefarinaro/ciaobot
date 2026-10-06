<template>
  <!-- Esc is stopped at the dialog and closes it by hand: the app's "Esc leaves
       Settings" shortcut listens on window and is registered before Reka's own
       Esc listener, so letting the key bubble would navigate away underneath. -->
  <DialogRoot :open="open" modal @update:open="value => { if (!value) emit('close') }">
    <DialogPortal>
      <DialogOverlay class="move-backdrop" />
      <DialogContent
        class="move-card"
        aria-modal="true"
        @open-auto-focus="onOpenAutoFocus"
        @keydown.esc.stop.prevent="emit('close')"
      >
        <DialogTitle as="p" class="move-title">Move workspace</DialogTitle>
        <DialogDescription as="p" class="move-hint">
          Ciaobot finishes running chats, stops, moves this folder with everything in it, and
          starts again from the new place. Open chats carry on where they were.
        </DialogDescription>

        <p class="move-label">Put it in</p>
        <div class="move-browser">
          <div class="move-browser-head">
            <button
              type="button"
              class="move-up"
              :disabled="!listing?.parent || loading"
              @click="listing?.parent && browse(listing.parent)"
            >
              Up
            </button>
            <code class="move-current">{{ listing?.display_path || '…' }}</code>
          </div>
          <ul class="move-dirs" aria-label="Folders">
            <li v-for="dir in listing?.dirs || []" :key="dir.path">
              <button type="button" class="move-dir" :disabled="loading" @click="browse(dir.path)">
                {{ dir.name }}
              </button>
            </li>
            <li v-if="listing && !listing.dirs.length" class="move-empty">No folders here</li>
          </ul>
        </div>

        <label class="move-label" for="workspace-move-name">Folder name</label>
        <input
          id="workspace-move-name"
          ref="nameInput"
          v-model="name"
          class="move-input"
          type="text"
          autocomplete="off"
          spellcheck="false"
        />
        <p class="move-dest">
          New location: <code>{{ target || '…' }}</code>
        </p>

        <ul v-if="plan && (plan.refusals.length || plan.warnings.length)" class="move-notes">
          <li v-for="refusal in plan.refusals" :key="refusal" class="move-note move-note--refusal">{{ refusal }}</li>
          <li v-for="warning in plan.warnings" :key="warning" class="move-note">{{ warning }}</li>
        </ul>
        <p v-if="error" class="move-note move-note--refusal" role="alert">{{ error }}</p>

        <div class="move-actions">
          <DialogClose as-child>
            <button type="button" class="move-action move-action--cancel">Cancel</button>
          </DialogClose>
          <button
            type="button"
            class="move-action move-action--primary"
            :disabled="!plan?.ok || planning || starting || plan.target !== target"
            @click="startMove"
          >
            {{ starting ? 'Starting…' : 'Move' }}
          </button>
        </div>
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

interface DirListing {
  path: string
  display_path: string
  parent: string | null
  dirs: Array<{ name: string; path: string }>
}

interface MovePlan {
  source: string
  target: string
  ok: boolean
  refusals: string[]
  warnings: string[]
}

const props = defineProps<{ open: boolean; workspaceRoot: string }>()
const emit = defineEmits<{ close: []; started: [target: string] }>()

const listing = ref<DirListing | null>(null)
const loading = ref(false)
const name = ref('')
const plan = ref<MovePlan | null>(null)
const planning = ref(false)
const starting = ref(false)
const error = ref('')
const nameInput = ref<HTMLInputElement | null>(null)

function basename(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() || 'ciaobot'
}

function parentOf(path: string): string {
  const cut = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  return cut > 0 ? path.slice(0, cut) : '~'
}

const target = computed(() => {
  const folder = name.value.trim()
  if (!listing.value || !folder || /[\\/]/.test(folder)) return ''
  const sep = listing.value.path.includes('\\') ? '\\' : '/'
  return listing.value.path.replace(/[\\/]$/, '') + sep + folder
})

async function browse(path: string) {
  loading.value = true
  error.value = ''
  try {
    listing.value = await api.get<DirListing>(`/api/workspace-move/dirs?path=${encodeURIComponent(path)}`)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

let planSeq = 0
async function refreshPlan() {
  const dest = target.value
  // Bumped before the early return too, so a plan still in flight for the
  // previous destination cannot land after the name was cleared.
  const seq = ++planSeq
  plan.value = null
  error.value = ''
  planning.value = false
  if (!dest) return
  planning.value = true
  try {
    const result = await api.post<MovePlan>('/api/workspace-move/plan', { target: dest })
    if (seq === planSeq) plan.value = result
  } catch (e) {
    if (seq === planSeq) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (seq === planSeq) planning.value = false
  }
}

async function startMove() {
  if (!plan.value?.ok) return
  starting.value = true
  error.value = ''
  try {
    await api.post('/api/workspace-move', { target: plan.value.target })
    emit('started', plan.value.target)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    starting.value = false
  }
}

function onOpenAutoFocus(event: Event) {
  event.preventDefault()
  nextTick(() => nameInput.value?.focus())
}

watch(target, () => { void refreshPlan() })

watch(
  () => props.open,
  value => {
    if (!value) return
    name.value = basename(props.workspaceRoot)
    plan.value = null
    error.value = ''
    void browse(parentOf(props.workspaceRoot))
  },
  { immediate: true },
)
</script>

<style scoped>
.move-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgb(0 0 0 / 55%);
}
.move-card {
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
.move-title {
  margin: 0 0 var(--space-2);
  font-weight: 600;
}
.move-hint {
  margin: 0 0 var(--space-4);
  color: var(--fg2);
}
.move-label {
  display: block;
  margin: 0 0 var(--space-1);
  color: var(--fg2);
  font-size: 0.875rem;
}
.move-browser {
  margin: 0 0 var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.move-browser-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-2);
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
}
.move-current {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 0.85rem;
}
.move-up,
.move-dir {
  min-height: 38px;
  border: 0;
  background: transparent;
  color: var(--fg);
  font: inherit;
  cursor: pointer;
}
.move-up {
  flex: none;
  padding: 0 var(--space-2);
  border-radius: var(--radius-sm);
  color: var(--accent);
}
.move-up:disabled {
  color: var(--fg3);
  cursor: default;
}
.move-dirs {
  max-height: 12rem;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  list-style: none;
}
.move-dir {
  display: block;
  width: 100%;
  padding: 0 var(--space-3);
  text-align: left;
  overflow-wrap: anywhere;
}
.move-dir:hover,
.move-up:hover:not(:disabled) {
  background: var(--bg3);
}
.move-up:focus-visible,
.move-dir:focus-visible,
.move-action:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.move-empty {
  padding: var(--space-2) var(--space-3);
  color: var(--fg3);
}
.move-input {
  width: 100%;
  min-height: 2.75rem;
  padding: 0 var(--space-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg);
  font: inherit;
}
.move-input:focus-visible {
  border-color: var(--accent);
  outline: none;
}
.move-dest {
  margin: var(--space-2) 0 var(--space-3);
  color: var(--fg2);
  font-size: 0.875rem;
  overflow-wrap: anywhere;
}
.move-notes {
  margin: 0 0 var(--space-3);
  padding: 0;
  list-style: none;
}
.move-note {
  margin: 0 0 var(--space-2);
  color: var(--fg2);
  font-size: 0.875rem;
}
.move-note--refusal {
  color: var(--error);
}
.move-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--space-2);
}
.move-action {
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
.move-action--cancel {
  border-color: var(--border-strong);
  background: var(--bg3);
}
.move-action--primary {
  border-color: var(--accent);
  background: var(--accent);
  color: var(--on-accent);
}
.move-action--primary:disabled {
  border-color: var(--border-strong);
  background: var(--bg3);
  color: var(--fg2);
  cursor: default;
}
@media (pointer: coarse) {
  .move-up,
  .move-dir {
    min-height: var(--touch);
  }
}
</style>
