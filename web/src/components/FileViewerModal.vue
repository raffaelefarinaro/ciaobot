<template>
  <div
    v-if="store.isOpen"
    class="fv-backdrop"
    @click.self="store.close()"
    @keydown.esc="store.close()"
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
            v-if="continuableChatId"
            class="fv-continue-btn"
            @click="continueFromTranscript"
            :disabled="isContinuing"
            title="Start a new chat seeded with this transcript"
          >
            {{ isContinuing ? 'Continuing…' : 'Continue this chat' }}
          </button>
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
          <button class="btn-icon" @click="store.close()" title="Close (Esc)" aria-label="Close">
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
        <button
          v-if="isMarkdown"
          class="fv-tab"
          :class="{ active: store.tab === 'backlinks' }"
          @click="loadBacklinks"
          type="button"
        >Backlinks<span v-if="backlinks.length" class="fv-tab-badge">{{ backlinks.length }}</span></button>
      </nav>

      <div class="fv-main">
        <div class="fv-body" :class="{ 'fv-body-image': store.kind === 'image', 'fv-body-csv': isCsv }" ref="bodyEl">
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
              <CsvViewer
                v-if="isCsv"
                :content="store.editBuffer"
                :read-only="false"
                @change="store.editBuffer = $event"
                style="flex: 1; min-height: 0;"
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
            <div v-if="store.kind === 'pdf' && store.pptxNeedsLibreoffice" class="fv-libreoffice-notice hint hint--warn">
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
            <HtmlArtifactViewer
              v-else-if="store.kind === 'html' && !store.editing"
              :file-path="store.path"
              :reload-token="store.loadToken"
              :view="store.htmlView"
              :source="store.content"
              :source-loading="store.sourceLoading"
              :source-error="store.sourceError"
              @update:view="store.setHtmlView"
            />
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
                <template v-for="(item, i) in listExtra.items" :key="i">
                  <a
                    v-if="item.path"
                    class="fv-meta-link file-link"
                    href="#"
                    @click.prevent="openRelated(item.path)"
                  >{{ item.label }}</a>
                  <span v-else class="fv-meta-link">{{ item.label }}</span>
                </template>
              </div>
              <dl v-if="fmExtraEntries.length" class="fv-meta-extra">
                <template v-for="entry in fmExtraEntries" :key="entry.key">
                  <dt>{{ entry.key }}</dt>
                  <dd>
                    <a
                      v-if="isUrl(entry.value)"
                      :href="entry.value"
                      target="_blank"
                      rel="noopener noreferrer"
                    >{{ entry.value }}</a>
                    <template v-else>{{ entry.value }}</template>
                  </dd>
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
            <CsvViewer
              v-else-if="isCsv"
              :content="store.content"
              :read-only="true"
              :commentable="true"
              :cell-comments="csvCellComments"
              @cell-select="onCsvCellSelect"
              @cell-activate="onCsvCellActivate"
            />
            <pre v-else class="fv-pre" ref="preEl" @click="onPreClick"><code ref="preCodeEl"><span v-for="(line, i) in contentLines" :key="i" :class="{ 'comment-highlight': isHighlightedLine(i + 1), 'pre-line': true }" :data-line="i + 1" :data-comment-id="commentIdForLine(i + 1)">{{ line }}</span></code></pre>
            </template>
          </template>

          <!-- Backlinks Tab -->
          <div v-if="store.tab === 'backlinks'" class="fv-backlinks-pane">
            <div v-if="loadingBacklinks" class="fv-loading">Loading backlinks…</div>
            <div v-else-if="backlinks.length === 0" class="fv-empty-backlinks">No incoming links found for this note</div>
            <ul v-else class="fv-backlinks-list">
              <li v-for="b in backlinks" :key="b.path">
                <button
                  type="button"
                  class="fv-backlink-item"
                  @click="openBacklink(b.path)"
                >
                  <span class="fv-backlink-title">{{ b.title }}</span>
                  <span class="fv-backlink-path">{{ b.path }}</span>
                </button>
              </li>
            </ul>
          </div>

        </div>

        <!-- Inline comment edit popover -->
        <CommentComposePopover
          :anchor="editingCommentId && editAnchor ? editAnchor : null"
          v-model="editDraftText"
          :images="editingCommentImages"
          @cancel="cancelEditComment"
          @save="saveEditComment(editingCommentId)"
          @upload="handleEditImageUpload($event, editingCommentId)"
          @remove-image="removeEditImage"
        />

        <!-- Inline comment draft popover -->
        <CommentComposePopover
          :anchor="commentDraft && draftAnchor ? draftAnchor : null"
          v-model="composeText"
          :images="commentDraftImages"
          @cancel="cancelComment"
          @save="saveComment"
          @upload="handleDraftImageUpload"
          @remove-image="removeDraftImage"
        />

        <!-- Floating "Comment" button anchored near the active selection. -->
        <button
          v-if="selectionAnchor"
          class="fv-comment-trigger"
          :style="{ top: selectionAnchor.top + 'px', left: selectionAnchor.left + 'px' }"
          @mousedown.prevent
          @click="isCsv ? openCommentForCsvCell() : openCommentForSelection()"
          type="button"
          :title="isCsv ? 'Comment on this cell' : 'Comment on this selection'"
        >
          <span class="fv-comment-trigger-icon">💬</span>
          Comment
        </button>

        <!-- Read-only comment popover. Triggered by tapping/clicking a highlight. -->
        <div
          v-if="activePopupComment"
          class="fv-comment-backdrop"
          @click="handleBackdropClick"
        ></div>
        <div
          v-if="activePopupComment"
          class="fv-comment-pop"
          :style="{ top: popupAnchor.top + 'px', left: popupAnchor.left + 'px' }"
          @mousedown.stop
        >
          <div class="fv-pop-header">
            <span class="fv-sidebar-card-line" v-if="commentLineLabel(activePopupComment)">{{ commentLineLabel(activePopupComment) }}</span>
            <div class="fv-sidebar-card-actions fv-pop-actions">
              <button class="fv-sidebar-card-edit" @click.stop="editFromPopup(activePopupComment)" title="Edit">✎</button>
              <button class="fv-sidebar-card-remove" @click.stop="deletePopupComment" title="Delete">×</button>
            </div>
          </div>
          <div v-if="activePopupComment.images?.length" class="fv-sidebar-card-images">
            <img v-for="img in activePopupComment.images" :key="img" :src="`/api/images/${img}`" :alt="img" class="card-image-thumb" @click.stop />
          </div>
          <div class="fv-sidebar-card-note">{{ activePopupComment.comment }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useFileViewerStore } from '../stores/fileViewer'
