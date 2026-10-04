<script setup lang="ts">
/**
 * The vault's category list (`/memory/categories`).
 *
 * One row per effective entry, disabled ones included, and one drawer to edit
 * it. The API takes the whole desired list on every write, so there is no
 * per-row save: the form edits a local draft and the panel's Save sends the
 * list, exactly as the server's contract requires.
 */
import SkeletonLoader from './SkeletonLoader.vue'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useModalFocus } from '../composables/useModalFocus'
import { useEntityTypesStore, isValidEntityTypeId, type EntityTypeKind, type EntityTypeRow } from '../stores/entityTypes'
import { useProjectStore } from '../stores/projects'

const store = useEntityTypesStore()
const projectStore = useProjectStore()
const workspace = computed(() => projectStore.activeWorkspace)

/** The rows the form is editing. The store's list is the saved truth. */
const draft = ref<EntityTypeRow[]>([])

function resetDraft(rows: EntityTypeRow[]) {
  draft.value = rows.map((row) => ({ ...row, aliases: [...row.aliases] }))
}

watch(() => store.types, (rows) => resetDraft(rows), { immediate: true })

/** The row as it was last saved, so a change can be told from a no-op. */
function savedRow(id: string): EntityTypeRow | undefined {
  return store.types.find((row) => row.id === id)
}

/** The editable half of a row: everything except the server's own count. */
function editableOf(row: EntityTypeRow) {
  return {
    id: row.id, label: row.label, kind: row.kind, folder: row.folder,
    description: row.description, aliases: [...row.aliases],
    stale_after_days: row.stale_after_days, enabled: row.enabled, builtin: row.builtin,
  }
}

function sameRow(a: EntityTypeRow | undefined, b: EntityTypeRow): boolean {
  if (!a) return false
  return JSON.stringify(editableOf(a)) === JSON.stringify(editableOf(b))
}

/** Ids whose draft row differs from the saved one — the panel's "unsaved". */
const changedIds = computed(() => {
  const out = new Set<string>()
  for (const row of draft.value) {
    if (!sameRow(savedRow(row.id), row)) out.add(row.id)
  }
  return out
})

const dirty = computed(() => changedIds.value.size > 0)
/** The rows the page lists: the types the agent writes itself are not a choice. */
const visibleRows = computed(() => draft.value.filter((row) => !row.hidden))
const onCount = computed(() => visibleRows.value.filter((row) => row.enabled).length)

/** A draft id the list already holds under another spelling, or ''. */
function idTaken(id: string): string {
  return draft.value.find((row) => row.id === id)?.id ?? ''
}

function setEnabled(id: string, enabled: boolean) {
  const next = draft.value.map((row) => (row.id === id ? { ...row, enabled } : row))
  draft.value = next
}

// ── Load states ───────────────────────────────────────────────────────────
//
// The four states are distinct, as everywhere else in Memory: a first load in
// flight, a first load that failed (an error and a Retry, never an "empty
// vault"), a failed refresh over rows that are still on screen, and a list that
// really is empty. A filtered array would collapse the first three into the
// last, so each is read off its own slot.

const catLoading = computed(() => !store.loadedWorkspace && !store.loadError)
const catFailed = computed(() => Boolean(store.loadError) && !store.types.length)
const catEmpty = computed(() => Boolean(store.loadedWorkspace) && !store.loadError && !store.types.length)

function load() {
  void store.ensureLoaded(workspace.value)
}

onMounted(load)
// The registry is per vault FILE, so two workspaces can share it; a switch is
// the one thing that re-reads it, and only when the held list is another scope's.
watch(workspace, load)

// ── The drawer ────────────────────────────────────────────────────────────

/**
 * Starting points for a new category.
 *
 * A folder nobody else claims, because the server refuses two enabled
 * categories sharing one — so these spell their own (`Customers`, `Countries`,
 * `Organisations`, `Events`) rather than reusing `People` or `Places`. `place`
 * ships as a builtin, so its preset is offered as already-present rather than as
 * an id the server would reject as a duplicate.
 */
