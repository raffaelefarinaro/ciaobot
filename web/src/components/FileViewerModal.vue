<template>
  <DialogRoot
    :open="store.isOpen"
    modal
    @update:open="onOpenChange"
  >
    <DialogOverlay as-child>
      <div class="fv-backdrop">
        <DialogContent
          as-child
          aria-modal="true"
          @open-auto-focus="onOpenAutoFocus"
          @close-auto-focus="onCloseAutoFocus"
          @escape-key-down="onEscapeKeyDown"
          @pointer-down-outside="onPointerDownOutside"
        >
          <div class="fv-modal" :class="{ 'fv-modal--doc': isProse }" tabindex="-1">
            <header :ref="setModalEl" class="fv-header">
              <div class="fv-titles">
                <DialogTitle as="div" class="fv-title" :title="store.path">{{ basename }}</DialogTitle>
                <!-- The folder, shortened as the pinned tile shows it; the
                     full path stays in the description for screen readers. -->
                <DialogDescription as="div" class="fv-subtitle" :title="store.path">
                  <span aria-hidden="true">{{ shortDirname(cleanPath(store.path)) }}<template v-if="store.line"> :{{ store.line }}</template></span>
                  <span class="sr-only">{{ store.path }}<template v-if="store.line"> :{{ store.line }}</template></span>
                </DialogDescription>
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
          <!-- Two text actions and the pin stay visible; the file utilities
               (copy path, download, open elsewhere, memory map) share one
               menu instead of a row of look-alike icons. The menu renders
               inside the dialog (no portal) so the dialog's focus trap and
               outside-click handling keep working. -->
          <button
            v-if="canEdit && !store.editing"
            type="button"
            class="fv-text-btn"
            @click="store.startEditing"
          >Edit</button>
          <button
            v-if="store.path"
            type="button"
            class="fv-text-btn fv-discuss-btn"
            title="Discuss in chat"
            @click="discussInChat"
          >Discuss</button>
          <button
            v-if="canPin"
            class="btn-icon"
            :class="{ active: isPinned }"
            :disabled="pinPending"
            :title="isPinned ? 'Unpin from sidebar' : 'Pin to sidebar'"
            :aria-label="isPinned ? 'Unpin from sidebar' : 'Pin to sidebar'"
            @click="togglePin"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a3 3 0 0 0-6 0z"/></svg>
          </button>
          <DropdownMenuRoot :modal="false">
            <DropdownMenuTrigger as-child>
              <button type="button" class="btn-icon" aria-label="More file actions" title="More">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" /></svg>
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent as-child align="end" :side-offset="6" :collision-padding="8">
              <div class="fv-actions-menu">
                <!-- On a phone Discuss moves here, so the filename keeps the bar. -->
                <DropdownMenuItem v-if="store.path" as-child class="fv-menu-discuss" @select="discussInChat">
                  <button type="button">Discuss in chat</button>
                </DropdownMenuItem>
                <DropdownMenuItem as-child @select.prevent="copyPath">
                  <button type="button">{{ copyState === 'ok' ? 'Copied' : 'Copy path' }}</button>
                </DropdownMenuItem>
                <DropdownMenuItem as-child :disabled="store.loading || !!store.error" @select="downloadFile">
                  <button type="button">Download</button>
                </DropdownMenuItem>
                <DropdownMenuItem v-if="canShareFile" as-child :disabled="store.loading || !!store.error" @select="shareFile">
                  <button type="button">Share file…</button>
                </DropdownMenuItem>
                <DropdownMenuItem v-if="canOpenExternally" as-child :disabled="store.loading || !!store.error || openExternalState === 'loading'" @select="openExternally">
                  <button type="button">{{ openExternalState === 'ok' ? 'Opened' : 'Open in default app' }}</button>
                </DropdownMenuItem>
                <DropdownMenuItem v-if="memoryPath" as-child @select="openInMemoryMap">
                  <button type="button">Open in memory map</button>
                </DropdownMenuItem>
              </div>
            </DropdownMenuContent>
          </DropdownMenuRoot>
          <button class="btn-icon" @click="requestClose" title="Close (Esc)" aria-label="Close">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
      </header>

      <div class="fv-main" ref="mainEl">
        <div class="fv-body" :class="{ 'fv-body-image': store.kind === 'image', 'fv-body-csv': isCsv, 'fv-body-html': store.kind === 'html' }" ref="bodyEl">
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
                ref="editTextareaEl"
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

          <template v-else>
            <div v-if="store.kind === 'pdf' && store.pptxNeedsLibreoffice" class="fv-libreoffice-notice hint hint--warn">
              <strong>LibreOffice is required to preview PowerPoint files.</strong>
              <span v-if="store.libreofficeInstallError"> {{ store.libreofficeInstallError }}</span>
              <button
                class="btn-primary btn-small"
                @click="store.installLibreofficeInChat"
              >Install in Chat</button>
            </div>
            <iframe
              v-else-if="store.kind === 'pdf'"
              class="fv-pdf-iframe"
              :src="`/api/workspace-binary?path=${encodeURIComponent(store.path)}&t=${store.loadToken}`"
              width="100%"
              height="100%"
              :title="`Preview of ${basename}`"
              style="border: none; flex: 1; min-height: 500px; display: block; border-radius: 4px;"
            ></iframe>
            <HtmlArtifactViewer
              v-else-if="store.kind === 'html' && !store.editing"
              ref="artifactViewerRef"
              :file-path="store.path"
              :reload-token="store.loadToken"
              :view="store.htmlView"
              :source="store.content"
              :source-loading="store.sourceLoading"
              :source-error="store.sourceError"
              @update:view="store.setHtmlView"
              @compose-comment="onArtifactCompose"
              @open-comment="onArtifactOpenComment"
              @bridge-ready="onArtifactBridgeReady"
            />
            <template v-else>
            <!-- Document properties from YAML frontmatter: a quiet header in
                 the document's own measure, not a card of uppercase pills. -->
            <section v-if="metadata.frontmatter" class="fv-meta" aria-label="Document properties">
              <div class="fv-meta-line">
                <span class="fv-meta-kind">
                  <template v-if="metadata.type">{{ humanizeVaultMetadata(metadata.type) }}</template>
                  <template v-if="metadata.type && metadata.status"> · </template>
                  <span v-if="metadata.status" class="fv-meta-status" :class="`fv-meta-status--${metadata.status}`">{{ humanizeVaultMetadata(metadata.status) }}</span>
                </span>
                <span v-if="metadata.updated" class="fv-meta-date">Updated {{ metadata.updated }}</span>
                <span v-else-if="metadata.created" class="fv-meta-date">Created {{ metadata.created }}</span>
              </div>
              <p v-if="metadata.showName" class="fv-meta-name" :title="metadata.name">{{ metadata.name }}</p>
              <p v-if="metadata.summary" class="fv-meta-summary">{{ metadata.summary }}</p>
              <p v-if="metadata.tags.length" class="fv-meta-tags">
                <span v-for="t in metadata.tags" :key="t" class="fv-meta-tag">#{{ t }}</span>
              </p>
              <dl v-if="metadata.listExtras.length || metadata.extraEntries.length" class="fv-meta-fields">
                <template v-for="listExtra in metadata.listExtras" :key="listExtra.key">
                  <dt>{{ humanizeVaultMetadata(listExtra.key) }}</dt>
                  <dd>
                    <template v-for="(item, i) in listExtra.items" :key="i">
                      <a
                        v-if="item.path"
                        class="fv-meta-link file-link"
                        href="#"
                        :title="item.path"
                        @click.prevent="openRelated(item.path)"
                      >{{ item.label }}</a>
                      <span v-else class="fv-meta-value">{{ item.label }}</span>
                    </template>
                  </dd>
                </template>
                <template v-for="entry in metadata.extraEntries" :key="entry.key">
                  <dt>{{ humanizeVaultMetadata(entry.key) }}</dt>
                  <dd>
                    <a
                      v-if="isVaultMetadataUrl(entry.value)"
                      :href="entry.value"
                      target="_blank"
                      rel="noopener noreferrer"
                    >{{ entry.value }}</a>
                    <template v-else>{{ entry.value }}</template>
                  </dd>
                </template>
              </dl>
            </section>
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

        </div>

        <!-- Inline comment edit popover -->
        <CommentComposePopover
          inline
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
          inline
          ref="composeDraftRef"
          :anchor="commentDraft && draftAnchor ? draftAnchor : null"
          v-model="composeText"
          :images="commentDraftImages"
          @cancel="artifactDraft ? cancelArtifactComment() : cancelComment()"
          @save="artifactDraft ? saveArtifactComment() : saveComment()"
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
          <svg class="fv-comment-trigger-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z" /></svg>
          Comment
        </button>

        <!-- Read-only comment popover. Triggered by tapping/clicking a highlight. -->
        <div
          v-if="activePopupComment"
          class="fv-comment-backdrop"
          @click="handleBackdropClick"
        ></div>
        <FocusScope
          v-if="activePopupComment"
          as-child
          loop
          :trapped="true"
          @mount-auto-focus="onPopupMountAutoFocus"
          @unmount-auto-focus="onPopupUnmountAutoFocus"
        >
          <div
            ref="popupEl"
            class="fv-comment-pop"
            role="dialog"
            aria-label="Comment"
            tabindex="-1"
            :style="{ top: placedPopupAnchor.top + 'px', left: placedPopupAnchor.left + 'px' }"
            @mousedown.stop
            @keydown="onPopupKeydown"
          >
            <div v-if="commentLineLabel(activePopupComment)" class="fv-pop-quote">{{ basename }} · line {{ commentLineLabel(activePopupComment) }}</div>
            <div v-if="activePopupComment.images?.length" class="fv-sidebar-card-images">
              <img v-for="img in activePopupComment.images" :key="img" :src="`/api/images/${img}`" :alt="img" class="card-image-thumb" @click.stop />
            </div>
            <div class="fv-sidebar-card-note">{{ activePopupComment.comment }}</div>
            <div class="fv-pop-actions">
              <button type="button" class="fv-sidebar-card-edit" @click.stop="editFromPopup(activePopupComment)">Edit</button>
              <button type="button" class="fv-sidebar-card-remove" @click.stop="deletePopupComment">Delete</button>
            </div>
          </div>
        </FocusScope>
        </div>
          </div>
        </DialogContent>
      </div>
    </DialogOverlay>
  </DialogRoot>