import { errorMessage } from '../lib/errorMessage'
import { useProjectStore } from '../stores/projects'
import { api } from '../lib/api'
import { parseFrontmatter } from '../lib/markdownFrontmatter'
import { renderFileMarkdown } from '../lib/safeMarkdown'
import { buildMarkdownIndex, resolveVaultLinkTarget } from '../lib/vaultLinks'
import { openWorkspaceFileExternally } from '../lib/openWorkspaceFile'
import { createTerminalDiffLines, terminalDiffPrefix, type TerminalDiffKind } from '../lib/terminalDiff'
import { isCsvPath } from '../lib/csv'
import { askConfirm } from '../lib/confirm'
import { useFileComments } from '../composables/useFileComments'
import { writeClipboard } from '../lib/codeCopy'
import CommentComposePopover from './CommentComposePopover.vue'
const CsvViewer = defineAsyncComponent(() => import('./CsvViewer.vue'))
const HtmlArtifactViewer = defineAsyncComponent(() => import('./HtmlArtifactViewer.vue'))

const store = useFileViewerStore()
const projectsStore = useProjectStore()

// Chat transcripts archived to the vault live at
// memory-vault/Logs/Chats/<chat_id>/<provider>/<timestamp>-<session>.md.
// When such a transcript is opened here (as a file, not from the chat view),
// expose a "Continue this chat" action that reuses the same /continue flow the
// archived chat panel offers, so the transcript preview isn't a dead end.
const continuableChatId = computed(() => {
  const m = (store.path || '').match(
    /(?:^|\/)Logs\/Chats\/(chat-[^/]+)\/[^/]+\/[^/]+\.md$/,
  )
  return m ? m[1] : null
})
const isContinuing = ref(false)

