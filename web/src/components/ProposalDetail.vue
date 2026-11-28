<template>
  <div class="pd" :class="{ 'pd--compact': compact }">
    <p class="pd-meta">
      <span class="pd-kind">{{ tag }}</span>
      <span class="pd-meta-text">{{ kindWords || 'Proposal' }}</span>
      <span v-if="proposal.target" class="pd-target">{{ proposal.target }}</span>
      <span v-if="when" class="pd-meta-text">· {{ when }}</span>
      <span v-if="chats" class="pd-meta-text">· from {{ chats }} {{ chats === 1 ? 'chat' : 'chats' }}</span>
    </p>

    <h3 :id="titleId" class="pd-title" tabindex="-1">{{ proposal.title || fallbackTitle }}</h3>
    <p v-if="proposal.summary" class="pd-summary">{{ proposal.summary }}</p>

    <section v-if="proposal.rationale" class="pd-section" :aria-labelledby="`${titleId}-why`">
      <h4 :id="`${titleId}-why`" class="pd-label">Why</h4>
      <p class="pd-text">{{ proposal.rationale }}</p>
    </section>

    <section v-if="proposal.proposedChange" class="pd-section" :aria-labelledby="`${titleId}-change`">
      <h4 :id="`${titleId}-change`" class="pd-label">What it changes</h4>
      <div class="pd-change">
        <p class="pd-change-head">
          <span class="pd-change-target">{{ proposal.target || 'Proposed change' }}</span>
          <span v-if="diff" class="pd-change-count" :aria-label="`${diff.added} lines added, ${diff.removed} removed`">+{{ diff.added }} −{{ diff.removed }}</span>
          <span v-else class="pd-change-count">proposed text</span>
        </p>
        <!-- A diff the file carries, line by line; the sign is a glyph as well as a colour. -->
        <div v-if="diff" class="pd-diff" role="list" aria-label="The change, line by line">
          <p
            v-for="(row, n) in diff.rows"
            :key="n"
            class="pd-diff-row"
            :class="rowClass(row.sign)"
            role="listitem"
          ><span class="pd-diff-sign" aria-hidden="true">{{ row.sign === '@' ? '' : row.sign }}</span><span class="sr-only">{{ signWords(row.sign) }}</span>{{ row.text }}</p>
        </div>
        <p v-else class="pd-plain">{{ proposal.proposedChange }}</p>
      </div>
    </section>

    <section v-if="sources.length" class="pd-section" :aria-labelledby="`${titleId}-from`">
      <h4 :id="`${titleId}-from`" class="pd-label">Came from</h4>
      <ReviewSources :sources="sources" :vault-root="vaultRoot" />
    </section>

    <section v-if="proposal.body && !compact" class="pd-section" :aria-labelledby="`${titleId}-more`">
      <h4 :id="`${titleId}-more`" class="pd-label">More detail</h4>
      <p class="pd-text pd-text--muted">{{ proposal.body }}</p>
    </section>

    <section v-if="problems.length" class="pd-section" :aria-labelledby="`${titleId}-check`">
      <h4 :id="`${titleId}-check`" class="pd-label">What ciao proposal-check finds</h4>
      <ul class="pd-problems">
        <li v-for="(problem, n) in problems" :key="n">{{ problem }}</li>
      </ul>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import ReviewSources from './ReviewSources.vue'
import { foundDate } from '../lib/unsureList'
import {
  proposalDiff, proposalKindWords, proposalSources, proposalVaultRoot, type DiffRow, type Proposal,
} from '../lib/proposalReview'

/**
 * One proposal file read in full, shared by the care page's proposal review
 * and Memory · Skills (its detail pane and its phone card): the kind and
 * target, the title and summary, why, what it changes (a diff when the file
 * carries one, else the proposed text word for word), where it came from and
 * the longer body. Every field is model-written text and is shown as plain
 * text, never as HTML or Markdown; only the evidence becomes links, through
 * `ReviewSources` (chats, http(s) links, vault files confined to the vault).
 */