</template>

<script setup lang="ts">
import {
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogRoot,
  DialogTitle,
  FocusScope,
  type PointerDownOutsideEvent,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRoot,
  DropdownMenuTrigger,
} from 'reka-ui'
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, ref, watch, type ComponentPublicInstance } from 'vue'
import { useFileViewerStore } from '../stores/fileViewer'
import { errorMessage } from '../lib/errorMessage'
import { useProjectStore } from '../stores/projects'
import { router } from '../router'
import { useMemoryMapStore, memorySectionPath } from '../stores/memoryMap'
import { buildVaultDocumentMetadata, humanizeVaultMetadata, isVaultMetadataUrl, renderVaultMarkdown } from '../lib/vaultDocument'
import { openWorkspaceFileExternally } from '../lib/openWorkspaceFile'
import { isLoopbackPage } from '../lib/loopback'
import { isCsvPath } from '../lib/csv'
import { shortDirname } from '../lib/chatActivity'
import { useFileComments } from '../composables/useFileComments'
import { useTypeToComment } from '../composables/useTypeToComment'
import { useViewportHeight } from '../composables/useViewportHeight'
import { clampAnchorLeft, clampAnchorTop } from '../lib/popoverAnchor'
import { startFileDiscussion } from '../lib/fileDiscussion'
import type { ArtifactHighlight } from '../lib/artifactBridge'
import { writeClipboard } from '../lib/codeCopy'
import { handleAppLinkClick } from '../lib/appLinks'
import { handleOpenLocalFileClick } from '../lib/openWorkspaceFile'
import CommentComposePopover from './CommentComposePopover.vue'
const CsvViewer = defineAsyncComponent(() => import('./CsvViewer.vue'))
const HtmlArtifactViewer = defineAsyncComponent(() => import('./HtmlArtifactViewer.vue'))

