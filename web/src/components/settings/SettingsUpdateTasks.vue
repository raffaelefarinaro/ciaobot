<script setup lang="ts">
// Settings → General → "Update task history".
//
// The Home group shows what an update left to do; this is the record of what
// already happened to it, so it owns its own fetch rather than reading the
// housekeeping store — Home may never have been opened in this session, and a
// history that could only be filled by visiting another page is not a record.
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../lib/api'
import { errorMessage, errorPayload } from '../../lib/errorMessage'
import { formatRelative } from '../../lib/time'
import { useProjectStore } from '../../stores/projects'
import type { UpdateTaskRow, UpdateTasksResponse } from '../../lib/types'

const projectStore = useProjectStore()

const rows = ref<UpdateTaskRow[]>([])
const loaded = ref(false)
const loading = ref(false)
const loadError = ref('')
const actionError = ref('')
const pending = ref<Set<string>>(new Set())
let loadSeq = 0

/** The workspace this list is about, or '' before one is known.
 *
 * Applicability and state are per workspace, and the route refuses a nameless
 * question rather than guessing, so there is no default here either: an empty
 * workspace means "not loaded yet", not "the primary one". */
const workspace = computed(() => projectStore.activeWorkspace || '')

/** One row's key: `(id, revision)`, matching the store's and the record's own
 *  identity. Two revisions of a task are two records and two rows. */
function keyOf(row: UpdateTaskRow): string {
  return `${row.id}@${row.revision}`
}

/** What belongs in a *history*.
 *
 * A task still on offer, started, or waiting for review is not history — the
 * Home group is showing it right now, and listing it here as well would put the
 * same live card in two places with two different sets of buttons. What is left
 * is the decided-and-finished end plus the genuinely uncertain: completed,
 * dismissed, failed, and anything whose applicability nobody could establish.
 * That last group is in on purpose. A task that answers `unknown` is not done
 * and not to-do, and the only place that can say so without nagging somebody on
 * Home every sixty seconds is a list they came to look at. */
const historyRows = computed(() =>
  rows.value.filter(
    (row) =>
      row.status === 'completed' ||
      row.status === 'dismissed' ||
      row.status === 'failed' ||
      row.applicability === 'unknown',
  ),
)

/** The history, split by what owns the record.
 *
 * Install first: a task the whole engine owns is a smaller set and a bigger
 * decision, and putting it last would bury it under one workspace's rows. The
 * workspace group names the workspace it belongs to, so an operator with two
 * can tell whose decision they are looking at — the same reason the Home strip
 * labels a group that is not the one you are standing in. */
const groups = computed(() => {
  const install: UpdateTaskRow[] = []
  const workspaceRows: UpdateTaskRow[] = []
  for (const row of historyRows.value) {
    if (row.scope === 'install') install.push(row)
    else workspaceRows.push(row)
  }
  return [
    { scope: 'install', label: 'This install', rows: install },
    { scope: 'workspace', label: `This workspace (${workspace.value})`, rows: workspaceRows },
  ].filter((group) => group.rows.length > 0)
})

/** The word for a row's state, plus the one that says it applies.
 *
 * `unknown` is worded as an absence of knowledge, never of work: a detector
 * that has not run has not established that there is nothing to do, and a
 * history that called that "not applicable" would be making the engine's
 * silence sound like a verdict. */
function stateLabel(row: UpdateTaskRow): string {
  switch (row.status) {
    case 'completed': return 'Done'
    case 'dismissed': return 'Hidden'
    case 'failed': return 'Attempt failed'
    case 'waiting_review': return 'Waiting for review'
    case 'in_progress': return 'Started'
    default: return 'Not started'
  }
}

function outcomeLabel(row: UpdateTaskRow): string {
  if (row.status === 'completed') return 'Verified'
  if (row.status === 'dismissed') return 'Hidden by you'
  if (row.status === 'failed') return 'Last attempt failed'
  return 'Not checked'
}

function revisionLabel(row: UpdateTaskRow): string {
  return row.revision > 1 ? `revision ${row.revision}` : 'first revision'
}

function isPending(row: UpdateTaskRow): boolean {
  return pending.value.has(keyOf(row))
}

