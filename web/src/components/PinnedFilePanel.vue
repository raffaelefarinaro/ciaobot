<template>
  <div class="pinned-file-panel" ref="rootEl">
    <!-- No brand and no page tag: this header sits beside the main pane's in the
         split view, and a second wordmark two inches away is the duplication the
         mark was moved out of the sidebar to end. The filename is the title. -->
    <PaneHeader :brand="false" @open-sidebar="$emit('close')">
      <template #title>
        <div class="header-left">
          <div class="header-breadcrumb">
            <span class="chat-title" :title="filePath">{{ basename }}</span>
            <span v-if="docDir" class="pfp-dir" :title="docDir">{{ shortDirname(cleanPath) }}</span>
          </div>
        </div>
      </template>
      <template #actions>
        <!-- Edit is the panel's one text action; the rest stay quiet icons. -->
        <button
          v-if="(kind === 'text' || (kind === 'html' && htmlView === 'code' && sourceLoaded)) && !isEditingText"
          class="pfp-edit-btn"
          @click="startEditingText"
          title="Edit"
          aria-label="Edit"
          :disabled="loading || !!error"
        >Edit</button>
        <!-- File utilities share one menu, like the file viewer modal, instead
             of a row of look-alike icons. -->
        <DropdownMenuRoot :modal="false">
          <DropdownMenuTrigger as-child>
            <button type="button" class="btn-icon" aria-label="More file actions" title="More">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" /></svg>
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuPortal>
            <DropdownMenuContent as-child align="end" :side-offset="6" :collision-padding="8">
              <div class="pfp-actions-menu">
                <DropdownMenuItem as-child @select="refresh">
                  <button type="button">Refresh</button>
                </DropdownMenuItem>
                <DropdownMenuItem as-child @select.prevent="copyPath">
                  <button type="button">{{ copyState === 'ok' ? 'Copied' : 'Copy path' }}</button>
                </DropdownMenuItem>
                <DropdownMenuItem as-child :disabled="loading || !!error" @select="downloadFile">
                  <button type="button">Download</button>
                </DropdownMenuItem>
                <DropdownMenuItem as-child :disabled="loading || !!error || openExternalState === 'loading'" @select="openExternally">
                  <button type="button">{{ openExternalState === 'ok' ? 'Opened' : 'Open in default app' }}</button>
                </DropdownMenuItem>
                <DropdownMenuItem v-if="memoryPath" as-child @select="void openInMemoryMap()">
                  <button type="button">Open in memory map</button>
                </DropdownMenuItem>
              </div>
            </DropdownMenuContent>
          </DropdownMenuPortal>
        </DropdownMenuRoot>
        <!-- Close sits last, where a window's close lives; the tile is a window. -->
        <button class="btn-icon close-btn desktop-only" @click="$emit('close')" title="Unpin file" aria-label="Unpin file">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>
        </button>
      </template>
    </PaneHeader>
    <div class="pfp-main" ref="mainEl">
      <div class="pfp-body" :class="{ 'pfp-body-csv': isCsv }" ref="bodyEl">
        <div v-if="loading" class="pfp-skeleton" role="status" aria-live="polite" aria-label="Loading file" aria-busy="true">
          <div class="pfp-skeleton-meta" aria-hidden="true">
            <span class="pfp-skeleton-pill pfp-skeleton-pill--type"></span>
            <span class="pfp-skeleton-pill pfp-skeleton-pill--status"></span>
            <span class="pfp-skeleton-date"></span>
          </div>
          <div class="pfp-skeleton-tags" aria-hidden="true">
            <span class="pfp-skeleton-tag"></span>
            <span class="pfp-skeleton-tag pfp-skeleton-tag--wide"></span>
            <span class="pfp-skeleton-tag"></span>
          </div>
          <div class="pfp-skeleton-block" aria-hidden="true">
            <span class="pfp-skeleton-line pfp-skeleton-line--title"></span>
            <span class="pfp-skeleton-line pfp-skeleton-line--long"></span>
            <span class="pfp-skeleton-line pfp-skeleton-line--medium"></span>
            <span class="pfp-skeleton-line pfp-skeleton-line--short"></span>
          </div>
          <div class="pfp-skeleton-block" aria-hidden="true">
            <span class="pfp-skeleton-line pfp-skeleton-line--long"></span>
            <span class="pfp-skeleton-line pfp-skeleton-line--long"></span>
            <span class="pfp-skeleton-line pfp-skeleton-line--medium"></span>
            <span class="pfp-skeleton-line pfp-skeleton-line--short"></span>
          </div>
          <div class="pfp-skeleton-block" aria-hidden="true">
            <span class="pfp-skeleton-line pfp-skeleton-line--medium"></span>
            <span class="pfp-skeleton-line pfp-skeleton-line--short"></span>
          </div>
        </div>
        <div v-else-if="error" class="pfp-error">{{ error }}</div>
        <img
          v-else-if="kind === 'image'"
          class="pfp-img"
          :src="`/api/workspace-image?path=${encodeURIComponent(cleanPath)}&t=${imageTimestamp}`"
          :alt="basename"
        />
        <div v-else-if="kind === 'pdf' && pptxNeedsLibreoffice" class="pfp-libreoffice-notice hint hint--warn">
          <strong>LibreOffice is required to preview PowerPoint files.</strong>
          <span v-if="libreofficeInstallError"> {{ libreofficeInstallError }}</span>
          <button
            class="btn-primary btn-small"
            @click="installLibreofficeInChat"
          >Install in Chat</button>
        </div>
        <iframe
          v-else-if="kind === 'pdf'"
          class="pfp-pdf-iframe"
          :src="`/api/workspace-binary?path=${encodeURIComponent(cleanPath)}&t=${imageTimestamp}`"
          width="100%"
          height="100%"
          style="border: none; flex: 1; min-height: 500px; display: block; border-radius: 4px;"
        ></iframe>
        <HtmlArtifactViewer
          v-else-if="kind === 'html' && !isEditingText"
          ref="artifactViewerRef"
          :file-path="cleanPath"
          :reload-token="imageTimestamp"
          :view="htmlView"
          :source="content"
          :source-loading="sourceLoading"
          :source-error="sourceError"
          @update:view="setHtmlView"
          @compose-comment="onArtifactCompose"
          @open-comment="onArtifactOpenComment"
          @bridge-ready="onArtifactBridgeReady"
        />
        <template v-else>
          <!-- Text Editing Mode -->
          <div v-if="isEditingText" class="pfp-edit-shell">
            <CsvViewer
              v-if="isCsv"
              :content="editBuffer"
              :read-only="false"
              @change="editBuffer = $event"
              style="flex: 1; min-height: 0;"
            />
            <textarea
              v-else
              class="pfp-edit-textarea"
              v-model="editBuffer"
              spellcheck="false"
              ref="editTextAreaEl"
            ></textarea>
            <div v-if="editError" class="pfp-error">{{ editError }}</div>
            <div class="pfp-edit-actions">
              <button class="pfp-btn-sm" @click="cancelEditingText" :disabled="editSaving">Cancel</button>
              <button class="pfp-btn-sm primary" @click="saveEdits" :disabled="editSaving">
                {{ editSaving ? 'Saving…' : 'Save' }}
              </button>
            </div>
          </div>

          <template v-else>
            <!-- Document properties from YAML frontmatter: a quiet header in
                 the document's own measure, not a card of uppercase pills. -->
            <section v-if="frontmatter" class="pfp-meta" aria-label="Document properties">
              <div class="pfp-meta-line">
                <span class="pfp-meta-kind">
                  <template v-if="fmType">{{ humanizeMeta(fmType) }}</template>
                  <template v-if="fmType && fmStatus"> · </template>
                  <span v-if="fmStatus" class="pfp-meta-status" :class="`pfp-meta-status--${fmStatus}`">{{ humanizeMeta(fmStatus) }}</span>
                </span>
                <span v-if="fmUpdated" class="pfp-meta-date">Updated {{ fmUpdated }}</span>
                <span v-else-if="fmCreated" class="pfp-meta-date">Created {{ fmCreated }}</span>
              </div>
              <p v-if="showFmName" class="pfp-meta-name" :title="fmName">{{ fmName }}</p>
              <p v-if="fmProse" class="pfp-meta-summary">{{ fmProse }}</p>
              <p v-if="fmTags.length" class="pfp-meta-tags">
                <span v-for="t in fmTags" :key="t" class="pfp-meta-tag">#{{ t }}</span>
              </p>
              <dl v-if="fmListExtras.length || fmExtraEntries.length" class="pfp-meta-fields">
                <template v-for="listExtra in fmListExtras" :key="listExtra.key">
                  <dt>{{ humanizeMeta(listExtra.key) }}</dt>
                  <dd>
                    <template v-for="(item, i) in listExtra.items" :key="i">
                      <a
                        v-if="item.path"
                        class="pfp-meta-link file-link"
                        href="#"
                        :title="item.path"
                        @click.prevent="openRelated(item.path)"
                      >{{ item.label }}</a>
                      <span v-else class="pfp-meta-value">{{ item.label }}</span>
                    </template>
                  </dd>
                </template>
                <template v-for="entry in fmExtraEntries" :key="entry.key">
                  <dt>{{ humanizeMeta(entry.key) }}</dt>
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
            </section>
            <div
              v-if="isMarkdown"
              class="pfp-md"
              ref="mdEl"
              v-html="renderedMarkdown"
              @click="onMdClick"
              @mouseover="onHighlightHover"
              @mouseout="onHighlightHoverOut"
            ></div>
            <CsvViewer
              v-else-if="isCsv"
              :content="content"
              :read-only="true"
              :commentable="true"
              :cell-comments="csvCellComments"
              @cell-select="onCsvCellSelect"
              @cell-activate="onCsvCellActivate"
            />
            <pre
              v-else
              class="pfp-pre"
              ref="preEl"
              @click="onPreClick"
              @mouseover="onHighlightHover"
              @mouseout="onHighlightHoverOut"
            ><code ref="preCodeEl"><span
              v-for="(line, i) in contentLines"
              :key="i"
              :class="{ 'comment-highlight': isHighlightedLine(i + 1), 'pre-line': true }"
              :data-line="i + 1"
              :data-comment-id="commentIdForLine(i + 1)"
            >{{ line }}</span></code></pre>
          </template>
        </template>
      </div>

      <CommentComposePopover
        :anchor="editingCommentId && editAnchor ? editAnchor : null"
        v-model="editDraftText"
        :images="editingCommentImages"
        @cancel="cancelEditComment"
        @save="editingCommentId && saveEditComment(editingCommentId)"
        @upload="editingCommentId && handleEditImageUpload($event, editingCommentId)"
        @remove-image="removeEditImage"
      />

      <CommentComposePopover
        ref="composeDraftRef"
        :anchor="commentDraft && draftAnchor ? draftAnchor : null"
        v-model="composeText"
        :images="commentDraftImages"
        @cancel="artifactDraft ? cancelArtifactComment() : cancelComment()"
        @save="artifactDraft ? saveArtifactComment() : saveComment()"
        @upload="handleDraftImageUpload"
        @remove-image="removeDraftImage"
      />

      <!-- Read popover: hover to preview, click to pin; edit opens the drawer. -->
      <div
        v-if="commentPopover?.pinned && popoverComment"
        class="pfp-comment-backdrop pfp-comment-backdrop--dim"
        @click="handlePfpBackdropClick"
      ></div>
      <FocusScope
        v-if="commentPopover && popoverComment"
        as-child
        loop
        :trapped="false"
        @mount-auto-focus="onPfpMountAutoFocus"
        @unmount-auto-focus="onPfpUnmountAutoFocus"
      >
        <div
          ref="commentPopEl"
          class="pfp-comment-pop"
          role="dialog"
          aria-label="Comment"
          tabindex="-1"
          :style="{ top: commentPopover.top + 'px', left: commentPopover.left + 'px' }"
          @mousedown.stop
          @mouseenter="onPopoverEnter"
          @mouseleave="onPopoverLeave"
          @keydown="onPfpKeydown"
        >
          <div v-if="commentLineLabel(popoverComment)" class="pfp-pop-quote">{{ fileBasenameForPop }} · line {{ commentLineLabel(popoverComment) }}</div>
          <div v-if="popoverComment.images?.length" class="pfp-sidebar-card-images">
            <img v-for="img in popoverComment.images" :key="img" :src="`/api/images/${img}`" :alt="img" class="card-image-thumb" @click.stop />
          </div>
          <div class="pfp-sidebar-card-note">{{ popoverComment.comment }}</div>
          <div class="pfp-pop-actions">
            <button type="button" class="pfp-sidebar-card-edit" @click.stop="editFromPopover(popoverComment)">Edit</button>
            <button type="button" class="pfp-sidebar-card-remove" @click.stop="deleteFromPopover(popoverComment.id)">Delete</button>
          </div>
        </div>
      </FocusScope>

      <!-- Floating "Comment" button anchored near the active selection. -->
      <button
        v-if="selectionAnchor"
        class="pfp-comment-trigger"
        :style="{ top: selectionAnchor.top + 'px', left: selectionAnchor.left + 'px' }"
        @mousedown.prevent
        @click="isCsv ? openCommentForCsvCell() : openCommentForSelection()"
        type="button"
        :title="isCsv ? 'Comment on this cell' : 'Comment on this selection'"
      >
        <svg class="pfp-comment-trigger-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z" /></svg>
        Comment
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuPortal,
  DropdownMenuRoot,
  DropdownMenuTrigger,
  FocusScope,
} from 'reka-ui'
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useProjectStore } from '../stores/projects'
import { parseFrontmatter } from '../lib/markdownFrontmatter'
import { renderFileMarkdown } from '../lib/safeMarkdown'
import { buildMarkdownIndex, resolveVaultLinkTarget } from '../lib/vaultLinks'
import { openWorkspaceFileExternally } from '../lib/openWorkspaceFile'
import { isCsvPath } from '../lib/csv'
import { shortDirname } from '../lib/chatActivity'
import { useHoverPinPopover } from '../composables/useHoverPinPopover'
import { useFileComments } from '../composables/useFileComments'
import { useTypeToComment } from '../composables/useTypeToComment'
import type { ArtifactHighlight } from '../lib/artifactBridge'
import { api } from '../lib/api'
import { askConfirm } from '../lib/confirm'
import PaneHeader from './PaneHeader.vue'
import CommentComposePopover from './CommentComposePopover.vue'
import { fileViewerKindForPath, useFileViewerStore } from '../stores/fileViewer'
import type { FileViewerKind, HtmlArtifactView } from '../stores/fileViewer'
import { useMemoryMapStore } from '../stores/memoryMap'
import { router } from '../router'
const CsvViewer = defineAsyncComponent(() => import('./CsvViewer.vue'))
const HtmlArtifactViewer = defineAsyncComponent(() => import('./HtmlArtifactViewer.vue'))

