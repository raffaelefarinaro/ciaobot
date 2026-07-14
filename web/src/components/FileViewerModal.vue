<template>
  <div
    v-if="store.isOpen"
    class="fv-backdrop"
    @click.self="store.close"
    @keydown.esc="store.close"
    tabindex="-1"
    ref="backdropEl"
  >
    <div class="fv-modal" role="dialog" aria-modal="true" :aria-label="basename" ref="modalEl">
      <header class="fv-header">
        <div class="fv-titles">
          <div class="fv-title" :title="store.path">{{ basename }}</div>
          <div class="fv-subtitle" :title="store.path">{{ store.path }}<span v-if="store.line"> :{{ store.line }}</span></div>
        </div>
        <div class="fv-actions">
          <button
            class="btn-icon"
            :class="{ ok: copyState === 'ok' }"
            @click="copyPath"
            :title="copyState === 'ok' ? 'Copied!' : 'Copy path'"
            :aria-label="copyState === 'ok' ? 'Copied' : 'Copy path'"
          >
            <svg v-if="copyState === 'ok'" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
            <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          </button>
          <button
            class="btn-icon"
            @click="downloadFile"
            title="Download"
            aria-label="Download"
            :disabled="store.loading || !!store.error"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          </button>
          <button
            class="btn-icon"
            :class="{ ok: openExternalState === 'ok' }"
            @click="openExternally"
            title="Open in default app"
            aria-label="Open in default app"
            :disabled="store.loading || !!store.error || openExternalState === 'loading'"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          </button>
          <button
            v-if="canPin"
            class="btn-icon"
            :class="{ active: isPinned }"
            :title="isPinned ? 'Unpin from sidebar' : 'Pin to sidebar'"
            :aria-label="isPinned ? 'Unpin from sidebar' : 'Pin to sidebar'"
            @click="togglePin"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a3 3 0 0 0-6 0z"/></svg>
          </button>
          <button
            v-if="canEdit && !store.editing"
            class="btn-icon"
            title="Edit"
            aria-label="Edit"
            @click="store.startEditing"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 1 1 3 3L12 15l-4 1 1-4z"/></svg>
          </button>
          <button class="btn-icon" @click="store.close" title="Close (Esc)" aria-label="Close">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
      </header>

      <!-- Tabs strip. Hidden in image mode (no diff/history makes sense for
           binary image files). History and Diff are also disabled when the
           viewer was opened without a chat context (e.g. clicking a path in
           the chat trace text — those flows have no chat_id to key by). -->
      <nav v-if="store.kind !== 'image' && store.kind !== 'pdf'" class="fv-tabs" aria-label="View mode">
        <button
          class="fv-tab"
          :class="{ active: store.tab === 'preview' }"
          @click="store.setTab('preview')"
          type="button"
        >Preview</button>
        <button
          class="fv-tab"
          :class="{ active: store.tab === 'history', disabled: !store.chatId }"
          :disabled="!store.chatId"
          :title="store.chatId ? '' : 'Open from an inline file card to see history'"
          @click="store.setTab('history')"
          type="button"
        >History<span v-if="store.snapshots.length" class="fv-tab-badge">{{ store.snapshots.length }}</span></button>
        <button
          class="fv-tab"
          :class="{ active: store.tab === 'diff', disabled: !store.chatId }"
          :disabled="!store.chatId"
          :title="store.chatId ? '' : 'Open from an inline file card to see diff'"
          @click="store.setTab('diff')"
          type="button"
        >Diff</button>
      </nav>

      <div class="fv-main">
        <div class="fv-body" :class="{ 'fv-body-image': store.kind === 'image', 'fv-body-excalidraw': store.kind === 'excalidraw' }" ref="bodyEl">
          <div v-if="store.loading" class="fv-loading">Loading…</div>
          <div v-else-if="store.error" class="fv-error">{{ store.error }}</div>
          <img
            v-else-if="store.kind === 'image'"
            class="fv-img"
            :src="`/api/workspace-image?path=${encodeURIComponent(store.path)}`"
            :alt="basename"
          />
          <!-- Edit mode: replaces the preview body with a textarea + actions.
               No history/diff while editing — finish or cancel first. -->
          <template v-else-if="store.editing">
            <div class="fv-edit-shell">
              <ExcalidrawViewer
                v-if="store.kind === 'excalidraw'"
                :content="store.editBuffer"
                :name="basename"
                :file-path="store.path"
                :chat-id="store.chatId"
                :read-only="false"
                @change="store.editBuffer = $event"
                style="flex: 1; min-height: 0; height: auto;"
              />
              <textarea
                v-else
                class="fv-edit-textarea"
                v-model="store.editBuffer"
                spellcheck="false"
                autofocus
              ></textarea>
              <div v-if="store.editError" class="fv-error">{{ store.editError }}</div>
              <div class="fv-edit-actions">
                <button class="fv-btn" @click="store.cancelEditing" :disabled="store.editSaving">Cancel</button>
                <button class="fv-btn primary" @click="store.saveEdits" :disabled="store.editSaving">
                  {{ store.editSaving ? 'Saving…' : 'Save' }}
                </button>
              </div>
            </div>
          </template>

          <!-- History tab: snapshot list with action labels and Restore. -->
          <template v-else-if="store.tab === 'history'">
            <div v-if="store.snapshotsLoading" class="fv-loading">Loading history…</div>
            <div v-else-if="store.snapshotsError" class="fv-error">{{ store.snapshotsError }}</div>
            <div v-else-if="!store.snapshots.length" class="fv-empty">No snapshots yet for this file in this chat.</div>
            <ul v-else class="fv-history-list">
              <li
                v-for="s in [...store.snapshots].reverse()"
                :key="s.seq"
                class="fv-history-item"
              >
                <div class="fv-history-line">
                  <span class="fv-history-seq">#{{ s.seq }}</span>
                  <span class="fv-history-action">{{ s.action }}</span>
                  <span class="fv-history-tool">{{ s.tool }}</span>
                  <span class="fv-history-ts">{{ formatHistoryTs(s.ts) }}</span>
                </div>
                <div class="fv-history-actions">
                  <button class="fv-btn-sm" @click="diffAgainstSeq(s.seq)" title="Compare this snapshot with the previous one">Diff</button>
                  <button class="fv-btn-sm" @click="restoreSeq(s.seq)" title="Write this snapshot back to disk">Restore</button>
                </div>
              </li>
            </ul>
          </template>

          <!-- Diff tab: terminal-style changed lines only. -->
          <template v-else-if="store.tab === 'diff'">
            <div v-if="store.diffLoading" class="fv-loading">Loading diff…</div>
            <div v-else-if="store.diffError" class="fv-error">{{ store.diffError }}</div>
            <div v-else-if="!store.snapshots.length" class="fv-empty">No snapshots yet for this file in this chat.</div>
            <div v-else class="fv-diff-shell">
              <div class="fv-diff-picker">
                <label class="fv-diff-label">From
                  <select v-model.number="store.diffSeqA" @change="store.setDiffSeqs(Number(store.diffSeqA), Number(store.diffSeqB))">
                    <option v-for="s in store.snapshots" :key="`a-${s.seq}`" :value="s.seq">#{{ s.seq }} {{ s.action }} {{ formatHistoryTs(s.ts) }}</option>
                  </select>
                </label>
                <span class="fv-diff-arrow">→</span>
                <label class="fv-diff-label">To
                  <select v-model.number="store.diffSeqB" @change="store.setDiffSeqs(Number(store.diffSeqA), Number(store.diffSeqB))">
                    <option :value="0">current on disk</option>
                    <option v-for="s in store.snapshots" :key="`b-${s.seq}`" :value="s.seq">#{{ s.seq }} {{ s.action }} {{ formatHistoryTs(s.ts) }}</option>
                  </select>
                </label>
              </div>
              <pre class="fv-diff-pre"><code><span
                v-for="(line, i) in diffLines"
                :key="i"
                :class="['fv-diff-line', `fv-diff-${line.kind}`]"
              >{{ diffPrefix(line.kind) }}{{ line.text }}