async function continueFromTranscript(): Promise<void> {
  const chatId = continuableChatId.value
  if (!chatId || isContinuing.value) return
  isContinuing.value = true
  try {
    await projectsStore.continueArchivedChat(chatId)
    store.close()
  } catch (e) {
    projectsStore.pushErrorToast('Could not continue chat', `${errorMessage(e)}`)
  } finally {
    isContinuing.value = false
  }
}

// Edit mode only makes sense for text files inside a chat-scoped flow that
// the snapshot store can record under. Image files and chat-less viewer
// opens (e.g. clicking a plain path in a chat trace) stay read-only.
const canEdit = computed(() => {
  if (store.loading || store.error || !store.chatId) return false
  // Editing an artifact edits its source, which only exists once Code view has
  // fetched it. Preview view has nothing to put in the textarea.
  if (store.kind === 'html') return store.htmlView === 'code' && store.sourceLoaded
  return store.kind === 'text'
})

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

interface BacklinkItem {
  path: string
  title: string
}
const backlinks = ref<BacklinkItem[]>([])
const loadingBacklinks = ref(false)
let backlinksRequestId = 0

async function loadBacklinks(): Promise<void> {
  await store.setTab('backlinks')
  if (!store.path) return
  const requestedPath = store.path
  const requestId = ++backlinksRequestId
  loadingBacklinks.value = true
  try {
    const data = await api.get<{ backlinks: BacklinkItem[] }>(
      `/api/vault/backlinks?path=${encodeURIComponent(requestedPath)}`,
    )
    if (requestId === backlinksRequestId && store.path === requestedPath) {
      backlinks.value = data.backlinks || []
    }
  } catch {
    if (requestId === backlinksRequestId && store.path === requestedPath) {
      backlinks.value = []
    }
  } finally {
    if (requestId === backlinksRequestId) {
      loadingBacklinks.value = false
    }
  }
}

async function openBacklink(path: string): Promise<void> {
  const chatId = store.chatId
  await store.open(path, null, chatId)
}

watch(
  () => store.loadToken,
  () => {
    backlinksRequestId++
    backlinks.value = []
    loadingBacklinks.value = false
  },
)