const props = defineProps<{ filePath: string }>()
defineEmits<{ (e: 'close'): void }>()

const projectsStore = useProjectStore()
const fileViewer = useFileViewerStore()
const memoryMapStore = useMemoryMapStore()
const memoryPath = computed(() => /\.(md|markdown)$/i.test(cleanPath.value) ? cleanPath.value : '')

async function openInMemoryMap(): Promise<void> {
  const target = memoryPath.value
  if (!target) return
  // Navigating to /memory makes ChatLayout unmount this panel, which throws
  // away `editBuffer` and any comment draft with it. Ask first, the same way
  // FileViewerModal's version awaits its guarded `close()`.
  if (isBusyAuthoring.value && !await askConfirm(
    'You have unsaved changes in this file. Are you sure you want to leave?',
    { title: 'Discard unsaved changes?', confirmLabel: 'Discard and open map', destructive: true },
  )) return
  // Recorded only once the user has accepted: the focus request outlives this
  // panel, so requesting it before the prompt would leave the map jumping to
  // this note the next time /memory opens, after a navigation that was cancelled.
  memoryMapStore.requestFocusOnOpen(target)
  await router.push('/memory')
}

// ── Loading & rendering ──────────────────────────────────────────────
const loading = ref(false)
const error = ref('')
const content = ref('')
const kind = ref<FileViewerKind>('text')
// Artifact state, mirroring the fileViewer store's. Source is fetched only for
// Code view: `error` blanks the whole body, so a text-fetch failure must never
// take down a page that renders fine.
const htmlView = ref<HtmlArtifactView>('preview')
const sourceLoading = ref(false)
const sourceError = ref('')
const sourceLoaded = ref(false)
const refreshed = ref(false)
const copyState = ref<'idle' | 'ok'>('idle')
async function copyPath(): Promise<void> {
  try {
    await navigator.clipboard.writeText(cleanPath.value)
    copyState.value = 'ok'
    setTimeout(() => { copyState.value = 'idle' }, 1500)
  } catch {
    /* clipboard unavailable: leave the label as is */
  }
}
const openExternalState = ref<'' | 'loading' | 'ok'>('')
const isEditingText = ref(false)
const editBuffer = ref('')
const editSaving = ref(false)
const editError = ref('')
const editTextAreaEl = ref<HTMLTextAreaElement>()
const imageTimestamp = ref(Date.now())