</span></code></pre>
            </div>
          </template>

          <template v-else>
            <!-- Metadata card synthesized from YAML frontmatter -->
            <ExcalidrawViewer
              v-if="store.kind === 'excalidraw'"
              :content="store.content"
              :name="basename"
              :file-path="store.path"
              :chat-id="store.chatId"
              :read-only="true"
            />
            <div v-else-if="store.kind === 'pdf' && store.pptxNeedsLibreoffice" class="fv-libreoffice-notice hint hint--warn">
              <strong>LibreOffice is required to preview PowerPoint files.</strong>
              <span v-if="store.libreofficeInstallError"> {{ store.libreofficeInstallError }}</span>
              <button
                class="btn-primary btn-small"
                :disabled="store.libreofficeInstalling"
                @click="store.installLibreoffice"
              >{{ store.libreofficeInstalling ? 'Installing…' : 'Install LibreOffice' }}</button>
            </div>
            <iframe
              v-else-if="store.kind === 'pdf'"
              class="fv-pdf-iframe"
              :src="`/api/workspace-binary?path=${encodeURIComponent(store.path)}&t=${store.loadToken}`"
              width="100%"
              height="100%"
              style="border: none; flex: 1; min-height: 500px; display: block; border-radius: 4px;"
            ></iframe>
            <template v-else>
            <div v-if="frontmatter" class="fv-meta-card">
              <div class="fv-meta-row">
                <span v-if="fmType" class="fv-meta-pill fv-meta-pill-type">{{ fmType }}</span>
                <span v-if="fmStatus" class="fv-meta-pill" :class="`fv-meta-pill-status-${fmStatus}`">{{ fmStatus }}</span>
                <span v-if="fmName && fmName !== basename.replace(/\.md$/, '')" class="fv-meta-name" :title="fmName">{{ fmName }}</span>
                <span class="fv-meta-spacer"></span>
                <span v-if="fmUpdated" class="fv-meta-date" :title="`Updated ${fmUpdated}`">↻ {{ fmUpdated }}</span>
                <span v-else-if="fmCreated" class="fv-meta-date" :title="`Created ${fmCreated}`">+ {{ fmCreated }}</span>
              </div>
              <div v-if="fmTags.length" class="fv-meta-row fv-meta-tags">
                <span v-for="t in fmTags" :key="t" class="fv-meta-tag">#{{ t }}</span>
              </div>
              <p v-if="fmProse" class="fv-meta-summary">{{ fmProse }}</p>
              <div
                v-for="listExtra in fmListExtras"
                :key="listExtra.key"
                class="fv-meta-row fv-meta-links"
              >
                <span class="fv-meta-links-label">{{ listExtra.key }}</span>
                <span v-for="item in listExtra.items" :key="item" class="fv-meta-link">{{ item }}</span>
              </div>
              <dl v-if="fmExtraEntries.length" class="fv-meta-extra">
                <template v-for="entry in fmExtraEntries" :key="entry.key">
                  <dt>{{ entry.key }}</dt>
                  <dd>{{ entry.value }}</dd>
                </template>
              </dl>
            </div>
            <div
              v-if="isMarkdown"
              class="fv-md"
              ref="mdEl"
              v-html="renderedMarkdown"
              @click="onMdClick"
            ></div>
            <pre v-else class="fv-pre" ref="preEl" @click="onPreClick"><code ref="preCodeEl"><span v-for="(line, i) in contentLines" :key="i" :class="{ 'comment-highlight': isHighlightedLine(i + 1), 'pre-line': true }" :data-line="i + 1" :data-comment-id="commentIdForLine(i + 1)">{{ line }}</span></code></pre>
            </template>
          </template>

        </div>

        <!-- Comment sidebar (desktop only) -->
        <div v-if="showSidebar" class="fv-comment-sidebar" ref="sidebarEl">
          <div class="fv-sidebar-header">
            <span class="fv-sidebar-title">Comments</span>
            <span class="fv-sidebar-count">{{ activeFileComments.length + (commentDraft ? 1 : 0) }}</span>
          </div>

          <!-- Draft composer: appears here instead of a floating popover -->
          <div v-if="commentDraft" class="fv-sidebar-draft" @mousedown.stop>
            <div class="fv-sidebar-draft-header">
              <span class="fv-sidebar-draft-label">New comment</span>
              <button class="fv-sidebar-card-remove" @click="cancelComment" title="Cancel">×</button>
            </div>
            <div class="fv-sidebar-card-quote">"{{ truncate(commentDraft.selection, 120) }}"</div>
            <textarea
              ref="sidebarDraftInputEl"
              v-model="commentDraft.text"
              class="fv-sidebar-draft-input"
              placeholder="Add a comment…"
              rows="3"
              @keydown="onCommentKeydown"
            ></textarea>
            <div v-if="commentDraftImages.length" class="fv-sidebar-draft-images">
              <span v-for="(img, i) in commentDraftImages" :key="img" class="draft-image-preview">
                <img :src="`/api/images/${img}`" :alt="img" class="draft-image-thumb" />
                <button class="draft-image-remove" @click="removeDraftImage(i)" title="Remove">×</button>
              </span>
            </div>
            <div class="fv-sidebar-draft-actions">
              <label class="image-btn-sm" title="Upload images">
                <input type="file" accept="image/*" multiple hidden @change="handleDraftImageUpload" />
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
              </label>
              <button class="fv-btn-sm" @click="cancelComment" type="button">Cancel</button>
              <button
                class="fv-btn-sm primary"
                :disabled="!commentDraft.text.trim()"
                @click="saveComment"
                type="button"
              >Add comment</button>
            </div>
          </div>

          <div class="fv-sidebar-list" ref="sidebarListEl">
            <div
              v-for="c in sidebarCards"
              :key="c.id"
              class="fv-sidebar-card"
              :class="{ 'is-pending': isPending(c.id), 'is-editing': editingCommentId === c.id }"
              :style="{ top: c.top + 'px' }"
              :data-card-id="c.id"
              :data-desired-top="c.top"
              @click="editingCommentId !== c.id && scrollToHighlight(c.id)"
            >
              <div class="fv-sidebar-card-header">
                <span class="fv-sidebar-card-file">{{ commentBasename(c.path) }}<span v-if="commentLineLabel(c)" class="fv-sidebar-card-line">:{{ commentLineLabel(c) }}</span></span>
                <div class="fv-sidebar-card-actions">
                  <button class="fv-sidebar-card-edit" @click.stop="startEditComment(c)" title="Edit">✎</button>
                  <button class="fv-sidebar-card-remove" @click.stop="deleteFileComment(c.path, c.id)" title="Delete">×</button>
                </div>
              </div>
              <div class="fv-sidebar-card-quote">"{{ truncate(c.selection, 120) }}"</div>
              <div v-if="editingCommentId !== c.id" class="fv-sidebar-card-note">{{ c.comment }}</div>
              <div v-if="editingCommentId !== c.id && c.images?.length" class="fv-sidebar-card-images">
                <img v-for="img in c.images" :key="img" :src="`/api/images/${img}`" :alt="img" class="card-image-thumb" @click.stop />
              </div>
              <div v-if="editingCommentId === c.id" class="fv-sidebar-edit-body" @mousedown.stop>
                <textarea
                  ref="editCommentInputEl"
                  v-model="editDraftText"
                  class="fv-sidebar-edit-input"
                  placeholder="Edit comment…"
                  rows="2"
                  @keydown="onEditKeydown"
                ></textarea>
                <div v-if="editingCommentImages.length" class="fv-sidebar-edit-images">
                  <span v-for="(img, i) in editingCommentImages" :key="img" class="draft-image-preview">
                    <img :src="`/api/images/${img}`" :alt="img" class="draft-image-thumb" />
                    <button class="draft-image-remove" @click="removeEditImage(i)" title="Remove">×</button>
                  </span>
                </div>
                <div class="fv-sidebar-edit-actions">
                  <label class="image-btn-sm" title="Upload images">
                    <input type="file" accept="image/*" multiple hidden @change="handleEditImageUpload($event, c.id)" />
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                  </label>
                  <button class="fv-btn-sm" @click="cancelEditComment" type="button">Cancel</button>
                  <button class="fv-btn-sm primary" @click="saveEditComment(c.id)" type="button">Save</button>
                </div>
              </div>
            </div>
            <div ref="sidebarSpacerEl" class="fv-sidebar-spacer"></div>
          </div>
        </div>

        <!-- Floating "Comment" button anchored near the active selection.
             Clicking it opens the sidebar draft composer. -->
        <button
          v-if="selectionAnchor"
          class="fv-comment-trigger"
          :style="{ top: selectionAnchor.top + 'px', left: selectionAnchor.left + 'px' }"
          @mousedown.prevent
          @click="openCommentForSelection"
          type="button"
          title="Comment on this selection"
        >
          <span class="fv-comment-trigger-icon">💬</span>
          Comment
        </button>

        <!-- Mobile read-only comment popup. Triggered by tapping a highlight. -->
        <div
          v-if="activePopupComment"
          class="fv-comment-popover read-only"
          :style="{ top: popupAnchor.top + 'px', left: popupAnchor.left + 'px' }"
          @mousedown.stop
        >
          <div class="fv-comment-quote">{{ truncate(activePopupComment.selection, 240) }}</div>
          <div class="fv-comment-note">{{ activePopupComment.comment }}</div>
          <div class="fv-comment-actions">
            <button class="fv-btn-sm" @click="closePopupComment" type="button">Close</button>
            <button class="fv-btn-sm danger" @click="deletePopupComment" type="button">Delete</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useFileViewerStore } from '../stores/fileViewer'
import { useProjectStore } from '../stores/projects'
import { parseFrontmatter } from '../lib/markdownFrontmatter'
import { renderFileMarkdown } from '../lib/safeMarkdown'
import { openWorkspaceFileExternally } from '../lib/openWorkspaceFile'
import { createTerminalDiffLines, terminalDiffPrefix, type TerminalDiffKind } from '../lib/terminalDiff'
const ExcalidrawViewer = defineAsyncComponent(() => import('./ExcalidrawViewer.vue'))

const store = useFileViewerStore()
const projectsStore = useProjectStore()

