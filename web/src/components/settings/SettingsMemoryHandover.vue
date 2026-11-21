<template>
  <div class="card">
    <div class="settings-card-header">
      <p class="section-title">Memory handover</p>
      <p class="hint">
        How much of {{ workspace }}'s memory suggestions Workspace care may settle on its own. It
        always predicts first; you decide whether it acts. New workspaces start at Dismiss and
        accept. You can change this choice at any time.
      </p>
    </div>

    <!-- First load. A pending read is its own state, never an empty claim. -->
    <div v-if="!data && !loadError" class="hint" role="status">Reading your decisions&hellip;</div>

    <!-- First-load failure: the error and a retry, never "no decisions yet". -->
    <template v-else-if="!data">
      <p class="action-result action-result--error" role="alert">
        Could not read the handover status: {{ loadError }}
      </p>
      <div class="action-row settings-actions">
        <button type="button" class="btn-secondary btn-small" :disabled="loading" @click="load()">
          {{ loading ? 'Checking...' : 'Try again' }}
        </button>
      </div>
    </template>

    <template v-else>
      <!-- A failed refresh over data already on screen keeps it and says it is stale. -->
      <p v-if="loadError" class="hint hint--warn" role="alert">
        Could not refresh: {{ loadError }}. Showing the last known rate.
        <button type="button" class="set-link" :disabled="loading" @click="load()">Try again</button>
      </p>

      <p v-if="!data.acting_available" class="handover-note" role="status">
        Handover cannot act on this install: there is no installed service that registers this
        workspace at this vault. Your choice is kept; Workspace care keeps predicting only.
      </p>

      <div class="handover-evidence">
        <p v-if="data.agreement.decided === 0" class="handover-line">
          No decisions with a prediction yet. Predictions appear after Workspace care has looked at
          your suggestions and you have decided some.
        </p>
        <template v-else>
          <p class="handover-line">
            Over your last {{ data.agreement.decided }} decisions with a prediction, Workspace care
            agreed {{ percent }}%.
          </p>
          <p class="hint hint--compact">
            It predicted dismiss {{ data.agreement.predicted_dismiss }} times and was right
            {{ data.agreement.dismiss_matched }}; predicted accept
            {{ data.agreement.predicted_accept }} times and was right
            {{ data.agreement.accept_matched }}.
          </p>
        </template>
        <p v-if="data.agreement.suggest_handover" class="handover-line" role="status">
          You could let it dismiss suggestions it predicts you'd dismiss.
        </p>
        <button type="button" class="set-link set-link--quiet" :disabled="loading" @click="load()">
          {{ loading ? 'Refreshing...' : 'Refresh' }}
        </button>
      </div>

      <div class="instance-toggle" role="radiogroup" aria-label="Memory handover" @keydown="onOptionKeydown">
        <button
          v-for="option in OPTIONS"
          :key="option.value"
          ref="optionEls"
          type="button"
          role="radio"
          class="toggle-btn"
          :class="{ active: selected === option.value }"
          :aria-checked="selected === option.value"
          :tabindex="option.value === tabStop ? 0 : -1"
          :disabled="unavailable(option.value)"
          :aria-disabled="saving || unavailable(option.value) ? 'true' : undefined"
          @click="choose(option.value)"
        >{{ option.label }}</button>
      </div>
      <p class="hint hint--compact handover-hint" role="status">{{ selectedHint }}</p>
      <p v-if="saveError" class="action-result action-result--error" role="alert">{{ saveError }}</p>

      <!-- Per-kind acceptance. Each row shows the choice the engine applies now and whether
           it is inherited from the handover above; a reset appears only for an explicit one. -->
      <div class="handover-kinds">
        <p class="handover-kinds__title">Per kind</p>
        <p class="hint hint--compact">
          Choose what happens to each kind's suggestions. A kind you have not set follows the
          handover above. Dismissals always follow the handover.
        </p>
        <div v-for="kind in KINDS" :key="kind.value" class="handover-kind">
          <div class="handover-kind__head">
            <span :id="`handover-kind-${kind.value}`" class="handover-kind__label">{{ kind.label }}</span>
            <span class="hint hint--compact handover-kind__caption">{{ kindCaption(kind.value) }}</span>
          </div>
          <div
            class="instance-toggle"
            role="radiogroup"
            :aria-labelledby="`handover-kind-${kind.value}`"
            @keydown="onKindKeydown($event, kind.value)"
          >
            <button
              v-for="option in KIND_OPTIONS"
              :key="option.value"
              type="button"
              role="radio"
              class="toggle-btn"
              :class="{ active: kindMode(kind.value) === option.value }"
              :aria-checked="kindMode(kind.value) === option.value"
              :tabindex="option.value === kindTabStop(kind.value) ? 0 : -1"
              :disabled="kindUnavailable(option.value)"
              :aria-disabled="saving || kindUnavailable(option.value) ? 'true' : undefined"
              @click="saveKind(kind.value, option.value)"
            >{{ option.label }}</button>
          </div>
          <button
            v-if="!kindInherited(kind.value)"
            type="button"
            class="set-link set-link--quiet handover-kind__reset"
            :disabled="saving"
            @click="saveKind(kind.value, null)"
          >Reset to inherited</button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api'