const CATEGORY_PRESETS = [
  { id: 'customer', label: 'Customer', kind: 'entity' as EntityTypeKind, folder: 'Customers',
    description: 'A person or company the user does business with. Use for accounts, deals and contacts.' },
  { id: 'place', label: 'Place', kind: 'entity' as EntityTypeKind, folder: 'Places',
    description: 'A physical place, city, region or venue. Use for addresses, travel and local context.' },
  { id: 'country', label: 'Country', kind: 'entity' as EntityTypeKind, folder: 'Countries',
    description: 'A country the user lives in, works in, or travels to. Use for context that is not an address.' },
  { id: 'organisation', label: 'Organisation', kind: 'entity' as EntityTypeKind, folder: 'Organisations',
    description: 'A company, institution or group the user belongs to or works with.' },
  { id: 'event', label: 'Event', kind: 'note' as EntityTypeKind, folder: 'Events',
    description: 'Something that happened at a time. Use for trips, launches, appointments and deadlines.' },
  { id: 'product', label: 'Product', kind: 'note' as EntityTypeKind, folder: 'products',
    description: 'A product, service or line the user ships or sells. Use for versions, pricing, positioning.' },
  { id: 'document', label: 'Document', kind: 'note' as EntityTypeKind, folder: 'Documents',
    description: "A durable document of the user's own. Use for plans, templates and written material." },
  { id: 'reference', label: 'Reference', kind: 'note' as EntityTypeKind, folder: 'references',
    description: 'Material quoted from elsewhere. Use for external analysis and citations.' },
]

/** Presets whose id the list already holds, so they are not offered as new. */
const takenPresets = computed(() => CATEGORY_PRESETS.filter((preset) => idTaken(preset.id)).map((preset) => preset.label))
const availablePresets = computed(() => CATEGORY_PRESETS.filter((preset) => !idTaken(preset.id)))

interface FormState {
  id: string
  label: string
  kind: EntityTypeKind
  folder: string
  description: string
  aliases: string
  stale_after_days: number
  /** The row being edited, or '' when this drawer is adding one. */
  editing: string
  /** True for a builtin: the id is a shipped key and cannot be renamed. */
  builtin: boolean
}

const form = reactive<FormState>({
  id: '', label: '', kind: 'entity', folder: '', description: '', aliases: '',
  stale_after_days: 0, editing: '', builtin: false,
})

/** Whether the drawer is adding a new category rather than editing one. */
const adding = computed(() => !form.editing)
const drawerOpen = ref(false)
const drawerEl = ref<HTMLElement | null>(null)
const idField = ref<HTMLInputElement | null>(null)
const labelField = ref<HTMLInputElement | null>(null)
const savingFromDrawer = ref(false)

/**
 * Where focus enters the drawer: the id while there is one to type, and the
 * label when there is not — a shipped or already-saved category opens on its
 * id read-only, so landing on it would put focus on a field that cannot change.
 */
const firstField = computed(() => (adding.value ? idField.value : labelField.value))

useModalFocus(drawerEl, drawerOpen, { initialFocus: firstField, onEscape: closeDrawer })

function openDrawerFor(row: EntityTypeRow) {
  store.clearError()
  form.id = row.id
  form.label = row.label
  form.kind = row.kind
  form.folder = row.folder
  form.description = row.description
  form.aliases = row.aliases.join(', ')
  form.stale_after_days = row.stale_after_days
  form.editing = row.id
  form.builtin = row.builtin
  drawerOpen.value = true
}

function openDrawerForPreset(preset: typeof CATEGORY_PRESETS[number]) {
  store.clearError()
  form.id = preset.id
  form.label = preset.label
  form.kind = preset.kind
  form.folder = preset.folder
  form.description = preset.description
  form.aliases = ''
  form.stale_after_days = 0
  form.editing = ''
  form.builtin = false
  drawerOpen.value = true
}