// Edit mode only makes sense for text files inside a chat-scoped flow that
// the snapshot store can record under. Image files and chat-less viewer
// opens (e.g. clicking a plain path in a chat trace) stay read-only.
const canEdit = computed(() => (store.kind === 'text' || store.kind === 'excalidraw') && !!store.chatId && !store.loading && !store.error)

// History tab timestamp formatting. Snapshots store ISO 8601; we want a
// short local form: "May 18, 14:32".
function formatHistoryTs(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

// Click Diff next to a snapshot row in History: compares it with the
// snapshot immediately before it (or the only snapshot vs current on disk
// when there's just one).
async function diffAgainstSeq(seq: number): Promise<void> {
  const snaps = store.snapshots
  const idx = snaps.findIndex(s => s.seq === seq)
  let a = 0, b = seq
  if (idx > 0) {
    a = snaps[idx - 1].seq
  } else {
    // First snapshot: diff against current on-disk content. 0 is the
    // sentinel for "current" in the store's _fetchSeq path.
    a = seq
    b = 0
  }
  await store.setTab('diff')
  await store.setDiffSeqs(a, b)
}

async function restoreSeq(seq: number): Promise<void> {
  if (!confirm(`Restore snapshot #${seq} to disk? This writes a new snapshot so it can be undone.`)) return
  const ok = await store.restoreSnapshot(seq)
  if (!ok) projectsStore.pushErrorToast('Restore failed', `Could not restore snapshot #${seq}. See network console for details.`)
}

const diffLines = computed(() => createTerminalDiffLines(store.diffContentA, store.diffContentB))

function diffPrefix(kind: TerminalDiffKind): string {
  return terminalDiffPrefix(kind)
}

// Split frontmatter off so the body renders cleanly and the metadata card
// at the top can show key fields as pills/chips. Mirrors PinnedFilePanel.
const splitContent = computed(() => parseFrontmatter(store.content))
const frontmatter = computed(() => splitContent.value.frontmatter)
const bodyOnly = computed(() => splitContent.value.body)

const fmTitle = computed(() => fmString('title'))
const fmName = computed(() => fmString('name'))
const fmType = computed(() => fmString('type'))
const fmStatus = computed(() => fmString('status'))
const fmTags = computed(() => fmList('tags'))
const fmCreated = computed(() => fmString('created'))
const fmUpdated = computed(() => fmString('updated'))

const PRIMARY_KEYS = new Set(['title', 'name', 'type', 'status', 'tags', 'created', 'updated'])
const PROSE_KEYS = new Set(['description', 'summary', 'notes'])
const LIST_EXTRA_KEYS = new Set(['aliases', 'related', 'links'])

const fmProse = computed(() => {
  for (const key of ['description', 'summary', 'notes']) {
    const v = fmString(key)
    if (v.trim()) return v
  }
  return ''
})

const fmListExtras = computed(() => {
  const out: { key: string; items: string[] }[] = []
  for (const key of ['aliases', 'related', 'links']) {
    const items = fmList(key)
    if (items.length) out.push({ key, items })
  }
  return out
})

const fmExtraEntries = computed(() => {
  const fm = frontmatter.value
  if (!fm) return [] as { key: string; value: string }[]
  const skip = new Set([...PRIMARY_KEYS, ...PROSE_KEYS, ...LIST_EXTRA_KEYS])
  const out: { key: string; value: string }[] = []
  for (const [k, v] of Object.entries(fm)) {
    if (skip.has(k)) continue
    if (v == null) continue
    const text = Array.isArray(v) ? v.join(', ') : String(v)
    if (!text.trim()) continue
    out.push({ key: k, value: text })
  }
  return out
})

function fmString(key: string): string {
  const v = frontmatter.value?.[key]
  if (v == null) return ''
  return Array.isArray(v) ? v.join(', ') : String(v)
}
function fmList(key: string): string[] {
  const v = frontmatter.value?.[key]
  if (v == null) return []
  return Array.isArray(v) ? v : [String(v)]
}
// Keep `fmTitle` in scope (read by linters as referenced for completeness
// even though the chip itself uses fmName / basename for the heading).
void fmTitle

// ── File comments (durable, shown in sidebar + highlights) ─────────
const activeFileComments = computed(() =>
  projectsStore.fileCommentsFor(cleanPath(store.path))
)
const showSidebar = computed(() => activeFileComments.value.length > 0 || commentDraft.value !== null)

function isPending(id: string): boolean {
  return projectsStore.pendingComments.some(c => c.id === id)
}

function deleteFileComment(path: string, id: string): void {
  projectsStore.removeFileComment(path, id)
  nextTick(() => applyHighlights())
}

// ── Pre line rendering & line-based highlighting ───────────────────
const contentLines = computed(() => {
  const text = bodyOnly.value
  if (text.endsWith('\n')) {
    return text.slice(0, -1).split('\n')
  }
  return text.split('\n')
})

const lineCommentMap = computed(() => {
  const map = new Map<number, string>()
  for (const c of activeFileComments.value) {
    if (!c.lineStart) continue
    const end = c.lineEnd || c.lineStart
    for (let l = c.lineStart; l <= end; l++) {
      // First comment wins for overlap
      if (!map.has(l)) map.set(l, c.id)
    }
  }
  return map
})

function isHighlightedLine(line: number): boolean {
  return lineCommentMap.value.has(line)
}

function commentIdForLine(line: number): string | undefined {
  return lineCommentMap.value.get(line)
}

// ── Sidebar Google Docs-style alignment ───────────────────────────────
const sidebarListEl = ref<HTMLElement>()
const sidebarSpacerEl = ref<HTMLElement>()
const commentPositions = ref<Record<string, number>>({})
const CARD_GAP = 8

function updateCommentPositions(): void {
  if (!bodyEl.value) return
  const body = bodyEl.value
  const positions: Record<string, number> = {}

  // Find the first highlight for each comment id and record its offsetTop
  const highlights = body.querySelectorAll('.comment-highlight[data-comment-id], .pre-line.comment-highlight[data-comment-id]')
  for (const el of Array.from(highlights)) {
    const id = (el as HTMLElement).dataset.commentId
    if (id && !(id in positions)) {
      positions[id] = (el as HTMLElement).offsetTop
    }
  }

  commentPositions.value = positions

  // Ensure the sidebar list is at least as tall as the body content so
  // absolutely positioned cards never float past the end.
  if (sidebarSpacerEl.value) {
    sidebarSpacerEl.value.style.height = Math.max(body.scrollHeight, 100) + 'px'
  }
}

// Push-down pass to keep cards from overlapping. Cards prefer to sit at
// their highlight's offsetTop (Google Docs style) but slide down by their
// own height + GAP whenever a previous card would otherwise spill into
// them. Mutates DOM directly so we don't re-render-loop on measured
// heights changing reactive state.
function layoutSidebarCards(): void {
  if (!sidebarListEl.value) return
  const els = Array.from(
    sidebarListEl.value.querySelectorAll<HTMLElement>('.fv-sidebar-card[data-card-id]')
  )
  if (!els.length) return
  // Sort by the desired top encoded on the element; fall back to current
  // style.top from the v-bind. Items without a desired position trail at
  // the end (those are unpositioned fallbacks).
  const items = els.map(el => {
    const desiredAttr = el.dataset.desiredTop
    const desired = desiredAttr ? parseFloat(desiredAttr) : NaN
    return {
      el,
      desiredTop: Number.isFinite(desired) ? desired : parseFloat(el.style.top || '0'),
      height: el.offsetHeight,
    }
  })
  items.sort((a, b) => a.desiredTop - b.desiredTop)
  let prevBottom = 0
  for (const item of items) {
    const top = Math.max(item.desiredTop, prevBottom + (prevBottom === 0 ? 0 : CARD_GAP))
    item.el.style.top = top + 'px'
    prevBottom = top + item.height
  }
  // Stretch the spacer if the laid-out cards exceed it, so the last card
  // is fully scrollable into view.
  if (sidebarSpacerEl.value) {
    const minHeight = prevBottom + CARD_GAP
    const cur = parseFloat(sidebarSpacerEl.value.style.height || '0')
    if (minHeight > cur) sidebarSpacerEl.value.style.height = minHeight + 'px'
  }
}

// Cards sorted by source line number first, then by visual position.
// Line-number ordering is more predictable than visual position when
// text wraps or images shift the layout. Falls back to visual position
// for comments without line info (e.g. legacy or cross-file selections).
const sidebarCards = computed(() => {
  const pos = commentPositions.value
  const cards = activeFileComments.value.map(c => ({ ...c, top: pos[c.id] ?? null as number | null }))
  cards.sort((a, b) => {
    const aLine = a.lineStart ?? Number.MAX_SAFE_INTEGER
    const bLine = b.lineStart ?? Number.MAX_SAFE_INTEGER
    if (aLine !== bLine) return aLine - bLine
    const ap = a.top ?? -1
    const bp = b.top ?? -1
    if (ap !== -1 && bp !== -1) return ap - bp
    if (ap !== -1) return 1
    if (bp !== -1) return -1
    return a.id.localeCompare(b.id)
  })
  let fallback = 0
  for (const c of cards) {
    if (c.top == null) {
      c.top = fallback
      fallback += 8
    }
  }
  return cards
})

function scrollToHighlight(id: string): void {
  if (!bodyEl.value) return
  const highlights = bodyEl.value.querySelectorAll('[data-comment-id]')
  for (const el of Array.from(highlights)) {
    if ((el as HTMLElement).dataset.commentId === id) {
      bodyEl.value.scrollTo({ top: (el as HTMLElement).offsetTop - 20, behavior: 'smooth' })
      break
    }
  }
}

// Scroll sync: keep sidebar vertically aligned with the document body.
let isSyncing = false
function syncBodyToSidebar(): void {
  if (isSyncing || !bodyEl.value || !sidebarListEl.value) return
  isSyncing = true
  sidebarListEl.value.scrollTop = bodyEl.value.scrollTop
  requestAnimationFrame(() => { isSyncing = false })
}
function syncSidebarToBody(): void {
  if (isSyncing || !bodyEl.value || !sidebarListEl.value) return
  isSyncing = true
  bodyEl.value.scrollTop = sidebarListEl.value.scrollTop
  requestAnimationFrame(() => { isSyncing = false })
}

function onBodyScroll(): void {
  onScrollReanchor()
  syncBodyToSidebar()
}

function attachScrollSync(): void {
  if (!bodyEl.value) return
  detachScrollSync()
  bodyEl.value.addEventListener('scroll', onBodyScroll, { passive: true })
  sidebarListEl.value?.addEventListener('scroll', syncSidebarToBody, { passive: true })
}
function detachScrollSync(): void {
  bodyEl.value?.removeEventListener('scroll', onBodyScroll)
  sidebarListEl.value?.removeEventListener('scroll', syncSidebarToBody)
}

// ── Mobile popup for reading a comment on tap ───────────────────────
const activePopupId = ref<string | null>(null)
const popupAnchor = ref<{ top: number; left: number }>({ top: 0, left: 0 })

const activePopupComment = computed(() => {
  if (!activePopupId.value) return null
  return activeFileComments.value.find(c => c.id === activePopupId.value) || null
})

function openPopupComment(e: MouseEvent, id: string): void {
  if (window.innerWidth > 640) return
  const rect = (e.target as HTMLElement).getBoundingClientRect()
  const modal = modalEl.value
  if (!modal) return
  const modalRect = modal.getBoundingClientRect()
  popupAnchor.value = {
    top: rect.bottom - modalRect.top + 6,
    left: Math.max(8, rect.left - modalRect.left),
  }
  activePopupId.value = id
}

function closePopupComment(): void {
  activePopupId.value = null
}

function deletePopupComment(): void {
  const id = activePopupId.value
  if (!id) return
  activePopupId.value = null
  const c = activeFileComments.value.find(x => x.id === id)
  if (c) deleteFileComment(c.path, id)
}

function onMdClick(e: MouseEvent): void {
  const target = e.target as HTMLElement | null
  if (!target) return

  const fileLink = target.closest('a.file-link') as HTMLAnchorElement | null
  if (fileLink) {
    e.preventDefault()
    e.stopPropagation()
    const linkedPath = fileLink.getAttribute('data-file-path') || ''
    const lineAttr = fileLink.getAttribute('data-line')
    const linkedLine = lineAttr ? parseInt(lineAttr, 10) : null
    const cid = store.chatId || ''
    if (/\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/i.test(linkedPath)) {
      void store.openImage(linkedPath, cid)
    } else {
      void store.open(linkedPath, Number.isFinite(linkedLine as number) ? linkedLine : null, cid)
    }
    return
  }

  const highlight = target.closest('.comment-highlight') as HTMLElement | null
  if (!highlight) return
  const id = highlight.dataset.commentId
  if (id) openPopupComment(e, id)
}

// ── Markdown text highlighting ──────────────────────────────────────
function clearHighlights(root: HTMLElement): void {
  const existing = root.querySelectorAll('.comment-highlight')
  for (const el of Array.from(existing)) {
    const parent = el.parentNode
    if (!parent) continue
    parent.replaceChild(document.createTextNode(el.textContent || ''), el)
    parent.normalize()
  }
}

function highlightInMarkdown(root: HTMLElement, selection: string, commentId: string): boolean {
  const text = selection.trim()
  if (!text) return false
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes: Text[] = []
  let node: Node | null
  while ((node = walker.nextNode())) nodes.push(node as Text)
  if (!nodes.length) return false

  let fullText = ''
  const offsets: { node: Text; start: number; end: number }[] = []
  for (const n of nodes) {
    const start = fullText.length
    fullText += n.textContent || ''
    offsets.push({ node: n, start, end: fullText.length })
  }

  const idx = fullText.indexOf(text)
  if (idx === -1) return false

  // Wrap the matching slice of each overlapping text node in its own span.
  // Using one Range across multiple nodes fails with range.surroundContents
  // when the range spans structural boundaries (table cells, paragraphs,
  // list items) or inline element boundaries (<strong>, <a>, etc.).
  //
  // Instead, we split each text node at the match boundaries using
  // splitText(), then replace the middle portion with a highlight span.
  // This avoids the common-ancestor restriction and works across any
  // element boundary because we only ever manipulate text nodes.
  //
  // We also skip whitespace-only text nodes so highlights don't bleed
  // into empty gaps between paragraphs or list items.
  //
  // Iterate in reverse so DOM mutations don't shift the offsets we still
  // need to act on.
  const matchStart = idx
  const matchEnd = idx + text.length
  let success = false
  for (let i = offsets.length - 1; i >= 0; i--) {
    const o = offsets[i]
    if (o.end <= matchStart || o.start >= matchEnd) continue
    const localStart = Math.max(0, matchStart - o.start)
    const localEnd = Math.min(o.end - o.start, matchEnd - o.start)
    if (localStart >= localEnd) continue

    const textNode = o.node
    const slice = textNode.textContent?.slice(localStart, localEnd) || ''
    if (!slice.trim()) continue  // Skip whitespace-only gaps

    try {
      const after = textNode.splitText(localEnd)
      const mid = textNode.splitText(localStart)
      const span = document.createElement('span')
      span.className = 'comment-highlight'
      span.dataset.commentId = commentId
      mid.parentNode?.replaceChild(span, mid)
      span.appendChild(mid)
      success = true
    } catch {
      // Skip this node; the others may still wrap successfully.
    }
  }
  return success
}

function applyHighlights(): void {
  if (!isCommentable.value) return
  if (store.kind === 'image') return

  if (isMarkdown.value) {
    const root = mdEl.value
    if (!root) return
    clearHighlights(root)
    for (const c of activeFileComments.value) {
      highlightInMarkdown(root, c.selection, c.id)
    }
  } else {
    // Pre highlighting is handled by the line-based v-for + CSS
    // but we still need to ensure click handlers are wired.
    const root = preCodeEl.value
    if (!root) return
    // Remove stale click listeners by clearing and re-adding is expensive;
    // instead we rely on event delegation via the pre element.
  }
}

// Event delegation for pre-line clicks (mobile popup)
function onPreClick(e: MouseEvent): void {
  const target = e.target as HTMLElement | null
  if (!target) return
  const line = target.closest('.pre-line') as HTMLElement | null
  if (!line) return
  const id = line.dataset.commentId
  if (id) openPopupComment(e, id)
}

function commentBasename(path: string): string {
  const idx = path.lastIndexOf('/')
  return idx === -1 ? path : path.slice(idx + 1)
}
function commentLineLabel(c: { lineStart?: number | null; lineEnd?: number | null }): string {
  if (!c.lineStart) return ''
  if (!c.lineEnd || c.lineEnd === c.lineStart) return String(c.lineStart)
  return `${c.lineStart}-${c.lineEnd}`
}

const backdropEl = ref<HTMLElement>()
const modalEl = ref<HTMLElement>()
const bodyEl = ref<HTMLElement>()
const mdEl = ref<HTMLElement>()
const preEl = ref<HTMLElement>()
const preCodeEl = ref<HTMLElement>()
const sidebarDraftInputEl = ref<HTMLTextAreaElement>()
const copyState = ref<'' | 'ok'>('')
const openExternalState = ref<'' | 'loading' | 'ok'>('')

const activePinKey = computed(() => {
  return projectsStore.activeChatId || projectsStore.activeChat?.project_id || ''
})
const canPin = computed(() => !!activePinKey.value && window.innerWidth > 768)
const isPinned = computed(() => {
  if (!activePinKey.value) return false
  return projectsStore.pinnedFileFor(activePinKey.value) === cleanPath(store.path)
})
function togglePin(): void {
  const key = activePinKey.value
  if (!key) return
  const path = cleanPath(store.path)
  if (isPinned.value) {
    projectsStore.unpinFile(key)
  } else {
    projectsStore.pinFile(key, path)
    store.close()
  }
}

// Selection-driven comment UX. Two states:
//   1. selectionAnchor != null  → floating "Comment" trigger sits near the
//      bottom-right of the live selection. Disappears on collapse/blur.
//   2. commentDraft != null     → user clicked the trigger; we capture the
//      selected text (so it survives selection loss when the textarea grabs
//      focus) and show an inline composer at the same anchor.
type Anchor = { top: number; left: number }
type LineRange = { start: number; end: number } | null
type CommentDraft = { selection: string; anchor: Anchor; text: string; lines: LineRange }
const selectionAnchor = ref<Anchor | null>(null)
const commentDraft = ref<CommentDraft | null>(null)
let lastSelectionText = ''
let lastSelectionLines: LineRange = null
let lastSelectionRange: Range | null = null

// Anything we render as text is fair game for commenting — the floating
// trigger should appear in both the markdown branch and the <pre> branch.
// Images stay opt-out.
const isCommentable = computed(() =>
  store.isOpen && !store.loading && !store.error && store.kind === 'text'
)

const basename = computed(() => {
  const p = store.path
  const idx = p.lastIndexOf('/')
  return idx === -1 ? p : p.slice(idx + 1)
})

const isMarkdown = computed(() => /\.(md|markdown)$/i.test(store.path))

// Directory portion of the current MD file, used to resolve relative image
// references inside the markdown. Strips any `:line` suffix the viewer
// accepts on text files so it doesn't end up joined into a bogus path.
const docDir = computed(() => {
  const cleaned = store.path.replace(/:\d+$/, '')
  const idx = cleaned.lastIndexOf('/')
  return idx === -1 ? '' : cleaned.slice(0, idx + 1)
})

// Join a workspace-relative dir with a (possibly dotted) relative path,
// collapsing `.` and `..` segments. Mirrors posixpath.normpath for the
// cases that appear in markdown image srcs.
function joinRelative(dir: string, rel: string): string {
  const parts = (dir + rel).split('/')
  const out: string[] = []
  for (const p of parts) {
    if (p === '' || p === '.') continue
    if (p === '..') { out.pop(); continue }
    out.push(p)
  }
  return out.join('/')
}

// True for anything that already resolves without our help: absolute URLs,
// protocol-relative, data/blob URIs, and site-root paths.
const _ABSOLUTE_SRC_RE = /^(?:[a-z][a-z0-9+.-]*:|\/\/|\/)/i

const renderedMarkdown = computed(() => {
  const dir = docDir.value
  return renderFileMarkdown(bodyOnly.value, {
    filePath: store.path,
    markdownPaths: store.markdownPaths,
    resolveImageSrc: (href) => {
      if (href && !_ABSOLUTE_SRC_RE.test(href)) {
        const resolved = joinRelative(dir, href)
        return `/api/workspace-image?path=${encodeURIComponent(resolved)}`
      }
      return href
    },
  })
})

// Scroll the modal into focus + jump to line if requested whenever the
// store finishes loading a new file.
watch(
  () => store.loadToken,
  () => {
    nextTick(() => {
      backdropEl.value?.focus()
      if (!store.line || store.loading || store.error) return
      // Plain-text view: line numbers map to <pre> children, so scroll a
      // proportional offset. We don't render explicit line markers yet, so
      // approximate by line-height * line.
      if (!isMarkdown.value && bodyEl.value) {
        const pre = bodyEl.value.querySelector('pre.fv-pre') as HTMLElement | null
        if (pre) {
          const total = (store.content.match(/\n/g)?.length || 1) + 1
          const ratio = Math.min(Math.max((store.line - 1) / total, 0), 1)
          const target = pre.scrollHeight * ratio - bodyEl.value.clientHeight / 3
          bodyEl.value.scrollTop = Math.max(target, 0)
        }
      }
    })
  },
)

// Re-apply markdown highlights and sidebar positions whenever the content
// or comment list changes. The third nextTick lets Vue re-render the cards
// at their desired tops before we measure heights and push them down to
// resolve overlaps.
watch(
  () => `${store.loadToken}|${store.path}|${activeFileComments.value.map(c => c.id).join(',')}|${renderedMarkdown.value.length}`,
  () => nextTick(() => {
    applyHighlights()
    nextTick(() => {
      updateCommentPositions()
      attachScrollSync()
      nextTick(() => layoutSidebarCards())
    })
  }),
  { flush: 'post' },
)

// ── Selection → comment ───────────────────────────────────────────
// Text selection inside the rendered markdown body opens up a "Comment"
// trigger; clicking it captures the selected text and shows a textarea so
// the user can attach a note to the next message. The note rides along on
// the next sendMessage as a structured <file-comment> block.

function truncate(s: string, n: number): string {
  if (!s) return ''
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}

// Strip the `:line` suffix that the viewer accepts on text files so the
// comment carries a clean workspace path. The line number is preserved
// separately on the draft if we can guess it from the selection.
function cleanPath(p: string): string {
  return p.replace(/:\d+$/, '')
}

// Convert a (container, offset) Range endpoint into a character offset
// relative to the start of `root.textContent`. Walks via a fresh Range +
// `toString().length`, which handles nested elements (links, em, strong)
// transparently. Returns null when the endpoint isn't inside `root`.
function charOffsetFrom(root: Element, container: Node, offset: number): number | null {
  if (!root.contains(container) && root !== container) return null
  const r = document.createRange()
  r.selectNodeContents(root)
  try {
    r.setEnd(container, offset)
  } catch {
    return null
  }
  return r.toString().length
}

// Count 1-indexed line number of `idx` within `text` (idx points at the
// character whose line we want — newlines before it are counted, the char
// at idx itself is not).
function lineAt(text: string, idx: number): number {
  let line = 1
  const limit = Math.min(idx, text.length)
  for (let i = 0; i < limit; i++) {
    if (text.charCodeAt(i) === 10) line++
  }
  return line
}

// Compute a {start, end} source line range for the active selection.
// Two strategies:
//   • <pre> branch: text-node offsets map 1:1 onto store.content, so we
//     read range.startOffset/endOffset directly via charOffsetFrom and
//     count newlines. This is exact.
//   • markdown branch: rendered DOM doesn't map onto source, so we
//     substring-search the source markdown for the first ~50 chars (start
//     line) and the last ~50 chars (end line) of the selection. Falls back
//     to a single line when the second search misses.
function computeSelectionLines(range: Range, selectionText: string): LineRange {
  const src = store.content
  if (!src) return null

  // Plain-text branch: exact mapping via offsets into the <code> root.
  const codeRoot = preCodeEl.value
  if (codeRoot && codeRoot.contains(range.startContainer)) {
    const startOff = charOffsetFrom(codeRoot, range.startContainer, range.startOffset)
    const endOff = charOffsetFrom(codeRoot, range.endContainer, range.endOffset)
    if (startOff != null && endOff != null) {
      const a = Math.min(startOff, endOff)
      const b = Math.max(startOff, endOff)
      const start = lineAt(src, a)
      // For end, look at the last char of the selection (b - 1) so a
      // selection ending at the start of a line doesn't bleed into it.
      const end = b > a ? lineAt(src, b - 1) : start
      return { start, end: Math.max(end, start) }
    }
  }

  // Markdown branch: best-effort substring lookup.
  const trimmed = selectionText.trim()
  if (!trimmed) return null
  const head = trimmed.slice(0, 60)
  let startIdx = src.indexOf(head)
  if (startIdx === -1) {
    // Try a shorter prefix from the first line of the rendered selection,
    // which is usually the most stable token to find in source.
    const firstLine = trimmed.split(/\n/, 1)[0].trim().slice(0, 30)
    if (firstLine.length >= 4) startIdx = src.indexOf(firstLine)
  }
  if (startIdx === -1) return null
  const start = lineAt(src, startIdx)

  const tail = trimmed.slice(-60).trim()
  if (tail.length >= 4 && tail !== head) {
    // Search starting after the head match so identical phrases earlier in
    // the doc don't pull the end line backwards.
    const tailIdx = src.indexOf(tail, startIdx)
    if (tailIdx !== -1) {
      const end = lineAt(src, tailIdx + tail.length - 1)
      return { start, end: Math.max(end, start) }
    }
  }
  return { start, end: start }
}

function updateSelectionAnchorFromRange(range: Range): void {
  const modal = modalEl.value
  const body = bodyEl.value
  if (!modal || !body) {
    selectionAnchor.value = null
    return
  }

  const rects = range.getClientRects()
  const endRect = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect()
  const bodyRect = body.getBoundingClientRect()
  const visible = endRect.bottom > bodyRect.top
    && endRect.top < bodyRect.bottom
    && endRect.right > bodyRect.left
    && endRect.left < bodyRect.right
  if (!visible) {
    selectionAnchor.value = null
    return
  }

  const modalRect = modal.getBoundingClientRect()
  const top = endRect.bottom - modalRect.top + 2
  const left = Math.max(8, endRect.right - modalRect.left + 6)
  selectionAnchor.value = { top, left }
}

function onScrollReanchor(): void {
  if (commentDraft.value || !lastSelectionRange) return
  try {
    if (!lastSelectionRange.startContainer.isConnected) {
      lastSelectionRange = null
      selectionAnchor.value = null
      return
    }
    updateSelectionAnchorFromRange(lastSelectionRange)
  } catch {
    lastSelectionRange = null
    selectionAnchor.value = null
  }
}

function onSelectionChange(): void {
  if (!isCommentable.value) {
    lastSelectionRange = null
    selectionAnchor.value = null
    return
  }
  // While the comment composer is open the selection has been "captured" —
  // don't keep retracking it (the textarea steals focus and would clear it).
  if (commentDraft.value) return
  const sel = window.getSelection()
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
    lastSelectionRange = null
    selectionAnchor.value = null
    return
  }
  const range = sel.getRangeAt(0)
  // Only react to selections inside the rendered file view — selecting text
  // in the path subtitle or modal chrome shouldn't trigger the comment UI.
  // Either the markdown div or the <pre> code branch counts.
  const targets: (HTMLElement | undefined)[] = [mdEl.value, preEl.value, preCodeEl.value]
  const inside = targets.some(
    el => el && el.contains(range.startContainer) && el.contains(range.endContainer)
  )
  if (!inside) {
    lastSelectionRange = null
    selectionAnchor.value = null
    return
  }
  const text = sel.toString().trim()
  if (!text) {
    lastSelectionRange = null
    selectionAnchor.value = null
    return
  }
  lastSelectionText = text
  lastSelectionLines = computeSelectionLines(range, text)
  lastSelectionRange = range.cloneRange()
  updateSelectionAnchorFromRange(range)
}