// .pptx preview needs LibreOffice (soffice) server-side to convert to PDF.
// Checked proactively so a missing install shows guidance instead of the
// iframe silently failing to load with a browser-level error.
const pptxNeedsLibreoffice = ref(false)
const libreofficeInstallError = ref('')
const markdownPaths = ref<string[]>([])

async function loadMarkdownPaths(): Promise<void> {
  try {
    const res = await api.get<{ paths: string[] }>('/api/vault-markdown-paths')
    markdownPaths.value = res.paths ?? []
  } catch {
    markdownPaths.value = []
  }
}

async function checkLibreofficeStatus(): Promise<void> {
  try {
    const res = await api.get<{ available: boolean }>('/api/libreoffice-status')
    pptxNeedsLibreoffice.value = !res.available
  } catch {
    // Best-effort: if the check itself fails, fall through to the iframe
    // and let it show whatever error the browser gives.
    pptxNeedsLibreoffice.value = false
  }
}

async function installLibreofficeInChat(): Promise<void> {
  try {
    await projectsStore.fixError({
      errorText:
        'LibreOffice (soffice) is not installed, so PowerPoint files cannot be previewed in the Ciaobot pinned file panel.',
      context:
        'Previewing a .pptx from the Ciaobot pinned file panel. Install LibreOffice (e.g. `brew install --cask libreoffice` on macOS) so soffice can render slides; Ciaobot never runs package installs on its own.',
      title: 'Install LibreOffice',
    })
  } catch (e) {
    libreofficeInstallError.value = e instanceof Error ? e.message : String(e)
  }
}

const rootEl = ref<HTMLElement>()
const mainEl = ref<HTMLElement>()
const bodyEl = ref<HTMLElement>()
const mdEl = ref<HTMLElement>()
const preEl = ref<HTMLElement>()
const preCodeEl = ref<HTMLElement>()

const cleanPath = computed(() => props.filePath.replace(/:\d+$/, ''))
const basename = computed(() => {
  const p = cleanPath.value
  const idx = p.lastIndexOf('/')
  return idx === -1 ? p : p.slice(idx + 1)
})
const fileBasenameForPop = computed(() => basename.value)
// Frontmatter words for display: "in-discussion" -> "In discussion".
function humanizeMeta(value: string): string {
  const text = String(value).replace(/[_-]+/g, ' ').trim()
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text
}
const isMarkdown = computed(() => /\.(md|markdown)$/i.test(cleanPath.value))
const isCsv = computed(() => isCsvPath(cleanPath.value))

const docDir = computed(() => {
  const idx = cleanPath.value.lastIndexOf('/')
  return idx === -1 ? '' : cleanPath.value.slice(0, idx + 1)
})

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

const _ABSOLUTE_SRC_RE = /^(?:[a-z][a-z0-9+.-]*:|\/\/|\/)/i

// Split frontmatter off so the body renders cleanly and the metadata card
// at the top can show key fields as pills/chips.
const splitContent = computed(() => parseFrontmatter(content.value))
const frontmatter = computed(() => splitContent.value.frontmatter)
const bodyOnly = computed(() => splitContent.value.body)

const renderedMarkdown = computed(() => {
  const dir = docDir.value
  return renderFileMarkdown(bodyOnly.value, {
    filePath: cleanPath.value,
    markdownPaths: markdownPaths.value,
    resolveImageSrc: (href) => {
      if (href && !_ABSOLUTE_SRC_RE.test(href)) {
        const resolved = joinRelative(dir, href)
        return `/api/workspace-image?path=${encodeURIComponent(resolved)}`
      }
      return href
    },
  })
})

// ── Metadata card (parsed frontmatter) ───────────────────────────────
// Surface the most useful fields as pills; prose fields (description, etc.)
// read as a summary; list fields (aliases, related) as compact chips.
// `title` is the canonical human label in the vault schema; `name` is the
// retired synonym still present on older pages. Prefer title, fall back.
const fmName = computed(() => fmString('title') || fmString('name'))
// The title usually repeats the file name or the body's first heading; show it
// only when it says something neither of those does.
const showFmName = computed(() => {
  const name = fmName.value.trim()
  if (!name || name === basename.value.replace(/\.md$/, '')) return false
  const heading = /^#\s+(.+?)\s*#*\s*$/m.exec(splitContent.value.body || '')
  return !heading || heading[1].trim() !== name
})
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

// `related`/`links` items are bare vault refs (`People/Mo`) to other notes —
// resolve to file paths so the pills are clickable (same as body links). A
// `[[...]]` wrapper is still tolerated for notes that predate the
// markdown-link swap. `aliases` name this note, not links, so stay plain.
const _LINK_LIST_KEYS = new Set(['related', 'links'])
const _linkIndex = computed(() => buildMarkdownIndex(markdownPaths.value || []))
const _linkPathSet = computed(() => new Set(markdownPaths.value || []))

function resolveListItem(raw: string): { label: string; path: string | null } {
  const inner = raw.replace(/^\[\[(.+)\]\]$/, '$1').trim()
  const [ref, alias] = inner.split('|')
  // A related note reads by its name, not its vault path; the full path stays
  // in the link's title.
  const label = (alias ?? (ref.split('/').pop() || ref).replace(/\.md$/i, '')).trim()
  const path = ref.trim()
    ? resolveVaultLinkTarget(ref.trim(), cleanPath.value, _linkIndex.value, _linkPathSet.value)
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
      : [{ label: items.join(', '), path: null }]
    out.push({ key, items: resolved })
  }
  return out
})

