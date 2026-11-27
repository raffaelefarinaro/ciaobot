<template>
  <section class="cpr" aria-labelledby="cpr-title">
    <p v-if="loading" class="cpr-note">Reading the proposal…</p>
    <p v-else-if="loadError" class="cpr-note cpr-note--error" role="alert">{{ loadError }}</p>

    <article v-else-if="proposal" class="cpr-card">
      <header class="cpr-head">
        <p class="cpr-kind">
          <span>{{ kindWords || 'Proposal' }}</span>
          <span v-if="proposal.target" class="cpr-target">{{ proposal.target }}</span>
        </p>
        <h3 id="cpr-title" class="cpr-title">{{ proposal.title || path }}</h3>
        <p v-if="proposal.summary" class="cpr-summary">{{ proposal.summary }}</p>
      </header>

      <section v-if="proposal.proposedChange" class="cpr-section" aria-labelledby="cpr-change">
        <h4 id="cpr-change" class="cpr-heading">What it changes</h4>
        <!-- eslint-disable-next-line vue/no-v-html -- renderMarkdown sanitizes -->
        <div class="cpr-md" v-html="renderMarkdown(proposal.proposedChange)" />
      </section>

      <section v-if="proposal.rationale" class="cpr-section" aria-labelledby="cpr-why">
        <h4 id="cpr-why" class="cpr-heading">Why</h4>
        <!-- eslint-disable-next-line vue/no-v-html -- renderMarkdown sanitizes -->
        <div class="cpr-md" v-html="renderMarkdown(proposal.rationale)" />
      </section>

      <section v-if="proposal.evidence.length" class="cpr-section" aria-labelledby="cpr-evidence">
        <h4 id="cpr-evidence" class="cpr-heading">Evidence</h4>
        <ul class="cpr-evidence">
          <li v-for="(entry, i) in evidence" :key="i">
            <a v-if="entry.url" class="cpr-link" :href="entry.url" target="_blank" rel="noopener noreferrer">{{ entry.text }}</a>
            <button v-else-if="entry.file" type="button" class="cpr-link" @click="fileViewer.open(entry.file)">{{ entry.text }}</button>
            <span v-else class="cpr-plain">{{ entry.text }}</span>
          </li>
        </ul>
      </section>

      <section v-if="proposal.body" class="cpr-section" aria-labelledby="cpr-more">
        <h4 id="cpr-more" class="cpr-heading">More detail</h4>
        <!-- eslint-disable-next-line vue/no-v-html -- renderMarkdown sanitizes -->
        <div class="cpr-md" v-html="renderMarkdown(proposal.body)" />
      </section>

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
import { safeExternalUrl } from '../lib/chatActions'
import type { CareReport } from '../lib/careReport'
import { startFileDiscussion } from '../lib/fileDiscussion'
import {
  approveDraft, parseProposal, proposalIsOpen, proposalKindWords, withProposalStatus, type Proposal,
} from '../lib/proposalReview'
import { renderMarkdown } from '../lib/safeMarkdown'
import { useCareReportStore } from '../stores/careReport'
import { useFileViewerStore } from '../stores/fileViewer'
import { useProjectStore } from '../stores/projects'

/**
 * One proposal file from the care report, read in place: what it changes, why,
 * and its evidence, then Approve or Reject. Approve never edits anything here:
 * it opens a new chat in General with the file pinned and a draft the person
 * sends, because a skill, guide or note change is made only in an attended
 * chat. Reject writes one thing, `status: rejected` in the file's front matter,
 * through the file viewer's own save path (`POST /api/workspace-file`).
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

const kindWords = computed(() => (proposal.value ? proposalKindWords(proposal.value.kind) : ''))
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

/** A vault path the viewer may open: relative, no `..`, no backslash. */
function vaultFile(entry: string): string {
  const value = entry.trim()
  if (!/\.[A-Za-z0-9]+$/.test(value) || /^[/~]|\\|^[A-Za-z]:/.test(value)) return ''
  if (value.split('/').some(part => part === '..' || part === '.' || part === '')) return ''
  return `${vaultPrefix.value}${value}`
}

const evidence = computed(() => (proposal.value?.evidence ?? []).map(entry => ({
  text: entry,
  url: safeExternalUrl(entry.trim()),
  file: vaultFile(entry),
})))