const store = useFileViewerStore()
const props = withDefaults(defineProps<{ opener?: HTMLElement | null }>(), { opener: null })
const opener = ref<HTMLElement | null>(props.opener)
watch(() => store.isOpen, (open, wasOpen) => {
  if (open && !wasOpen && document.activeElement instanceof HTMLElement) {
    opener.value = document.activeElement
  }
}, { flush: 'sync' })
const projectsStore = useProjectStore()
const memoryMapStore = useMemoryMapStore()
let closePending = false

async function requestClose(): Promise<void> {
  if (!store.isOpen || closePending) return
  closePending = true
  try {
    await store.dismissSharedPin()
  } finally {
    closePending = false
  }
}

function onOpenChange(open: boolean) {
  if (!open) void requestClose()
}

function focusViewer(): void {
  const target = modalEl.value ?? bodyEl.value?.closest<HTMLElement>('.fv-modal')
  target?.focus()
}

function onOpenAutoFocus(event: Event) {
  event.preventDefault()
  focusViewer()
  nextTick(focusViewer)
}

function onCloseAutoFocus(event: Event) {
  event.preventDefault()
  nextTick(() => {
    if (opener.value?.isConnected) opener.value.focus()
  })
}

function onEscapeKeyDown(event: KeyboardEvent) {
  event.preventDefault()
  void requestClose()
}

function onPointerDownOutside(event: PointerDownOutsideEvent) {
  event.preventDefault()
  const original = event.detail.originalEvent
  if (original.button !== 0 || original.ctrlKey) return
  void requestClose()
}

const memoryPath = computed(() => {
  const path = store.path.replace(/:\d+$/, '')
  return /\.(md|markdown)$/i.test(path) ? path : ''
})

async function openInMemoryMap(): Promise<void> {
  // Read the path before closing: `close()` clears `store.path`, which
  // `memoryPath` derives from.
  const target = memoryPath.value
  if (!target) return
  // `close()` is false when the file has unsaved edits and the discard prompt
  // is declined. The focus request outlives this modal, so recording it before
  // that check would leave the map jumping to this note the next time /memory
  // opens — after a navigation the user cancelled.
  if (!(await store.close())) return
  // Not `requestFocus`: the map is not mounted yet, so nothing is listening
  // for the signal, and the graph load it does on arrival clears `selectedId`
  // anyway. The map drains this once its nodes are placed.
  memoryMapStore.requestFocusOnOpen(target)
  await router.push(memorySectionPath('map'))
}

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

const metadata = computed(() => buildVaultDocumentMetadata(
  store.content,
  store.path,
  store.markdownPaths,
))
const renderedMarkdown = computed(() => renderVaultMarkdown(metadata.value.body, store.path, store.markdownPaths, true))

function openRelated(path: string): void {
  const cid = store.chatId || ''
  if (/\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/i.test(path)) {
    void store.openImage(path, cid)
  } else {
    void store.open(path, null, cid)
  }
}

// ── File comments (durable, shown inline as highlights and popovers) ─
const activeFileComments = computed(() =>
  projectsStore.fileCommentsFor(cleanPath(store.path))
)

// ── Pre line rendering & line-based highlighting ───────────────────
const contentLines = computed(() => {
  const text = metadata.value.body
  if (text.endsWith('\n')) {
    return text.slice(0, -1).split('\n')
  }
  return text.split('\n')
})

let popupOpenTimestamp = 0

