<template>
  <div class="pinned-file-panel" ref="rootEl">
    <!-- No brand and no page tag: this header sits beside the main pane's in the
         split view, and a second wordmark two inches away is the duplication the
         mark was moved out of the sidebar to end. The filename is the title. -->
    <PaneHeader :brand="false" @open-sidebar="$emit('close')">
      <template #title>
        <div class="header-left">
          <button class="close-btn desktop-only" @click="$emit('close')" title="Unpin file">&times;</button>
          <div class="header-breadcrumb">
            <span class="chat-title" :title="filePath">{{ basename }}</span>
          </div>
        </div>
      </template>
      <template #actions>
        <button
          v-if="(kind === 'text' || (kind === 'html' && htmlView === 'code' && sourceLoaded)) && !isEditingText"
          class="btn-icon"
          @click="startEditingText"
          title="Edit"
          aria-label="Edit"
          :disabled="loading || !!error"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
            <path d="M18.5 2.5a2.121 2.121 0 1 1 3 3L12 15l-4 1 1-4z"></path>
          </svg>
        </button>
        <button
          class="btn-icon"
          :class="{ ok: refreshed }"
          @click="refresh"
          title="Refresh"
          aria-label="Refresh"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
        </button>
        <button
          class="btn-icon"
          @click="downloadFile"
          title="Download"
          aria-label="Download"
          :disabled="loading || !!error"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        </button>
        <button
          class="btn-icon"
          :class="{ ok: openExternalState === 'ok' }"
          @click="openExternally"
          title="Open in default app"
          aria-label="Open in default app"
          :disabled="loading || !!error || openExternalState === 'loading'"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
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
          :file-path="cleanPath"
          :reload-token="imageTimestamp"
          :view="htmlView"
          :source="content"
          :source-loading="sourceLoading"
          :source-error="sourceError"
          @update:view="setHtmlView"
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
            <!-- Metadata card synthesized from YAML frontmatter -->
            <div v-if="frontmatter" class="pfp-meta-card">
              <div class="pfp-meta-row">
                <span v-if="fmType" class="pfp-meta-pill pfp-meta-pill-type">{{ fmType }}</span>
                <span v-if="fmStatus" class="pfp-meta-pill" :class="`pfp-meta-pill-status-${fmStatus}`">{{ fmStatus }}</span>
                <span v-if="fmName && fmName !== basename.replace(/\.md$/, '')" class="pfp-meta-name" :title="fmName">{{ fmName }}</span>
                <span class="pfp-meta-spacer"></span>
                <span v-if="fmUpdated" class="pfp-meta-date" :title="`Updated ${fmUpdated}`">↻ {{ fmUpdated }}</span>
                <span v-else-if="fmCreated" class="pfp-meta-date" :title="`Created ${fmCreated}`">+ {{ fmCreated }}</span>
              </div>
              <div v-if="fmTags.length" class="pfp-meta-row pfp-meta-tags">
                <span v-for="t in fmTags" :key="t" class="pfp-meta-tag">#{{ t }}</span>
              </div>
              <p v-if="fmProse" class="pfp-meta-summary">{{ fmProse }}</p>
              <div
                v-for="listExtra in fmListExtras"
                :key="listExtra.key"
                class="pfp-meta-row pfp-meta-links"
              >
                <span class="pfp-meta-links-label">{{ listExtra.key }}</span>
                <template v-for="(item, i) in listExtra.items" :key="i">
                  <a
                    v-if="item.path"
                    class="pfp-meta-link file-link"
                    href="#"
                    @click.prevent="openRelated(item.path)"
                  >{{ item.label }}</a>
                  <span v-else class="pfp-meta-link">{{ item.label }}</span>
                </template>
              </div>
              <dl v-if="fmExtraEntries.length" class="pfp-meta-extra">
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
        @cancel="cancelComment"
        @save="saveComment"
        @upload="handleDraftImageUpload"
        @remove-image="removeDraftImage"
      />

      <!-- Read popover: hover to preview, click to pin; edit opens the drawer. -->
      <div
        v-if="commentPopover?.pinned && popoverComment"
        class="pfp-comment-backdrop pfp-comment-backdrop--dim"
        @click="handlePfpBackdropClick"
      ></div>
      <div
        v-if="commentPopover && popoverComment"
        class="pfp-comment-pop"
        :style="{ top: commentPopover.top + 'px', left: commentPopover.left + 'px' }"
        @mousedown.stop
        @mouseenter="onPopoverEnter"
        @mouseleave="onPopoverLeave"
      >
        <div class="pfp-pop-header">
          <span class="pfp-sidebar-card-line" v-if="commentLineLabel(popoverComment)">{{ commentLineLabel(popoverComment) }}</span>
          <div class="pfp-sidebar-card-actions pfp-pop-actions">
            <button class="pfp-sidebar-card-edit" @click.stop="editFromPopover(popoverComment)" title="Edit">✎</button>
            <button class="pfp-sidebar-card-remove" @click.stop="deleteFromPopover(popoverComment.id)" title="Delete">×</button>
          </div>
        </div>
        <div v-if="popoverComment.images?.length" class="pfp-sidebar-card-images">
          <img v-for="img in popoverComment.images" :key="img" :src="`/api/images/${img}`" :alt="img" class="card-image-thumb" @click.stop />
        </div>
        <div class="pfp-sidebar-card-note">{{ popoverComment.comment }}</div>
      </div>

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
        <span class="pfp-comment-trigger-icon">💬</span>
        Comment
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useProjectStore } from '../stores/projects'
import { parseFrontmatter } from '../lib/markdownFrontmatter'
import { renderFileMarkdown } from '../lib/safeMarkdown'
import { buildMarkdownIndex, resolveVaultLinkTarget } from '../lib/vaultLinks'
import { openWorkspaceFileExternally } from '../lib/openWorkspaceFile'
import { isCsvPath } from '../lib/csv'
import { useHoverPinPopover } from '../composables/useHoverPinPopover'
import { useFileComments } from '../composables/useFileComments'
import { useTypeToComment } from '../composables/useTypeToComment'
import { api } from '../lib/api'
import PaneHeader from './PaneHeader.vue'
import CommentComposePopover from './CommentComposePopover.vue'
import { fileViewerKindForPath, useFileViewerStore } from '../stores/fileViewer'
import type { FileViewerKind, HtmlArtifactView } from '../stores/fileViewer'
const CsvViewer = defineAsyncComponent(() => import('./CsvViewer.vue'))
const HtmlArtifactViewer = defineAsyncComponent(() => import('./HtmlArtifactViewer.vue'))