const commentDraftImages = ref<string[]>([])
const editingCommentImages = ref<string[]>([])

function openCommentForSelection(): void {
  if (!selectionAnchor.value || !lastSelectionText) return
  commentDraft.value = {
    selection: lastSelectionText,
    anchor: selectionAnchor.value,
    text: '',
    lines: lastSelectionLines,
  }
  commentDraftImages.value = []
  selectionAnchor.value = null
  lastSelectionRange = null
  // Clear the native selection — the sidebar now "owns" the highlighted
  // text, and leaving it selected makes the page look noisy.
  window.getSelection()?.removeAllRanges()
  nextTick(() => sidebarDraftInputEl.value?.focus())
}

function cancelComment(): void {
  commentDraft.value = null
  commentDraftImages.value = []
  lastSelectionText = ''
  lastSelectionLines = null
  lastSelectionRange = null
}

function saveComment(): void {
  const draft = commentDraft.value
  if (!draft) return
  const note = draft.text.trim()
  if (!note) return
  projectsStore.addPendingComment({
    path: cleanPath(store.path),
    selection: draft.selection,
    comment: note,
    lineStart: draft.lines?.start ?? null,
    lineEnd: draft.lines?.end ?? null,
    images: commentDraftImages.value.length ? commentDraftImages.value : undefined,
  })
  commentDraft.value = null
  commentDraftImages.value = []
  lastSelectionText = ''
  lastSelectionLines = null
  lastSelectionRange = null
}

