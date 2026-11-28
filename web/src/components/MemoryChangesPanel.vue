<template>
  <section class="mch" aria-labelledby="mch-title">
    <h2 id="mch-title" class="mch-title">Changes</h2>
    <p class="mch-lede">What Ciaobot changed on its own, newest first. Undo opens a chat with the earlier text, ready to put it back.</p>

    <div class="mch-filters" role="group" aria-label="Show">
      <button
        v-for="f in CHANGE_FILTERS"
        :key="f.id"
        type="button"
        class="mch-filter"
        :class="{ active: filter === f.id }"
        :aria-pressed="filter === f.id"
        @click="filter = f.id"
      >{{ f.label }}</button>
    </div>

    <p v-if="!feed && !changes.error" class="mch-note" role="status">Reading what changed…</p>
    <div v-else-if="!feed" class="mch-note mch-note--error" role="alert">
      Could not read the changes: {{ changes.error }}
      <button type="button" class="btn-small mch-retry" @click="changes.refresh()">Try again</button>
    </div>

    <template v-else>
      <p v-if="feed.undo_log === 'unreadable'" class="mch-note">
        The undo log (Workspace/Memory-Consolidations.md) could not be read safely, so replaced and removed lines are not listed.
      </p>
      <p v-else-if="feed.undo_log === 'tail'" class="mch-note">The undo log is long; only its newest entries are listed.</p>
      <p v-if="changes.error" class="mch-note">Could not refresh. Showing the last list read.</p>
      <p class="sr-only" role="status">{{ status }}</p>

      <p v-if="!groups.length" class="mch-empty">{{ emptyWords }}</p>

      <section
        v-for="group in groups"
        :key="group.id"
        class="mch-group"
        :aria-labelledby="`mch-g-${group.id}`"
        :data-change-group="group.kind"
      >
        <div class="mch-group-head">
          <h3 :id="`mch-g-${group.id}`" class="mch-group-title">{{ groupTitle(group, now) }}</h3>
          <span class="mch-group-meta">{{ groupMeta(group) }}</span>
        </div>
        <ul class="mch-list">
          <li v-for="entry in group.entries" :key="entry.id" class="mch-row" :data-change-kind="entry.kind">
            <span class="mch-mark" :class="`mch-mark--${entryMark(entry).glyph}`">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <line v-if="entryMark(entry).glyph === 'remove'" x1="6" y1="12" x2="18" y2="12" />
                <path v-else-if="entryMark(entry).glyph === 'edit'" d="M4 20h4L19 9l-4-4L4 16v4z" />
                <path v-else-if="entryMark(entry).glyph === 'merge'" d="M7 4v6a5 5 0 0 0 5 5h0a5 5 0 0 0 5-5V4M12 15v5" />
                <path v-else-if="entryMark(entry).glyph === 'suggest'" d="M12 3l2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.6 6.6 19.5l1.2-6L3.3 9.3l6.1-.7z" />
                <g v-else-if="entryMark(entry).glyph === 'task'"><rect x="4" y="4" width="16" height="16" rx="2" /><polyline points="8 12 11 15 16 9" /></g>
                <path v-else d="M4 5h16v11H9l-5 4z" />
              </svg>
              <span class="sr-only">{{ entryMark(entry).label }}:</span>
            </span>
            <div class="mch-body">
              <p class="mch-sentence">{{ entrySentence(entry) }}</p>
              <p v-if="entrySource(entry, titles)" class="mch-source">{{ entrySource(entry, titles) }}</p>
            </div>
            <div class="mch-actions">
              <button
                v-if="entry.undo"
                type="button"
                class="btn-small mch-undo"
                :disabled="opening !== ''"
                :aria-label="clipLabel(`Undo in a chat: ${entrySentence(entry)}`)"
                @click="undo(entry)"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M9 14L4 9l5-5" /><path d="M4 9h10a6 6 0 0 1 0 12h-3" />
                </svg>
                <span>{{ opening === entry.id ? 'Opening…' : 'Undo' }}</span>
              </button>
              <button
                v-else-if="entry.kind === 'skill-suggestion' && entry.path"
                type="button"
                class="mch-link"
                :aria-label="clipLabel(`Review: ${entrySentence(entry)}`)"
                @click="review(entry)"
              >Review</button>
            </div>
          </li>
        </ul>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  CHANGE_FILTERS, clipLabel, entryMark, entrySentence, entrySource, filterGroups, groupMeta, groupTitle, passTitles, undoChatTitle, undoSeed,
  type ChangeEntry, type ChangeFilter,
} from '../lib/changesFeed'
import { startFileDiscussion } from '../lib/fileDiscussion'
import { fetchWorkspaceGuide } from '../lib/workspaceGuide'
import { useChangesStore } from '../stores/changes'
import { useProjectStore } from '../stores/projects'