function openPopupCommentForElement(el: HTMLElement, id: string): void {
  const rect = el.getBoundingClientRect()
  const container = mainEl.value ?? modalEl.value
  if (!container) return
  const containerRect = container.getBoundingClientRect()
  popupAnchor.value = {
    top: rect.bottom - containerRect.top + 6,
    left: rect.left - containerRect.left,
  }
  activePopupId.value = id
  popupOpenTimestamp = Date.now()
  measurePopup()
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

function onBodyScroll(): void {
  onScrollReanchor()
}

function attachBodyScrollListener(): void {
  if (!bodyEl.value) return
  detachBodyScrollListener()
  bodyEl.value.addEventListener('scroll', onBodyScroll, { passive: true })
}
function detachBodyScrollListener(): void {
  bodyEl.value?.removeEventListener('scroll', onBodyScroll)
}

// ── Mobile popup for reading a comment on tap ───────────────────────
const activePopupId = ref<string | null>(null)
const popupAnchor = ref<{ top: number; left: number }>({ top: 0, left: 0 })
const popupHeight = ref(96)
let skipPopupFocusRestore = false

const activePopupComment = computed(() => {
  if (!activePopupId.value) return null
  return activeFileComments.value.find(c => c.id === activePopupId.value) || null
})

// The popup is absolute inside .fv-main, so its clamp boundary is that
// scroll pane rather than the window. `viewportH` still matters on iOS: the
// software keyboard can make the modal shorter after the popup opens.
const placedPopupAnchor = computed(() => {
  const anchor = popupAnchor.value
  const main = mainEl.value
  if (!main) return anchor
  const rect = main.getBoundingClientRect()
  const mainHeight = main.clientHeight || rect.height || viewportH.value
  const visibleHeight = Math.max(80, Math.min(mainHeight, viewportH.value - Math.max(0, rect.top)))
  const mainWidth = main.clientWidth || rect.width || window.innerWidth
  return {
    top: clampAnchorTop(anchor.top, popupHeight.value, visibleHeight),
    left: clampAnchorLeft(anchor.left, 280, mainWidth),
  }
})

function measurePopup(): void {
  nextTick(() => {
    const height = popupEl.value?.offsetHeight
    if (height) popupHeight.value = height
  })
}

function openPopupComment(e: MouseEvent, id: string): void {
  const target = e.target as HTMLElement
  const rect = target.getBoundingClientRect()
  const container = mainEl.value ?? modalEl.value
  if (!container) return
  const containerRect = container.getBoundingClientRect()
  popupAnchor.value = {
    top: rect.bottom - containerRect.top + 6,
    left: rect.left - containerRect.left,
  }
  activePopupId.value = id
  popupOpenTimestamp = Date.now()
  measurePopup()
}

function closePopupComment(): void {
  activePopupId.value = null
  popupHeight.value = 96
}

function onPopupMountAutoFocus(event: Event): void {
  event.preventDefault()
  nextTick(() => popupEl.value?.querySelector<HTMLElement>('button')?.focus())
}

function onPopupUnmountAutoFocus(event: Event): void {
  if (!skipPopupFocusRestore) return
  skipPopupFocusRestore = false
  // Edit/selection handoffs focus their next editor synchronously. Do not let
  // the old nested scope's deferred restoration pull focus back to the mark.
  event.preventDefault()
}

function onPopupKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return
  // Escape belongs to the nested read popup. Without stopping it here the
  // enclosing file Dialog would close as well.
  event.preventDefault()
  event.stopPropagation()
  closePopupComment()
}

function handleBackdropClick(): void {
  if (Date.now() - popupOpenTimestamp < 150) return
  closePopupComment()
}

function deletePopupComment(): void {
  const id = activePopupId.value
  if (!id) return
  closePopupComment()
  const c = activeFileComments.value.find(x => x.id === id)
  if (c) comments.deleteFileComment(id)
}

function editFromPopup(c: { id: string; comment: string; images?: string[] }): void {
  const local = placedPopupAnchor.value
  skipPopupFocusRestore = true
  closePopupComment()
  comments.startEditComment(c, comments.toViewportAnchor(local))
}

