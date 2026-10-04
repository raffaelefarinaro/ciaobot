<template>
  <!-- Import conversations: the Memory → Import section (#1029, C5; completed
       as one journey by #1041, C8). It is its own scrolling page rather than a
       card on the map: the listing can be hundreds of rows long, and the
       controls below it — Review, Cancel, and everything the confirmation states
       — have to stay reachable. The scan is engine-host and opt-in — nothing is
       listed until the reader presses Find, and the panel says so rather than
       discovering on mount. Ciaobot's own and unreadable rows are drawn,
       disabled, and never selectable; selection is client-side and never
       auto-filled.

       The confirmation below the list is the whole point of this half: exactly
       what would be processed, which provider and model would receive it, an
       estimate, and the batch cap, all before anything runs. From there the
       panel files a batch (C6) and `<ImportRuns>` owns everything after it — the
       run (C7), its progress, its cancel, the proposals it filed and the
       retention the store enforces. One journey, two components: the consent
       screen and the runs list, sharing the listing's row rules and its touch
       floor. Both are reachable from here in the app and from the first-run
       welcome, which is the same page rather than a second flow. -->
  <section class="import-sources card" aria-labelledby="import-sources-title">
    <div class="import-head">
      <h2 id="import-sources-title" class="import-title">Import conversations</h2>
      <div class="import-head-actions">
        <button
          type="button"
          class="btn-primary btn-small"
          :disabled="loading || !workspace"
          @click="load"
        >{{ listed ? 'Look again' : 'Find conversations' }}</button>
        <button
          v-if="listed"
          type="button"
          class="btn-small import-quiet"
          @click="clearSelection"
        >Cancel selection</button>
      </div>
    </div>
    <p class="hint">
      Looks in this computer's own Claude Code and OpenCode folders for this workspace.
      Nothing is read until you choose a conversation, and nothing is sent anywhere until
      you confirm. Ciaobot's own conversations are never offered.
    </p>

    <!-- Load state 1: the first listing is in flight. -->
    <p v-if="loading && !listed" class="import-status" role="status" aria-live="polite">
      Looking for conversations on this computer…
    </p>
    <!-- Load state 2: the first listing failed. Never an empty-state claim. -->
    <p v-else-if="loadError && !listed" class="import-status import-error" role="alert">
      {{ loadError }}
      <button type="button" class="btn-small import-quiet" @click="load">Retry</button>
    </p>
    <template v-else-if="listed">
      <!-- Load state 3: a refresh failed over rows already on screen. The rows
           stay and are marked stale, rather than being cleared into a lie. -->
      <p v-if="loadError" class="import-status import-error" role="alert">
        These rows may be out of date: {{ loadError }}
        <button type="button" class="btn-small import-quiet" @click="load">Retry</button>
      </p>
      <p v-if="loading" class="import-status" role="status" aria-live="polite">Looking again…</p>

      <!-- Load state 4: a genuine empty listing, said in its own words. -->
      <p v-else-if="!candidates.length && !excluded.length && !unsupported.length" class="import-status">
        No past conversations found for {{ workspace }}. Run Claude Code or OpenCode in this
        workspace and look again.
      </p>

      <template v-else>
        <p v-for="note in truncatedNotes" :key="note" class="import-status import-warn">{{ note }}</p>
        <ul v-if="candidates.length" class="import-list">
          <li v-for="row in candidates" :key="keyOf(row)" class="import-row">
            <label class="import-check">
              <input
                type="checkbox"
                :checked="selected.includes(keyOf(row))"
                @change="toggle(row)"
              />
              <span class="import-row-body">
                <span class="import-row-title">{{ labelOf(row) }}</span>
                <span class="import-row-meta">{{ metaOf(row) }}</span>
              </span>
            </label>
          </li>
        </ul>
        <!-- Excluded and unsupported rows are shown with their reason, never
             selectable: a conversation the user can see in their own tool and
             cannot import here has to say why. -->
        <details v-if="excluded.length" class="import-aside">
          <summary class="import-aside-title">
            Not importable ({{ excluded.length }})
          </summary>
          <ul class="import-list">
            <li v-for="row in excluded" :key="keyOf(row.ref)" class="import-row import-row--off">
              <span class="import-row-body">
                <span class="import-row-title">{{ labelOf(row.ref) }}</span>
                <span class="import-row-meta">{{ row.message || row.reason }}</span>
              </span>
            </li>
          </ul>
        </details>
        <details v-if="unsupported.length" class="import-aside">
          <summary class="import-aside-title">Sources unavailable ({{ unsupported.length }})</summary>
          <ul class="import-list">
            <li v-for="row in unsupported" :key="row.provider" class="import-row import-row--off">
              <span class="import-row-body">
                <span class="import-row-title">{{ row.provider }}</span>
                <span class="import-row-meta">{{ row.message || row.reason }}</span>
              </span>
            </li>
          </ul>
        </details>
      </template>

      <div class="import-actions">
        <button
          type="button"
          class="btn-primary btn-small"
          :disabled="!selected.length || previewing"
          @click="previewSelection"
        >{{ previewing ? 'Reading…' : `Review ${selected.length} selected` }}</button>
        <button
          v-if="selected.length"
          type="button"
          class="btn-small import-quiet"
          @click="clearSelection"
        >Cancel</button>
      </div>
      <p v-if="previewError" class="import-status import-error" role="alert">{{ previewError }}</p>

      <!-- Everything a person is told before the first model call. The batch is
           filed from here (C6) and the extraction is started from the runs list
           below (C7), because those are two different writes and the second one
           is the only route in this journey that reads a conversation. -->
      <section v-if="preview" class="import-confirm" aria-labelledby="import-confirm-title">
        <h3 id="import-confirm-title" class="import-confirm-title">Before anything runs</h3>
        <p class="import-confirm-line">
          <strong>{{ preview.conversations.length }}</strong> of
          <strong>{{ selected.length }}</strong> selected can be processed, into
          <code>{{ preview.destination }}</code>.
        </p>
        <ul class="import-confirm-list">
          <li v-for="row in processable" :key="keyOf(row.source)">
            {{ labelOf(row.source) }} — {{ row.message_count }} turns
            <span v-if="row.first_date">, dated {{ row.first_date }}</span>
            <span v-if="row.already_imported">, already imported</span>
            <span v-if="Object.keys(row.omitted).length">, omitted: {{ omittedText(row) }}</span>
          </li>
          <li v-for="row in refused" :key="keyOf(row.source)" class="import-confirm-refused">
            {{ labelOf(row.source) }} — {{ row.message || row.reason }}
          </li>
        </ul>
        <!-- One sentence, on purpose. "{provider} / {model} would receive this
             text" reads as a promise, and the run resolves the model per
             conversation's provider — so the two facts sit together or the first
             one contradicts the second. -->
        <p class="import-confirm-line">
          This workspace's default is <strong>{{ preview.provider }} / {{ preview.model }}</strong>:
          about <strong>{{ preview.estimated_chars.toLocaleString() }}</strong> characters across
          <strong>{{ preview.estimated_messages.toLocaleString() }}</strong> turns, and at most
          <strong>{{ preview.batch_cap }}</strong> conversations per batch. Each conversation
          is read by the model Ciaobot uses for its own insights on that conversation's
          provider, which can differ from the workspace default. Every fact it finds waits
          for you in <strong>To decide</strong>; the import writes nothing into your memory
          by itself.
        </p>
        <div class="import-confirm-actions">
          <button
            type="button"
            class="btn-primary btn-small"
            :disabled="!processable.length || filing"
            @click="fileBatch"
          >{{ filing ? 'Filing…' : `File an import of ${processable.length} conversation${processable.length === 1 ? '' : 's'}` }}</button>
          <button
            type="button"
            class="btn-small import-quiet"
            @click="clearSelection"
          >Cancel</button>
        </div>
        <p v-if="fileError" class="import-status import-error" role="alert">{{ fileError }}</p>
      </section>
    </template>

    <!-- The filed batches, their progress, the cancel and the retention: the rest
         of the same journey, and the half that outlives the selection. It reads
         Ciaobot's own state only — a filed batch, never provider history. -->
    <ImportRuns />
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../lib/api'
import { useImportStore } from '../stores/import'
import { useProjectStore } from '../stores/projects'
import ImportRuns from './ImportRuns.vue'

interface ImportSource {
  provider: string
  source_id: string
  project_hint?: string
}
interface ExcludedRow {
  ref: ImportSource
  reason: string
  message: string
}
interface UnsupportedRow {
  provider: string
  reason: string
  message: string
}
interface SourcesPayload {
  available: ImportSource[]
  excluded: ExcludedRow[]
  unsupported: UnsupportedRow[]
  truncated: Record<string, boolean>
}
interface PreviewRow {
  source: ImportSource
  state: 'ready' | 'excluded' | 'unreadable'
  classification: string
  message_count: number
  estimated_chars: number
  first_date: string
  omitted: Record<string, number>
  already_imported: boolean
  reason: string
  message: string
}
interface PreviewPayload {
  conversations: PreviewRow[]
  provider: string
  model: string
  estimated_chars: number
  estimated_messages: number
  batch_cap: number
  destination: string
}

const store = useProjectStore()
const workspace = computed(() => store.activeWorkspace || '')
/** The filed batches. Only the run list's writes go through it from here: this
 *  panel owns the listing, the selection and the consent, and the store owns
 *  everything that outlives the page. */
const runs = useImportStore()

/** `listed` is "a listing is on screen", not "a listing was requested". */
const listed = ref(false)
const loading = ref(false)
/** A first-load failure and a failed refresh are different: the first has no
 * rows to keep, the second does. One slot each, never merged. */
const loadError = ref('')
const rows = ref<SourcesPayload>({ available: [], excluded: [], unsupported: [], truncated: {} })
const selected = ref<string[]>([])
const previewing = ref(false)
const previewError = ref('')
const preview = ref<PreviewPayload | null>(null)
/** Filing a batch is the panel's one write, so it gets its own busy flag and its
 *  own error slot: the preview's refusal and the file's are different answers,
 *  and a failed create must not be read as "the preview failed". */
const filing = ref(false)
const fileError = ref('')

/**
 * Ticket for the newest scan, and a monotonic one so two scans of the *same*
 * workspace can also race.
 *
 * Both reads here are one workspace's answer, and two things can move under them:
 * the `1`–`9` shortcuts switch the pane, and a second Find supersedes the first.
 * A stale answer is dropped rather than drawn. That is not tidiness — under
 * another workspace it puts one workspace's conversation ids beside another
 * workspace's name, and `POST /api/import/batches` checks that the ids are
 * Ciaobot's own and not whose history they name, so the person would be shown
 * another workspace's checkboxes and could file them under their own consent.
 *
 * `loading` is only cleared by whoever still holds the ticket, so a dropped
 * answer cannot switch off the progress line a newer scan is drawing, and the
 * workspace watcher clears it instead — otherwise Find would stay disabled for a
 * workspace whose own scan has not been made yet.
 */
let scanSeq = 0

/** The same ticket for the consent preview, retired by `discardPreview`. */
let previewSeq = 0

const candidates = computed(() => rows.value.available)
const excluded = computed(() => rows.value.excluded)
const unsupported = computed(() => rows.value.unsupported)
const processable = computed(() => preview.value?.conversations.filter(c => c.state === 'ready') ?? [])
const refused = computed(() => preview.value?.conversations.filter(c => c.state !== 'ready') ?? [])

const truncatedNotes = computed(() =>
  Object.entries(rows.value.truncated)
    .filter(([, capped]) => capped)
    .map(([provider]) => `${provider} lists one page of conversations, so some are not shown.`),
)

function keyOf(source: ImportSource): string {
  return `${source.provider}:${source.source_id}`
}

const PROVIDER_LABELS: Record<string, string> = {
  claude_code: 'Claude Code',
  opencode: 'OpenCode',
}

/** The source's own name, and its own hint verbatim below it.
 *
 * The hint is not a path and not a title: for Claude Code it is the one-way
 * slug `agent_paths.claude_project_slug` folded the workspace path into, and for
 * OpenCode it is the directory the session recorded. Dressing either as a
 * heading would state something neither can support, so both rows say what the
 * source is and show the hint as the weak locator it is. */
function labelOf(source: ImportSource): string {
  return PROVIDER_LABELS[source.provider] ?? source.provider
}

function metaOf(source: ImportSource): string {
  const hint = source.project_hint ? ` · ${source.project_hint}` : ''
  return `session ${source.source_id}${hint}`
}

function omittedText(row: PreviewRow): string {
  return Object.entries(row.omitted).map(([kind, count]) => `${kind} ×${count}`).join(', ')
}

/**
 * Drop the consent screen, and the request that would put it back.
 *
 * The preview answers over one selection, so retiring the selection — or ticking
 * one row while the read is open — retires the request with it: its answer
 * describes rows the reader can no longer act on, and the progress line stops
 * with it rather than waiting for an answer that is no longer wanted.
 */
function discardPreview() {
  previewSeq++
  previewing.value = false
  preview.value = null
}

function toggle(source: ImportSource) {
  const key = keyOf(source)
  // No auto-select-all anywhere: a person ticks what they mean to process.
  selected.value = selected.value.includes(key)
    ? selected.value.filter(k => k !== key)
    : [...selected.value, key]
  discardPreview()
}

function clearSelection() {
  selected.value = []
  previewError.value = ''
  fileError.value = ''
  discardPreview()
}

/**
 * File a batch over the previewed, processable rows (C6).
 *
 * Only the ids of the rows the preview called `ready` go on the wire: a refused
 * row has already said why it will not be processed, and asking the store to
 * file it would only earn the same refusal. Nothing is read and no model is
 * called by this — the batch is a filed *intent*, and the run below it is the
 * first thing in the journey that opens a conversation.
 *
 * The selection is cleared afterwards on success so the consent screen cannot be
 * pressed twice: a second create would be refused by C6 anyway (one open batch
 * per workspace), and a refusal the reader has to interpret is worse than a
 * screen that has moved on to the run they just filed. The filed batch is on
 * screen the moment the POST answers — it is adopted onto the runs list, oldest
 * first — so nothing is re-read here.
 */
async function fileBatch() {
  if (!workspace.value || !processable.value.length || filing.value) return
  // The same guard the two reads take: a create that answers after the `1`–`9`
  // shortcuts have moved the pane belongs to a workspace this screen has left, so
  // its answer is not this screen's to report — and calling a batch that exists a
  // failure is how a person is talked into pressing the button again and meeting
  // a 409.
  const forWorkspace = workspace.value
  filing.value = true
  fileError.value = ''
  const sources = processable.value.map(row => ({
    provider: row.source.provider,
    source_id: row.source.source_id,
  }))
  try {
    const batch = await runs.file(forWorkspace, sources)
    if (forWorkspace !== workspace.value) return
    if (!batch) {
      // The sentence belongs beside the button that was pressed, so it is
      // moved here and cleared from the runs list rather than printed twice.
      fileError.value = runs.error || 'The import could not be filed.'
      runs.clearError()
      return
    }
    clearSelection()
  } finally {
    filing.value = false
  }
}

async function load() {
  if (!workspace.value) return
  const forWorkspace = workspace.value
  const ticket = ++scanSeq
  loading.value = true
  loadError.value = ''
  try {
    const payload = await api.get<{ sources: SourcesPayload }>(
      `/api/import/sources?workspace=${encodeURIComponent(forWorkspace)}`,
    )
    if (ticket !== scanSeq || forWorkspace !== workspace.value) return
    rows.value = payload.sources
    listed.value = true
    clearSelection()
  } catch (err) {
    if (ticket !== scanSeq || forWorkspace !== workspace.value) return
    loadError.value = err instanceof Error ? err.message : String(err)
  } finally {
    if (ticket === scanSeq) loading.value = false
  }
}

async function previewSelection() {
  if (!workspace.value || !selected.value.length) return
  const forWorkspace = workspace.value
  const ticket = ++previewSeq
  previewing.value = true
  previewError.value = ''
  preview.value = null
  try {
    const chosen = candidates.value.filter(s => selected.value.includes(keyOf(s)))
    const payload = await api.post<{ preview: PreviewPayload }>('/api/import/preview', {
      workspace: forWorkspace,
      sources: chosen.map(s => ({ provider: s.provider, source_id: s.source_id })),
    })
    if (ticket !== previewSeq || forWorkspace !== workspace.value) return
    preview.value = payload.preview
  } catch (err) {
    if (ticket !== previewSeq || forWorkspace !== workspace.value) return
    previewError.value = err instanceof Error ? err.message : String(err)
  } finally {
    if (ticket === previewSeq) previewing.value = false
  }
}

// A workspace switch drops the listing: the rows named the old one, and a
// selection that outlived it would be a selection of conversations the reader
// cannot see. It also invalidates whatever is still in flight and clears the busy
// flags, or the rows would be gone but the answers would come back and stay.
watch(workspace, () => {
  scanSeq++
  loading.value = false
  filing.value = false
  loadError.value = ''
  listed.value = false
  rows.value = { available: [], excluded: [], unsupported: [], truncated: {} }
  clearSelection()
})
</script>

<style scoped src="./importSources.css"></style>
<style scoped>
.import-sources {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: var(--space-3);
}
.import-title { margin: 0; font-size: var(--text-base); font-weight: 650; }
.import-confirm { border-top: 1px solid var(--border); padding-top: var(--space-2); display: flex; flex-direction: column; gap: var(--space-2); }
.import-confirm-actions { display: flex; gap: var(--space-2); flex-wrap: wrap; }
.import-confirm-title { margin: 0; font-size: var(--text-sm); font-weight: 650; }
.import-confirm-line { margin: 0; font-size: var(--text-sm); color: var(--fg2); }
.import-confirm-list { margin: 0; padding-left: 1.2em; font-size: var(--text-sm); color: var(--fg2); display: flex; flex-direction: column; gap: 2px; }
.import-confirm-refused { color: var(--fg3); }
</style>