function openRelated(path: string): void {
  if (/\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/i.test(path)) {
    void fileViewer.openImage(path)
  } else {
    void fileViewer.open(path, null)
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
// Render a bare http(s) frontmatter value (e.g. `url:`) as a clickable link.
// Only http/https so the href can't be a javascript:/data: scheme.
function isUrl(value: string): boolean {
  return /^https?:\/\/\S+$/.test(value.trim())
}
function fmList(key: string): string[] {
  const v = frontmatter.value?.[key]
  if (v == null) return []
  return Array.isArray(v) ? v : [String(v)]
}


const contentLines = computed(() => {
  const text = bodyOnly.value
  if (text.endsWith('\n')) {
    return text.slice(0, -1).split('\n')
  }
  return text.split('\n')
})

async function load(): Promise<void> {
  if (!props.filePath) return
  isEditingText.value = false
  editBuffer.value = ''
  editError.value = ''
  htmlView.value = 'preview'
  sourceLoading.value = false
  sourceError.value = ''
  sourceLoaded.value = false
  // Images are the panel's own case (the store has openImage for them);
  // everything else comes from the shared classifier so this stays one
  // implementation of "what kind of file is this" rather than a second copy.
  const isImg = /\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i.test(props.filePath)
  if (isImg) {
    kind.value = 'image'
    loading.value = false
    error.value = ''
    content.value = ''
    imageTimestamp.value = Date.now()
    return
  }
  kind.value = fileViewerKindForPath(cleanPath.value)
  if (kind.value === 'pdf') {
    loading.value = false
    error.value = ''
    content.value = ''
    imageTimestamp.value = Date.now()
    pptxNeedsLibreoffice.value = false
    libreofficeInstallError.value = ''
    if (/\.pptx$/i.test(cleanPath.value)) void checkLibreofficeStatus()
    return
  }
  if (kind.value === 'html') {
    // Bumping the token here is what makes the stream-end auto-reload below
    // refresh the frame after the model revises the artifact. Without it the
    // panel would keep showing the pre-edit page until a manual refresh.
    loading.value = false
    error.value = ''
    content.value = ''
    imageTimestamp.value = Date.now()
    return
  }
  loading.value = true
  error.value = ''
  content.value = ''
  try {
    const url = `/api/workspace-file?path=${encodeURIComponent(cleanPath.value)}`
    const pathsPromise = isMarkdown.value ? loadMarkdownPaths() : Promise.resolve()
    const [resp] = await Promise.all([
      fetch(url, { credentials: 'same-origin' }),
      pathsPromise,
    ])
    if (!resp.ok) {
      if (resp.status === 404) error.value = 'File not found.'
      else if (resp.status === 403) error.value = 'Forbidden: path is outside the workspace.'
      else if (resp.status === 413) error.value = 'File is too large to preview (>2 MB).'
      else if (resp.status === 415) error.value = 'Unsupported file type.'
      else error.value = `Failed to load file (HTTP ${resp.status}).`
      return
    }
    content.value = await resp.text()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function loadSource(force = false): Promise<void> {
  if (!cleanPath.value || (sourceLoaded.value && !force)) return
  sourceLoading.value = true
  sourceError.value = ''
  try {
    const resp = await fetch(
      `/api/workspace-file?path=${encodeURIComponent(cleanPath.value)}`,
      { credentials: 'same-origin' },
    )
    if (!resp.ok) {
      sourceError.value = resp.status === 413
        ? 'Source is too large to show (>2 MB).'
        : `Failed to load source (HTTP ${resp.status}).`
      return
    }
    content.value = await resp.text()
    sourceLoaded.value = true
  } catch (e) {
    sourceError.value = e instanceof Error ? e.message : String(e)
  } finally {
    sourceLoading.value = false
  }
}

async function setHtmlView(view: HtmlArtifactView): Promise<void> {
  htmlView.value = view
  if (view === 'code') await loadSource()
}

function startEditingText(): void {
  editBuffer.value = content.value
  isEditingText.value = true
  editError.value = ''
  nextTick(() => {
    editTextAreaEl.value?.focus()
  })
}

function cancelEditingText(): void {
  isEditingText.value = false
  editBuffer.value = ''
  editError.value = ''
}

async function saveEdits(): Promise<void> {
  if (!isEditingText.value) return
  editSaving.value = true
  editError.value = ''
  try {
    const body = {
      chat_id: projectsStore.activeChatId || '',
      path: cleanPath.value,
      content: editBuffer.value,
    }
    const resp = await fetch('/api/workspace-file', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!resp.ok) {
      editError.value = `Save failed (HTTP ${resp.status}).`
      return
    }
    content.value = editBuffer.value
    isEditingText.value = false
    editBuffer.value = ''
    // Artifacts render from a URL, so the frame needs a new token to pick up
    // the save; adopting the buffer only updates Code view.
    if (kind.value === 'html') imageTimestamp.value = Date.now()
  } catch (e) {
    editError.value = e instanceof Error ? e.message : String(e)
  } finally {
    editSaving.value = false
  }
}

function refresh(): void {
  refreshed.value = true
  load().then(() => {
    setTimeout(() => { refreshed.value = false }, 800)
  })
}

function downloadFile(): void {
  if (loading.value || error.value) return
  const cleaned = cleanPath.value.replace(/:\d+$/, '')
  const name = (() => {
    const idx = cleaned.lastIndexOf('/')
    return idx === -1 ? cleaned : cleaned.slice(idx + 1)
  })()
  const a = document.createElement('a')
  a.download = name || 'download'
  if (kind.value === 'image') {
    a.href = `/api/workspace-image?path=${encodeURIComponent(cleaned)}`
  } else if (kind.value === 'pdf') {
    a.href = `/api/workspace-binary?path=${encodeURIComponent(cleaned)}&raw=1`
  } else if (kind.value === 'html') {
    // Straight from the endpoint, not from `content`: an artifact in Preview
    // view has never fetched its source, so the in-memory blob would be empty.
    a.href = `/api/workspace-file?path=${encodeURIComponent(cleaned)}`
  } else {
    const blob = new Blob([content.value], { type: 'text/plain;charset=utf-8' })
    a.href = URL.createObjectURL(blob)
    setTimeout(() => URL.revokeObjectURL(a.href), 5000)
  }
  document.body.appendChild(a)
  a.click()
  a.remove()
}

async function openExternally(): Promise<void> {
  if (loading.value || error.value || openExternalState.value === 'loading') return
  openExternalState.value = 'loading'
  const result = await openWorkspaceFileExternally(cleanPath.value)
  if (result.ok) {
    openExternalState.value = 'ok'
    setTimeout(() => { openExternalState.value = '' }, 1200)
    return
  }
  openExternalState.value = ''
  projectsStore.pushErrorToast('Could not open file', result.error)
}

// Set when an auto-reload was skipped because the user had unsaved work open;
// applied as soon as the panel goes idle (see the isBusyAuthoring watcher).
const deferredReload = ref(false)

watch(() => props.filePath, () => load(), { immediate: true })

watch(
  () => [props.filePath, projectsStore.activeChatId, projectsStore.activeChatId ? projectsStore.streaming[projectsStore.activeChatId] : false] as const,
  ([filePath, chatId, isStreaming], oldValues) => {
    const wasStreaming = oldValues ? oldValues[2] : false
    // Reload the file so the panel shows the model's latest version once its
    // turn ends. Never while the user is mid-edit or mid-comment: `load()`
    // resets the edit session, and wiping half-typed work the moment the model
    // stops is the same class of loss the fileViewer store guards with
    // canReplaceOpenFile. The reload is deferred until they save or cancel.
    if (filePath && chatId && wasStreaming && !isStreaming) {
      if (isBusyAuthoring.value) deferredReload.value = true
      else load()
    }
  }
)

// ── Comments ─────────────────────────────────────────────────────────
// Mirrors FileViewerModal's comment system on a smaller scale: shows
// existing comments as highlights + sidebar, and lets the user select
// text and add a new comment that piggybacks on the next chat message.
const commentsForFile = computed(() =>
  projectsStore.fileCommentsFor(cleanPath.value)
)

// On-demand comment surfaces (replace the old fixed-width side column):
//  - showCommentList: the drawer overlay listing every comment (header pill).
//  - commentPopover: hover-preview / click-to-pin read popover on a highlight.
const showCommentList = ref(false)
const commentPopEl = ref<HTMLElement | null>(null)

// A markdown highlight span, or a whole line in the plain-text viewer.
function highlightElFromEvent(e: MouseEvent): HTMLElement | null {
  const target = e.target as HTMLElement | null
  if (!target) return null
  const hl = target.closest('.comment-highlight') as HTMLElement | null
  if (hl?.dataset.commentId) return hl
  const line = target.closest('.pre-line') as HTMLElement | null
  if (line?.dataset.commentId) return line
  return null
}

const {
  popover: commentPopover,
  comment: popoverComment,
  show: showCommentPopover,
  close: closeCommentPopover,
  clearPendingClose: clearHoverClose,
  onTargetOver: onHighlightHover,
  onTargetOut: onHighlightHoverOut,
  onPopoverEnter,
  onPopoverLeave,
} = useHoverPinPopover({
  resolveTarget: highlightElFromEvent,
  anchorFor: el => comments.anchorFromElement(el),
  findComment: id => commentsForFile.value.find(c => c.id === id) ?? null,
  hasTargets: () => commentsForFile.value.length > 0,
  onPin: () => comments.cancelEditComment(),
})

let pfpPinTimestamp = 0
let skipPfpFocusRestore = false
watch(() => commentPopover.value?.pinned, (pinned) => {
  if (pinned) {
    pfpPinTimestamp = Date.now()
    nextTick(() => commentPopEl.value?.querySelector<HTMLElement>('button')?.focus())
    setTimeout(() => commentPopEl.value?.querySelector<HTMLElement>('button')?.focus(), 0)
  }
})

function onPfpMountAutoFocus(event: Event): void {
  // Hover previews are informational; pinning explicitly moves focus to Edit.
  event.preventDefault()
  if (commentPopover.value?.pinned) {
    nextTick(() => commentPopEl.value?.querySelector<HTMLElement>('button')?.focus())
    setTimeout(() => commentPopEl.value?.querySelector<HTMLElement>('button')?.focus(), 0)
  }
}

function onPfpUnmountAutoFocus(event: Event): void {
  if (!skipPfpFocusRestore) return
  skipPfpFocusRestore = false
  event.preventDefault()
}

function onPfpKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return
  event.preventDefault()
  event.stopPropagation()
  closeCommentPopover()
}

function handlePfpBackdropClick(): void {
  if (Date.now() - pfpPinTimestamp < 150) return
  closeCommentPopover()
}

function deleteFromPopover(id: string): void {
  skipPfpFocusRestore = true
  closeCommentPopover()
  comments.deleteFileComment(id)
  pushArtifactHighlights()
}

function editFromPopover(c: { id: string; comment: string; images?: string[] }): void {
  const popAnchor = commentPopover.value
    ? comments.toViewportAnchor({ top: commentPopover.value.top, left: commentPopover.value.left })
    : null
  skipPfpFocusRestore = true
  closeCommentPopover()
  comments.startEditComment(c, popAnchor)
}

// Highlight rendering inside the rendered markdown body. We strip-and-
// reapply on every comment list change so deleting a comment removes the
// highlight cleanly. clearHighlights / highlightInMarkdown live in
// useFileComments (shared with FileViewerModal); this surface keeps its own
// kind guards (image/PDF/artifact) and feeds the shared helpers.
function applyHighlights(): void {
  if (kind.value === 'image' || kind.value === 'pdf' || kind.value === 'html') return
  if (isMarkdown.value) {
    const root = mdEl.value
    if (!root) return
    comments.clearHighlights(root)
    for (const c of commentsForFile.value) {
      comments.highlightInMarkdown(root, c.selection, c.id)
    }
    const draft = comments.commentDraft.value
    if (draft?.selection) {
      comments.highlightInMarkdown(root, draft.selection, comments.DRAFT_COMMENT_ID)
    }
  }
}

const isProgrammaticScrolling = ref(false)
let programmaticScrollTimer: ReturnType<typeof setTimeout> | null = null

function scrollToHighlight(id: string): void {
  if (!bodyEl.value) return
  const matches: HTMLElement[] = []
  const highlights = bodyEl.value.querySelectorAll('[data-comment-id]')
  for (const el of Array.from(highlights)) {
    if ((el as HTMLElement).dataset.commentId === id) matches.push(el as HTMLElement)
  }
  if (!matches.length) return
  isProgrammaticScrolling.value = true
  bodyEl.value.scrollTo({ top: Math.max(matches[0].offsetTop - 20, 0), behavior: 'smooth' })
  // Pulse the matching highlights for ~1s so it's obvious which one we
  // scrolled to. We pulse every fragment of the same highlight at once,
  // so a multi-cell or multi-line selection still reads as one item.
  for (const el of matches) {
    el.classList.remove('comment-pulse')
    // Force reflow so re-adding the class restarts the animation when the
    // user clicks the same comment twice in a row.
    void el.offsetWidth
    el.classList.add('comment-pulse')
  }
  if (programmaticScrollTimer) clearTimeout(programmaticScrollTimer)
  programmaticScrollTimer = setTimeout(() => {
    isProgrammaticScrolling.value = false
    programmaticScrollTimer = null
    showCommentPopover(id, matches[0], true)
  }, 350)
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
    if (/\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/i.test(linkedPath)) {
      void fileViewer.openImage(linkedPath)
    } else {
      void fileViewer.open(linkedPath, Number.isFinite(linkedLine as number) ? linkedLine : null)
    }
    return
  }

  const highlight = target.closest('.comment-highlight') as HTMLElement | null
  if (!highlight) return
  const id = highlight.dataset.commentId
  if (id) showCommentPopover(id, highlight, true)
}

function onPreClick(e: MouseEvent): void {
  const target = e.target as HTMLElement | null
  if (!target) return
  const line = target.closest('.pre-line') as HTMLElement | null
  if (!line) return
  const id = line.dataset.commentId
  if (id) showCommentPopover(id, line, true)
}

// Reapply highlights on content / comment changes.
watch(
  () => `${cleanPath.value}|${content.value.length}|${commentsForFile.value.map(c => c.id).join(',')}`,
  () => nextTick(() => applyHighlights()),
  { flush: 'post' },
)

// ── Selection → comment composer ─────────────────────────────────────
// Selection/draft state and the comment subsystem live in useFileComments
// (shared with FileViewerModal). Only the surface-specific inputs are wired
// here: the path/content sources, the anchor coordinate root (mainEl), and
// closing this panel's hover-pin read popover when a compose opens.
// Commenting stays available while the model works, matching FileViewerModal:
// a comment is staged locally and rides along on the next message the user
// sends (queued), so there is nothing to wait for.
// Markdown/CSV comment through text/line/cell anchors; HTML artifacts comment
// through the frame bridge (see the artifact section below).
const isCommentable = computed(() =>
  !loading.value && !error.value
  && kind.value !== 'image' && kind.value !== 'pdf' && kind.value !== 'html'
)

const comments = useFileComments({
  path: () => cleanPath.value,
  content: () => content.value,
  commentsForFile,
  isCommentable,
  containerEl: mainEl,
  bodyEl,
  mdEl,
  preEl,
  preCodeEl,
  closeReadPopover: closeCommentPopover,
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
// The bridge script inside the sandboxed frame posts selection/element
// anchors over postMessage; this converts them into the same pending file
// comments the markdown path produces, so chips, send-along, and the
// <user-comment-reference> formatting all work unchanged. Anchoring is a CSS
// selector plus text offsets inside the rendered page; the quote (verbatim
// text) is the durable part the model reads — the selector only has to
// re-find the highlight until the next revision reloads the frame.
const artifactViewerRef = ref<InstanceType<typeof HtmlArtifactViewer> | null>(null)

// Draft highlight pushed into the frame while the compose popover is open, so
// the annotation stays visible after the selection collapses.
const artifactDraftHighlight = ref<ArtifactHighlight | null>(null)

// Anchor captured with the compose flow; null for non-artifact drafts.
let artifactDraft: {
  selector: string
  startOffset: number
  endOffset: number
  elementTag: string | null
  wholeElement: boolean
} | null = null

const artifactHighlights = computed<ArtifactHighlight[]>(() =>
  commentsForFile.value
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

// Push the durable highlights (plus the open draft) into the frame. Called on
// comment-list changes and on the bridge's ready handshake; a no-op outside
// Preview.
function pushArtifactHighlights(): void {
  if (kind.value !== 'html' || htmlView.value !== 'preview') return
  const list = artifactDraftHighlight.value
    ? [...artifactHighlights.value, artifactDraftHighlight.value]
    : artifactHighlights.value
  nextTick(() => artifactViewerRef.value?.sendHighlights(list))
}

// The comment list changing is the only push this watcher can own. It must NOT
// try to cover frame loads: on mount and on an imageTimestamp bump the message
// would go out before the new document's bridge exists and would be dropped.
// Loads are covered by @bridge-ready below.
watch(
  () => commentsForFile.value.map(c => c.id).join(','),
  () => pushArtifactHighlights(),
)

// Sole load-time push: the bridge posts `ready` once its DOM is parsed, which
// covers pinning an artifact that already has comments, toggling Code→Preview,
// and the post-revision reload.
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
  closeCommentPopover()
  // Anchor the compose popover in viewport coordinates: CommentComposePopover
  // teleports to body as position: fixed and clamps itself. The bridge
  // measured frame-relative coordinates, so add the iframe's viewport offset.
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

// Save path for an artifact comment: same store call as the markdown path but
// with the anchor fields instead of line numbers. Kept separate from the
// shared saveComment rather than wrapped, so the two stay readable.
function saveArtifactComment(): void {
  const draft = commentDraft.value
  if (!draft || !artifactDraft) return
  const note = draft.text.trim()
  if (!note) return
  projectsStore.addPendingComment({
    path: cleanPath.value,
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

// The shared cancel knows nothing about the artifact draft, so cancelling
// through it alone left the draft <mark> in the frame forever — re-sent by
// every later push, and inert on click because DRAFT_COMMENT_ID matches no
// stored comment.
function cancelArtifactComment(): void {
  artifactDraft = null
  artifactDraftHighlight.value = null
  cancelComment()
  pushArtifactHighlights()
}

function onArtifactOpenComment(p: { id: string; frameX: number; frameY: number }): void {
  const match = commentsForFile.value.find(c => c.id === p.id)
  if (!match) return
  // Clicking a stored mark abandons an open draft, so it goes through the
  // artifact-aware cancel — otherwise the draft mark survives in the frame.
  if (artifactDraft) cancelArtifactComment()
  else comments.cancelComment()
  // Anchor the read popover at the clicked mark: build a point-like element
  // whose rect sits where the bridge measured the mark, in viewport coords —
  // comments.anchorFromElement subtracts the container rect itself.
  const frameRect = artifactViewerRef.value?.frameEl?.getBoundingClientRect()
  if (!frameRect) return
  const x = frameRect.left + p.frameX
  const y = frameRect.top + p.frameY
  showCommentPopover(
    p.id,
    {
      getBoundingClientRect: () => ({
        x, y, top: y, left: x, bottom: y, right: x, width: 0, height: 0,
        toJSON: () => ({}),
      }),
    } as unknown as HTMLElement,
    true,
  )
}

const composeDraftRef = ref<InstanceType<typeof CommentComposePopover> | null>(null)

// Selecting text and typing (or pasting, or hitting Cmd+D) opens the composer
// directly, so the "Comment" pill is a hint rather than a required click.
useTypeToComment({
  isActive: () => !!selectionAnchor.value && !commentDraft.value,
  open: (initialText: string) => {
    if (isCsv.value) openCommentForCsvCell(initialText)
    else openCommentForSelection(initialText)
  },
  dictate: () => nextTick(() => composeDraftRef.value?.toggleDictation()),
  addImages: (files: File[]) => addDraftImages(files),
})

// Anything the user has half-written in the panel: a text edit, a new comment,
// or an edit of an existing one. Auto-reloads hold off while it is true, so a
// model turn finishing mid-sentence never throws the work away — and then run
// once the panel is idle again, so the view never stays silently stale.
const isBusyAuthoring = computed(
  () => isEditingText.value || commentDraft.value !== null || editingCommentId.value !== null,
)
watch(isBusyAuthoring, (busy) => {
  if (busy || !deferredReload.value) return
  deferredReload.value = false
  refresh()
})

// Re-anchor the floating comment trigger on scroll, and close the hover-pin
// read popover so it never floats detached from its highlight. Kept here (not
// in useFileComments): the panel also suppresses re-anchoring while a
// programmatic scroll-to-highlight is in flight.
function onScrollReanchor(): void {
  if (isProgrammaticScrolling.value) return
  // A read popover is pinned to a highlight's screen position, so scrolling
  // the document underneath it would leave it floating detached. Close it.
  if (commentPopover.value) closeCommentPopover()
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

function onJumpPinnedCommentEvent(e: Event): void {
  const customEv = e as CustomEvent<{ id?: string; line?: number | null }>
  const { id, line } = customEv.detail || {}
  if (id) {
    scrollToHighlight(id)
  } else if (line != null) {
    const match = commentsForFile.value.find(c => c.lineStart === line)
    if (match) scrollToHighlight(match.id)
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('selectionchange', comments.onSelectionChange)
}
onMounted(() => {
  bodyEl.value?.addEventListener('scroll', onScrollReanchor, { passive: true })
  if (typeof window !== 'undefined') {
    window.addEventListener('ciao:jump-pinned-comment', onJumpPinnedCommentEvent)
  }
})
onBeforeUnmount(() => {
  clearHoverClose()
  if (typeof document !== 'undefined') {
    document.removeEventListener('selectionchange', comments.onSelectionChange)
  }
  if (typeof window !== 'undefined') {
    window.removeEventListener('ciao:jump-pinned-comment', onJumpPinnedCommentEvent)
  }
  bodyEl.value?.removeEventListener('scroll', onScrollReanchor)
})

function pathsMatch(pathA: string, pathB: string): boolean {
  if (!pathA || !pathB) return false

  const normalize = (p: string) => {
    let clean = p.replace(/\\/g, '/').replace(/^file:\/\/\/?/i, '')
    if (clean.startsWith('./')) {
      clean = clean.slice(2)
    }
    const lastDot = clean.lastIndexOf('.')
    const lastSlash = clean.lastIndexOf('/')
    if (lastDot > lastSlash) {
      clean = clean.slice(0, lastDot)
    }
    return clean.toLowerCase()
  }

  const cleanA = normalize(pathA)
  const cleanB = normalize(pathB)

  return cleanA === cleanB || cleanA.endsWith('/' + cleanB) || cleanB.endsWith('/' + cleanA)
}

const isModifiedInLastTurn = computed(() => {
  const msgs = projectsStore.activeMessages
  if (!msgs || msgs.length === 0) return false

  let lastUserIdx = -1
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'user') {
      lastUserIdx = i
      break
    }
  }

  const targetPath = cleanPath.value
  for (let i = lastUserIdx + 1; i < msgs.length; i++) {
    const msg = msgs[i]
    if (msg.tool_name === '_filecard' && msg.file_path) {
      if (pathsMatch(msg.file_path, targetPath)) {
        return true
      }
    }
  }
  return false
})

watch(
  [() => projectsStore.isStreaming, () => isModifiedInLastTurn.value],
  ([isStreaming, isModified], [wasStreaming, wasModified]) => {
    if (isStreaming) return
    const justStoppedStreaming = wasStreaming && !isStreaming
    const justFlippedModified = !wasModified && isModified
    // Same guard as the stream-end reload above: the modified-file refresh must
    // not clobber an edit — or a comment the user is mid-way through writing
    // while the model works — so it waits for them to finish.
    if (isModified && (justStoppedStreaming || justFlippedModified)) {
      if (isBusyAuthoring.value) deferredReload.value = true
      else refresh()
    }
  }
)

// Reset draft when the file changes.
watch(() => props.filePath, () => {
  comments.selectionAnchor.value = null
  comments.draftAnchor.value = null
  comments.commentDraft.value = null
  artifactDraft = null
  artifactDraftHighlight.value = null
  closeCommentPopover()
  showCommentList.value = false
  comments.lastSelectionText = ''
  comments.lastSelectionLines = null
  comments.lastSelectionRange = null
  isEditingText.value = false
  editBuffer.value = ''
  editError.value = ''
  deferredReload.value = false
})

// ChatLayout's global Cmd/Ctrl+Enter send shortcut checks this before acting:
// while a text edit or comment draft is open here, focus can sit on this
// panel's own Save/Add-comment button rather than a textarea, so the global
// handler would otherwise send the unrelated chat composer draft instead.
defineExpose({ isBusyAuthoring })
</script>

<style scoped>
.pinned-file-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  /* The tile's own tone (ChatLayout .chat-split-side): the sidebar's surface. */
  background: var(--bg2);
  position: relative;
}

/* A window title bar, not a page header: shorter than the chat's, on the tile
   surface, with its own tight inset instead of the page grid's gutter. 52px =
   the chat header's 61px less the tile's 8px inset and 1px border, so the two
   header rules meet on one line across the gap. */
.pinned-file-panel > :deep(.pane-header) {
  height: 52px;
  padding: 0 4px 0 12px;
  background: transparent;
}

/* Unified Header styles matching ChatPanel */
.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  text-align: left;
}

.close-btn {
  color: var(--fg2);
}
.close-btn:hover { color: var(--fg); }

/* The file's folder, muted after its name: where it lives, not a second title. */
.pfp-dir {
  /* Gives way long before the filename does. */
  flex: 0 1000 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--fg3);
  font-size: var(--text-sm);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pfp-edit-btn {
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
}
.pfp-edit-btn:hover:not(:disabled) { border-color: var(--border-strong); }
.pfp-edit-btn:disabled { cursor: default; opacity: 0.55; }
@media (pointer: coarse) {
  .pfp-edit-btn { min-height: var(--touch); }
}

.header-breadcrumb {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
  position: relative;
}

.chat-title {
  /* Token, not 16px: this title sits beside the chat pane's in the split view,
     and a literal here stopped answering the Appearance font-scale setting —
     so raising the scale grew the chat title and left this one behind. */
  font-size: var(--text-lg);
  font-weight: 650;
  color: var(--fg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  /* The filename keeps its width and the folder takes the squeeze; only a name
     longer than the whole row ellipses. */
  flex: 0 0 auto;
  max-width: 100%;
}

.desktop-only { display: inline-flex; }
@media (max-width: 768px) { .desktop-only { display: none; } }

.btn-icon.ok {
  color: var(--success);
}

/* ── Body + sidebar split ──────────────────────────────────────────── */
.pfp-main {
  flex: 1;
  display: flex;
  min-height: 0;
  position: relative;
}
.pfp-body {
  flex: 1;
  overflow: auto;
  /* A document, not a code pane: generous margins and, for prose, a
     readable measure (see .pfp-md). */
  padding: 28px 32px 48px;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
@media (max-width: 700px) {
  .pfp-body { padding: 20px 16px 40px; }
}
.pfp-body-csv {
  overflow: hidden !important;
}
.pfp-error {
  padding: 24px;
  text-align: center;
  color: var(--error, #f87171);
}

/* ── Skeleton loading (mirrors file + metadata card shape) ─────────────── */
.pfp-skeleton {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 8px 0 12px;
}
.pfp-skeleton-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg2, rgba(255, 255, 255, 0.03));
}
.pfp-skeleton-pill,
.pfp-skeleton-date,
.pfp-skeleton-tag,
.pfp-skeleton-line {
  display: block;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--bg2) 0%, var(--bg3) 50%, var(--bg2) 100%);
  background-size: 200% 100%;
  animation: pfp-skeleton-sweep 1.4s ease-in-out infinite;
}
.pfp-skeleton-pill { height: 18px; }
.pfp-skeleton-pill--type { width: 62px; }
.pfp-skeleton-pill--status { width: 54px; }
.pfp-skeleton-date { width: 72px; height: 11px; margin-left: auto; }
.pfp-skeleton-tags {
  display: flex;
  gap: 6px;
  margin-top: -6px;
}
.pfp-skeleton-tag { width: 44px; height: 18px; border-radius: 4px; }
.pfp-skeleton-tag--wide { width: 68px; }
.pfp-skeleton-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.pfp-skeleton-line { height: 10px; }
.pfp-skeleton-line--title { width: 46%; height: 14px; }
.pfp-skeleton-line--long { width: 92%; }
.pfp-skeleton-line--medium { width: 68%; }
.pfp-skeleton-line--short { width: 42%; }
@keyframes pfp-skeleton-sweep {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}
@media (prefers-reduced-motion: reduce) {
  .pfp-skeleton-pill,
  .pfp-skeleton-date,
  .pfp-skeleton-tag,
  .pfp-skeleton-line {
    animation: none;
  }
}
.pfp-libreoffice-notice {
  margin: 24px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
}
.pfp-img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  display: block;
}
.pfp-pre {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  color: var(--fg);
}
.pfp-pre code {
  display: block;
}
.pre-line {
  display: block;
  padding: 0 4px;
}
.pre-line.comment-highlight {
  background: color-mix(in srgb, var(--accent2) 22%, transparent);
  cursor: pointer;
}

/* ── Metadata card (parsed frontmatter) ─────────────────────────── */
/* Document properties: sits above the prose in the same measure, reads as
   metadata (muted, sentence case, normal font), and ends on a hairline. */
.pfp-actions-menu {
  z-index: 100;
  min-width: 190px;
  padding: 4px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: 0 12px 32px rgb(0 0 0 / 35%);
}
.pfp-actions-menu button {
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
.pfp-actions-menu button:hover,
.pfp-actions-menu button[data-highlighted] { background: var(--bg3); }
.pfp-actions-menu button[data-disabled] { color: var(--fg3); cursor: default; }
.pfp-actions-menu button:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
@media (pointer: coarse) {
  .pfp-actions-menu button { min-height: var(--touch); }
}

.pfp-meta {
  width: 100%;
  max-width: 680px;
  margin: 0 0 20px;
  padding: 0 0 16px;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}
.pfp-meta-line {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  color: var(--fg2);
}
.pfp-meta-kind { flex: 1; min-width: 0; font-weight: 600; }
.pfp-meta-status { font-weight: 600; }
.pfp-meta-status--active,
.pfp-meta-status--in-progress { color: var(--success); }
.pfp-meta-status--draft,
.pfp-meta-status--in-discussion { color: var(--warning); }
.pfp-meta-status--completed,
.pfp-meta-status--archived { color: var(--fg3); }
.pfp-meta-date { flex: none; color: var(--fg3); font-variant-numeric: tabular-nums; }
.pfp-meta-name { margin: 6px 0 0; color: var(--fg2); }
.pfp-meta-summary {
  margin: 8px 0 0;
  color: var(--fg2);
  line-height: 1.55;
}
.pfp-meta-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  margin: 8px 0 0;
}
.pfp-meta-tag { color: var(--fg3); }
.pfp-meta-fields {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 6px 16px;
  margin: 12px 0 0;
}
.pfp-meta-fields dt { color: var(--fg3); }
.pfp-meta-fields dd {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 12px;
  min-width: 0;
  margin: 0;
  color: var(--fg);
  overflow-wrap: anywhere;
}
.pfp-meta-link { color: var(--accent); }
@media (max-width: 700px) {
  .pfp-meta-fields { grid-template-columns: minmax(0, 1fr); gap: 2px 0; }
  .pfp-meta-fields dd { margin-bottom: 6px; }
}

.pfp-md {
  font-size: var(--text-base);
  line-height: 1.65;
  /* Readable measure for prose; code and tables keep their own scroll. */
  width: 100%;
  max-width: 680px;
}
/* Same violet as chat comments: one colour means "you left a note here". */
.pfp-md :deep(.comment-highlight) {
  background: color-mix(in srgb, var(--accent2) 26%, transparent);
  border-bottom: 1px solid color-mix(in srgb, var(--accent2) 70%, transparent);
  border-radius: 2px;
  padding: 0 1px;
  cursor: pointer;
  transition: background-color 220ms ease-out, box-shadow 220ms ease-out;
}
.pfp-md :deep(.comment-highlight:hover) {
  background: color-mix(in srgb, var(--accent2) 40%, transparent);
}
.pfp-md :deep(.comment-highlight.comment-pulse) {
  animation: pfp-comment-pulse 1s ease-out 1;
}
@keyframes pfp-comment-pulse {
  0%   { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent2) 0%, transparent); }
  25%  { box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent2) 35%, transparent); }
  100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent2) 0%, transparent); }
}
@media (prefers-reduced-motion: reduce) {
  .pfp-md :deep(.comment-highlight.comment-pulse) { animation: none; }
}
.pfp-md :deep(p) { margin: 0.6em 0; }
.pfp-md :deep(:first-child) { margin-top: 0; }
.pfp-md :deep(:last-child) { margin-bottom: 0; }
.pfp-md :deep(pre) {
  background: var(--bg);
  padding: 8px 12px;
  border-radius: var(--radius-sm, 6px);
  overflow-x: auto;
  font-size: var(--text-sm);
  font-family: var(--font-mono);
}
.pfp-md :deep(code) {
  font-family: var(--font-mono);
}
.pfp-md :deep(:not(pre) > code) {
  background: color-mix(in srgb, var(--fg) 8%, transparent);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 0.9em;
}
.pfp-md :deep(:is(h1, h2, h3, h4)) {
  margin-top: 1.2em;
  margin-bottom: 0.4em;
  line-height: 1.35;
  font-weight: 700;
}
.pfp-md :deep(h1) { font-size: 1.5em; }
.pfp-md :deep(h2) { font-size: 1.25em; }
.pfp-md :deep(h3) { font-size: 1.1em; }
.pfp-md :deep(a) {
  color: var(--accent);
  text-decoration: underline;
}
.pfp-md :deep(a:hover) {
  color: var(--accent-strong);
}
/* A vault link whose target does not exist: readable, but not styled or
   shaped like something you can tap. Mirrors the file viewer modal. */
