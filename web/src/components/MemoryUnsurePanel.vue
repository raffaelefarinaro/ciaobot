<template>
  <section class="mu" aria-labelledby="mu-title">
    <h2 id="mu-title" class="mu-title">Unsure</h2>
    <p class="mu-lede">
      What Ciaobot was not sure enough to write down yet. It is one Markdown file you can edit:
      tick a line to say yes, or delete it to say no. A later run acts on what you ticked.
    </p>
    <p v-if="loading" class="mu-note">Reading the list…</p>
    <p v-else-if="error" class="mu-note mu-note--error" role="alert">{{ error }}</p>
    <p v-else-if="!found" class="mu-note">Nothing is on the list for this workspace.</p>
    <template v-else>
      <p class="mu-count">{{ openCount === 0 ? 'Nothing is waiting for you.' : openCount === 1 ? '1 item waiting.' : `${openCount} items waiting.` }}</p>
      <ul v-if="openLines.length" class="mu-list">
        <li v-for="(line, i) in openLines" :key="i" class="mu-item">{{ line }}</li>
      </ul>
    </template>
    <div class="mu-actions">
      <button type="button" class="mu-link" :disabled="!found" :title="`Open ${unsurePath}`" @click="openList">Open Unsure.md</button>
      <span aria-hidden="true">·</span>
      <button type="button" class="mu-link" :disabled="!found" title="Start a chat about this list" @click="discuss">Go through it with Ciaobot</button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import { startFileDiscussion } from '../lib/fileDiscussion'
import { parseUnsureLines } from '../lib/unsureList'

/**
 * The Unsure list is a plain Markdown file the agent appends to
 * (`Workspace/Unsure.md`), so this panel reads it and offers the file itself:
 * there is nothing to accept or dismiss here, and no queue behind it.
 */
const store = useProjectStore()
const fileViewer = useFileViewerStore()

const loading = ref(false)
const error = ref('')
const found = ref(false)
const content = ref('')

const unsurePath = computed(() => {
  const info = store.workspaces.find(w => w.name === store.activeWorkspace)
  const root = (info?.vault_root || `memory-vault/${store.activeWorkspace}`).replace(/\/+$/, '')
  return `${root}/Workspace/Unsure.md`
})
const openLines = computed(() => parseUnsureLines(content.value).filter(l => !l.ticked).map(l => l.text))
const openCount = computed(() => openLines.value.length)

let fetchSeq = 0
async function load(): Promise<void> {
  const seq = ++fetchSeq
  loading.value = true
  error.value = ''
  found.value = false
  content.value = ''
  try {
    // `exact=1`: no fuzzy fallback, so a workspace with no list yet never
    // shows another workspace's.
    const resp = await fetch(`/api/workspace-file?exact=1&path=${encodeURIComponent(unsurePath.value)}`, { credentials: 'same-origin' })
    if (seq !== fetchSeq) return
    if (resp.status === 404) return
    if (!resp.ok) {
      error.value = `Could not read the list (HTTP ${resp.status}).`
      return
    }
    const text = await resp.text()
    if (seq !== fetchSeq) return
    content.value = text
    found.value = true
  } catch (e) {
    if (seq === fetchSeq) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (seq === fetchSeq) loading.value = false
  }
}
watch(() => [store.activeWorkspace, unsurePath.value], () => { void load() }, { immediate: true })

function openList(): void {
  void fileViewer.open(unsurePath.value)
}
async function discuss(): Promise<void> {
  await startFileDiscussion(store, {
    path: unsurePath.value,
    seed: 'Let\'s go through the Unsure list. Tick what is right, delete what is wrong, and write down what I confirm.',
    title: 'Go through the Unsure list',
  })
}
</script>

<style scoped>
.mu { display: flex; flex-direction: column; gap: 12px; padding: 16px 0; }
.mu-title { margin: 0; font-size: var(--text-lg); }
.mu-lede, .mu-note, .mu-count { margin: 0; color: var(--fg3); font-size: var(--text-sm); line-height: 1.5; }
.mu-count { color: var(--fg); }
.mu-note--error { color: var(--error-text); }
.mu-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.mu-item {
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  font-size: var(--text-sm);
  line-height: 1.45;
  overflow-wrap: anywhere;
}
.mu-actions { display: flex; align-items: center; gap: 8px; }
.mu-link {
  min-height: var(--touch);
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm);
}
@media (pointer: coarse) { .mu-link { min-width: var(--touch); } }
.mu-link:hover:not(:disabled) { text-decoration: underline; text-underline-offset: 3px; }
.mu-link:disabled { color: var(--fg3); cursor: default; }
.mu-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
</style>