function openDrawerEmpty() {
  store.clearError()
  form.id = ''
  form.label = ''
  form.kind = 'entity'
  form.folder = ''
  form.description = ''
  form.aliases = ''
  form.stale_after_days = 0
  form.editing = ''
  form.builtin = false
  drawerOpen.value = true
}

function closeDrawer() {
  if (savingFromDrawer.value) return
  drawerOpen.value = false
  store.clearError()
}

/** The id is the merge key and the folder half of a path, so it is fixed once
 * the category exists: renaming it would orphan every note that names it. */
const idReadOnly = computed(() => !adding.value)

/** The row the drawer is editing, or undefined when it is adding one. */
const editingRow = computed(() => (form.editing ? savedRow(form.editing) : undefined))

const idError = computed(() => {
  if (adding.value && form.id && !isValidEntityTypeId(form.id)) {
    return 'A lower-case word, hyphen-separated — customer, not Customer.'
  }
  if (adding.value && idTaken(form.id)) return `${form.id} is already one of your categories.`
  return ''
})

/** Only after the field has been touched: an empty Add drawer is not an error,
 * it is the first screen of the form, and a red line on it says nothing. */
const labelError = computed(() => {
  if (form.label.trim()) return ''
  return form.label ? 'A category needs a label.' : ''
})

const daysError = computed(() => {
  const value = form.stale_after_days
  if (Number.isInteger(value) && value >= 0) return ''
  return 'Days must be a whole number, 0 or more.'
})

/** What blocks a save. A missing label blocks it whether or not the field has
 * been touched — only the *sentence* about it waits for that. */
const formValid = computed(() => !idError.value && !daysError.value && form.label.trim() !== '')

/** `0` means the caller's own default, so the field says so rather than showing 0. */
function staleHint(row: EntityTypeRow): string {
  if (!row.stale_after_days) return 'Default'
  return row.stale_after_days === 1 ? '1 day' : `${row.stale_after_days} days`
}

function parseAliases(value: string): string[] {
  return value.split(',').map((alias) => alias.trim()).filter(Boolean)
}

function rowFromForm(): EntityTypeRow {
  const id = form.id.trim()
  const previous = form.editing ? savedRow(form.editing) : undefined
  return {
    id,
    label: form.label.trim(),
    kind: form.kind,
    folder: form.folder.trim(),
    description: form.description.trim(),
    aliases: parseAliases(form.aliases),
    stale_after_days: Math.max(0, Math.round(form.stale_after_days || 0)),
    // Adding is always enabled: a category the user just created and cannot
    // see used would read as a broken save. An edit takes the flag from the
    // DRAFT, never from the saved row: the drawer owns no enabled control, so
    // reading the saved row would let a Save here undo a switch the user had
    // just flipped off in the list.
    enabled: adding.value
      ? true
      : (draft.value.find((row) => row.id === form.editing)?.enabled ?? previous?.enabled ?? true),
    builtin: form.builtin,
    core: previous?.core ?? false,
    hidden: previous?.hidden ?? false,
    note_count: previous?.note_count ?? 0,
  }
}

/** The list a save would send: the edited row in place, or appended. */
function nextRows(): EntityTypeRow[] {
  const row = rowFromForm()
  if (adding.value) return [...draft.value, row]
  return draft.value.map((existing) => (existing.id === form.editing ? row : existing))
}

async function saveFromDrawer() {
  if (savingFromDrawer.value || !formValid.value) return
  savingFromDrawer.value = true
  const rows = nextRows()
  const ok = await store.save(workspace.value, rows)
  savingFromDrawer.value = false
  if (!ok) return
  drawerOpen.value = false
  store.clearError()
}

/** Remove a custom category. A builtin is not offered one: it can be turned
 * off, and that is the whole of what the server allows. */
async function removeRow(row: EntityTypeRow | undefined) {
  if (!row || row.builtin || store.saving) return
  const remaining = draft.value.filter((existing) => existing.id !== row.id)
  if (!await store.save(workspace.value, remaining)) return
  // The row is gone, so the form describing it is: leaving the drawer open on a
  // category that no longer exists would let the next Save write it straight
  // back.
  drawerOpen.value = false
  store.clearError()
}