.pfp-md :deep(.vault-link-unresolved) {
  color: var(--fg2);
  text-decoration: underline dotted;
  cursor: help;
}
.pfp-md :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 6px;
  display: block;
  margin: 0.6em 0;
  background: var(--bg2, rgba(255, 255, 255, 0.04));
}
.pfp-md :deep(ul),
.pfp-md :deep(ol) {
  padding-left: 22px;
  margin: 0.6em 0;
  list-style-position: outside;
}
.pfp-md :deep(li) {
  padding-left: 2px;
  margin: 0.15em 0;
}
.pfp-md :deep(li > p) { margin: 0.2em 0; }
.pfp-md :deep(table) {
  border-collapse: collapse;
  margin: 10px 0;
  font-size: 13px;
  border: 1px solid var(--fg2);
}
.pfp-md :deep(th),
.pfp-md :deep(td) {
  border: 1px solid var(--fg2);
  padding: 5px 9px;
  vertical-align: top;
}
.pfp-md :deep(th) {
  background: var(--bg3, var(--bg2, rgba(255, 255, 255, 0.06)));
  font-weight: 600;
  text-align: left;
}
.pfp-md :deep(blockquote) {
  margin: 0.6em 0;
  padding: 0 0 0 12px;
  border-left: 3px solid var(--border);
  color: var(--fg2);
}
.pfp-md :deep(hr) {
  border: 0;
  border-top: 1px solid var(--border);
  margin: 1.25em 0;
}