async function handleDraftImageUpload(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  if (!input.files?.length) return
  const chatId = projectsStore.activeChatId
  if (!chatId) return
  try {
    const refs = await projectsStore.uploadImageRefs(chatId, Array.from(input.files))
    commentDraftImages.value.push(...refs)
  } catch (err) {
    console.error('Comment image upload failed:', err)
  }
  input.value = ''
}

function removeDraftImage(index: number): void {
  commentDraftImages.value.splice(index, 1)
}

function onCommentKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    e.preventDefault()
    cancelComment()
    return
  }
  // Cmd/Ctrl+Enter saves — handy when the textarea has multi-line content.
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault()
    saveComment()
  }
}

// ── Edit existing comment ────────────────────────────────────────────
const editingCommentId = ref<string | null>(null)
const editDraftText = ref('')
const editCommentInputEl = ref<HTMLTextAreaElement>()

function startEditComment(c: { id: string; comment: string; images?: string[] }): void {
  editingCommentId.value = c.id
  editDraftText.value = c.comment
  editingCommentImages.value = c.images ? [...c.images] : []
  nextTick(() => editCommentInputEl.value?.focus())
}

function cancelEditComment(): void {
  editingCommentId.value = null
  editDraftText.value = ''
  editingCommentImages.value = []
}

