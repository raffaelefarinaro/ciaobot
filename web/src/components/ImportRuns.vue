<template>
  <!-- The run half of the import journey (#1041, C8): the batches C6 filed, the
       progress C7's runner records, the cancel that stops future work without
       unsending provider input, and the retention the store actually enforces.
       It is a child of the panel rather than part of it because the panel's own
       job ends where this begins — it chooses and consents, this remembers what
       was filed. Neither owns a second copy of the other's state: both read the
       same store and the same workspace.

       The five list states are the section's whole contract, and they are the
       same five the discovery list above has to keep: a first read in flight, a
       first read that failed, a refresh that failed over rows already on screen
       (kept and marked stale), a genuine empty list, and a filter that hid
       everything. None of them may read as "there is nothing here". -->
  <section class="import-runs" aria-labelledby="import-runs-title">
    <div class="import-runs-head">
      <h3 id="import-runs-title" class="import-runs-title">Import runs</h3>
      <div class="import-runs-head-actions">
        <button
          type="button"
          class="btn-small import-quiet"
          :disabled="loading || !workspace"
          @click="refresh()"
        >{{ loading && !polling ? 'Checking…' : 'Check for progress' }}</button>
      </div>
    </div>
    <p class="hint">
      Every import filed for <strong>{{ workspace || 'this workspace' }}</strong>, with how far it got.
      Nothing starts on its own: a filed import waits here until you start it.
    </p>

    <!-- Load state 1: the first read is in flight. -->
    <p v-if="loading && !store.loaded" class="import-status" role="status" aria-live="polite">
      Reading your import runs…
    </p>
    <!-- Load state 2: the first read failed. An error with a Retry, never an
         empty list — an import filed an hour ago is still there. -->
    <p v-else-if="store.loadError && !store.loaded" class="import-status import-error" role="alert">
      {{ store.loadError }}
      <button type="button" class="btn-small import-quiet" @click="refresh()">Retry</button>
    </p>
    <template v-else-if="store.loaded">
      <!-- Load state 3: a refresh failed over rows already on screen. They stay
           and are marked stale; clearing them would claim the imports are gone. -->
      <p v-if="store.loadError" class="import-status import-error" role="alert">
        These runs may be out of date: {{ store.loadError }}
        <button type="button" class="btn-small import-quiet" @click="refresh()">Retry</button>
      </p>
      <p v-if="loading && !polling" class="import-status" role="status" aria-live="polite">
        Checking for progress…
      </p>

      <!-- Load state 4: no import has ever been filed for this workspace. -->
      <p v-else-if="!store.batches.length" class="import-status">
        No imports filed for {{ workspace || 'this workspace' }} yet. Choose conversations above,
        confirm them, and the batch you file appears here.
      </p>

      <template v-else>
        <div class="import-run-chips" role="group" aria-label="Show imports">
          <button
            v-for="chip in FILTERS"
            :key="chip.id"
            type="button"
            class="import-chip"
            :aria-pressed="filter === chip.id"
            @click="filter = chip.id"
          >{{ chip.label }}</button>
        </div>

        <!-- Load state 5: a filter hid everything. The rows are still there, so
             this must not read as an empty list, and it must offer the way back. -->
        <p v-if="!visible.length" class="import-status">
          No import matches “{{ activeFilterLabel }}”.
          <button type="button" class="btn-small import-quiet" @click="filter = 'all'">Show all imports</button>
        </p>

        <ul v-else class="import-run-list">
          <li v-for="batch in visible" :key="batch.batch_id" class="import-run">
            <div class="import-run-head">
              <span class="import-run-status" :data-status="batch.status">{{ statusOf(batch) }}</span>
              <span v-if="batch.updated_at" class="import-run-meta">
                filed {{ formatRelative(batch.updated_at) || batch.updated_at.slice(0, 10) }}
              </span>
            </div>

            <!-- One batch at a time is C6's rule, so the bar is the whole batch
                 and the conversation being read is named beside it: a person
                 watching a run wants to know which file it is on. -->
            <p v-if="batch.status === 'running'" class="import-status">
              Reading {{ inFlight(batch) }}
            </p>
            <progress
              v-if="batch.status === 'running'"
              class="import-run-bar"
              :value="batch.progress.completed_sources"
              :max="batch.progress.total_sources || 1"
              :aria-label="`Extraction progress: ${batch.progress.completed_sources} of ${batch.progress.total_sources} conversations`"
            />
            <p class="import-run-line">
              {{ batch.progress.completed_sources }} of {{ batch.progress.total_sources }} conversations
              read · {{ batch.progress.proposals_filed }} proposal{{ batch.progress.proposals_filed === 1 ? '' : 's' }} filed
              <span v-if="batch.progress.skipped">, {{ batch.progress.skipped }} skipped</span>
            </p>
            <p v-if="batch.error" class="import-status import-error">{{ batch.error }}</p>

            <div class="import-run-actions">
              <button
                v-if="batch.status === 'queued'"
                type="button"
                class="btn-primary btn-small"
                :disabled="store.isBusy(batch.batch_id)"
                @click="store.start(workspace, batch.batch_id)"
              >Start extraction</button>
              <router-link
                v-if="batch.progress.proposals_filed > 0"
                class="import-run-review"
                to="/memory/review"
              >Review the {{ batch.progress.proposals_filed }} filed proposal{{ batch.progress.proposals_filed === 1 ? '' : 's' }} in To decide</router-link>
              <button
                v-if="store.isOpen(batch)"
                type="button"
                class="btn-small import-quiet"
                :disabled="store.isBusy(batch.batch_id)"
                @click="store.cancel(workspace, batch.batch_id)"
              >Cancel</button>
              <button
                v-if="!store.isOpen(batch)"
                type="button"
                class="btn-small import-quiet"
                :disabled="store.isBusy(batch.batch_id)"
                @click="store.forget(workspace, batch.batch_id)"
              >Remove this record</button>
            </div>

            <!-- What a cancel does and does not do, said where the button is:
                 keeping the proposals is the point, and text already handed to
                 the provider is gone whatever this button says. Which of those
                 is true depends on whether the run has started, so the sentence
                 does too. -->
            <p v-if="store.isOpen(batch)" class="import-run-note">
              {{ openNote(batch) }}
            </p>
            <p v-else class="import-run-note">
              {{ settledNote(batch) }}
            </p>

            <!-- The evidence this batch recorded, one row per filed bullet: which
                 conversation, which message inside it. The fact's own date rides
                 on the proposal, so review is where a date is read. -->
            <details v-if="batch.provenance.length" class="import-aside">
              <summary class="import-aside-title">
                What this filed ({{ batch.provenance.length }})
              </summary>
              <ul class="import-list">
                <li v-for="(row, index) in batch.provenance" :key="`${row.source_id}:${row.anchor}:${index}`" class="import-row import-row--off">
                  <span class="import-row-body">
                    <span class="import-row-title">
                      {{ providerName(row.provider) }} · session {{ row.source_id }}
                    </span>
                    <span class="import-row-meta">
                      message {{ row.anchor || 'unknown' }} — {{ row.accepted ? 'accepted' : 'waiting for review' }}
                    </span>
                  </span>
                </li>
              </ul>
            </details>
          </li>
        </ul>
      </template>
    </template>

    <p v-if="store.error" class="import-status import-error" role="alert">{{ store.error }}</p>

    <!-- Retention, as C6 enforces it: an undecided import's private snapshot is
         dropped on a timer, an accepted fact keeps its provenance, and removing
         a record never deletes a memory. The window is the server's own number
         (retention_days), not a copy here. -->
    <p class="import-run-retention">{{ retentionNote }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { formatRelative } from '../lib/time'
import { useImportStore, type ImportBatch, type ImportRunFilter } from '../stores/import'
import { useProjectStore } from '../stores/projects'

/**
 * How often an open batch is re-read.
 *
 * While a batch is `queued` or `running` the only source of progress is
 * `GET /api/import/batches`: the run route starts the extraction and answers
 * before any of it happens, so there is no socket event to subscribe to. 2.5s is
 * long enough not to hammer the engine through a conversation-length extraction
 * and short enough that "watch it progress" reads as progress. The poll only
 * exists while something is open, so a settled list is never polled at all.
 */
const POLL_MS = 2500

const FILTERS: { id: ImportRunFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'open', label: 'Still open' },
  { id: 'settled', label: 'Settled' },
]

const store = useImportStore()
const projects = useProjectStore()
const workspace = computed(() => projects.activeWorkspace || '')

const filter = ref<ImportRunFilter>('all')
/** A read in flight, whatever asked for it. */
const loading = computed(() => store.loading)
/** True while the re-read that filled the screen was the poll, not a press. A
 *  poll every 2.5s must not flash "Checking for progress…" at a person who is
 *  watching a run. */
const polling = ref(false)

const activeFilterLabel = computed(
  () => FILTERS.find(chip => chip.id === filter.value)?.label ?? 'All',
)

const visible = computed(() => {
  if (filter.value === 'open') return store.batches.filter((row) => store.isOpen(row))
  if (filter.value === 'settled') return store.batches.filter((row) => !store.isOpen(row))
  return store.batches
})

const hasOpen = computed(() => store.batches.some((row) => store.isOpen(row)))

/**
 * The window as the sweep actually prunes on.
 *
 * `prune_expired` drops *any* terminal batch whose `updated_at` is older than the
 * window, whatever became of its proposals, and it runs once per boot — so the
 * honest sentence is "about N days, checked when Ciaobot starts" and says nothing
 * about what the reader decided. "If you never decided its proposals" would have
 * promised a longer life for an undecided import than the store gives it.
 */
const retentionNote = computed(() => {
  const days = store.retentionDays
  const window = days > 0 ? `${days} days` : 'a limited time'
  return (
    `Ciaobot keeps a private copy of what an import read, and drops it ${window} after the ` +
    'import settles (checked when Ciaobot starts). Facts you accepted keep a short ' +
    'record of the conversation and message they came from, so an imported fact stays ' +
    'attributable after that copy is gone. Removing an import only removes this record: ' +
    'proposals already filed stay queued, and a fact you accepted stays in your memory. ' +
    'The conversations on this computer are never modified or deleted.'
  )
})

const PROVIDER_NAMES: Record<string, string> = {
  claude_code: 'Claude Code',
  opencode: 'OpenCode',
}

function providerName(provider: string): string {
  return PROVIDER_NAMES[provider] ?? provider
}

/** The conversation the run is on right now, named as its own id. */
function inFlight(batch: ImportBatch): string {
  const sourceId = batch.progress.current_source_id
  return sourceId ? `session ${sourceId}` : 'a conversation'
}

/**
 * A status in the reader's words, not C6's vocabulary. A partial run is the one
 * that needs its own sentence: some conversations were read and filed, some were
 * not read at all, and neither number is the other.
 */
function statusOf(batch: ImportBatch): string {
  switch (batch.status) {
    case 'queued':
      return 'Filed, waiting to start'
    case 'running':
      return 'Running'
    case 'done':
      return 'Finished'
    case 'cancelled':
      return 'Cancelled'
    case 'partial':
      return `Partly done — ${batch.progress.skipped} conversation${batch.progress.skipped === 1 ? '' : 's'} could not be read`
    default:
      return 'Did not finish'
  }
}

/**
 * What the Cancel button will do, per status.
 *
 * Split because the two halves are not both true at once. A `queued` batch has
 * read nothing and sent nothing, so warning that text "cannot be unsent" there
 * describes a loss that has not happened and makes a still-free cancel sound
 * destructive.
 */
function openNote(batch: ImportBatch): string {
  if (batch.status === 'queued') {
    return (
      'Nothing has been read or sent yet. Cancel drops this record, and a ' +
      'conversation is only read once you start the run.'
    )
  }
  return (
    'Cancelling stops the conversations not yet started. Proposals already filed stay ' +
    'queued for you to decide, and text already sent to the configured provider cannot ' +
    'be unsent.'
  )
}

/** What a settled batch left behind, said per status. */
function settledNote(batch: ImportBatch): string {
  const filed = batch.progress.proposals_filed
  if (batch.status === 'cancelled') {
    return (
      `Cancelled after ${batch.progress.completed_sources} of ${batch.progress.total_sources} ` +
      `conversations. ${filed} proposal${filed === 1 ? '' : 's'} stay queued for you to decide; ` +
      'text already sent to the configured provider cannot be unsent.'
    )
  }
  if (batch.status === 'failed') {
    return (
      'The run stopped on an error. Nothing was written to your memory, and anything already ' +
      'filed stays queued for you to decide.'
    )
  }
  if (!filed) {
    return 'This run filed nothing worth keeping. Your memory is unchanged.'
  }
  return (
    `Filed ${filed} proposal${filed === 1 ? '' : 's'}, each carrying the date of the message it ` +
    'came from. Nothing is saved until you accept one in To decide.'
  )
}

async function refresh(): Promise<void> {
  if (!workspace.value) return
  await store.reload(workspace.value)
}

let timer: ReturnType<typeof setTimeout> | null = null

function disarm(): void {
  if (timer !== null) {
    clearTimeout(timer)
    timer = null
  }
}

function arm(): void {
  disarm()
  if (!workspace.value || !hasOpen.value) return
  timer = setTimeout(async () => {
    polling.value = true
    try {
      await store.reload(workspace.value)
    } finally {
      polling.value = false
    }
    arm()
  }, POLL_MS)
}

onUnmounted(disarm)

// A workspace switch re-reads: the store drops the previous workspace's rows on
// its own, so nothing here has to know what the last workspace held.
watch(
  workspace,
  (name) => {
    if (!name) return
    void store.ensureLoaded(name)
    arm()
  },
  { immediate: true },
)

// The poll follows the rows rather than a clock: a run that starts or settles
// arms it, and one that finishes stops it.
watch(hasOpen, arm)

watch(filter, () => store.clearError())
</script>

<style scoped src="./importSources.css"></style>
<style scoped>
.import-runs {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  /* It is the lower half of the panel's own card, not a card inside one: a
     second surface here would read as a separate feature. A rule between the
     two halves is what says "same journey, next step". */
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}
.import-runs-title { margin: 0; font-size: var(--text-base); font-weight: 650; }
.import-run-chips { display: flex; gap: 6px; flex-wrap: wrap; }
.import-chip {
  display: inline-flex;
  align-items: center;
  min-height: var(--touch);
  padding: 0 11px;
  border: 1px solid var(--border);
  border-radius: 9999px;
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}
.import-chip[aria-pressed='true'] {
  border-color: color-mix(in srgb, var(--accent2) 70%, var(--border));
  background: color-mix(in srgb, var(--accent2) 20%, transparent);
  color: var(--fg);
}
.import-run-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
.import-run { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: var(--space-2); display: flex; flex-direction: column; gap: var(--space-2); }
.import-run-head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-2); flex-wrap: wrap; }
.import-run-status { font-size: var(--text-sm); font-weight: 650; }
/* Progress is the one place the accent means "happening now", per DESIGN.md. */
.import-run-bar { width: 100%; height: var(--touch); accent-color: var(--accent); }
.import-run-meta, .import-run-note { font-size: var(--text-xs); color: var(--fg3); overflow-wrap: anywhere; }
.import-run-line { margin: 0; font-size: var(--text-sm); color: var(--fg2); }
.import-run-review { min-height: var(--touch); display: inline-flex; align-items: center; font-size: var(--text-sm); color: var(--accent); }
.import-run-retention { margin: 0; font-size: var(--text-xs); color: var(--fg3); border-top: 1px solid var(--border); padding-top: var(--space-2); }
</style>