const props = defineProps<{ filePath: string }>()
defineEmits<{ (e: 'close'): void }>()

const projectsStore = useProjectStore()
const fileViewer = useFileViewerStore()

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
  const label = (alias ?? ref).trim()
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
      : items.map((raw) => ({ label: raw, path: null }))
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
watch(() => commentPopover.value?.pinned, (pinned) => {
  if (pinned) pfpPinTimestamp = Date.now()
})

function handlePfpBackdropClick(): void {
  if (Date.now() - pfpPinTimestamp < 150) return
  closeCommentPopover()
}

function deleteFromPopover(id: string): void {
  closeCommentPopover()
  comments.deleteFileComment(id)
}

function editFromPopover(c: { id: string; comment: string; images?: string[] }): void {
  const popAnchor = commentPopover.value
    ? comments.toViewportAnchor({ top: commentPopover.value.top, left: commentPopover.value.left })
    : null
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
// Artifacts are not commentable: a comment anchors to a markdown highlight or
// a text line, and a rendered page in a sandboxed frame offers neither. This is
// a real capability loss versus .md, which is why the html-artifact skill tells
// the model to keep prose in markdown.
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
</script>

<style scoped>
.pinned-file-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  background: var(--bg);
  position: relative;
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
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: 20px;
  padding: 0 4px;
  line-height: 1;
  font-family: var(--font);
  min-width: 30px;
  min-height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.close-btn:hover { color: var(--fg); }

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
  font-weight: 600;
  color: var(--fg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
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
  padding: 14px 20px 20px;
  min-width: 0;
  display: flex;
  flex-direction: column;
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
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
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
  background: rgba(250, 204, 21, 0.18);
  cursor: pointer;
}

/* ── Metadata card (parsed frontmatter) ─────────────────────────── */
.pfp-meta-card {
  margin: 0 0 16px;
  padding: 10px 12px;
  background: var(--bg2, rgba(255, 255, 255, 0.03));
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: 100%;
}
.pfp-meta-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-height: 22px;
}
.pfp-meta-pill {
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
.pfp-meta-pill-type {
  background: rgba(96, 165, 250, 0.18);
  color: #93c5fd;
}
.pfp-meta-pill-status-active {
  background: rgba(34, 197, 94, 0.18);
  color: #86efac;
}
.pfp-meta-pill-status-completed,
.pfp-meta-pill-status-archived {
  background: rgba(148, 163, 184, 0.18);
  color: #cbd5e1;
}
.pfp-meta-pill-status-draft {
  background: rgba(250, 204, 21, 0.18);
  color: #fde68a;
}
.pfp-meta-name {
  color: var(--fg2);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  margin-left: 4px;
}
.pfp-meta-spacer {
  flex: 1;
  min-width: 0;
}
.pfp-meta-date {
  color: var(--fg2);
  font-size: 11px;
  white-space: nowrap;
}
.pfp-meta-tags {
  margin-top: -2px;
}
.pfp-meta-tag {
  font-size: 11px;
  color: var(--fg2);
  background: transparent;
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.pfp-meta-summary {
  margin: 2px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--border);
  font-size: 13px;
  line-height: 1.55;
  color: var(--fg);
}
.pfp-meta-links {
  gap: 4px 6px;
}
.pfp-meta-links-label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--fg2);
  margin-right: 2px;
}
.pfp-meta-link {
  font-size: 11px;
  color: var(--fg2);
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.pfp-meta-extra {
  margin: 8px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--border);
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 2px 12px;
  font-size: 12px;
}
.pfp-meta-extra dt {
  color: var(--fg2);
  font-weight: 600;
  text-transform: lowercase;
}
.pfp-meta-extra dd {
  margin: 0;
  color: var(--fg);
  word-break: break-word;
}