async function saveDraft() {
  if (store.saving || !dirty.value) return
  // Branches on the answer like its siblings: a success adopts the server's
  // list, so the draft is re-seeded from it and nothing is pending any more; a
  // failure leaves the rows the user built alone and puts the server's sentence
  // under these buttons, which is where a list-level error belongs.
  const ok = await store.save(workspace.value, draft.value)
  if (ok) store.clearError()
}
</script>

<template>
  <section class="cat-panel" aria-labelledby="cat-heading">
    <header class="cat-head">
      <h2 id="cat-heading" class="cat-h">{{ onCount }} of {{ visibleRows.length }} categories on</h2>
      <p class="cat-lede">
        The kinds of notes this vault keeps, and the <code>type:</code> each one is
        written with. Turn one off to keep its notes but stop claiming that type.
      </p>
    </header>

    <!-- What the two memory layers are, and what a category is for — above the
         controls, and outside the load branches on purpose: it is true while the
         list is loading and after a failure too, so it must never be read as a
         claim about an empty registry (#979). Static text, no request, no state. -->
    <section class="cat-why" aria-labelledby="cat-why-heading">
      <h3 id="cat-why-heading" class="cat-why-h">How memory is organized</h3>
      <p class="cat-why-line">
        <strong>Profile &amp; preferences</strong> — a small, bounded block kept in
        your workspace guide and loaded into every conversation, so you stop
        repeating yourself. <em>Keeps answers short and jargon-free.</em>
      </p>
      <p class="cat-why-line">
        <strong>Notes, by category</strong> — everything else is a note you own,
        filed under one of the categories in this list.
        <em>The decision behind a launch, who someone is, the reference you keep
        coming back to.</em>
      </p>
      <p class="cat-why-line cat-why-foot">
        A category is a filing rule, not a request: changing one shapes how
        <strong>future</strong> notes are organized, and no empty folder is created
        for a category you never write in.
      </p>
    </section>

    <SkeletonLoader v-if="catLoading" label="Loading categories" :count="6" />

    <div v-else-if="catFailed" class="cat-failed" role="alert">
      <p class="cat-failed-text">{{ store.loadError }}</p>
      <button type="button" class="cat-btn" @click="store.reload(workspace)">Retry</button>
    </div>

    <template v-else>
      <!-- A refresh that failed keeps the rows it had; saying so beats
           replacing a list the user can still act on with an error card. -->
      <p v-if="store.loadError" class="cat-stale" role="status">
        <span>{{ store.loadError }} These are the categories as they were last read.</span>
        <button type="button" class="cat-link" @click="store.reload(workspace)">Retry</button>
      </p>

      <div class="cat-actions">
        <button type="button" class="cat-btn" @click="openDrawerEmpty()">Add category</button>
        <!-- Pink marks the one action worth taking, so it only wears it while
             there is something to save; a quiet "Saved" says the same thing
             without competing with the list for attention. -->
        <button
          type="button"
          class="cat-btn cat-save"
          :class="{ 'cat-btn--primary': dirty }"
          :disabled="!dirty || store.saving"
          @click="saveDraft"
        >{{ store.saving ? 'Saving…' : dirty ? 'Save changes' : 'Saved' }}</button>
        <p v-if="store.error" class="cat-error" role="alert">{{ store.error }}</p>
      </div>

      <p v-if="catEmpty" class="hint">This vault has no categories yet.</p>

      <div v-else class="cat-table-wrap" tabindex="0" role="region" aria-label="Categories">
        <table>
          <thead>
            <tr>
              <th scope="col">Category</th>
              <th scope="col" class="cat-col-wide">Kind</th>
              <th scope="col">Folder</th>
              <th scope="col" class="cat-num">Notes</th>
              <th scope="col" class="cat-col-wide">Stale after</th>
              <th scope="col" class="cat-actions-col">On</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in visibleRows"
              :key="row.id"
              class="cat-row"
              :class="{ 'cat-row--off': !row.enabled }"
              @click="openDrawerFor(row)"
            >
              <td class="cat-name">
                <!-- The row opens the drawer, but a plain row handler is
                     unreachable by keyboard: the label is a real button. -->
                <button
                  type="button"
                  class="cat-name-btn"
                  :aria-label="`Edit ${row.label}`"
                  @click.stop="openDrawerFor(row)"
                >{{ row.label }}</button>
                <span class="badge" :class="row.builtin ? 'badge--builtin' : 'badge--muted'">
                  {{ row.builtin ? 'Built-in' : 'Custom' }}
                </span>
                <span v-if="changedIds.has(row.id)" class="cat-flag">Unsaved</span>
              </td>
              <td class="cat-muted cat-col-wide">{{ row.kind === 'entity' ? 'Entity' : 'Note' }}</td>
              <td class="cat-muted"><code>{{ row.folder || '—' }}</code></td>
              <td class="cat-num">{{ row.note_count.toLocaleString() }}</td>
              <td class="cat-muted cat-col-wide">{{ staleHint(row) }}</td>
              <td class="cat-actions-col">
                <button
                  type="button"
                  role="switch"
                  class="cat-switch"
                  :class="{ on: row.enabled }"
                  :aria-checked="row.enabled ? 'true' : 'false'"
                  :aria-disabled="row.core ? 'true' : undefined"
                  :disabled="row.core"
                  :title="row.core ? `${row.label} is required by Ciaobot and stays on` : undefined"
                  :aria-label="row.core ? `${row.label} is required and always on` : `${row.enabled ? 'Disable' : 'Enable'} ${row.label}`"
                  @click.stop="setEnabled(row.id, !row.enabled)"
                >
                  <!-- The button is the tap target and the track inside it is
                       the visual, so a touch layout can have both: 44×44 to hit
                       and the 40px pill to look at. -->
                  <span class="cat-switch-track" aria-hidden="true">
                    <span class="cat-switch-dot"></span>
                  </span>
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- One drawer for both jobs: Add opens it empty (or from a preset), a row
         opens it on that row. -->
    <div v-if="drawerOpen" class="modal-backdrop" @click.self="closeDrawer">
      <div
        ref="drawerEl"
        class="modal-sheet cat-drawer"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="adding ? 'cat-add-title' : 'cat-edit-title'"
      >
        <header class="cat-drawer-head">
          <h3 :id="adding ? 'cat-add-title' : 'cat-edit-title'">{{ adding ? 'Add category' : form.label }}</h3>
          <button type="button" class="cat-close" aria-label="Close" @click="closeDrawer">×</button>
        </header>

        <div v-if="adding && availablePresets.length" class="cat-presets">
          <p class="cat-presets-label" id="cat-presets-label">Start from</p>
          <div class="cat-preset-row" role="group" aria-labelledby="cat-presets-label">
            <button
              v-for="preset in availablePresets"
              :key="preset.id"
              type="button"
              class="cat-preset"
              @click="openDrawerForPreset(preset)"
            >{{ preset.label }}</button>
          </div>
          <p v-if="takenPresets.length" class="cat-presets-note">
            Already in your list: {{ takenPresets.join(', ') }}.
          </p>
        </div>

        <form class="cat-form" novalidate @submit.prevent="saveFromDrawer">
          <div class="cat-field">
            <label for="cat-id">ID</label>
            <input
              id="cat-id"
              ref="idField"
              v-model="form.id"
              type="text"
              autocomplete="off"
              :readonly="idReadOnly"
              :aria-invalid="idError ? 'true' : undefined"
              :aria-describedby="idError ? 'cat-id-error' : 'cat-id-help'"
            />
            <p v-if="idError" id="cat-id-error" class="cat-field-error" role="alert">{{ idError }}</p>
            <p v-else id="cat-id-help" class="cat-field-note">
              {{ idReadOnly
                ? 'The id is how notes name this category, so it cannot change once it exists.'
                : 'What a note writes as type:. Lower-case, hyphen-separated.' }}
            </p>
          </div>

          <div class="cat-field">
            <label for="cat-label">Label</label>
            <input
              id="cat-label"
              ref="labelField"
              v-model="form.label"
              type="text"
              autocomplete="off"
              :aria-invalid="labelError ? 'true' : undefined"
              :aria-describedby="labelError ? 'cat-label-error' : undefined"
            />
            <p v-if="labelError" id="cat-label-error" class="cat-field-error" role="alert">{{ labelError }}</p>
          </div>

          <div class="cat-field">
            <label for="cat-description">Use this when…</label>
            <textarea id="cat-description" v-model="form.description" rows="2"></textarea>
          </div>

          <div class="cat-field">
            <label for="cat-folder">Folder</label>
            <input id="cat-folder" v-model="form.folder" type="text" autocomplete="off" />
            <p class="cat-field-note">Where new notes of this type go. Two categories cannot share one.</p>
          </div>

          <div class="cat-field">
            <label for="cat-aliases">Aliases</label>
            <input id="cat-aliases" v-model="form.aliases" type="text" autocomplete="off" />
            <p class="cat-field-note">Comma-separated. Other spellings a note might use for this type.</p>
          </div>

          <div class="cat-field">
            <label for="cat-days">Stale after (days)</label>
            <input
              id="cat-days"
              v-model.number="form.stale_after_days"
              type="number"
              min="0"
              step="1"
              :aria-invalid="daysError ? 'true' : undefined"
              :aria-describedby="daysError ? 'cat-days-error' : 'cat-days-help'"
            />
            <p v-if="daysError" id="cat-days-error" class="cat-field-error" role="alert">{{ daysError }}</p>
            <p v-else id="cat-days-help" class="cat-field-note">0 uses the default. A note past it asks to be re-checked.</p>
          </div>

          <div class="cat-field">
            <label for="cat-kind">Kind</label>
            <select id="cat-kind" v-model="form.kind">
              <option value="entity">Entity — a record of the thing</option>
              <option value="note">Note — a document about something</option>
            </select>
          </div>

          <!-- The scope note, in one line: this edits the list the vault is
               configured with, and the consumers that read it are still to come
               (A2a). Saying so is honest; faking the behaviour is not. -->
          <p class="cat-scope">
            Folder, aliases and stale-after are stored now; they start changing how
            notes are sorted, linted and aged once that wiring lands.
          </p>

          <p v-if="store.error" class="cat-error" role="alert">{{ store.error }}</p>

          <div class="cat-drawer-actions">
            <button
              v-if="!adding && !form.builtin"
              type="button"
              class="cat-btn cat-btn--danger"
              :disabled="store.saving"
              @click="removeRow(editingRow)"
            >Delete category</button>
            <span v-else-if="!adding" class="cat-field-note">
              A built-in category can be turned off, but not deleted.
            </span>
            <span class="cat-drawer-spacer"></span>
            <button type="button" class="cat-btn" :disabled="savingFromDrawer" @click="closeDrawer">Cancel</button>
            <button
              type="submit"
              class="cat-btn cat-btn--primary"
              :disabled="!formValid || savingFromDrawer"
            >{{ savingFromDrawer ? 'Saving…' : 'Save' }}</button>
          </div>
        </form>
      </div>
    </div>
  </section>
</template>

<style scoped>
.cat-panel { display: flex; flex-direction: column; min-width: 0; }

.cat-head { margin-bottom: var(--space-4); }
.cat-h {
  margin: 0;
  color: var(--fg);
  font-size: calc(16px * var(--font-scale, 1));
  font-weight: 700;
  letter-spacing: -0.02em;
  line-height: 1.3;
}
.cat-lede {
  margin: 6px 0 0;
  max-width: 64ch;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.55;
}
.cat-lede code { font-family: var(--font-mono); }

/* The explanation: one neutral surface, quiet type, no accent — it is reading,
   not a control, and nothing in it is clickable. */
.cat-why {
  margin-bottom: var(--space-3);
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
}
.cat-why-h {
  margin: 0 0 4px;
  color: var(--fg);
  font-size: var(--text-sm);
  font-weight: 700;
}
.cat-why-line {
  margin: 0;
  /* The surface spans the pane like the table below it, but the reading keeps
     a measure: prose across a 1400px pane is 130 characters a line. */
  max-width: 78ch;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.55;
}
.cat-why-line strong { color: var(--fg); font-weight: 600; }
.cat-why-line em { font-style: normal; color: var(--fg3); }
.cat-why-foot { margin-top: 4px; }


.cat-failed {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-left: 3px solid var(--error);
  border-radius: var(--radius-sm);
  background: var(--bg2);
}
.cat-failed-text { margin: 0; color: var(--fg); font-size: var(--text-sm); }

.cat-stale {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-2);
  margin: 0 0 var(--space-3);
  padding: 8px 12px;
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--warning);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--warning) 6%, var(--bg2));
  color: var(--fg2);
  font-size: var(--text-sm);
}