async function readFile(): Promise<string> {
  const resp = await fetch(`/api/workspace-file?exact=1&path=${encodeURIComponent(openPath.value)}`, { credentials: 'same-origin' })
  if (resp.status === 404) throw new Error('The proposal file is not there any more.')
  if (!resp.ok) throw new Error(`Could not read the proposal (HTTP ${resp.status}).`)
  return resp.text()
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  status.value = ''
  try {
    const parsed = parseProposal(await readFile())
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
    const chat = await startFileDiscussion(store, {
      path: openPath.value,
      seed: approveDraft(proposal.value.kind, openPath.value),
      title: `Approve: ${(proposal.value.title || props.path).slice(0, 60)}`,
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
    await care.refresh()
    const current = await readFile()
    const fresh = parseProposal(current)
    if (!fresh) {
      proposal.value = null
      loadError.value = 'The proposal file changed and is no longer in the proposal format, so nothing was written.'
      return
    }
    if (!engineOpen.value || !proposalIsOpen(fresh.status)) {
      proposal.value = fresh
      status.value = 'This proposal was already decided or changed, so nothing was written.'
      return
    }
    const next = withProposalStatus(current, 'rejected')
    if (next === null) throw new Error('This file is not in the proposal format, so it was left as it is.')
    const resp = await fetch('/api/workspace-file', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ path: openPath.value, content: next }),
    })
    if (!resp.ok) throw new Error(`Could not save the file (HTTP ${resp.status}).`)
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

/* The same window as the report: one bordered card, hairline sections. */
.cpr-card {
  display: flex;
  flex-direction: column;
  padding: var(--space-2) var(--space-5) var(--space-4);
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  container-type: inline-size;
}
.cpr-head { display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-4) 0; }
.cpr-kind {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1) var(--space-3);
  margin: 0;
  color: var(--fg3);
  font-size: var(--text-sm);
}
.cpr-target { font-family: var(--font-mono); font-size: var(--text-xs); line-height: 1.9; overflow-wrap: anywhere; }
.cpr-title { margin: 0; font-size: var(--text-lg); font-weight: 650; line-height: 1.35; }
.cpr-summary { margin: 0; color: var(--fg2); font-size: var(--text-base); line-height: 1.5; max-width: 68ch; }
.cpr-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) 0;
  border-top: 1px solid var(--border);
}
.cpr-heading { margin: 0; color: var(--fg3); font-size: var(--text-sm); font-weight: 600; }
.cpr-md { color: var(--fg); font-size: var(--text-sm); line-height: 1.55; max-width: 72ch; overflow-wrap: anywhere; }
.cpr-md :deep(p) { margin: 0.5em 0; }
.cpr-md :deep(:first-child) { margin-top: 0; }
.cpr-md :deep(:last-child) { margin-bottom: 0; }
.cpr-md :deep(ul), .cpr-md :deep(ol) { margin: 0.5em 0; padding-left: 1.4em; }
.cpr-md :deep(pre) {
  margin: 0.6em 0;
  padding: var(--space-2) var(--space-3);
  overflow-x: auto;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-xs);
}
.cpr-md :deep(code) { font-family: var(--font-mono); font-size: 0.92em; }
.cpr-evidence { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; }
.cpr-evidence li { display: flex; align-items: center; min-height: 32px; }
.cpr-link {
  min-height: 32px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  text-align: left;
  text-decoration: none;
  overflow-wrap: anywhere;
}
.cpr-link:hover { text-decoration: underline; text-underline-offset: 3px; }
.cpr-plain { color: var(--fg2); font-family: var(--font-mono); font-size: var(--text-xs); overflow-wrap: anywhere; }
@media (pointer: coarse) { .cpr-evidence li, .cpr-link { min-height: var(--touch); } }

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
.cpr-file { min-height: var(--touch); font-family: inherit; font-size: var(--text-sm); }
.cpr-status { margin: 0; color: var(--fg2); font-size: var(--text-sm); }
.cpr-status:empty { display: none; }
@container (max-width: 520px) {
  .cpr-card { padding: var(--space-1) var(--space-4) var(--space-3); }
  .cpr-action { flex: 1 1 8rem; }
}
</style>