function saveEditComment(id: string): void {
  const note = editDraftText.value.trim()
  if (!note) return
  const path = cleanPath(store.path)
  projectsStore.updateFileComment(path, id, note)
  // Sync images: remove existing ones that are gone, add new ones
  const existing = projectsStore.fileCommentsFor(path).find(c => c.id === id)
  const existingImages = existing?.images || []
  const nextImages = editingCommentImages.value
  for (const img of existingImages) {
    if (!nextImages.includes(img)) projectsStore.removeFileCommentImage(path, id, img)
  }
  for (const img of nextImages) {
    if (!existingImages.includes(img)) projectsStore.addFileCommentImage(path, id, img)
  }
  editingCommentId.value = null
  editDraftText.value = ''
  editingCommentImages.value = []
}

async function handleEditImageUpload(e: Event, id: string): Promise<void> {
  const input = e.target as HTMLInputElement
  if (!input.files?.length) return
  const chatId = projectsStore.activeChatId
  if (!chatId) return
  try {
    const refs = await projectsStore.uploadImageRefs(chatId, Array.from(input.files))
    const path = cleanPath(store.path)
    for (const ref of refs) {
      projectsStore.addFileCommentImage(path, id, ref)
    }
    // Refresh local edit state from store
    const c = projectsStore.fileCommentsFor(path).find(x => x.id === id)
    if (c?.images) editingCommentImages.value = [...c.images]
  } catch (err) {
    console.error('Comment image upload failed:', err)
  }
  input.value = ''
}

function removeEditImage(index: number): void {
  editingCommentImages.value.splice(index, 1)
}

function onEditKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    e.preventDefault()
    cancelEditComment()
    return
  }
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault()
    const id = editingCommentId.value
    if (id) saveEditComment(id)
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('selectionchange', onSelectionChange)
}
function onSidebarResize(): void {
  updateCommentPositions()
  nextTick(() => layoutSidebarCards())
}
if (typeof window !== 'undefined') {
  window.addEventListener('resize', onSidebarResize)
}
onBeforeUnmount(() => {
  if (typeof document !== 'undefined') {
    document.removeEventListener('selectionchange', onSelectionChange)
  }
  if (typeof window !== 'undefined') {
    window.removeEventListener('resize', onSidebarResize)
  }
  detachScrollSync()
})

// Reset draft + anchor whenever the file changes or the modal closes.
watch(
  () => store.loadToken,
  () => {
    selectionAnchor.value = null
    commentDraft.value = null
    lastSelectionText = ''
    lastSelectionLines = null
    lastSelectionRange = null
  },
)
watch(
  () => store.isOpen,
  (open) => {
    if (!open) {
      selectionAnchor.value = null
      commentDraft.value = null
      lastSelectionRange = null
      activePopupId.value = null
      detachScrollSync()
    } else {
      nextTick(() => {
        updateCommentPositions()
        attachScrollSync()
        nextTick(() => layoutSidebarCards())
      })
    }
  },
)

async function copyPath(): Promise<void> {
  try {
    await navigator.clipboard.writeText(store.path)
    copyState.value = 'ok'
    setTimeout(() => { copyState.value = '' }, 1200)
  } catch { /* clipboard may be unavailable; silently ignore */ }
}

async function openExternally(): Promise<void> {
  if (store.loading || store.error || openExternalState.value === 'loading') return
  openExternalState.value = 'loading'
  const result = await openWorkspaceFileExternally(store.path)
  if (result.ok) {
    openExternalState.value = 'ok'
    setTimeout(() => { openExternalState.value = '' }, 1200)
    return
  }
  openExternalState.value = ''
  projectsStore.pushErrorToast('Could not open file', result.error)
}

// Download the currently-open file. For images we hand the browser the
// workspace-image URL and let it stream the bytes directly; for text we
// already have the content in memory so a Blob is the simplest path.
function downloadFile(): void {
  if (store.loading || store.error) return
  const cleaned = store.path.replace(/:\d+$/, '')
  const name = (() => {
    const idx = cleaned.lastIndexOf('/')
    return idx === -1 ? cleaned : cleaned.slice(idx + 1)
  })()
  const a = document.createElement('a')
  a.download = name || 'download'
  if (store.kind === 'image') {
    a.href = `/api/workspace-image?path=${encodeURIComponent(cleaned)}`
  } else if (store.kind === 'pdf') {
    a.href = `/api/workspace-binary?path=${encodeURIComponent(cleaned)}&raw=1`
  } else {
    const blob = new Blob([store.content], { type: 'text/plain;charset=utf-8' })
    a.href = URL.createObjectURL(blob)
    setTimeout(() => URL.revokeObjectURL(a.href), 5000)
  }
  document.body.appendChild(a)
  a.click()
  a.remove()
}

watch(
  () => [store.isOpen, store.path, store.chatId, projectsStore.streaming[store.chatId]] as const,
  ([isOpen, path, chatId, isStreaming], oldValues) => {
    const wasStreaming = oldValues ? oldValues[3] : false
    if (isOpen && path && chatId && wasStreaming && !isStreaming) {
      store.open(path, store.line, chatId)
    }
  }
)