/* ── Comment sidebar ──────────────────────────────────────────────── */
/* On-demand comment drawer: an overlay pinned to the right edge of the panel,
   toggled from the header pill. It is out of flow, so the document body keeps
   its full width. (The old fixed-width column used to crush the text.) */
.pfp-comment-backdrop {
  position: absolute;
  inset: 0;
  z-index: 20;
  background: transparent;
}
.pfp-comment-backdrop--dim {
  z-index: 31;
  background: rgba(0, 0, 0, 0.32);
}
.pfp-comment-drawer {
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
.pfp-drawer-close {
  background: transparent;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: calc(16px * var(--font-scale));
  line-height: 1;
  padding: 0 4px;
  margin-left: 6px;
}
.pfp-drawer-close:hover { color: var(--fg); }
.pfp-drawer-empty {
  padding: 16px 14px;
  color: var(--fg2);
  font-size: var(--text-sm);
}
.pfp-sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.pfp-sidebar-title {
  font-weight: 600;
  font-size: var(--text-sm);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.pfp-sidebar-count {
  font-size: var(--text-xs);
  color: var(--fg2);
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--border);
}
.pfp-sidebar-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.pfp-sidebar-card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: var(--text-sm);
  cursor: pointer;
  transition: border-color 0.15s;
}
.pfp-sidebar-card:hover {
  border-color: var(--accent, #60a5fa);
}
.pfp-sidebar-card-header {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  margin-bottom: 4px;
}
.pfp-sidebar-card-line {
  font-size: var(--text-xs);
  color: var(--fg2);
  margin-right: auto;
}
.pfp-sidebar-card-remove {
  background: transparent;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: calc(14px * var(--font-scale));
  line-height: 1;
  padding: 0 4px;
}
.pfp-sidebar-card-remove:hover { color: var(--error, #f87171); }
.pfp-sidebar-card-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  opacity: 0;
  transition: opacity 0.15s;
}
.pfp-sidebar-card:hover .pfp-sidebar-card-actions,
.pfp-sidebar-card.is-editing .pfp-sidebar-card-actions { opacity: 1; }
.pfp-sidebar-card-edit {
  background: transparent;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: var(--text-sm);
  line-height: 1;
  padding: 0 4px;
}
.pfp-sidebar-card-edit:hover { color: var(--accent, #60a5fa); }
.pfp-sidebar-edit-body { margin-top: 4px; }
.pfp-sidebar-edit-input {
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
.pfp-sidebar-edit-input:focus { border-color: var(--accent, #60a5fa); }
.pfp-sidebar-edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}
.pfp-sidebar-card-quote {
  color: var(--fg2);
  font-style: italic;
  margin-bottom: 4px;
  word-break: break-word;
}
.pfp-sidebar-card-note {
  color: var(--fg);
  line-height: 1.45;
  word-break: break-word;
  white-space: pre-wrap;
}
.pfp-pop-quote {
  margin-bottom: 4px;
  color: var(--fg3);
  font-size: var(--text-xs);
}
.pfp-pop-actions .pfp-sidebar-card-edit,
.pfp-pop-actions .pfp-sidebar-card-remove {
  min-height: 28px;
  padding: 0;
  color: var(--accent);
  font: inherit;
  font-size: var(--text-sm);
}
.pfp-pop-actions .pfp-sidebar-card-edit:hover,
.pfp-pop-actions .pfp-sidebar-card-remove:hover {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 3px;
}
@media (pointer: coarse) {
  .pfp-pop-actions .pfp-sidebar-card-edit,
  .pfp-pop-actions .pfp-sidebar-card-remove { min-height: var(--touch); }
}
.pfp-sidebar-draft-images {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.pfp-sidebar-card-images {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.pfp-sidebar-edit-images {
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

/* ── Floating Comment trigger ────────────────────────────────────── */
/* Comment trigger pill. Shape and behaviour match the danger-red variant
 * used in ChatPanel and FileViewerModal so the "Comment" affordance looks
 * the same regardless of where the user is in the app. */
.pfp-comment-trigger {
  position: absolute;
  z-index: 30;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 30px;
  padding: 0 10px;
  border: none;
  border-radius: 8px;
  background: var(--fg);
  color: var(--bg);
  cursor: pointer;
  font-size: var(--text-sm);
  font-weight: 600;
  box-shadow: 0 8px 20px rgb(0 0 0 / 25%);
}
.pfp-comment-trigger:hover { filter: brightness(1.08); }
.pfp-comment-trigger-icon { flex: none; }
@media (pointer: coarse) {
  .pfp-comment-trigger { min-height: var(--touch); }
}

/* Header "💬 N" pill: toggles the comment drawer. */
.pfp-comments-toggle {
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
.pfp-comments-toggle:hover { background: var(--bg3); color: var(--fg); }
.pfp-comments-toggle.active { border-color: var(--accent, #60a5fa); color: var(--fg); }
.pfp-comments-toggle-icon { font-size: var(--text-sm); line-height: 1; }
.pfp-comments-toggle-count { font-variant-numeric: tabular-nums; }

/* Floating comment popovers: compose (at the selection) and read (at a pin). */
.pfp-comment-pop {
  position: absolute;
  z-index: 32;
  width: 280px;
  max-width: calc(100% - 16px);
  box-sizing: border-box;
  padding: 10px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  background: var(--bg2);
  box-shadow: 0 14px 36px rgb(0 0 0 / 28%);
  font-size: var(--text-sm);
}
.pfp-pop-header {
  display: none;
}
.pfp-pop-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 6px;
}

/* Sidebar draft composer: sits between header and the scrollable list. */
.pfp-sidebar-draft {
  padding: 10px 12px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}
.pfp-sidebar-draft-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.pfp-sidebar-draft-label {
  font-weight: 600;
  font-size: var(--text-xs);
  color: var(--accent, #60a5fa);
  text-transform: uppercase;
  letter-spacing: 0.02em;
  flex: 1;
}
.pfp-sidebar-draft-input {
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
.pfp-sidebar-draft-input:focus { border-color: var(--accent, #60a5fa); }
.pfp-sidebar-draft-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}
.pfp-btn-sm {
  padding: 4px 10px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--fg);
  cursor: pointer;
  font-size: var(--text-sm);
}
.pfp-btn-sm:hover { background: var(--border); }
.pfp-btn-sm.primary {
  background: var(--accent, #60a5fa);
  color: var(--on-accent);
  border-color: transparent;
}
.pfp-btn-sm.primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Edit mode */
.pfp-edit-shell {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  gap: 8px;
}
.pfp-edit-textarea {
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
  outline: none;
  box-sizing: border-box;
}
.pfp-edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 4px;
}

/* ── Mobile fallback ─────────────────────────────────────────────── */
@media (max-width: 720px) {
  /* Drawer becomes a bottom sheet; comments stay reachable (they used to be
     display:none here, i.e. unreachable). */
  .pfp-comment-drawer {
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
  /* Popovers span the width so the note is readable on a phone. */
  .pfp-comment-pop {
    left: 8px !important;
    right: 8px;
    width: auto;
    max-width: none;
  }
}
@media (pointer: coarse) {
  .image-btn-sm,
  .pfp-comment-trigger {
    min-width: var(--touch);
    min-height: var(--touch);
  }

  .draft-image-remove {
    box-sizing: content-box;
    top: -12px;
    right: -12px;
    padding: 14px;
  }
}
</style>