.cat-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}
.cat-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 34px;
  padding: 0 14px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg3);
  color: var(--fg);
  font: inherit;
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
  transition: border-color 120ms var(--ease);
}
.cat-btn:hover:not(:disabled) { border-color: var(--fg3); }
.cat-btn:disabled { opacity: 0.55; cursor: default; }
.cat-btn--primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
.cat-btn--danger { background: none; border-color: var(--border); color: var(--fg2); font-weight: 500; }
.cat-btn--danger:hover:not(:disabled) { color: var(--error); border-color: var(--error); }
.cat-link {
  min-height: 28px;
  padding: 0 4px;
  border: 0;
  background: none;
  color: var(--accent);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}
.cat-link:hover { text-decoration: underline; text-underline-offset: 3px; }

.cat-error {
  margin: 0;
  flex-basis: 100%;
  color: var(--error);
  font-size: var(--text-sm);
  line-height: 1.5;
}

/* ── The list ──────────────────────────────────────────────────────────────
   A table, but a compact one: it fits its content instead of stretching. */
.cat-table-wrap {
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.cat-table-wrap:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.cat-table-wrap table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
.cat-table-wrap th {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border-strong);
  color: var(--fg3);
  font-size: var(--text-xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  white-space: nowrap;
}
.cat-table-wrap td {
  padding: 4px 10px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.cat-table-wrap tbody tr:last-child td { border-bottom: 0; }
.cat-table-wrap tbody tr:hover { background: var(--bg2); }
.cat-num { text-align: right; font-variant-numeric: tabular-nums; }
.cat-muted { color: var(--fg3); white-space: nowrap; }
.cat-muted code { font-family: var(--font-mono); }

/* At phone width the two metadata columns are what pushes the enable switch
   off the right edge, and the switch is the row's whole point — so Kind and
   Stale after go, and stay readable in the drawer. The label, the folder and
   the count stay, because they are what identifies the row. */
@media (max-width: 700px) {
  .cat-col-wide { display: none; }
  .cat-table-wrap th,
  .cat-table-wrap td { padding-inline: 6px; }
}

.cat-row { cursor: pointer; }
/* A disabled category is still listed and still editable — it is quiet, not
   gone, and the switch stays legible. */
.cat-row--off .cat-name-btn,
.cat-row--off .cat-muted,
.cat-row--off .cat-num { color: var(--fg3); }

.cat-name { display: flex; align-items: center; flex-wrap: wrap; gap: 4px var(--space-2); }
/* The label is the row's action, so it is a button and carries the full 44px
   target; it reads as the plain text it replaced. */
.cat-name-btn {
  display: flex;
  align-items: center;
  min-height: var(--touch);
  padding: 0;
  border: 0;
  background: none;
  font: inherit;
  color: var(--fg);
  font-weight: 600;
  text-align: left;
  cursor: pointer;
}
.cat-name-btn:hover { text-decoration: underline; text-underline-offset: 3px; }
.cat-name-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.cat-flag {
  padding: 1px 6px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
  color: var(--fg3);
  font-size: var(--text-xs);
  white-space: nowrap;
}

.cat-actions-col { width: 1%; white-space: nowrap; }
/* The button is the target and the track inside it is the picture, so the two
   can differ in size: a touch layout widens the button to the full 44px while
   the pill stays the 40px it has always been. */
.cat-switch {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 24px;
  padding: 0;
  border: 0;
  background: none;
  cursor: pointer;
}
.cat-switch-track {
  display: inline-flex;
  align-items: center;
  width: 40px;
  height: 24px;
  padding: 2px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-pill);
  background: var(--bg);
  transition: background 120ms var(--ease), border-color 120ms var(--ease);
}
.cat-switch.on .cat-switch-track { background: var(--accent); border-color: var(--accent); }
.cat-switch:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.cat-switch-dot {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--fg2);
  transition: transform 120ms var(--ease);
}
.cat-switch.on .cat-switch-dot { background: var(--on-accent); transform: translateX(16px); }
/* A core category stays on: the switch reads as a fact, not a control. */
.cat-switch:disabled { opacity: 0.5; cursor: not-allowed; }
/* The hit area is the switch, and a touch layout needs the full 44px of it in
   both directions — DESIGN.md's 44×44, which a 40px track cannot be on its own. */