function onMdClick(e: MouseEvent): void {
  const target = e.target as HTMLElement | null
  if (!target) return
  // The task opens behind this dialog, so the dialog closes first; a refused
  // close (unsaved edits kept) leaves the user where they were.
  if (handleAppLinkClick(e, () => store.close())) return
  if (handleOpenLocalFileClick(e, (m) => projectsStore.pushErrorToast('Could not open file', m))) return

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

const modalEl = ref<HTMLElement>()
const mainEl = ref<HTMLElement>()
const bodyEl = ref<HTMLElement>()
const popupEl = ref<HTMLElement>()
const mdEl = ref<HTMLElement>()
const preEl = ref<HTMLElement>()
const preCodeEl = ref<HTMLElement>()
const editTextareaEl = ref<HTMLTextAreaElement>()
const viewportH = useViewportHeight()

function setModalEl(value: Element | ComponentPublicInstance | null): void {
  modalEl.value = value instanceof HTMLElement
    ? value.closest<HTMLElement>('.fv-modal') ?? undefined
    : undefined
}

const copyState = ref<'' | 'ok'>('')
const openExternalState = ref<'' | 'loading' | 'ok'>('')
// The file opens on the engine's machine, so only offer it there: from a phone
// or another Mac it would open on a screen nobody is looking at.
const canOpenExternally = isLoopbackPage()

// The chat on screen whose split view can take the file; '' where there is
// none (a phone, a project page, Home, Settings, the memory map). Pinning
// anywhere else would put the file beside a chat that is not shown.
const activePinKey = computed(() => store.dockTarget)
const canPin = computed(() => !!activePinKey.value)
const isPinned = computed(() => {
  if (!activePinKey.value) return false
  return projectsStore.pinnedFileFor(activePinKey.value) === cleanPath(store.path)
})
const pinPending = computed(() => {
  const key = activePinKey.value
  return !!key && projectsStore.isChatPinPending(key)
})
async function togglePin(): Promise<void> {
  const key = activePinKey.value
  if (!key || pinPending.value) return
  const path = cleanPath(store.path)
  if (isPinned.value) {
    await projectsStore.unpinFile(key)
  } else {
    const generation = store.loadToken
    const pinned = await projectsStore.pinFile(key, path)
    if (pinned && store.loadToken === generation && cleanPath(store.path) === path) void store.close()
  }
}

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
  cancelComment, saveComment, handleDraftImageUpload, addDraftImages, removeDraftImage,
  cancelEditComment, saveEditComment, handleEditImageUpload, removeEditImage,
} = comments
comments.setApplyHighlights(applyHighlights)

// ── Artifact comments (HTML preview) ─────────────────────────────────
// Same flow as PinnedFilePanel: the bridge inside the frame posts selection/
// element anchors, this stages them as pending file comments. The modal's
// read popover is modal-local (not Teleported), so the frame coords convert
// through modalEl; the compose popover is Teleported, so it wants viewport.
const artifactViewerRef = ref<InstanceType<typeof HtmlArtifactViewer> | null>(null)

// Anchor captured with the compose flow; null for non-artifact drafts.
let artifactDraft: {
  selector: string
  startOffset: number
  endOffset: number
  elementTag: string | null
  wholeElement: boolean
} | null = null

// Draft highlight pushed into the frame while the compose popover is open, so
// the annotation stays visible after the selection collapses.
const artifactDraftHighlight = ref<ArtifactHighlight | null>(null)

const artifactHighlights = computed<ArtifactHighlight[]>(() =>
  activeFileComments.value
    .filter(c => !!c.artifactSelector)
    .map(c => ({
      id: c.id,
      selector: c.artifactSelector as string,
      quote: c.selection,
      startOffset: c.artifactStartOffset,
      endOffset: c.artifactEndOffset,
      wholeElement: c.artifactWholeElement ?? false,
    })),
)

// Without this the modal rendered no marks at all: it staged anchors fine but
// never pushed the list back into the frame, which also left the mark-click
// handler below unreachable.
function pushArtifactHighlights(): void {
  if (store.kind !== 'html' || store.editing || store.htmlView !== 'preview') return
  const list = artifactDraftHighlight.value
    ? [...artifactHighlights.value, artifactDraftHighlight.value]
    : artifactHighlights.value
  nextTick(() => artifactViewerRef.value?.sendHighlights(list))
}

// Comment-list changes only. Frame loads are covered by @bridge-ready: a push
// on mount or right after a loadToken bump reaches the previous document and
// is dropped.
watch(
  () => activeFileComments.value.map(c => c.id).join(','),
  () => pushArtifactHighlights(),
)

function onArtifactBridgeReady(): void {
  pushArtifactHighlights()
}

function onArtifactCompose(a: {
  selector: string
  quote: string
  startOffset: number
  endOffset: number
  elementTag?: string
  wholeElement?: boolean
  frameX: number
  frameY: number
}): void {
  if (commentDraft.value || editingCommentId.value) return
  // Close any read popover first, as the pinned panel already does: without
  // this, clicking an existing highlight and then selecting other text in the
  // frame floated the read and compose popovers over it at the same time.
  if (activePopupId.value) skipPopupFocusRestore = true
  closePopupComment()
  const frameRect = artifactViewerRef.value?.frameEl?.getBoundingClientRect()
  draftAnchor.value = frameRect
    ? { top: frameRect.top + a.frameY + 8, left: frameRect.left + a.frameX }
    : { top: 120, left: 120 }
  commentDraft.value = {
    selection: a.quote,
    text: '',
    lines: null,
    cell: null,
  }
  artifactDraft = {
    selector: a.selector,
    startOffset: a.startOffset,
    endOffset: a.endOffset,
    elementTag: a.elementTag ?? null,
    wholeElement: a.wholeElement ?? false,
  }
  commentDraftImages.value = []
  artifactDraftHighlight.value = {
    id: comments.DRAFT_COMMENT_ID,
    selector: a.selector,
    quote: a.quote,
    startOffset: a.startOffset,
    endOffset: a.endOffset,
    wholeElement: a.wholeElement,
  }
  pushArtifactHighlights()
}

// The shared cancel knows nothing about the artifact draft; without this the
// draft mark stayed in the frame and was re-sent by every later push.
function cancelArtifactComment(): void {
  artifactDraft = null
  artifactDraftHighlight.value = null
  cancelComment()
  pushArtifactHighlights()
}