.pfp-md {
  font-size: var(--text-base);
  line-height: 1.6;
  max-width: 100%;
}
.pfp-md :deep(.comment-highlight) {
  background: rgba(250, 204, 21, 0.18);
  border-radius: 3px;
  padding: 0 1px;
  cursor: pointer;
  /* Animate the colour transition so the pulse fade-back feels natural. */
  transition: background-color 220ms ease-out, box-shadow 220ms ease-out;
}
.pfp-md :deep(.comment-highlight.comment-pulse) {
  animation: pfp-comment-pulse 1s ease-out 1;
}
@keyframes pfp-comment-pulse {
  0%   { background: rgba(250, 204, 21, 0.18); box-shadow: 0 0 0 0 rgba(250, 204, 21, 0); }
  20%  { background: rgba(250, 204, 21, 0.55); box-shadow: 0 0 0 4px rgba(250, 204, 21, 0.45); }
  60%  { background: rgba(250, 204, 21, 0.40); box-shadow: 0 0 0 2px rgba(250, 204, 21, 0.20); }
  100% { background: rgba(250, 204, 21, 0.18); box-shadow: 0 0 0 0 rgba(250, 204, 21, 0); }
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
  word-break: break-word;
  white-space: pre-wrap;
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
  padding: 4px 10px;
  background: var(--error);
  color: white;
  border: none;
  border-radius: 999px;
  cursor: pointer;
  font-size: var(--text-sm);
  font-weight: 600;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}
.pfp-comment-trigger:hover { filter: brightness(1.08); }
.pfp-comment-trigger-icon { font-size: var(--text-sm); line-height: 1; }

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
  background: var(--bg);
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--accent, #60a5fa);
  border-radius: 8px;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
  padding: 10px 12px;
  box-sizing: border-box;
}
.pfp-pop-header {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 6px;
}
.pfp-pop-actions {
  opacity: 1 !important;
  margin-left: auto;
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
  color: white;
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
</style>