// Click Diff next to a snapshot row in History: compares it with the
// snapshot immediately before it (or the only snapshot vs current on disk
// when there's just one).
async function diffAgainstSeq(seq: number): Promise<void> {
  const snaps = store.snapshots
  const idx = snaps.findIndex((s: { seq: number }) => s.seq === seq)
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
  if (!await askConfirm(`Restore snapshot #${seq} to disk? This writes a new snapshot so it can be undone.`, {
    title: 'Restore snapshot',
    confirmLabel: 'Restore',
    destructive: true,
  })) return
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

// `title` is the canonical human label in the vault schema; `name` is the
// retired synonym still present on older pages. Prefer title, fall back.
const fmName = computed(() => fmString('title') || fmString('name'))
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

// `related`/`links` frontmatter items are bare vault refs (`People/Mo`) —
// resolve them to file paths so the pills are clickable (same resolution as
// body links). A `[[...]]` wrapper is still tolerated so notes that predate
// the markdown-link swap keep working. `aliases` are alternative names for
// THIS note, not links, so they stay plain text.
const _LINK_LIST_KEYS = new Set(['related', 'links'])
const _linkIndex = computed(() => buildMarkdownIndex(store.markdownPaths || []))
const _linkPathSet = computed(() => new Set<string>(store.markdownPaths || []))

function resolveListItem(raw: string): { label: string; path: string | null } {
  const inner = raw.replace(/^\[\[(.+)\]\]$/, '$1').trim()
  const [ref, alias] = inner.split('|')
  const label = (alias ?? ref).trim()
  const path = ref.trim()
    ? resolveVaultLinkTarget(ref.trim(), store.path, _linkIndex.value, _linkPathSet.value)
    : null
  return { label, path }
}

const fmListExtras = computed(() => {
  const out: { key: string; items: { label: string; path: string | null }[] }[] = []
  for (const key of ['aliases', 'related', 'links']) {
    const items = fmList(key)
    if (!items.length) continue
    const resolved = _LINK_LIST_KEYS.has(key)
      ? items.map(resolveListItem)
      : items.map((raw) => ({ label: raw, path: null }))
    out.push({ key, items: resolved })
  }
  return out
})

function openRelated(path: string): void {
  const cid = store.chatId || ''
  if (/\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/i.test(path)) {
    void store.openImage(path, cid)
  } else {
    void store.open(path, null, cid)
  }
}

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
// A frontmatter value that is a bare http(s) URL (e.g. `url:`) renders as a
// clickable link rather than plain text. Only http/https so the href can't be
// a javascript:/data: scheme.
function isUrl(value: string): boolean {
  return /^https?:\/\/\S+$/.test(value.trim())
}

// ── File comments (durable, shown in sidebar + highlights) ─────────
const activeFileComments = computed(() =>
  projectsStore.fileCommentsFor(cleanPath(store.path))
)

// ── Pre line rendering & line-based highlighting ───────────────────
const contentLines = computed(() => {
  const text = bodyOnly.value
  if (text.endsWith('\n')) {
    return text.slice(0, -1).split('\n')
  }
  return text.split('\n')
})

// ── Sidebar Google Docs-style alignment ───────────────────────────────
const sidebarListEl = ref<HTMLElement>()
const sidebarSpacerEl = ref<HTMLElement>()
const commentPositions = ref<Record<string, number>>({})
const CARD_GAP = 8

function updateCommentPositions(): void {
  if (!bodyEl.value) return
  const body = bodyEl.value
  const bodyRect = body.getBoundingClientRect()
  const positions: Record<string, number> = {}

  // Find the first highlight for each comment id and record its top relative
  // to the scrollable body content. We use getBoundingClientRect instead of
  // offsetTop because CSV cells live inside their own positioned overflow
  // container (.csv-scroll), so their offsetParent is that inner scroller
  // rather than the body — offsetTop would be measured against the wrong
  // origin and the card would land at the top-left instead of beside its cell.
  const highlights = body.querySelectorAll('.comment-highlight[data-comment-id], .pre-line.comment-highlight[data-comment-id]')
  for (const el of Array.from(highlights)) {
    const id = (el as HTMLElement).dataset.commentId
    if (id && !(id in positions)) {
      const rect = (el as HTMLElement).getBoundingClientRect()
      positions[id] = rect.top - bodyRect.top + body.scrollTop
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

let popupOpenTimestamp = 0

function openPopupCommentForElement(el: HTMLElement, id: string): void {
  const rect = el.getBoundingClientRect()
  const modal = modalEl.value
  if (!modal) return
  const modalRect = modal.getBoundingClientRect()
  popupAnchor.value = {
    top: rect.bottom - modalRect.top + 6,
    left: Math.max(8, rect.left - modalRect.left),
  }
  activePopupId.value = id
  popupOpenTimestamp = Date.now()
}

function scrollToHighlight(id: string): void {
  if (!bodyEl.value) return
  const highlights = bodyEl.value.querySelectorAll('[data-comment-id]')
  for (const el of Array.from(highlights)) {
    if ((el as HTMLElement).dataset.commentId === id) {
      const targetEl = el as HTMLElement
      const top = targetEl.offsetTop - 60
      bodyEl.value.scrollTo({ top: Math.max(top, 0), behavior: 'smooth' })
      targetEl.classList.remove('comment-pulse')
      void targetEl.offsetWidth
      targetEl.classList.add('comment-pulse')
      window.setTimeout(() => targetEl.classList.remove('comment-pulse'), 1200)

      window.setTimeout(() => {
        if (targetEl) {
          openPopupCommentForElement(targetEl, id)
        }
      }, 350)
      break
    }
  }
}

function scrollToTarget(): void {
  const line = store.line
  if (!bodyEl.value || line == null) return

  // 1. Try finding a comment matching this line
  const comment = activeFileComments.value.find(
    c => (c.lineStart ?? 0) <= line && (c.lineEnd ?? c.lineStart ?? 0) >= line
  )
  if (comment) {
    scrollToHighlight(comment.id)
    return
  }

  // 2. Try finding exact line element in code view (.pre-line[data-line="X"])
  const lineEl = bodyEl.value.querySelector(`.pre-line[data-line="${line}"]`) as HTMLElement | null
  if (lineEl) {
    bodyEl.value.scrollTo({ top: Math.max(lineEl.offsetTop - 60, 0), behavior: 'smooth' })
    lineEl.classList.remove('comment-pulse')
    void lineEl.offsetWidth
    lineEl.classList.add('comment-pulse')
    window.setTimeout(() => lineEl.classList.remove('comment-pulse'), 1200)
    return
  }

  // 3. Fallback: scroll proportionally
  const total = (store.content.match(/\n/g)?.length || 1) + 1
  const ratio = Math.min(Math.max((line - 1) / total, 0), 1)
  const target = bodyEl.value.scrollHeight * ratio - bodyEl.value.clientHeight / 3
  bodyEl.value.scrollTo({ top: Math.max(target, 0), behavior: 'smooth' })
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
  observeBody()
}
function detachScrollSync(): void {
  bodyEl.value?.removeEventListener('scroll', onBodyScroll)
  sidebarListEl.value?.removeEventListener('scroll', syncSidebarToBody)
  unobserveBody()
}

// Re-measure comment anchors whenever the body's rendered content changes.
// Async children (the lazily-loaded CsvViewer parses its content in its own
// watcher, images decode, fonts settle) can paint their highlight elements
// *after* the reposition watcher already ran, which would otherwise leave
// every card stranded at the top-left fallback. The observers close that
// race by re-running the measurement once the DOM actually settles.
let bodyMutationObserver: MutationObserver | null = null
let bodyResizeObserver: ResizeObserver | null = null
let remeasureRaf = 0

function scheduleRemeasure(): void {
  if (remeasureRaf) return
  remeasureRaf = requestAnimationFrame(() => {
    remeasureRaf = 0
    updateCommentPositions()
    nextTick(() => layoutSidebarCards())
  })
}

function observeBody(): void {
  unobserveBody()
  const body = bodyEl.value
  if (!body) return
  if (typeof MutationObserver !== 'undefined') {
    bodyMutationObserver = new MutationObserver(() => scheduleRemeasure())
    bodyMutationObserver.observe(body, { childList: true, subtree: true, characterData: true })
  }
  if (typeof ResizeObserver !== 'undefined') {
    bodyResizeObserver = new ResizeObserver(() => scheduleRemeasure())
    bodyResizeObserver.observe(body)
  }
}

function unobserveBody(): void {
  bodyMutationObserver?.disconnect()
  bodyMutationObserver = null
  bodyResizeObserver?.disconnect()
  bodyResizeObserver = null
  if (remeasureRaf) {
    cancelAnimationFrame(remeasureRaf)
    remeasureRaf = 0
  }
}

// ── Mobile popup for reading a comment on tap ───────────────────────
const activePopupId = ref<string | null>(null)
const popupAnchor = ref<{ top: number; left: number }>({ top: 0, left: 0 })

const activePopupComment = computed(() => {
  if (!activePopupId.value) return null
  return activeFileComments.value.find(c => c.id === activePopupId.value) || null
})

function openPopupComment(e: MouseEvent, id: string): void {
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

function handleBackdropClick(): void {
  if (Date.now() - popupOpenTimestamp < 150) return
  closePopupComment()
}

function deletePopupComment(): void {
  const id = activePopupId.value
  if (!id) return
  activePopupId.value = null
  const c = activeFileComments.value.find(x => x.id === id)
  if (c) comments.deleteFileComment(id)
}

function editFromPopup(c: { id: string; comment: string; images?: string[] }): void {
  const local = popupAnchor.value
  closePopupComment()
  comments.startEditComment(c, comments.toViewportAnchor(local))
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
// clearHighlights / highlightInMarkdown live in useFileComments; this
// surface keeps its own guard set (commentable state, image/CSV exclusions)
// and feeds the shared highlight helpers.
function applyHighlights(): void {
  if (!isCommentable.value) return
  if (store.kind === 'image') return
  if (isCsv.value) return // cell highlights are driven by CsvViewer props

  if (isMarkdown.value) {
    const root = mdEl.value
    if (!root) return
    comments.clearHighlights(root)
    for (const c of activeFileComments.value) {
      comments.highlightInMarkdown(root, c.selection, c.id)
    }
    const draft = comments.commentDraft.value
    if (draft?.selection) {
      comments.highlightInMarkdown(root, draft.selection, comments.DRAFT_COMMENT_ID)
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

const backdropEl = ref<HTMLElement>()
const modalEl = ref<HTMLElement>()
const bodyEl = ref<HTMLElement>()
const mdEl = ref<HTMLElement>()
const preEl = ref<HTMLElement>()
const preCodeEl = ref<HTMLElement>()
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

// Automatically pin opened documents to the side panel if desktop split view is available
// and no file is currently pinned in the active chat/project, bypassing the modal overlay.
watch(
  [() => store.isOpen, () => store.path],
  ([isOpen, currentPath]) => {
    if (!isOpen || !currentPath) return
    const key = activePinKey.value
    if (canPin.value && key && !projectsStore.pinnedFileFor(key)) {
      projectsStore.pinFile(key, cleanPath(currentPath))
      store.close()
    }
  },
  { immediate: true },
)

// Selection-driven comment UX. Two states:
//   1. selectionAnchor != null  → floating "Comment" trigger sits near the
//      bottom-right of the live selection. Disappears on collapse/blur.
//   2. commentDraft != null     → user clicked the trigger; we capture the
//      selected text (so it survives selection loss when the textarea grabs
//      focus) and show an inline composer at the same anchor.
// Anything we render as text is fair game for commenting — the floating
// trigger should appear in both the markdown branch and the <pre> branch.
// Images stay opt-out.
const isCommentable = computed(() =>
  store.isOpen && !store.loading && !store.error && store.kind === 'text'
)

// Shared file-comment subsystem (selection anchoring, draft/edit state,
// highlight helpers, image uploads) lives in useFileComments — the same
// logic PinnedFilePanel uses. Only the surface-specific inputs are wired
// here: the path/content sources and the anchor coordinate root (modalEl).
const comments = useFileComments({
  path: () => cleanPath(store.path),
  content: () => store.content,
  commentsForFile: activeFileComments,
  isCommentable,
  containerEl: modalEl,
  bodyEl,
  mdEl,
  preEl,
  preCodeEl,
  scrollToHighlight,
})
// Script code reaches the shared methods/state through `comments.*`; only
// the template-bound names are destructured here.
const {
  selectionAnchor, draftAnchor, commentDraft, composeText, csvCellComments,
  commentDraftImages, editingCommentId, editDraftText, editingCommentImages, editAnchor,
  isHighlightedLine, commentIdForLine, commentLineLabel,
  onCsvCellSelect, onCsvCellActivate, openCommentForSelection, openCommentForCsvCell,
  cancelComment, saveComment, handleDraftImageUpload, removeDraftImage,
  cancelEditComment, saveEditComment, handleEditImageUpload, removeEditImage,
} = comments
comments.setApplyHighlights(applyHighlights)

const basename = computed(() => {
  const p = store.path
  const idx = p.lastIndexOf('/')
  return idx === -1 ? p : p.slice(idx + 1)
})

const isMarkdown = computed(() => /\.(md|markdown)$/i.test(store.path))
const isCsv = computed(() => isCsvPath(store.path))

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
      if (store.loading || store.error) return
      scrollToTarget()
    })
  },
)

// Re-apply markdown highlights and sidebar positions whenever the content
// or comment list changes.
watch(
  () => `${store.loadToken}|${store.path}|${activeFileComments.value.map(c => c.id).join(',')}|${renderedMarkdown.value.length}|${store.content.length}`,
  () => nextTick(() => {
    applyHighlights()
    scrollToTarget()
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


// Strip the `:line` suffix that the viewer accepts on text files so the
// comment carries a clean workspace path. The line number is preserved
// separately on the draft if we can guess it from the selection.
function cleanPath(p: string): string {
  return p.replace(/:\d+$/, '')
}

// Re-anchor the floating comment trigger on scroll. Kept here (not in
// useFileComments): PinnedFilePanel's variant additionally suppresses
// re-anchoring during programmatic scrolls and closes its hover-pin popover.
function onScrollReanchor(): void {
  if (comments.commentDraft.value || !comments.lastSelectionRange) return
  try {
    if (!comments.lastSelectionRange.startContainer.isConnected) {
      comments.lastSelectionRange = null
      comments.selectionAnchor.value = null
      return
    }
    comments.updateSelectionAnchorFromRange(comments.lastSelectionRange)
  } catch {
    comments.lastSelectionRange = null
    comments.selectionAnchor.value = null
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('selectionchange', comments.onSelectionChange)
}
function onSidebarResize(): void {
  updateCommentPositions()
  nextTick(() => layoutSidebarCards())
}
function onBeforeUnload(e: BeforeUnloadEvent): void {
  if (store.isDirty) {
    e.preventDefault()
    e.returnValue = ''
  }
}
if (typeof window !== 'undefined') {
  window.addEventListener('resize', onSidebarResize)
  window.addEventListener('beforeunload', onBeforeUnload)
}
onBeforeUnmount(() => {
  if (typeof document !== 'undefined') {
    document.removeEventListener('selectionchange', comments.onSelectionChange)
  }
  if (typeof window !== 'undefined') {
    window.removeEventListener('resize', onSidebarResize)
    window.removeEventListener('beforeunload', onBeforeUnload)
  }
  detachScrollSync()
})

// Reset draft + anchor whenever the file changes or the modal closes.
watch(
  () => store.loadToken,
  () => {
    comments.selectionAnchor.value = null
    comments.commentDraft.value = null
    comments.lastSelectionText = ''
    comments.lastSelectionLines = null
    comments.lastSelectionRange = null
  },
)
watch(
  () => store.isOpen,
  (open) => {
    if (!open) {
      comments.selectionAnchor.value = null
      comments.commentDraft.value = null
      comments.lastSelectionRange = null
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
  const copied = await writeClipboard(store.path)
  if (!copied) return
  copyState.value = 'ok'
  setTimeout(() => { copyState.value = '' }, 1200)
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
  } else if (store.kind === 'html') {
    // Straight from the endpoint: an artifact in Preview view has never
    // fetched its source, so an in-memory Blob would be empty.
    a.href = `/api/workspace-file?path=${encodeURIComponent(cleaned)}`
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
  color: var(--success);
}
.fv-actions .btn-icon:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.fv-continue-btn {
  margin-right: 6px;
  padding: 4px 10px;
  font-size: 12px;
  font-weight: 600;
  color: var(--bg);
  background: var(--accent);
  border: none;
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
}
.fv-continue-btn:hover {
  filter: brightness(1.08);
}
.fv-continue-btn:disabled {
  opacity: 0.6;
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
.fv-body-csv {
  display: flex;
  flex-direction: column;
  overflow: hidden;
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
  font-size: var(--text-base);
  line-height: 1.6;
  max-width: 88ch;
}
.fv-md :deep(p) { margin: 0.6em 0; }
.fv-md :deep(:first-child) { margin-top: 0; }
.fv-md :deep(:last-child) { margin-bottom: 0; }
.fv-md :deep(pre) {
  background: var(--bg);
  padding: 8px 12px;
  border-radius: var(--radius-sm, 6px);
  overflow-x: auto;
  font-size: var(--text-sm);
  font-family: var(--font-mono);
}
.fv-md :deep(code) {
  font-family: var(--font-mono);
}
.fv-md :deep(:not(pre) > code) {
  background: color-mix(in srgb, var(--fg) 8%, transparent);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 0.9em;
}
.fv-md :deep(:is(h1, h2, h3, h4)) {
  margin-top: 1.2em;
  margin-bottom: 0.4em;
  line-height: 1.35;
  font-weight: 700;
}
.fv-md :deep(h1) { font-size: 1.5em; }
.fv-md :deep(h2) { font-size: 1.25em; }
.fv-md :deep(h3) { font-size: 1.1em; }
.fv-md :deep(a) {
  color: var(--accent);
  text-decoration: underline;
}
.fv-md :deep(a:hover) {
  color: var(--accent-strong);
}
.fv-md :deep(.vault-link-unresolved) {
  color: var(--fg2);
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
  background: var(--error);
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

/* Comment drawer overlay */
.fv-main {
  position: relative;
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
.fv-comment-backdrop {
  position: absolute;
  inset: 0;
  z-index: 20;
  background: transparent;
}
.fv-comment-drawer {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 21;
  width: 300px;
  max-width: 85%;
  border-left: 1px solid var(--border);
  background: var(--bg2, rgba(20, 20, 40, 0.98));
  box-shadow: -6px 0 20px rgba(0, 0, 0, 0.28);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.fv-drawer-close {
  background: transparent;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: calc(16px * var(--font-scale));
  line-height: 1;
  padding: 0 4px;
  margin-left: auto;
}
.fv-drawer-close:hover { color: var(--fg); }
.fv-drawer-empty {
  padding: 16px 14px;
  color: var(--fg2);
  font-size: var(--text-sm);
}

.fv-comments-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 28px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: transparent;
  color: var(--fg2);
  cursor: pointer;
  font-size: var(--text-sm);
  font-weight: 600;
  transition: background 120ms var(--ease), color 120ms var(--ease), border-color 120ms var(--ease);
}
.fv-comments-toggle:hover { background: var(--bg3); color: var(--fg); }
.fv-comments-toggle.active { border-color: var(--accent, #60a5fa); color: var(--fg); }
.fv-comments-toggle-icon { font-size: var(--text-sm); line-height: 1; }
.fv-comments-toggle-count { font-variant-numeric: tabular-nums; }

.fv-comment-pop {
  position: absolute;
  z-index: 32;
  width: 280px;
  max-width: calc(100% - 16px);
  background: var(--bg2, rgba(20, 20, 40, 0.98));
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.32);
  padding: 10px 12px;
  box-sizing: border-box;
}
.fv-pop-header {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 6px;
}
.fv-pop-actions {
  opacity: 1 !important;
  margin-left: auto;
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
:deep(.comment-highlight[data-comment-id="__draft__"]),
.pre-line.comment-highlight[data-comment-id="__draft__"] {
  background: rgba(234, 179, 8, 0.45);
  border-bottom-color: rgba(234, 179, 8, 0.9);
}

:deep(.comment-highlight.comment-pulse),
.pre-line.comment-highlight.comment-pulse,
.pre-line.comment-pulse {
  animation: fv-comment-pulse 1.1s var(--ease, ease) 1;
}
@keyframes fv-comment-pulse {
  0%   { background: rgba(234, 179, 8, 0.25); box-shadow: 0 0 0 0 rgba(234, 179, 8, 0); }
  25%  { background: rgba(234, 179, 8, 0.75); box-shadow: 0 0 0 6px rgba(234, 179, 8, 0.25); }
  100% { background: rgba(234, 179, 8, 0.25); box-shadow: 0 0 0 0 rgba(234, 179, 8, 0); }
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
    max-height: calc(100dvh - var(--safe-top) - var(--safe-bottom));
    height: calc(100dvh - var(--safe-top) - var(--safe-bottom));
    margin-top: var(--safe-top);
    margin-bottom: var(--safe-bottom);
    width: 100vw;
  }
  .fv-body { padding: 14px 16px; }
  .fv-md { max-width: none; }
  .fv-comment-drawer {
    left: 0;
    top: auto;
    width: auto;
    max-width: none;
    max-height: 60vh;
    border-left: none;
    border-top: 1px solid var(--border);
    border-top-left-radius: 14px;
    border-top-right-radius: 14px;
    box-shadow: 0 -6px 20px rgba(0, 0, 0, 0.32);
  }
  .fv-comment-pop {
    left: 8px !important;
    right: 8px;
    width: auto;
    max-width: none;
  }
}

/* Backlinks Pane */
.fv-backlinks-pane {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.fv-empty-backlinks {
  color: var(--fg2);
  font-size: var(--text-sm);
  padding: 24px 0;
  text-align: center;
}
.fv-backlinks-list {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.fv-backlink-item {
  width: 100%;
  min-height: var(--touch);
  display: flex;
  flex-direction: column;
  padding: 10px 14px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 120ms var(--ease);
}
.fv-backlink-item:hover {
  background: var(--bg3);
}
.fv-backlink-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.fv-backlink-title {
  font-weight: 600;
  font-size: var(--text-base);
  color: var(--fg);
}
.fv-backlink-path {
  font-size: var(--text-xs);
  color: var(--fg2);
}
</style>