import { askConfirm } from '../../lib/confirm'
import { apiErrorMessage } from '../../lib/errorMessage'
import { useProjectStore } from '../../stores/projects'
import type {
  HandoverResponse,
  MemoryApprovalKind,
  MemoryApprovalMode,
  MemoryHandover,
} from '../../lib/types'

// The operator's switch for what the nightly run may settle on its own. The
// card owns its data and saves through the existing workspace PATCH, one field
// at a time. It never saves unprompted: the suggestion is text, not an action.

const OPTIONS: { value: MemoryHandover; label: string; hint: string }[] = [
  { value: 'off', label: 'Off', hint: 'Predict only. Workspace care records what it would do and never acts.' },
  { value: 'dismiss', label: 'Dismiss', hint: 'Workspace care may dismiss suggestions it predicts you would dismiss.' },
  {
    value: 'all',
    label: 'Dismiss and accept',
    hint:
      'Workspace care may also let queued text reach always-loaded memory, up to 5 changes per run. Each one appears in History with Undo.',
  },
]

const LEGACY_LABEL: Record<MemoryHandover, string> = {
  off: 'Off',
  dismiss: 'Dismiss',
  all: 'Dismiss and accept',
}

const KINDS: { value: MemoryApprovalKind; label: string }[] = [
  { value: 'memory', label: 'Memory' },
  { value: 'profile', label: 'Profile' },
]

const KIND_OPTIONS: { value: MemoryApprovalMode; label: string }[] = [
  { value: 'approve', label: 'Ask me' },
  { value: 'auto', label: 'Let Workspace care add it' },
]

const CONFIRM_ACCEPT =
  "Let Workspace care add facts to this workspace's always-loaded memory? Every change is listed in History and can be undone."

const projectStore = useProjectStore()
const workspace = computed(() => projectStore.activeWorkspace)

const data = ref<HandoverResponse | null>(null)
const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
const saveError = ref('')
const selected = ref<MemoryHandover>('off')
const kindSelected = ref<Partial<Record<MemoryApprovalKind, MemoryApprovalMode>>>({})
const optionEls = ref<HTMLButtonElement[]>([])
const selectedHint = computed(() => OPTIONS.find((o) => o.value === selected.value)?.hint ?? '')
let ticket = 0
// Bumped when the workspace changes. A save or read started under an older
// value may finish, but it must never write into the workspace now on screen.
let workspaceToken = 0
// The workspace token a save started under, so a save for the old workspace
// never blocks or adopts state for the new one.
let saveToken = -1

const percent = computed(() => {
  const rate = data.value?.agreement.rate
  return rate == null ? 0 : Math.round(rate * 100)
})