@media (pointer: coarse) {
  .cat-switch { min-width: var(--touch); min-height: var(--touch); height: var(--touch); }
  /* A short label ("Idea") is less than 44px of text, so the row's own target
     is widened to the same minimum the switch just claimed. */
  .cat-name-btn { min-width: var(--touch); }
  .cat-btn { min-height: var(--touch); }
  .cat-link { min-height: var(--touch); }
}

/* ── The drawer ─────────────────────────────────────────────────────────── */
.cat-drawer { max-width: 560px; }
.cat-drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--border);
}
.cat-drawer-head h3 { margin: 0; font-size: var(--text-lg); font-weight: 700; }
.cat-close {
  width: var(--touch);
  height: var(--touch);
  border: 0;
  background: none;
  color: var(--fg2);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}
.cat-close:hover { color: var(--fg); }

.cat-presets { padding: var(--space-3) var(--space-4) 0; }
.cat-presets-label {
  margin: 0 0 6px;
  color: var(--fg3);
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.cat-preset-row { display: flex; flex-wrap: wrap; gap: 6px; }
.cat-preset {
  min-height: 32px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  transition: border-color 120ms var(--ease), color 120ms var(--ease);
}
.cat-preset:hover { color: var(--fg); border-color: var(--fg3); }
@media (pointer: coarse) { .cat-preset { min-height: var(--touch); } }
.cat-presets-note { margin: 6px 0 0; color: var(--fg3); font-size: var(--text-sm); }

.cat-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
  overflow-y: auto;
}
.cat-field { display: flex; flex-direction: column; gap: 4px; }
.cat-field > label {
  color: var(--fg2);
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.cat-field input,
.cat-field select,
.cat-field textarea {
  width: 100%;
  min-height: var(--touch);
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg);
  font: inherit;
  font-size: var(--text-base);
}
.cat-field input:focus-visible,
.cat-field select:focus-visible,
.cat-field textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: -1px; }
.cat-field input[readonly] { color: var(--fg3); background: var(--bg2); }
.cat-field-note { margin: 0; color: var(--fg3); font-size: var(--text-sm); line-height: 1.5; }
.cat-field-error { margin: 0; color: var(--error); font-size: var(--text-sm); line-height: 1.5; }

.cat-scope {
  margin: 0;
  padding: 8px 12px;
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--accent2);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--accent2) 6%, var(--bg2));
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
}

.cat-drawer-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
}
.cat-drawer-spacer { flex: 1; }
</style>