function saveArtifactComment(): void {
  const draft = commentDraft.value
  if (!draft || !artifactDraft) return
  const note = draft.text.trim()
  if (!note) return
  projectsStore.addPendingComment({
    path: cleanPath(store.path),
    selection: draft.selection,
    comment: note,
    artifactSelector: artifactDraft.selector,
    artifactStartOffset: artifactDraft.startOffset,
    artifactEndOffset: artifactDraft.endOffset,
    artifactElementTag: artifactDraft.elementTag,
    artifactWholeElement: artifactDraft.wholeElement,
    images: commentDraftImages.value.length ? commentDraftImages.value : undefined,
  })
  commentDraft.value = null
  draftAnchor.value = null
  commentDraftImages.value = []
  artifactDraft = null
  artifactDraftHighlight.value = null
}

function onArtifactOpenComment(p: { id: string; frameX: number; frameY: number }): void {
  // The popup template is v-if="activePopupComment", so an id with no stored
  // comment (a draft mark, or a stale frame) would open nothing and silently
  // swallow the click. Bail before touching the anchor state.
  if (!activeFileComments.value.some(c => c.id === p.id)) return
  if (artifactDraft) cancelArtifactComment()
  const container = mainEl.value ?? modalEl.value
  const frameRect = artifactViewerRef.value?.frameEl?.getBoundingClientRect()
  if (!container || !frameRect) return
  const containerRect = container.getBoundingClientRect()
  popupAnchor.value = {
    top: frameRect.top - containerRect.top + p.frameY + 6,
    left: frameRect.left - containerRect.left + p.frameX,
  }
  activePopupId.value = p.id
  popupOpenTimestamp = Date.now()
  measurePopup()
}

const composeDraftRef = ref<InstanceType<typeof CommentComposePopover> | null>(null)

// Selecting text and typing (or pasting) opens the composer
// directly, so the "Comment" pill is a hint rather than a required click.
useTypeToComment({
  isActive: () => !!selectionAnchor.value && !commentDraft.value,
  open: (initialText: string) => {
    if (isCsv.value) openCommentForCsvCell(initialText)
    else openCommentForSelection(initialText)
  },
  addImages: (files: File[]) => addDraftImages(files),
})

const basename = computed(() => {
  const p = store.path
  const idx = p.lastIndexOf('/')
  return idx === -1 ? p : p.slice(idx + 1)
})

const isMarkdown = computed(() => /\.(md|markdown)$/i.test(store.path))
const isCsv = computed(() => isCsvPath(store.path))
// A text document reads at its measure, so the window is sized to it rather
// than leaving the prose on the left of a 1200px box. Images, PDFs, artifacts
// and CSV tables keep the wide window.
const isProse = computed(() => store.kind === 'text' && !isCsv.value)

// Scroll the modal into focus + jump to line if requested whenever the
// store finishes loading a new file.
watch(
  () => store.loadToken,
  () => {
    setTimeout(() => {
      focusViewer()
      if (store.loading || store.error) return
      scrollToTarget()
    })
  },
)

watch(
  () => store.editing,
  editing => {
    if (editing) nextTick(() => editTextareaEl.value?.focus())
  },
)