async function load(): Promise<void> {
  const ws = workspace.value
  const mine = ++ticket
  loading.value = true
  try {
    const res = await api.get<HandoverResponse>(`/api/proposals/handover?workspace=${encodeURIComponent(ws)}`)
    if (mine !== ticket) return
    data.value = res
    loadError.value = ''
    if (!ownSaveInFlight()) {
      selected.value = res.handover
      kindSelected.value = Object.fromEntries(KINDS.map((k) => [k.value, res.accept_resolved[k.value].mode]))
    }
  } catch (e) {
    if (mine !== ticket) return
    loadError.value = apiErrorMessage(e, 'The request failed.')
  } finally {
    if (mine === ticket) loading.value = false
  }
}

function ownSaveInFlight(): boolean {
  return saving.value && saveToken === workspaceToken
}

// Off stays reachable so a saved dismiss/all can always be turned back off.
function unavailable(value: MemoryHandover): boolean {
  return value !== 'off' && !data.value?.acting_available
}

// One tab stop: the saved choice, or the first enabled option when that one is
// disabled. While saving the buttons stay enabled (aria-disabled only), or the
// focused one would drop focus to the page body mid-keystroke.
const tabStop = computed<MemoryHandover>(() => {
  if (!unavailable(selected.value)) return selected.value
  return OPTIONS.find((o) => !unavailable(o.value))?.value ?? 'off'
})

// Radio-group keys: arrows select (and so save) the neighbour, wrapping past
// the ends and skipping disabled options; Home/End jump to the first/last.
function onOptionKeydown(e: KeyboardEvent): void {
  const enabled = OPTIONS.map((o, i) => ({ o, i })).filter(({ o }) => !unavailable(o.value))
  if (!enabled.length) return
  const at = enabled.findIndex(({ o }) => o.value === selected.value)
  let target: number
  switch (e.key) {
    case 'ArrowRight':
    case 'ArrowDown':
      target = (at + 1) % enabled.length
      break
    case 'ArrowLeft':
    case 'ArrowUp':
      target = (at <= 0 ? enabled.length : at) - 1
      break
    case 'Home':
      target = 0
      break
    case 'End':
      target = enabled.length - 1
      break
    default:
      return
  }
  e.preventDefault()
  if (saving.value) return
  const next = enabled[target]
  optionEls.value[next.i]?.focus()
  void choose(next.o.value)
}

async function choose(value: MemoryHandover): Promise<void> {
  if (saving.value) return
  if (!data.value || value === data.value.handover) {
    selected.value = data.value?.handover ?? 'off'
    return
  }
  const name = workspace.value
  const token = workspaceToken
  saveError.value = ''
  if (value === 'all') {
    const ok = await askConfirm(CONFIRM_ACCEPT, { title: 'Memory handover', confirmLabel: 'Allow', destructive: false })
    if (!ok || token !== workspaceToken) {
      if (token === workspaceToken && data.value) selected.value = data.value.handover
      return
    }
  }
  selected.value = value
  saving.value = true
  saveToken = token
  try {
    await projectStore.updateWorkspace(name, { memory_handover: value })
    saving.value = false // load() only adopts the saved value while not saving
    if (token === workspaceToken) await load()
  } catch (e) {
    if (token === workspaceToken) {
      saveError.value = apiErrorMessage(e, 'The setting could not be saved.')
      selected.value = data.value?.handover ?? 'off'
    }
  } finally {
    saving.value = false
  }
}

// The effective mode a kind's row shows: the choice the operator made in this
// card, else what the engine resolved on the last read.
function kindMode(kind: MemoryApprovalKind): MemoryApprovalMode {
  return kindSelected.value[kind] ?? data.value?.accept_resolved[kind]?.mode ?? 'approve'
}

function kindInherited(kind: MemoryApprovalKind): boolean {
  return !(kind in (data.value?.memory_approval ?? {}))
}

function kindCaption(kind: MemoryApprovalKind): string {
  if (!data.value) return ''
  if (!kindInherited(kind)) return 'Set for this kind.'
  return `Inherited from the handover above (${LEGACY_LABEL[data.value.handover]}).`
}

// Accepting a kind needs the install to be able to act; "Ask me" is always
// available, so a saved auto can be turned back off on any install.
function kindUnavailable(mode: MemoryApprovalMode): boolean {
  return mode === 'auto' && !data.value?.acting_available
}

