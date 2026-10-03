<template>
  <!-- Import conversations: the Memory entry point for #1029 (C5).
       The scan is engine-host and opt-in — nothing is listed until the reader
       presses Find, and the panel says so rather than discovering on mount.
       Ciaobot's own and unreadable rows are drawn, disabled, and never
       selectable; selection is client-side and never auto-filled. The
       confirmation below it is the whole point of the panel: exactly what would
       be processed, which provider and model would receive it, an estimate, and
       the batch cap, all before anything runs. -->
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

      <!-- Everything a person is told before the first model call. Extraction
           is C7 and the batch store is C6: there is no Start here on purpose. -->
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
        <p class="import-confirm-line">
          {{ preview.provider }} / {{ preview.model }} would receive this text, about
          <strong>{{ preview.estimated_chars.toLocaleString() }}</strong> characters across
          <strong>{{ preview.estimated_messages.toLocaleString() }}</strong> turns.
          At most <strong>{{ preview.batch_cap }}</strong> conversations per batch.
          Extraction files proposals for a person to review; it writes nothing itself.
        </p>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../lib/api'
import { useProjectStore } from '../stores/projects'

interface ImportSource {
  provider: string
  source_id: string
  project_hint?: string
  path?: string
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

const candidates = computed(() => rows.value.available)
const excluded = computed(() => rows.value.excluded)
const unsupported = computed(() => rows.value.unsupported)
const processable = computed(() => preview.value?.conversations.filter(c => c.state === 'ready') ?? [])
const refused = computed(() => preview.value?.conversations.filter(c => c.state !== 'ready') ?? [])

const truncatedNotes = computed(() =>
  Object.entries(rows.value.truncated)
    .filter(([, capped]) => capped)
    .map(([provider]) => `${provider} lists one page of conversations, so older ones are not shown.`),
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

function toggle(source: ImportSource) {
  const key = keyOf(source)
  // No auto-select-all anywhere: a person ticks what they mean to process.
  selected.value = selected.value.includes(key)
    ? selected.value.filter(k => k !== key)
    : [...selected.value, key]
  preview.value = null
}

function clearSelection() {
  selected.value = []
  preview.value = null
  previewError.value = ''
}

async function load() {
  if (!workspace.value || loading.value) return
  loading.value = true
  loadError.value = ''
  try {
    const payload = await api.get<{ sources: SourcesPayload }>(
      `/api/import/sources?workspace=${encodeURIComponent(workspace.value)}`,
    )
    rows.value = payload.sources
    listed.value = true
    clearSelection()
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function previewSelection() {
  if (!workspace.value || !selected.value.length) return
  previewing.value = true
  previewError.value = ''
  preview.value = null
  try {
    const chosen = candidates.value.filter(s => selected.value.includes(keyOf(s)))
    const payload = await api.post<{ preview: PreviewPayload }>('/api/import/preview', {
      workspace: workspace.value,
      sources: chosen.map(s => ({ provider: s.provider, source_id: s.source_id })),
    })
    preview.value = payload.preview
  } catch (err) {
    previewError.value = err instanceof Error ? err.message : String(err)
  } finally {
    previewing.value = false
  }
}

// A workspace switch drops the listing: the rows named the old one, and a
// selection that outlived it would be a selection of conversations the reader
// cannot see.
watch(workspace, () => {
  listed.value = false
  rows.value = { available: [], excluded: [], unsupported: [], truncated: {} }
  clearSelection()
})
</script>

<style scoped>
.import-sources {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0 var(--space-3) var(--space-3);
  padding: var(--space-3);
}
.import-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.import-title { margin: 0; font-size: var(--text-base); font-weight: 650; }
.import-head-actions, .import-actions { display: flex; gap: var(--space-2); flex-wrap: wrap; }
.import-quiet { min-height: var(--touch); }
.import-status { margin: 0; color: var(--fg2); font-size: var(--text-sm); display: flex; gap: var(--space-2); align-items: center; flex-wrap: wrap; }
.import-error { color: var(--error); }
.import-warn { color: var(--warning); }
.import-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
.import-row { border: 1px solid var(--border); border-radius: var(--radius-sm); }
.import-row--off { opacity: 0.75; }
/* The whole row is the hit target: a checkbox alone is under the 44px floor. */
.import-check {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  min-height: var(--touch);
  padding: var(--space-2);
  cursor: pointer;
}
.import-row-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.import-row-title { font-size: var(--text-sm); }
.import-row-meta { font-size: var(--text-xs); color: var(--fg3); overflow-wrap: anywhere; }
.import-aside { margin-top: var(--space-1); }
.import-aside-title { cursor: pointer; min-height: var(--touch); display: flex; align-items: center; font-size: var(--text-sm); color: var(--fg2); }
.import-confirm { border-top: 1px solid var(--border); padding-top: var(--space-2); display: flex; flex-direction: column; gap: var(--space-2); }
.import-confirm-title { margin: 0; font-size: var(--text-sm); font-weight: 650; }
.import-confirm-line { margin: 0; font-size: var(--text-sm); color: var(--fg2); }
.import-confirm-list { margin: 0; padding-left: 1.2em; font-size: var(--text-sm); color: var(--fg2); display: flex; flex-direction: column; gap: 2px; }
.import-confirm-refused { color: var(--fg3); }
</style>