// Global Esc handler — Vue's @keydown on the backdrop only fires when the
// backdrop has focus, which it might lose to inner content. Belt and braces.
function onKey(e: KeyboardEvent): void {
  if (store.isOpen && e.key === 'Escape') store.close()
}
if (typeof window !== 'undefined') {
  window.addEventListener('keydown', onKey)
}
</script>

<style scoped>
.fv-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 16px;
  outline: none;
}
.fv-modal {
  background: var(--bg);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 8px;
  width: min(1200px, 100%);
  height: min(94vh, 1100px);
  display: flex;
  flex-direction: column;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
  overflow: hidden;
  position: relative;
}
.fv-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.fv-titles {
  flex: 1;
  min-width: 0;
}
.fv-title {
  font-weight: 600;
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fv-subtitle {
  font-size: 11px;
  color: var(--fg2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}
.fv-actions {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
  align-items: center;
}
.fv-actions .btn-icon {
  color: var(--fg2);
}
.fv-actions .btn-icon:hover {
  color: var(--fg);
}
.fv-actions .btn-icon.active {
  background: var(--accent);
  color: var(--bg);
}
.fv-actions .btn-icon.ok {
  color: var(--ok, #4ade80);
}
.fv-actions .btn-icon:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.fv-btn {
  background: transparent;
  color: var(--fg2);
  border: 1px solid transparent;
  border-radius: 4px;
  padding: 4px 8px;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  transition: background 0.15s, color 0.15s;
}
.fv-btn:hover {
  background: var(--border);
  color: var(--fg);
}
.fv-body-image {
  /* Centre standalone images and ditch the inner padding so the modal
     framing looks intentional regardless of aspect ratio. */
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  background: var(--bg2, rgba(255, 255, 255, 0.04));
}
.fv-img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  display: block;
}
.fv-loading,
.fv-error {
  padding: 24px;
  text-align: center;
  color: var(--fg2);
}
.fv-error {
  color: var(--error, #f87171);
}
.fv-libreoffice-notice {
  margin: 24px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
}
.fv-pre {
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre;
  overflow-x: auto;
  color: var(--fg);
}
/* ── Metadata card (parsed frontmatter) ─────────────────────────── */
.fv-meta-card {
  margin: 0 0 16px;
  padding: 10px 12px;
  background: var(--bg2, rgba(255, 255, 255, 0.03));
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: 88ch;
}
.fv-meta-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-height: 22px;
}
.fv-meta-pill {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  background: var(--border);
  color: var(--fg);
  white-space: nowrap;
}
.fv-meta-pill-type {
  background: rgba(96, 165, 250, 0.18);
  color: #93c5fd;
}
.fv-meta-pill-status-active {
  background: rgba(34, 197, 94, 0.18);
  color: #86efac;
}
.fv-meta-pill-status-completed,
.fv-meta-pill-status-archived {
  background: rgba(148, 163, 184, 0.18);
  color: #cbd5e1;
}
.fv-meta-pill-status-draft {
  background: rgba(250, 204, 21, 0.18);
  color: #fde68a;
}
.fv-meta-name {
  color: var(--fg2);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  margin-left: 4px;
}
.fv-meta-spacer { flex: 1; min-width: 0; }
.fv-meta-date {
  color: var(--fg2);
  font-size: 11px;
  white-space: nowrap;
}
.fv-meta-tags { margin-top: -2px; }
.fv-meta-tag {
  font-size: 11px;
  color: var(--fg2);
  background: transparent;
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.fv-meta-summary {
  margin: 2px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--border);
  font-size: 13px;
  line-height: 1.55;
  color: var(--fg);
}
.fv-meta-links { gap: 4px 6px; }
.fv-meta-links-label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--fg2);
  margin-right: 2px;
}
.fv-meta-link {
  font-size: 11px;
  color: var(--fg2);
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.fv-meta-extra {
  margin: 8px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--border);
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 2px 12px;
  font-size: 12px;
}
.fv-meta-extra dt {
  color: var(--fg2);
  font-weight: 600;
  text-transform: lowercase;
}
.fv-meta-extra dd {
  margin: 0;
  color: var(--fg);
  word-break: break-word;
}

.fv-md {
  font-size: 14px;
  line-height: 1.6;
  max-width: 88ch;
}
.fv-md :deep(p) { margin: 0.6em 0; }
.fv-md :deep(:first-child) { margin-top: 0; }
.fv-md :deep(:last-child) { margin-bottom: 0; }
.fv-md :deep(pre) {
  background: var(--bg2, rgba(255, 255, 255, 0.04));
  padding: 10px 12px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 12px;
}
.fv-md :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.fv-md :deep(:not(pre) > code) {
  background: var(--bg2, rgba(255, 255, 255, 0.06));
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 0.92em;
}
.fv-md :deep(:is(h1, h2, h3, h4)) {
  margin-top: 1.2em;
  margin-bottom: 0.4em;
  line-height: 1.3;
}
.fv-md :deep(h1) { font-size: 1.6em; }
.fv-md :deep(h2) { font-size: 1.3em; }
.fv-md :deep(h3) { font-size: 1.1em; }
.fv-md :deep(a) {
  color: var(--accent);
  text-decoration: underline;
}
.fv-md :deep(a:hover) {
  color: var(--accent-strong);
}
.fv-md :deep(.wikilink-unresolved) {
  color: var(--fg-muted, #888);
  text-decoration: underline dotted;
  cursor: help;
}
.fv-md :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 6px;
  display: block;
  margin: 0.6em 0;
  background: var(--bg2, rgba(255, 255, 255, 0.04));
}
/* Lists: pull markers in tight to the text so they read as bullets, not
   floating dots in the left gutter. Mirrors the chat bubble styling. */
.fv-md :deep(ul),
.fv-md :deep(ol) {
  padding-left: 22px;
  margin: 0.6em 0;
  list-style-position: outside;
}
.fv-md :deep(li) {
  padding-left: 2px;
  margin: 0.15em 0;
}
.fv-md :deep(li > p) { margin: 0.2em 0; }
/* Tables: match the chat-bubble look — bordered cells, header row tinted,
   so dense data renders as a real table instead of misaligned columns. */
.fv-md :deep(table) {
  border-collapse: collapse;
  margin: 10px 0;
  font-size: 13px;
  border: 1px solid var(--fg2);
}
.fv-md :deep(th),
.fv-md :deep(td) {
  border: 1px solid var(--fg2);
  padding: 5px 9px;
  vertical-align: top;
}
.fv-md :deep(th) {
  background: var(--bg3, var(--bg2, rgba(255, 255, 255, 0.06)));
  font-weight: 600;
  text-align: left;
}
.fv-md :deep(blockquote) {
  margin: 0.6em 0;
  padding: 0 0 0 12px;
  border-left: 3px solid var(--border);
  color: var(--fg2);
}
.fv-md :deep(hr) {
  border: 0;
  border-top: 1px solid var(--border);
  margin: 1.25em 0;
}

/* Selection-driven comment UI. The trigger is a small floating chip the
   user clicks to "capture" the current selection; the popover then lets
   them write a note that gets attached to the next outgoing message. */
/* Comment trigger pill. Shape and behaviour match the danger-red variant
 * used in ChatPanel and PinnedFilePanel so the "Comment" affordance looks
 * the same regardless of where the user is in the app. */