function kindTabStop(kind: MemoryApprovalKind): MemoryApprovalMode {
  const mode = kindMode(kind)
  return kindUnavailable(mode) ? 'approve' : mode
}

// Radio-group keys for one kind's two options: arrows and Home/End move the
// selection (and so save), skipping an option the install cannot use.
function onKindKeydown(e: KeyboardEvent, kind: MemoryApprovalKind): void {
  const enabled = KIND_OPTIONS.map((o) => o.value).filter((mode) => !kindUnavailable(mode))
  if (!enabled.length) return
  const at = enabled.indexOf(kindMode(kind))
  let target: MemoryApprovalMode | undefined
  switch (e.key) {
    case 'ArrowRight':
    case 'ArrowDown':
      target = enabled[(at + 1) % enabled.length]
      break
    case 'ArrowLeft':
    case 'ArrowUp':
      target = enabled[(at <= 0 ? enabled.length : at) - 1]
      break
    case 'Home':
      target = enabled[0]
      break
    case 'End':
      target = enabled[enabled.length - 1]
      break
    default:
      return
  }
  e.preventDefault()
  if (saving.value || target === undefined) return
  const index = KIND_OPTIONS.findIndex((o) => o.value === target)
  const buttons = (e.currentTarget as HTMLElement | null)?.querySelectorAll<HTMLButtonElement>('[role="radio"]')
  buttons?.[index]?.focus()
  void saveKind(kind, target)
}

// Saves one kind through the workspace PATCH, one key at a time. `null` resets
// that kind to inherited. The other kinds are left as they are on the server.
async function saveKind(kind: MemoryApprovalKind, mode: MemoryApprovalMode | null): Promise<void> {
  if (saving.value || !data.value) return
  if (mode === null ? kindInherited(kind) : mode === kindMode(kind)) return
  const name = workspace.value
  const token = workspaceToken
  const previous = kindMode(kind)
  if (mode === 'auto') {
    const ok = await askConfirm(CONFIRM_ACCEPT, { title: 'Memory handover', confirmLabel: 'Allow', destructive: false })
    if (!ok || token !== workspaceToken || saving.value) return
  }
  saveError.value = ''
  if (mode !== null) kindSelected.value = { ...kindSelected.value, [kind]: mode }
  saving.value = true
  saveToken = token
  try {
    await projectStore.updateWorkspace(name, { memory_approval: { [kind]: mode } })
    saving.value = false
    if (token === workspaceToken) await load()
  } catch (e) {
    if (token === workspaceToken) {
      saveError.value = apiErrorMessage(e, 'The setting could not be saved.')
      if (mode !== null) kindSelected.value = { ...kindSelected.value, [kind]: previous }
    }
  } finally {
    saving.value = false
  }
}

onMounted(load)
watch(workspace, () => {
  // Another workspace's rate must never stand in for this one's.
  workspaceToken += 1
  data.value = null
  loadError.value = ''
  saveError.value = ''
  kindSelected.value = {}
  void load()
})
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.action-result--error { color: var(--error); }
.handover-note {
  margin: 0 0 var(--space-3);
  color: var(--fg);
  font-size: var(--text-sm);
  line-height: 1.5;
}
.handover-evidence {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}
.handover-evidence p { margin: 0; }
.handover-line { color: var(--fg); font-size: var(--text-sm); line-height: 1.5; }
.handover-hint { margin-top: var(--space-2); }

.handover-kinds {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin-top: var(--space-4);
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}
.handover-kinds__title { margin: 0; color: var(--fg); font-size: var(--text-sm); font-weight: 600; }
.handover-kinds > .hint { margin: 0; }
.handover-kind { display: flex; flex-direction: column; gap: var(--space-2); }
.handover-kind__head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-1) var(--space-3);
}
.handover-kind__label { color: var(--fg); font-size: var(--text-sm); font-weight: 600; }
.handover-kind__caption { margin: 0; }
.handover-kind__reset {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
}
@media (pointer: coarse) {
  .handover-kind__reset { min-height: var(--touch); }
}
</style>