/**
 * Memory · Changes: what Ciaobot changed on its own, grouped by the last care
 * run and by day, newest first, read from the records the engine already keeps
 * (`GET /api/memory/changes`). Every string is model-written and shown as
 * text. Nothing here writes: Undo opens a chat with the entry's note (or the
 * guide, for a region) pinned and the undo log's earlier text quoted as data,
 * and the person decides there.
 */
const store = useProjectStore()
const changes = useChangesStore()
const router = useRouter()

watch(() => store.activeWorkspace, ws => changes.show(ws), { immediate: true })
onMounted(() => { void changes.refresh() })

const feed = computed(() => (changes.feed && changes.feed.workspace === store.activeWorkspace ? changes.feed : null))
const filter = ref<ChangeFilter>('all')
const groups = computed(() => (feed.value ? filterGroups(feed.value.groups, filter.value) : []))
const titles = computed(() => passTitles(feed.value?.groups ?? []))
const now = new Date()
const emptyWords = computed(() => filter.value === 'all'
  ? 'Nothing recorded yet. Lines Ciaobot replaces or removes, care runs, skill suggestions and chats read for memory show up here.'
  : 'Nothing of this kind recorded.')

const opening = ref('')
const status = ref('')

async function undo(entry: ChangeEntry): Promise<void> {
  const current = feed.value
  if (!current || opening.value) return
  opening.value = entry.id
  status.value = ''
  try {
    let pinned = entry.open_path || ''
    if (entry.region) {
      const guide = await fetchWorkspaceGuide(current.workspace)
      pinned = guide.path
    }
    pinned = pinned || current.undo_log_path
    const chat = await startFileDiscussion(store, {
      path: pinned,
      seed: undoSeed(entry, { pinned, undoLogPath: current.undo_log_path, passTitles: titles.value }),
      title: undoChatTitle(entry),
      workspace: current.workspace,
    })
    if (chat) status.value = 'Opened a chat to put the earlier text back.'
  } finally {
    opening.value = ''
  }
}

function review(entry: ChangeEntry): void {
  if (entry.path) void router.push({ path: '/memory/care', query: { proposal: entry.path } })
}
</script>

<style scoped>
.mch {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4) 0 var(--space-6);
  max-width: 760px;
  container-type: inline-size;
}
.mch-title { margin: 0; font-size: var(--text-lg); }
.mch-lede, .mch-note { margin: 0; color: var(--fg3); font-size: var(--text-sm); line-height: 1.5; }
.mch-note--error { color: var(--error-text); display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
.mch-empty { margin: var(--space-3) 0 0; color: var(--fg2); font-size: var(--text-base); line-height: 1.5; }

/* The filter: one segmented control, the shape Memory's map toolbar uses. */
.mch-filters {
  display: inline-flex;
  align-self: flex-start;
  flex-wrap: wrap;
  gap: 2px;
  margin-top: var(--space-2);
  padding: 2px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.mch-filter {
  min-height: var(--touch);
  padding: 0 var(--space-4);
  border: 0;
  border-radius: calc(var(--radius-sm) - 2px);
  background: transparent;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
}
.mch-filter:hover { color: var(--fg); }
.mch-filter.active { background: var(--bg3); color: var(--fg); }
.mch-filter:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@container (max-width: 520px) {
  .mch-filters { display: flex; align-self: stretch; }
  .mch-filter { flex: 1 1 0; padding: 0 var(--space-2); }
}

.mch-group { display: flex; flex-direction: column; margin-top: var(--space-3); }
.mch-group-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--border);
}
.mch-group-title { margin: 0; font-size: var(--text-base); font-weight: 650; }
.mch-group-meta { flex: none; color: var(--fg3); font-size: var(--text-sm); font-variant-numeric: tabular-nums; }

.mch-list { margin: 0; padding: 0; list-style: none; }
.mch-row {
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--space-3);
  min-height: var(--touch);
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--border);
}
.mch-mark { display: flex; align-self: start; padding-top: 3px; color: var(--fg3); }
.mch-mark--remove { color: var(--error-text); }
.mch-mark--suggest { color: var(--accent); }
.mch-body { min-width: 0; }
.mch-sentence { margin: 0; color: var(--fg); font-size: var(--text-base); line-height: 1.45; overflow-wrap: anywhere; }
.mch-source { margin: 2px 0 0; color: var(--fg3); font-size: var(--text-sm); line-height: 1.45; overflow-wrap: anywhere; }
.mch-actions { display: flex; align-items: center; }
.mch-undo { display: inline-flex; align-items: center; gap: var(--space-1); min-height: var(--touch); }
.mch-link {
  min-height: var(--touch);
  padding: 0 var(--space-1);
  border: 0;
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
}
.mch-link:hover { color: var(--fg); text-decoration: underline; text-underline-offset: 3px; }
.mch-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
.mch-retry { min-height: var(--touch); }
</style>