// Re-apply markdown highlights whenever the content or comment list changes.
watch(
  () => `${store.loadToken}|${store.path}|${activeFileComments.value.map(c => c.id).join(',')}|${renderedMarkdown.value.length}|${store.content.length}`,
  () => nextTick(() => {
    applyHighlights()
    scrollToTarget()
    nextTick(attachBodyScrollListener)
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
function onBeforeUnload(e: BeforeUnloadEvent): void {
  if (store.isDirty) {
    e.preventDefault()
    e.returnValue = ''
  }
}
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', onBeforeUnload)
}
onBeforeUnmount(() => {
  if (typeof document !== 'undefined') {
    document.removeEventListener('selectionchange', comments.onSelectionChange)
  }
  if (typeof window !== 'undefined') {
    window.removeEventListener('beforeunload', onBeforeUnload)
  }
  detachBodyScrollListener()
})

// Reset draft + anchor whenever the file changes or the modal closes.
watch(
  () => store.loadToken,
  () => {
    comments.selectionAnchor.value = null
    comments.commentDraft.value = null
    artifactDraft = null
    artifactDraftHighlight.value = null
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
      artifactDraft = null
      artifactDraftHighlight.value = null
      comments.lastSelectionRange = null
      activePopupId.value = null
      detachBodyScrollListener()
    } else {
      nextTick(attachBodyScrollListener)
    }
  },
  { immediate: true },
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

async function discussInChat(): Promise<void> {
  const path = store.path
  if (!path) return
  // Comments pending on this file are appended by startFileDiscussion; the
  // "help me understand" nudge only fits the case where there are none.
  const seed = projectsStore.fileComments[path]?.length
    ? `Let's discuss the file \`${path}\`.`
    : `Let's discuss the file \`${path}\`. Help me understand, review, or improve it.`
  const chat = await startFileDiscussion(projectsStore, { path, seed })
  if (chat) await store.close(true)
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

const canShareFile = computed(() => typeof navigator !== 'undefined' && typeof navigator.share === 'function')

async function shareFile(): Promise<void> {
  if (store.loading || store.error || !store.path) return
  const cleaned = store.path.replace(/:\d+$/, '')
  const name = cleaned.split('/').pop() || 'file.txt'
  try {
    let file: File
    if (store.kind === 'image' || store.kind === 'pdf' || store.kind === 'html') {
      const endpoint = store.kind === 'image' ? '/api/workspace-image'
        : store.kind === 'pdf' ? '/api/workspace-binary' : '/api/workspace-file'
      const url = `${endpoint}?path=${encodeURIComponent(cleaned)}${store.kind === 'pdf' ? '&raw=1' : ''}`
      const response = await fetch(url, { credentials: 'same-origin' })
      if (!response.ok) throw new Error('Could not load the file to share.')
      const blob = await response.blob()
      file = new File([blob], name, { type: blob.type || 'application/octet-stream' })
    } else {
      file = new File([store.content], name, { type: 'text/plain' })
    }
    if (!navigator.canShare?.({ files: [file] })) throw new Error('This browser cannot share this file type. Use Download instead.')
    await navigator.share({ files: [file], title: name })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') return
    projectsStore.pushErrorToast('Could not share file', error instanceof Error ? error.message : String(error))
  }
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
  /* The pinned tile's window (ChatLayout .chat-split-side): the sidebar's
     surface, 14px radius, a 1px border. */
  background: var(--bg2);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  width: min(1200px, 100%);
  height: min(94vh, 1100px);
  display: flex;
  flex-direction: column;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
  overflow: hidden;
  position: relative;
}
.fv-modal--doc {
  /* The document's 680px measure plus the body's 32px margins. */
  width: min(760px, 100%);
}
.fv-modal--doc .fv-md,
.fv-modal--doc .fv-meta { max-width: none; }
/* The pinned tile's title bar: filename, then the muted folder on the same
   line, giving way before the filename does. */
.fv-header {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 52px;
  padding: 4px 8px 4px 16px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  box-sizing: border-box;
}
.fv-titles {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.fv-title {
  /* The filename keeps its width and the folder takes the squeeze; only a
     name longer than the whole bar ellipses. */
  flex: 0 0 auto;
  max-width: 100%;
  font-weight: 650;
  font-size: var(--text-lg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fv-subtitle {
  flex: 0 1000 auto;
  min-width: 0;
  font-size: var(--text-sm);
  color: var(--fg3);
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
  background: var(--bg3);
  color: var(--accent);
}
.fv-text-btn {
  display: inline-flex;
  align-items: center;
  min-height: 32px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-elev);
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm);
  font-weight: 600;
  white-space: nowrap;
}
.fv-text-btn:hover { border-color: var(--border-strong); }
.fv-actions-menu .fv-menu-discuss { display: none; }
@media (max-width: 640px) {
  .fv-discuss-btn { display: none; }
  .fv-actions-menu .fv-menu-discuss { display: flex; }
}
@media (pointer: coarse) {
  .fv-text-btn { min-height: var(--touch); }
}
.fv-actions-menu {
  z-index: 100;
  min-width: 190px;
  padding: 4px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: 0 12px 32px rgb(0 0 0 / 35%);
}
.fv-actions-menu button {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 36px;
  padding: 0 12px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
}
.fv-actions-menu button:hover,
.fv-actions-menu button[data-highlighted] { background: var(--bg3); outline: none; }
.fv-actions-menu button[data-disabled] { opacity: 0.45; cursor: default; }
@media (pointer: coarse) {
  .fv-actions-menu button { min-height: var(--touch); }
}
.fv-actions .btn-icon.ok {
  color: var(--success);
}
.fv-actions .btn-icon:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.fv-continue-btn {
  margin-right: 4px;
  min-height: 32px;
  padding: 0 12px;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--on-accent);
  background: var(--accent);
  border: none;
  border-radius: 8px;
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
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  color: var(--fg);
}
/* Document properties: sits above the prose in the same measure, reads as
   metadata (muted, sentence case, normal font), and ends on a hairline. */
.fv-meta {
  width: 100%;
  max-width: 680px;
  margin: 0 0 20px;
  padding: 0 0 16px;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}
.fv-meta-line {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  color: var(--fg2);
}
.fv-meta-kind { flex: 1; min-width: 0; font-weight: 600; }
.fv-meta-status { font-weight: 600; }
.fv-meta-status--active,
.fv-meta-status--in-progress { color: var(--success); }
.fv-meta-status--draft,
.fv-meta-status--in-discussion { color: var(--warning); }
.fv-meta-status--completed,
.fv-meta-status--archived { color: var(--fg3); }
.fv-meta-date { flex: none; color: var(--fg3); font-variant-numeric: tabular-nums; }
.fv-meta-name { margin: 6px 0 0; color: var(--fg2); }
.fv-meta-summary {
  margin: 8px 0 0;
  color: var(--fg2);
  line-height: 1.55;
}
.fv-meta-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  margin: 8px 0 0;
}
.fv-meta-tag { color: var(--fg3); }
.fv-meta-fields {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 6px 16px;
  margin: 12px 0 0;
}
.fv-meta-fields dt { color: var(--fg3); }
.fv-meta-fields dd {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 12px;
  min-width: 0;
  margin: 0;
  color: var(--fg);
  overflow-wrap: anywhere;
}
.fv-meta-link { color: var(--accent); }
@media (max-width: 700px) {
  .fv-meta-fields { grid-template-columns: minmax(0, 1fr); gap: 2px 0; }
  .fv-meta-fields dd { margin-bottom: 6px; }
}


.fv-md {
  font-size: var(--text-base);
  line-height: 1.65;
  /* Readable measure for prose, as in the pinned panel. */
  max-width: 680px;
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
  border-left: 1px solid var(--border-strong);
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
  min-height: 30px;
  padding: 0 10px;
  border: none;
  border-radius: 8px;
  background: var(--fg);
  color: var(--bg);
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 8px 20px rgb(0 0 0 / 25%);
  user-select: none;
}
.fv-comment-trigger:hover { filter: none; opacity: 0.9; }
.fv-comment-trigger-icon { flex: none; }
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
  padding: 28px 32px 48px;
  position: relative;
  min-width: 0;
}
.fv-comment-backdrop {
  position: absolute;
  inset: 0;
  z-index: 20;
  background: transparent;
}
.fv-comment-pop {
  position: absolute;
  z-index: 32;
  width: 280px;
  max-width: calc(100% - 16px);
  background: var(--bg2);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  box-shadow: 0 14px 36px rgb(0 0 0 / 28%);
  padding: 10px 12px;
  box-sizing: border-box;
  font-size: var(--text-sm);
}
.fv-pop-quote {
  margin-bottom: 4px;
  color: var(--fg3);
  font-size: var(--text-xs);
}
.fv-pop-actions {
  display: flex;
  gap: 12px;
  margin-top: 8px;
}
.fv-pop-actions .fv-sidebar-card-edit,
.fv-pop-actions .fv-sidebar-card-remove {
  width: auto;
  height: auto;
  min-height: 28px;
  padding: 0;
  border-radius: 0;
  background: none;
  color: var(--accent);
  font: inherit;
  font-size: var(--text-sm);
  line-height: 1.2;
}
.fv-pop-actions .fv-sidebar-card-edit:hover,
.fv-pop-actions .fv-sidebar-card-remove:hover {
  background: none;
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 3px;
}
@media (pointer: coarse) {
  .fv-pop-actions .fv-sidebar-card-edit,
  .fv-pop-actions .fv-sidebar-card-remove { min-height: var(--touch); }
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
.fv-sidebar-card-note {
  color: var(--fg);
  word-break: break-word;
}
.fv-sidebar-card-images {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.card-image-thumb {
  height: 36px;
  width: 36px;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg);
}
/* Text highlights inside the document.
   Use :deep() because markdown highlights are created dynamically via
   DOM manipulation and won't carry Vue's scoped attribute. */
:deep(.comment-highlight) {
  /* Same violet as chat and the pinned panel: one colour for "you left a
     note here" everywhere comments appear. */
  background: color-mix(in srgb, var(--accent2) 26%, transparent);
  border-bottom: 1px solid color-mix(in srgb, var(--accent2) 70%, transparent);
  border-radius: 2px;
  cursor: pointer;
  transition: background 0.15s;
}
:deep(.comment-highlight:hover) {
  background: color-mix(in srgb, var(--accent2) 40%, transparent);
}
:deep(.comment-highlight[data-comment-id="__draft__"]),
.pre-line.comment-highlight[data-comment-id="__draft__"] {
  background: color-mix(in srgb, var(--accent) 26%, transparent);
  border-bottom-color: color-mix(in srgb, var(--accent) 70%, transparent);
}

:deep(.comment-highlight.comment-pulse),
.pre-line.comment-highlight.comment-pulse,
.pre-line.comment-pulse {
  animation: fv-comment-pulse 1.1s var(--ease, ease) 1;
}
@keyframes fv-comment-pulse {
  0%   { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent2) 0%, transparent); }
  25%  { box-shadow: 0 0 0 5px color-mix(in srgb, var(--accent2) 30%, transparent); }
  100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent2) 0%, transparent); }
}
@media (prefers-reduced-motion: reduce) {
  :deep(.comment-highlight.comment-pulse),
  .pre-line.comment-highlight.comment-pulse,
  .pre-line.comment-pulse { animation: none; }
}

/* Pre line wrappers */
.pre-line {
  display: block;
  white-space: pre;
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
  .fv-body-html { padding: 0; }
  .fv-body-html :deep(.hav-toolbar) { padding: 14px 16px 0; }
  .fv-body-html :deep(.hav-code) { padding-right: 16px; padding-left: 16px; }
  .fv-md { max-width: none; }
  .fv-comment-pop {
    left: 8px !important;
    right: 8px;
    width: auto;
    max-width: none;
  }
}

/* Coarse-pointer targets: the visible marks are small, the hit areas are not. */
@media (pointer: coarse) {
  .fv-comment-trigger {
    min-width: var(--touch);
    min-height: var(--touch);
  }

  .fv-sidebar-card-remove,
  .fv-sidebar-card-edit {
    width: var(--touch);
    height: var(--touch);
  }
}

</style>

<!-- The openable dead link: a `file:` path rendered as a button the engine
     can act on. Shared with the chat panel and the pinned-file panel. -->
<style scoped src="./markdownDeadLink.css"></style>