async function load(): Promise<void> {
  if (!workspace.value) return
  const seq = ++loadSeq
  const wanted = workspace.value
  loading.value = true
  loadError.value = ''
  try {
    const data = await api.get<UpdateTasksResponse>(
      `/api/update-tasks?workspace=${encodeURIComponent(wanted)}`,
    )
    // A workspace switch (or a second, later load) may have landed while this
    // was in flight; its answer is about a workspace the reader left.
    if (seq !== loadSeq) return
    rows.value = data.tasks ?? []
    loaded.value = true
  } catch (e) {
    if (seq !== loadSeq) return
    // The previous rows stay: a failed refresh is not a cleared history, and an
    // empty one would claim nothing has ever happened.
    loadError.value = 'Could not read the update task history: ' + errorMessage(e, 'the request failed')
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

/** POST one transition, then re-list.
 *
 * Read-mostly by design: the only write this card offers is a reopen, and the
 * route answers 409 rather than doing anything when a reopen does not apply
 * (a task that is not dismissed has an attempt in flight or a verdict already
 * reached, and overriding either is a decision nobody asked for). A refusal is
 * reported in place; the row is never quietly dropped for having failed to
 * change. */
async function transition(row: UpdateTaskRow, action: 'reopen' | 'dismiss'): Promise<void> {
  if (!workspace.value) return
  const key = keyOf(row)
  pending.value = new Set(pending.value).add(key)
  actionError.value = ''
  try {
    await api.post<{ ok: boolean; error?: string }>(
      `/api/update-tasks/${encodeURIComponent(row.id)}/${action}` +
        `?workspace=${encodeURIComponent(workspace.value)}`,
    )
    await load()
  } catch (e) {
    actionError.value =
      action === 'reopen'
        ? 'Could not reopen it: ' + reason(e)
        : 'Could not hide it: ' + reason(e)
  } finally {
    const next = new Set(pending.value)
    next.delete(key)
    pending.value = next
  }
}

/** Plain-language reading of a refusal.
 *
 * The server says *why* in its own words, but those words name ids and versions
 * an operator has no way to act on, so the shape of the refusal is what gets
 * shown. A reopen that was refused is almost always a row that is no longer
 * hidden — which is a no-op the operator should be told about rather than read
 * as a failure. */
function reason(error: unknown): string {
  const payload = errorPayload(error)
  const server = String(payload?.error || errorMessage(error, ''))
  if (server.includes('already completed')) {
    return 'it is already marked done at this revision, so there is nothing to reopen.'
  }
  if (server.includes('no longer shipped') || server.includes('unknown update task')) {
    return 'this install no longer ships that task.'
  }
  if (server.includes('needs engine')) {
    return 'it arrived in a later Ciaobot version, so this engine cannot run it.'
  }
  if (!server) return 'the engine did not answer. Try again.'
  return 'the engine declined, so nothing was changed.'
}

async function openChat(chatId: string): Promise<void> {
  if (!chatId) return
  const { router } = await import('../../router')
  await router.push(`/chat/${chatId}`)
}

onMounted(load)
watch(workspace, (next, previous) => {
  if (next && next !== previous) {
    rows.value = []
    void load()
  }
})
</script>

<template>
  <div class="card">
    <div class="settings-card-header settings-card-header--split">
      <div>
        <p class="section-title">Update task history</p>
        <p class="hint">
          What Ciaobot's own updates left behind, and what became of it. Hiding a
          task on Home is a "not this one" for this workspace and this revision —
          it does not cancel a chat, and it does not mark anything done. Reopening
          re-reads whether the work still applies rather than rerunning old
          instructions.
        </p>
      </div>
      <div class="settings-card-header-actions">
        <button
          type="button"
          class="btn-secondary btn-small"
          :disabled="loading || !workspace"
          @click="load"
        >{{ loading ? 'Checking…' : 'Check again' }}</button>
      </div>
    </div>

    <p v-if="!loaded" class="hint" role="status">Checking the update task history&hellip;</p>

    <p v-if="loadError" class="action-result action-result--error" role="alert">
      {{ loadError }}
    </p>

    <p v-if="actionError" class="action-result action-result--error" role="alert">
      {{ actionError }}
    </p>

    <p v-if="loaded && !loadError && !historyRows.length" class="set-empty">
      <strong>Nothing to show here yet.</strong>
      Ciaobot has not finished, failed or hidden an update task in this
      workspace. Anything on offer is on the Home screen.
    </p>

    <template v-for="group in groups" :key="group.scope">
      <p class="set-group-label">{{ group.label }}</p>
      <div class="set-list">
        <div v-for="row in group.rows" :key="keyOf(row)" class="set-row">
          <div class="set-row-head">
            <div class="set-row-main">
              <p class="set-row-title">
                {{ row.title }}
                <span class="set-tag">{{ stateLabel(row) }}</span>
              </p>
              <p class="set-row-sub">{{ row.why }}</p>
              <p class="set-row-sub">
                <span v-if="row.since_version">Since Ciaobot {{ row.since_version }} · </span>
                <span>{{ revisionLabel(row) }} · </span>
                <span>{{ outcomeLabel(row) }}
                  <template v-if="row.status === 'completed' && row.updated_at">
                    {{ formatRelative(row.updated_at) }}
                  </template>
                </span>
                <template v-if="row.status === 'dismissed' && row.updated_at">
                  · hidden {{ formatRelative(row.updated_at) }}
                </template>
              </p>
              <!-- Two clocks, never merged. `Checked` is when a detector last
                   produced this row's answer and stays put while the answer is
                   inside the server's freshness window; `Decided` is when the
                   record was written. Rendering one under the other's name would
                   claim a re-check that never happened, or hide a decision that
                   did. -->
              <p class="set-row-sub">
                Checked {{ row.applicability_checked_at ? formatRelative(row.applicability_checked_at) : 'never' }}
                <template v-if="row.updated_at && row.status !== 'completed'">
                  · Decided {{ formatRelative(row.updated_at) }}
                </template>
              </p>
            </div>
            <div class="set-row-actions">
              <button
                v-if="row.status === 'dismissed'"
                type="button"
                class="btn-primary btn-small"
                :disabled="isPending(row)"
                @click="transition(row, 'reopen')"
              >{{ isPending(row) ? 'Reopening…' : 'Reopen' }}</button>
              <button
                v-else
                type="button"
                class="btn-secondary btn-small"
                :disabled="isPending(row)"
                @click="load"
              >Recheck</button>
              <button
                v-if="row.chat_id"
                type="button"
                class="btn-secondary btn-small"
                @click="openChat(row.chat_id)"
              >Open its chat</button>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
/* One word naming which record set a list below belongs to. Not a section
   heading: the card's own title already says what this page is, and a second
   heading per group would put two levels of chrome in front of two rows. */
.set-group-label {
  margin: var(--space-3) 0 0;
  font-size: var(--text-xs);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--fg3);
}

.set-group-label:first-of-type {
  margin-top: 0;
}

/* The state word sits inside the title line rather than in its own column: the
   rows are one-sentence things and a second column of short words would
   out-shout the task they describe. */
.set-row-title .set-tag {
  margin-left: var(--space-2);
  vertical-align: middle;
}

.set-row-sub {
  margin-top: 2px;
}
</style>
