<template>
  <section class="cpr" aria-labelledby="cpr-title">
    <p v-if="loading" class="cpr-note">Reading the proposal…</p>
    <p v-else-if="loadError" class="cpr-note cpr-note--error" role="alert">{{ loadError }}</p>

    <article v-else-if="proposal" class="cpr-card">
      <ProposalDetail :proposal="proposal" :open-path="openPath" title-id="cpr-title" />

      <footer class="cpr-foot">
        <template v-if="canDecide">
          <p class="cpr-how">{{ howWords }}</p>
          <div class="cpr-actions">
            <button type="button" class="btn-primary btn-small cpr-action" :disabled="busy !== ''" @click="approve">
              {{ busy === 'approve' ? 'Opening…' : 'Approve' }}
            </button>
            <button type="button" class="btn-small cpr-action" :disabled="busy !== ''" @click="reject">
              {{ busy === 'reject' ? 'Rejecting…' : 'Reject' }}
            </button>
            <button type="button" class="cpr-link cpr-file" @click="fileViewer.open(openPath)">Open the file</button>
          </div>
        </template>
        <p v-else-if="open" class="cpr-how">{{ readOnlyWords }}</p>
        <p v-else class="cpr-how">{{ doneWords }}</p>
        <p class="cpr-status" role="status">{{ status }}</p>
      </footer>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import ProposalDetail from './ProposalDetail.vue'
import type { CareReport } from '../lib/careReport'
import { approveProposal, readProposalFile, rejectProposal } from '../lib/proposalDecision'
import { parseProposal, proposalIsOpen, type Proposal } from '../lib/proposalReview'
import { useCareReportStore } from '../stores/careReport'
import { useFileViewerStore } from '../stores/fileViewer'
import { useProjectStore } from '../stores/projects'

/**
 * One proposal file from the care report, read in place (`ProposalDetail`,
 * shared with Memory · Skills), then Approve or Reject (`lib/proposalDecision`).
 * Approve never edits anything here: it opens a new chat in General with the
 * file pinned and a draft the person sends, because a skill, guide or note
 * change is made only in an attended chat. Reject writes one thing,
 * `status: rejected` in the file's front matter, through the file viewer's own
 * save path (`POST /api/workspace-file`).
 */
const props = defineProps<{ report: CareReport; path: string }>()

const store = useProjectStore()
const care = useCareReportStore()
const fileViewer = useFileViewerStore()

const REPORT_TAIL = 'Workspace/Care-Report.md'

/** The vault folder as the file viewer names it, from the report's own path. */
const vaultPrefix = computed(() => (props.report.report_path.endsWith(REPORT_TAIL)
  ? props.report.report_path.slice(0, -REPORT_TAIL.length)
  : ''))
/** The item as the engine read it: its `proposal_status` comes from a no-symlink, bounded read. */
const item = computed(() => props.report.needs_you.find(entry => entry.path === props.path) ?? null)
const openPath = computed(() => item.value?.open_path || `${vaultPrefix.value}${props.path}`)
/**
 * Approve and Reject are offered only for a proposal the last care report lists
 * and the engine itself read as open. A file it could not read safely (a
 * symlink, say) or one the report does not list can be read here, not decided.
 */
const engineOpen = computed(() => item.value?.proposal_status === 'draft' || item.value?.proposal_status === 'ready')

const proposal = ref<Proposal | null>(null)
const loading = ref(true)
const loadError = ref('')
const status = ref('')
const busy = ref<'' | 'approve' | 'reject'>('')

const open = computed(() => !!proposal.value && proposalIsOpen(proposal.value.status))
const canDecide = computed(() => open.value && engineOpen.value)
const readOnlyWords = computed(() => (item.value
  ? 'The app could not read this proposal safely, so it is shown here to read only.'
  : 'This proposal is not in the last care report, so it is shown here to read only.'))
const howWords = computed(() => (proposal.value?.kind === 'upstream-issue'
  ? 'The issue is written in a chat, with you, and filed only when you say so.'
  : 'The change is made in a chat, with you.'))
const doneWords = computed(() => (proposal.value?.status === 'rejected'
  ? 'This proposal was rejected. Nothing else was changed.'
  : 'This proposal was approved and applied.'))

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  status.value = ''
  try {
    const parsed = parseProposal(await readProposalFile(openPath.value))
    if (!parsed) throw new Error('This file is not in the proposal format. Open it to read it.')
    proposal.value = parsed
  } catch (cause) {
    proposal.value = null
    loadError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

watch(() => props.path, () => { void load() }, { immediate: true })

async function approve(): Promise<void> {
  if (!proposal.value || !canDecide.value || busy.value) return
  busy.value = 'approve'
  status.value = ''
  try {
    const chat = await approveProposal(store, {
      openPath: openPath.value,
      proposal: proposal.value,
      title: proposal.value.title || props.path,
      workspace: props.report.workspace,
    })
    if (chat) status.value = 'A new chat is open with the request. Read it, then press Send.'
  } finally {
    busy.value = ''
  }
}

async function reject(): Promise<void> {
  if (!proposal.value || !canDecide.value || busy.value) return
  busy.value = 'reject'
  status.value = ''
  try {
    // Ask the engine again (its read refuses a symlink) and re-read the file:
    // either may have changed since the review opened, and a proposal already
    // applied or rejected is never overwritten.
    const outcome = await rejectProposal(openPath.value, async () => {
      await care.refresh()
      return engineOpen.value
    })
    if (outcome.kind === 'not-proposal') {
      proposal.value = null
      loadError.value = 'The proposal file changed and is no longer in the proposal format, so nothing was written.'
      return
    }
    if (outcome.kind === 'decided') {
      proposal.value = outcome.fresh
      status.value = 'This proposal was already decided or changed, so nothing was written.'
      return
    }
    proposal.value = { ...proposal.value, status: 'rejected' }
    status.value = 'Rejected.'
    void care.refresh()
  } catch (cause) {
    status.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    busy.value = ''
  }
}
</script>

<style scoped>
.cpr { display: flex; flex-direction: column; gap: var(--space-3); margin-top: var(--space-2); }
.cpr-link:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}
.cpr-note { margin: 0; color: var(--fg3); font-size: var(--text-sm); line-height: 1.5; }
.cpr-note--error { color: var(--error-text); }

/* The same window as the report: one bordered card; the proposal, then a hairline and the decision. */
.cpr-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding: var(--space-5) var(--space-5) var(--space-4);
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  container-type: inline-size;
}
.cpr-link {
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  text-align: left;
  text-decoration: none;
}
.cpr-link:hover { text-decoration: underline; text-underline-offset: 3px; }

.cpr-foot {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-top: var(--space-3);
  border-top: 1px solid var(--border);
}
.cpr-how { margin: 0; color: var(--fg2); font-size: var(--text-sm); }
.cpr-actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2) var(--space-3); }
.cpr-action { min-height: var(--touch); min-width: 7.5rem; }
.cpr-file { min-height: var(--touch); font-size: var(--text-sm); }
.cpr-status { margin: 0; color: var(--fg2); font-size: var(--text-sm); }
.cpr-status:empty { display: none; }
@container (max-width: 520px) {
  .cpr-card { padding: var(--space-4) var(--space-4) var(--space-3); }
  .cpr-action { flex: 1 1 8rem; }
}
</style>