.fv-comment-trigger {
  position: absolute;
  z-index: 5;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  font-size: var(--text-sm);
  font-weight: 600;
  color: white;
  background: var(--danger, #e06c75);
  border: none;
  border-radius: 999px;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
  user-select: none;
}
.fv-comment-trigger:hover { filter: brightness(1.08); }
.fv-comment-trigger-icon { font-size: var(--text-sm); line-height: 1; }
.fv-comment-popover {
  position: absolute;
  z-index: 6;
  width: min(420px, 90%);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px 12px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.45);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.fv-comment-quote {
  font-size: 12px;
  line-height: 1.4;
  color: var(--fg2);
  border-left: 3px solid var(--accent, #60a5fa);
  padding: 2px 0 2px 8px;
  max-height: 6em;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
.fv-comment-input {
  width: 100%;
  resize: vertical;
  min-height: 60px;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.45;
  color: var(--fg);
  background: var(--bg2, rgba(255, 255, 255, 0.04));
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  outline: none;
  box-sizing: border-box;
}
.fv-comment-input:focus { border-color: var(--accent, #60a5fa); }
.fv-comment-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}
.fv-btn-sm {
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--fg);
  cursor: pointer;
}
.fv-btn-sm:hover { background: var(--bg2, rgba(255, 255, 255, 0.04)); }
.fv-btn-sm.primary {
  background: var(--accent, #60a5fa);
  border-color: var(--accent, #60a5fa);
  color: var(--bg);
}
.fv-btn-sm.danger {
  background: var(--error, #f87171);
  border-color: var(--error, #f87171);
  color: white;
}

/* Tabs strip: Preview / History / Diff */
.fv-tabs {
  display: flex;
  gap: 4px;
  padding: 6px 18px 0;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.fv-tab {
  font-size: 13px;
  padding: 6px 12px;
  border: none;
  background: transparent;
  color: var(--fg2);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  border-radius: 6px 6px 0 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.fv-tab:hover:not(.disabled):not(.active) { color: var(--fg); background: var(--bg2); }
.fv-tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.fv-tab.disabled,
.fv-tab[disabled] {
  opacity: 0.4;
  cursor: not-allowed;
}
.fv-tab-badge {
  background: var(--bg3, rgba(255, 255, 255, 0.06));
  color: var(--fg);
  border-radius: 8px;
  padding: 0 6px;
  font-size: 11px;
  margin-left: 4px;
}

/* History list */
.fv-empty {
  padding: 24px;
  color: var(--fg2);
  text-align: center;
}
.fv-history-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.fv-history-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
}
.fv-history-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.fv-history-seq {
  font-weight: 600;
  color: var(--fg);
  font-family: var(--font);
}
.fv-history-action {
  color: var(--accent);
  font-size: 12px;
}
.fv-history-tool {
  color: var(--fg2);
  font-size: 11px;
}
.fv-history-ts {
  color: var(--fg2);
  font-size: 11px;
  margin-left: auto;
  white-space: nowrap;
}
.fv-history-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

/* Diff view */
.fv-diff-shell {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.fv-diff-picker {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.fv-diff-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--fg2);
}
.fv-diff-label select {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--fg);
  padding: 3px 6px;
  border-radius: 4px;
  font-size: 12px;
}
.fv-diff-arrow {
  color: var(--fg2);
}
.fv-diff-pre {
  flex: 1;
  margin: 0;
  padding: 12px 18px;
  overflow: auto;
  font-family: var(--font, monospace);
  font-size: 12px;
  line-height: 1.5;
  background: var(--bg);
}
.fv-diff-line {
  display: block;
  white-space: pre-wrap;
  word-break: break-word;
}
.fv-diff-skip,
.fv-diff-empty {
  color: var(--fg2);
}
.fv-diff-ins {
  background: rgba(34, 197, 94, 0.16);
  color: #86efac;
}
.fv-diff-del {
  background: rgba(248, 113, 113, 0.16);
  color: #fca5a5;
}
:root.theme-light .fv-diff-ins {
  background: rgba(34, 197, 94, 0.15);
  color: #15803d;
}
:root.theme-light .fv-diff-del {
  background: rgba(248, 113, 113, 0.15);
  color: #b91c1c;
}

/* Edit mode */
.fv-edit-shell {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: 8px;
}
.fv-edit-textarea {
  flex: 1;
  width: 100%;
  resize: none;
  background: var(--bg);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px;
  font-family: var(--font, monospace);
  font-size: 13px;
  line-height: 1.5;
}
.fv-edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 4px;
}
.fv-btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--bg);
}

/* Main layout: content + optional comment sidebar */
.fv-main {
  flex: 1;
  display: flex;
  overflow: hidden;
  min-height: 0;
}
.fv-body {
  flex: 1;
  overflow: auto;
  padding: 18px 28px 28px;
  position: relative;
  min-width: 0;
}

/* Comment sidebar */
.fv-comment-sidebar {
  width: 280px;
  flex-shrink: 0;
  border-left: 1px solid var(--border);
  background: var(--bg2, rgba(255, 255, 255, 0.04));
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.fv-sidebar-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.fv-sidebar-title {
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--fg);
}
.fv-sidebar-count {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--accent, #60a5fa);
  background: var(--bg);
  padding: 1px 6px;
  border-radius: 999px;
}
.fv-sidebar-list {
  flex: 1;
  overflow-y: auto;
  padding: 10px 12px;
  position: relative;
}
.fv-sidebar-card {
  position: absolute;
  left: 10px;
  right: 10px;
  padding: 8px 10px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 6px;
  font-size: var(--text-xs);
  line-height: 1.4;
  color: var(--fg);
  cursor: pointer;
  transition: transform 0.1s, box-shadow 0.1s;
}
.fv-sidebar-card:hover {
  transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}
.fv-sidebar-spacer {
  height: 100px;
}
.fv-sidebar-card.is-pending {
  border-left-color: var(--accent2, #a78bfa);
}
.fv-sidebar-card-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.fv-sidebar-card-file {
  font-weight: 600;
  font-size: var(--text-xs);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
}
.fv-sidebar-card-line {
  color: var(--fg2);
  font-weight: 400;
}
.fv-sidebar-card-remove {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--fg2);
  font-size: calc(14px * var(--font-scale));
  line-height: 16px;
  cursor: pointer;
}
.fv-sidebar-card-remove:hover { background: var(--bg2); color: var(--fg); }
.fv-sidebar-card-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  opacity: 0;
  transition: opacity 0.15s;
}
.fv-sidebar-card:hover .fv-sidebar-card-actions,
.fv-sidebar-card.is-editing .fv-sidebar-card-actions { opacity: 1; }
.fv-sidebar-card-edit {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 16px;
  cursor: pointer;
}
.fv-sidebar-card-edit:hover { background: var(--bg2); color: var(--fg); }
.fv-sidebar-edit-body { margin-top: 4px; }
.fv-sidebar-edit-input {
  width: 100%;
  resize: vertical;
  min-height: 44px;
  font-family: inherit;
  font-size: var(--text-base);
  line-height: 1.45;
  color: var(--fg);
  background: var(--bg2, rgba(255, 255, 255, 0.04));
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  outline: none;
  box-sizing: border-box;
  margin-bottom: 6px;
}
.fv-sidebar-edit-input:focus { border-color: var(--accent, #60a5fa); }
.fv-sidebar-edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}
.fv-sidebar-card-quote {
  color: var(--fg2);
  font-style: italic;
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.fv-sidebar-card-note {
  color: var(--fg);
  word-break: break-word;
}
.fv-sidebar-draft-images {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.fv-sidebar-card-images {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.fv-sidebar-edit-images {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.draft-image-preview {
  position: relative;
  display: inline-flex;
}
.draft-image-thumb {
  height: 40px;
  width: 40px;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg);
}
.card-image-thumb {
  height: 36px;
  width: 36px;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg);
}
.draft-image-remove {
  position: absolute;
  top: -4px;
  right: -4px;
  width: 16px;
  height: 16px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: var(--bg3);
  color: var(--fg);
  font-size: 12px;
  line-height: 14px;
  cursor: pointer;
  box-shadow: 0 1px 3px rgba(0,0,0,0.3);
}
.image-btn-sm {
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--fg2);
  transition: background 120ms var(--ease), color 120ms var(--ease), border-color 120ms var(--ease);
}
.image-btn-sm:hover { background: var(--bg3); color: var(--fg); border-color: var(--fg2); }

/* Sidebar draft composer: sits between header and the scrollable list. */
.fv-sidebar-draft {
  padding: 10px 12px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}
.fv-sidebar-draft-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.fv-sidebar-draft-label {
  font-weight: 600;
  font-size: var(--text-xs);
  color: var(--accent, #60a5fa);
  text-transform: uppercase;
  letter-spacing: 0.02em;
  flex: 1;
}
.fv-sidebar-draft-input {
  width: 100%;
  resize: vertical;
  min-height: 60px;
  font-family: inherit;
  font-size: var(--text-base);
  line-height: 1.45;
  color: var(--fg);
  background: var(--bg2, rgba(255, 255, 255, 0.04));
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  outline: none;
  box-sizing: border-box;
  margin-bottom: 8px;
}
.fv-sidebar-draft-input:focus {
  border-color: var(--accent, #60a5fa);
}
.fv-sidebar-draft-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}

/* Text highlights inside the document.
   Use :deep() because markdown highlights are created dynamically via
   DOM manipulation and won't carry Vue's scoped attribute. */
:deep(.comment-highlight) {
  background: rgba(234, 179, 8, 0.25);
  border-bottom: 2px solid rgba(234, 179, 8, 0.6);
  cursor: pointer;
  transition: background 0.15s;
}
:deep(.comment-highlight:hover) {
  background: rgba(234, 179, 8, 0.4);
}

/* Pre line wrappers */
.pre-line {
  display: block;
  white-space: pre;
}

/* Read-only comment popup (mobile) */
.fv-comment-note {
  font-size: 13px;
  line-height: 1.45;
  color: var(--fg);
  word-break: break-word;
}

@media (max-width: 640px) {
  .fv-backdrop { padding: 0; }
  .fv-modal {
    border-radius: 0;
    /* Pull the top and bottom edges in by the iOS safe-area insets so the
       header (with the × close button) and any scroll content don't slide
       under the status bar / home indicator. Without this, the close
       button gets covered by the status bar on notched phones and the
       bottom of the file gets covered by the home indicator. */
    max-height: calc(100dvh - var(--safe-top) - var(--safe-bottom));
    height: calc(100dvh - var(--safe-top) - var(--safe-bottom));
    margin-top: var(--safe-top);
    margin-bottom: var(--safe-bottom);
    width: 100vw;
  }
  .fv-body { padding: 14px 16px; }
  .fv-md { max-width: none; }
  .fv-comment-popover { width: calc(100% - 20px); left: 10px !important; }
  .fv-comment-sidebar { display: none; }
  .fv-main { flex-direction: column; }
}
</style>