const props = withDefaults(defineProps<{
  proposal: Proposal
  /** The file as the file viewer opens it; its vault folder confines the evidence links. */
  openPath: string
  titleId: string
  /** The phone card: no body, tighter type. */
  compact?: boolean
  /** What the checker finds wrong with the file, as the engine reported it. */
  problems?: string[]
}>(), { compact: false, problems: () => [] })

const TAGS: Record<string, string> = {
  'skill-edit': 'edit',
  'guide-edit': 'guide',
  'upstream-issue': 'issue',
  'note-retire': 'retire',
}

const tag = computed(() => TAGS[props.proposal.kind] ?? 'proposal')
const kindWords = computed(() => proposalKindWords(props.proposal.kind))
const fallbackTitle = computed(() => props.openPath.split('/').pop() ?? props.openPath)
const when = computed(() => foundDate(props.proposal.created))
const diff = computed(() => proposalDiff(props.proposal.proposedChange))
const sources = computed(() => proposalSources(props.proposal.evidence))
const chats = computed(() => sources.value.filter(s => s.kind === 'chat').length)
const vaultRoot = computed(() => proposalVaultRoot(props.openPath))

function rowClass(sign: DiffRow['sign']): string {
  return sign === '+' ? 'pd-diff-row--add' : sign === '-' ? 'pd-diff-row--del' : sign === '@' ? 'pd-diff-row--head' : ''
}
function signWords(sign: DiffRow['sign']): string {
  return sign === '+' ? 'Added: ' : sign === '-' ? 'Removed: ' : ''
}
</script>

<style scoped>
.pd { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
.pd--compact { gap: var(--space-3); }

.pd-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; margin: 0; color: var(--fg3); font-size: var(--text-sm); }
/* Squared tag (DESIGN: --radius-xs is for tags that must not read as counts). */
.pd-kind {
  padding: 1px 7px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.5;
}
.pd-target { color: var(--fg); font-family: var(--font-mono); font-size: var(--text-xs); font-weight: 600; overflow-wrap: anywhere; }

.pd-title {
  margin: 0;
  color: var(--fg);
  font-size: calc(var(--text-lg) * 1.25);
  font-weight: 700;
  line-height: 1.3;
  overflow-wrap: anywhere;
  text-wrap: pretty;
}
.pd--compact .pd-title { font-size: var(--text-lg); }
.pd-title:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: var(--radius-xs); }
.pd-summary { margin: -4px 0 0; color: var(--fg2); line-height: 1.5; max-width: 68ch; overflow-wrap: anywhere; }

.pd-section { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.pd-label { margin: 0; color: var(--fg3); font-size: var(--text-sm); font-weight: 600; }
.pd-text { margin: 0; color: var(--fg); line-height: 1.55; max-width: 68ch; overflow-wrap: anywhere; white-space: pre-line; }
.pd-text--muted, .pd--compact .pd-text { color: var(--fg2); }

.pd-change {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-elev, var(--bg2));
}
.pd-change-head {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
  margin: 0;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--bg3);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}
.pd-change-target { min-width: 0; overflow-wrap: anywhere; }
.pd-change-count { flex: none; color: var(--fg3); font-variant-numeric: tabular-nums; }
.pd-diff { max-height: 48dvh; overflow: auto; padding: 4px 0; }
.pd-diff-row {
  margin: 0;
  padding: 1px 14px 1px 30px;
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.6;
  text-indent: -16px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.pd-diff-sign { display: inline-block; width: 16px; text-indent: 0; font-weight: 700; }
.pd-diff-row--add { background: color-mix(in srgb, var(--success) 16%, transparent); }
.pd-diff-row--add .pd-diff-sign { color: var(--success); }
.pd-diff-row--del { background: color-mix(in srgb, var(--error) 14%, transparent); }
.pd-diff-row--del .pd-diff-sign { color: var(--error-text); }
.pd-diff-row--head { color: var(--fg3); }
.pd-plain {
  margin: 0;
  max-height: 48dvh;
  overflow: auto;
  padding: 10px 14px;
  color: var(--fg);
  font-size: var(--text-sm);
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.pd-problems { margin: 0; padding-left: 1.2em; color: var(--fg2); font-size: var(--text-sm); line-height: 1.5; }
</style>
