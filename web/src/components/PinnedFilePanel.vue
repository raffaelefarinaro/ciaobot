<template>
  <div class="pinned-file-panel" ref="rootEl">
    <PaneHeader @open-sidebar="$emit('close')">
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
          v-if="kind === 'excalidraw'"
          class="btn-icon"
          :class="{ active: isEditingExcalidraw }"
          @click="isEditingExcalidraw = !isEditingExcalidraw"
          :title="isEditingExcalidraw ? 'Disable editing' : 'Enable editing'"
          aria-label="Edit Diagram"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
            <path d="M18.5 2.5a2.121 2.121 0 1 1 3 3L12 15l-4 1 1-4z"></path>
          </svg>
        </button>
        <button
          v-if="kind === 'text' && !isEditingText"
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
      <div class="pfp-body" :class="{ 'pfp-body-excalidraw': kind === 'excalidraw', 'pfp-body-csv': isCsv }" ref="bodyEl">
        <div v-if="loading" class="pfp-loading">Loading…</div>
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
            :disabled="libreofficeInstalling"
            @click="installLibreoffice"
          >{{ libreofficeInstalling ? 'Installing…' : 'Install LibreOffice' }}</button>
        </div>
        <iframe
          v-else-if="kind === 'pdf'"
          class="pfp-pdf-iframe"
          :src="`/api/workspace-binary?path=${encodeURIComponent(cleanPath)}&t=${imageTimestamp}`"
          width="100%"
          height="100%"
          style="border: none; flex: 1; min-height: 500px; display: block; border-radius: 4px;"
        ></iframe>
        <ExcalidrawViewer
          v-else-if="kind === 'excalidraw'"
          :content="content"
          :name="basename"
          :file-path="cleanPath"
          :chat-id="projectsStore.activeChatId || ''"
          :read-only="!isEditingExcalidraw"
          @change="content = $event"
          style="flex: 1; min-height: 0; height: auto;"
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
            ></div>
            <CsvViewer
              v-else-if="isCsv"
              :content="content"
              :read-only="true"
            />
            <pre
              v-else
              class="pfp-pre"
              ref="preEl"
              @click="onPreClick"
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

      <!-- Comment sidebar -->
      <div v-if="commentsForFile.length || commentDraft" class="pfp-comment-sidebar">
        <div class="pfp-sidebar-header">
          <span class="pfp-sidebar-title">Comments</span>
          <span class="pfp-sidebar-count">{{ commentsForFile.length + (commentDraft ? 1 : 0) }}</span>
        </div>

        <!-- Draft composer -->
        <div v-if="commentDraft" class="pfp-sidebar-draft" @mousedown.stop>
          <div class="pfp-sidebar-draft-header">
            <span class="pfp-sidebar-draft-label">New comment</span>
            <button class="pfp-sidebar-card-remove" @click="cancelComment" title="Cancel">×</button>
          </div>
          <div class="pfp-sidebar-card-quote">"{{ truncate(commentDraft.selection, 120) }}"</div>
          <textarea
            ref="commentInputEl"
            v-model="commentDraft.text"
            class="pfp-sidebar-draft-input"
            placeholder="Add a comment…"
            rows="3"
            @keydown="onCommentKeydown"
          ></textarea>
          <div v-if="commentDraftImages.length" class="pfp-sidebar-draft-images">
            <span v-for="(img, i) in commentDraftImages" :key="img" class="draft-image-preview">
              <img :src="`/api/images/${img}`" :alt="img" class="draft-image-thumb" />
              <button class="draft-image-remove" @click="removeDraftImage(i)" title="Remove">×</button>
            </span>
          </div>
          <div class="pfp-sidebar-draft-actions">
            <label class="image-btn-sm" title="Upload images">
              <input type="file" accept="image/*" multiple hidden @change="handleDraftImageUpload" />
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
            </label>
            <button class="pfp-btn-sm" @click="cancelComment" type="button">Cancel</button>
            <button
              class="pfp-btn-sm primary"
              :disabled="!commentDraft.text.trim()"
              @click="saveComment"
              type="button"
            >Add comment</button>
          </div>
        </div>

        <div class="pfp-sidebar-list">
          <div
            v-for="c in commentsForFile"
            :key="c.id"
            class="pfp-sidebar-card"
            :class="{ 'is-editing': editingCommentId === c.id }"
            @click="editingCommentId !== c.id && scrollToHighlight(c.id)"
          >
            <div class="pfp-sidebar-card-header">
              <span class="pfp-sidebar-card-line" v-if="commentLineLabel(c)">{{ commentLineLabel(c) }}</span>
              <div v-if="!projectsStore.isStreaming" class="pfp-sidebar-card-actions">
                <button class="pfp-sidebar-card-edit" @click.stop="startEditComment(c)" title="Edit">✎</button>
                <button class="pfp-sidebar-card-remove" @click.stop="deleteFileComment(c.id)" title="Delete">×</button>
              </div>
            </div>
            <div class="pfp-sidebar-card-quote">"{{ truncate(c.selection, 120) }}"</div>
            <div v-if="editingCommentId !== c.id && c.images?.length" class="pfp-sidebar-card-images">
              <img v-for="img in c.images" :key="img" :src="`/api/images/${img}`" :alt="img" class="card-image-thumb" @click.stop />
            </div>
            <div v-if="editingCommentId !== c.id" class="pfp-sidebar-card-note">{{ c.comment }}</div>
            <div v-if="editingCommentId === c.id" class="pfp-sidebar-edit-body" @mousedown.stop>
              <textarea
                ref="editCommentInputEl"
                v-model="editDraftText"
                class="pfp-sidebar-edit-input"
                placeholder="Edit comment…"
                rows="2"
                @keydown="onEditKeydown"
              ></textarea>
              <div v-if="editingCommentImages.length" class="pfp-sidebar-edit-images">
                <span v-for="(img, i) in editingCommentImages" :key="img" class="draft-image-preview">
                  <img :src="`/api/images/${img}`" :alt="img" class="draft-image-thumb" />
                  <button class="draft-image-remove" @click="removeEditImage(i)" title="Remove">×</button>
                </span>
              </div>
              <div class="pfp-sidebar-edit-actions">
                <label class="image-btn-sm" title="Upload images">
                  <input type="file" accept="image/*" multiple hidden @change="handleEditImageUpload($event, c.id)" />
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                </label>
                <button class="pfp-btn-sm" @click="cancelEditComment" type="button">Cancel</button>
                <button class="pfp-btn-sm primary" @click="saveEditComment(c.id)" type="button">Save</button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Floating "Comment" button anchored near the active selection. -->
      <button
        v-if="selectionAnchor"
        class="pfp-comment-trigger"
        :style="{ top: selectionAnchor.top + 'px', left: selectionAnchor.left + 'px' }"
        @mousedown.prevent
        @click="openCommentForSelection"
        type="button"
        title="Comment on this selection"
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
import { parseFrontmatter, type FrontmatterValue } from '../lib/markdownFrontmatter'
import { renderFileMarkdown } from '../lib/safeMarkdown'
import { buildMarkdownIndex, resolveWikilinkTarget } from '../lib/wikilinks'
import { openWorkspaceFileExternally } from '../lib/openWorkspaceFile'
import { isCsvPath } from '../lib/csv'
import { api } from '../lib/api'
import PaneHeader from './PaneHeader.vue'
import { useFileViewerStore } from '../stores/fileViewer'
const ExcalidrawViewer = defineAsyncComponent(() => import('./ExcalidrawViewer.vue'))
const CsvViewer = defineAsyncComponent(() => import('./CsvViewer.vue'))

const props = defineProps<{ filePath: string }>()
defineEmits<{ (e: 'close'): void }>()

const projectsStore = useProjectStore()
const fileViewer = useFileViewerStore()

// ── Loading & rendering ──────────────────────────────────────────────
const loading = ref(false)
const error = ref('')
const content = ref('')
const kind = ref<'text' | 'image' | 'excalidraw' | 'pdf'>('text')
const refreshed = ref(false)
const openExternalState = ref<'' | 'loading' | 'ok'>('')
const isEditingExcalidraw = ref(false)
const isEditingText = ref(false)
const editBuffer = ref('')
const editSaving = ref(false)
const editError = ref('')
const editTextAreaEl = ref<HTMLTextAreaElement>()
const imageTimestamp = ref(Date.now())

// .pptx preview needs LibreOffice (soffice) server-side to convert to PDF.
// Checked proactively so a missing install shows a real "Install" button
// instead of the iframe silently failing to load with a browser-level error.
const pptxNeedsLibreoffice = ref(false)
const libreofficeInstalling = ref(false)
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

async function installLibreoffice(): Promise<void> {
  libreofficeInstalling.value = true
  libreofficeInstallError.value = ''
  try {
    const res = await api.post<{ ok: boolean; error?: string }>('/api/libreoffice-install', {})
    if (res.ok) {
      await checkLibreofficeStatus()
      if (!pptxNeedsLibreoffice.value) imageTimestamp.value = Date.now()
    } else {
      libreofficeInstallError.value = res.error || 'Installation failed.'
    }
  } catch (e) {
    libreofficeInstallError.value = e instanceof Error ? e.message : String(e)
  } finally {
    libreofficeInstalling.value = false
  }
}

const rootEl = ref<HTMLElement>()
const mainEl = ref<HTMLElement>()
const bodyEl = ref<HTMLElement>()
const mdEl = ref<HTMLElement>()
const preEl = ref<HTMLElement>()
const preCodeEl = ref<HTMLElement>()
const commentInputEl = ref<HTMLTextAreaElement>()

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

// `related`/`links` items are [[wikilink]] refs to other vault notes —
// resolve to file paths so the pills are clickable (same as body wikilinks).
// `aliases` are alternative names for this note, not links, so stay plain.
const _LINK_LIST_KEYS = new Set(['related', 'links'])
const _wikiIndex = computed(() => buildMarkdownIndex(markdownPaths.value || []))
const _wikiPathSet = computed(() => new Set(markdownPaths.value || []))

function resolveListItem(raw: string): { label: string; path: string | null } {
  const inner = raw.replace(/^\[\[(.+)\]\]$/, '$1').trim()
  const [ref, alias] = inner.split('|')
  const label = (alias ?? ref).trim()
  const path = ref.trim()
    ? resolveWikilinkTarget(ref.trim(), cleanPath.value, _wikiIndex.value, _wikiPathSet.value)
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

// Suppress unused-symbol warning when this file is imported in environments
// where FrontmatterValue isn't referenced as a runtime type.
type _Frontmatter = FrontmatterValue

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
  const isImg = /\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i.test(props.filePath)
  if (isImg) {
    kind.value = 'image'
    loading.value = false
    error.value = ''
    content.value = ''
    imageTimestamp.value = Date.now()
    return
  }
  const isPdfOrPptx = /\.(pdf|pptx)$/i.test(cleanPath.value)
  if (isPdfOrPptx) {
    kind.value = 'pdf'
    loading.value = false
    error.value = ''
    content.value = ''
    imageTimestamp.value = Date.now()
    pptxNeedsLibreoffice.value = false
    libreofficeInstallError.value = ''
    if (/\.pptx$/i.test(cleanPath.value)) void checkLibreofficeStatus()
    return
  }
  const isExcalidraw = /\.excalidraw$/i.test(cleanPath.value)
  if (isExcalidraw) {
    kind.value = 'excalidraw'
  } else {
    kind.value = 'text'
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

watch(() => props.filePath, () => load(), { immediate: true })

watch(
  () => [props.filePath, projectsStore.activeChatId, projectsStore.activeChatId ? projectsStore.streaming[projectsStore.activeChatId] : false] as const,
  ([filePath, chatId, isStreaming], oldValues) => {
    const wasStreaming = oldValues ? oldValues[2] : false
    if (filePath && chatId && wasStreaming && !isStreaming) {
      load()
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

function deleteFileComment(id: string): void {
  projectsStore.removeFileComment(cleanPath.value, id)
  nextTick(() => applyHighlights())
}

const lineCommentMap = computed(() => {
  const map = new Map<number, string>()
  for (const c of commentsForFile.value) {
    if (!c.lineStart) continue
    const end = c.lineEnd || c.lineStart
    for (let l = c.lineStart; l <= end; l++) {
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

function commentLineLabel(c: { lineStart?: number | null; lineEnd?: number | null }): string {
  if (!c.lineStart) return ''
  if (!c.lineEnd || c.lineEnd === c.lineStart) return String(c.lineStart)
  return `${c.lineStart}-${c.lineEnd}`
}

function truncate(s: string, n: number): string {
  if (!s) return ''
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}

// Highlight rendering inside the rendered markdown body. We strip-and-
// reapply on every comment list change so deleting a comment removes the
// highlight cleanly.
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
  if (kind.value === 'image' || kind.value === 'pdf') return
  if (isMarkdown.value) {
    const root = mdEl.value
    if (!root) return
    clearHighlights(root)
    for (const c of commentsForFile.value) {
      highlightInMarkdown(root, c.selection, c.id)
    }
  }
}

function scrollToHighlight(id: string): void {
  if (!bodyEl.value) return
  const matches: HTMLElement[] = []
  const highlights = bodyEl.value.querySelectorAll('[data-comment-id]')
  for (const el of Array.from(highlights)) {
    if ((el as HTMLElement).dataset.commentId === id) matches.push(el as HTMLElement)
  }
  if (!matches.length) return
  bodyEl.value.scrollTo({ top: matches[0].offsetTop - 20, behavior: 'smooth' })
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
  window.setTimeout(() => {
    for (const el of matches) el.classList.remove('comment-pulse')
  }, 1100)
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
  if (id) scrollToHighlight(id)
}

function onPreClick(e: MouseEvent): void {
  const target = e.target as HTMLElement | null
  if (!target) return
  const line = target.closest('.pre-line') as HTMLElement | null
  if (!line) return
  const id = line.dataset.commentId
  if (id) scrollToHighlight(id)
}

// Reapply highlights on content / comment changes.
watch(
  () => `${cleanPath.value}|${content.value.length}|${commentsForFile.value.map(c => c.id).join(',')}`,
  () => nextTick(() => applyHighlights()),
  { flush: 'post' },
)

// ── Selection → comment composer ─────────────────────────────────────
type Anchor = { top: number; left: number }
type LineRange = { start: number; end: number } | null
type CommentDraft = { selection: string; text: string; lines: LineRange }
const selectionAnchor = ref<Anchor | null>(null)
const commentDraft = ref<CommentDraft | null>(null)
let lastSelectionText = ''
let lastSelectionLines: LineRange = null
let lastSelectionRange: Range | null = null

const isCommentable = computed(() => !loading.value && !error.value && kind.value !== 'image' && kind.value !== 'pdf' && !projectsStore.isStreaming)

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

function lineAt(text: string, idx: number): number {
  let line = 1
  const limit = Math.min(idx, text.length)
  for (let i = 0; i < limit; i++) {
    if (text.charCodeAt(i) === 10) line++
  }
  return line
}

function computeSelectionLines(range: Range, selectionText: string): LineRange {
  const src = content.value
  if (!src) return null
  const codeRoot = preCodeEl.value
  if (codeRoot && codeRoot.contains(range.startContainer)) {
    const startOff = charOffsetFrom(codeRoot, range.startContainer, range.startOffset)
    const endOff = charOffsetFrom(codeRoot, range.endContainer, range.endOffset)
    if (startOff != null && endOff != null) {
      const a = Math.min(startOff, endOff)
      const b = Math.max(startOff, endOff)
      const start = lineAt(src, a)
      const end = b > a ? lineAt(src, b - 1) : start
      return { start, end: Math.max(end, start) }
    }
  }
  const trimmed = selectionText.trim()
  if (!trimmed) return null
  const head = trimmed.slice(0, 60)
  let startIdx = src.indexOf(head)
  if (startIdx === -1) {
    const firstLine = trimmed.split(/\n/, 1)[0].trim().slice(0, 30)
    if (firstLine.length >= 4) startIdx = src.indexOf(firstLine)
  }
  if (startIdx === -1) return null
  const start = lineAt(src, startIdx)
  const tail = trimmed.slice(-60).trim()
  if (tail.length >= 4 && tail !== head) {
    const tailIdx = src.indexOf(tail, startIdx)
    if (tailIdx !== -1) {
      const end = lineAt(src, tailIdx + tail.length - 1)
      return { start, end: Math.max(end, start) }
    }
  }
  return { start, end: start }
}

function updateSelectionAnchorFromRange(range: Range): void {
  const main = mainEl.value
  const body = bodyEl.value
  if (!main || !body) {
    selectionAnchor.value = null
    return
  }

  // Anchor the trigger at the END of the selection (where the cursor lands
  // after a drag-select), not at the bounding box of the whole range.
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

  const mainRect = main.getBoundingClientRect()
  const triggerWidth = 110  // approximate; matches the rendered "💬 Comment" pill
  const panelPad = 8
  const top = endRect.bottom - mainRect.top + 2
  const idealLeft = endRect.right - mainRect.left + 6
  const maxLeft = main.clientWidth - triggerWidth - panelPad
  const left = Math.max(panelPad, Math.min(idealLeft, maxLeft))
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
  if (commentDraft.value) return
  const sel = window.getSelection()
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
    lastSelectionRange = null
    selectionAnchor.value = null
    return
  }
  const range = sel.getRangeAt(0)
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
    text: '',
    lines: lastSelectionLines,
  }
  commentDraftImages.value = []
  selectionAnchor.value = null
  lastSelectionRange = null
  window.getSelection()?.removeAllRanges()
  nextTick(() => commentInputEl.value?.focus())
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
    path: cleanPath.value,
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
  const path = cleanPath.value
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
    const path = cleanPath.value
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
onMounted(() => {
  bodyEl.value?.addEventListener('scroll', onScrollReanchor, { passive: true })
})
onBeforeUnmount(() => {
  if (typeof document !== 'undefined') {
    document.removeEventListener('selectionchange', onSelectionChange)
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
    if (isStreaming) {
      cancelComment()
      editingCommentId.value = null
    } else {
      const justStoppedStreaming = wasStreaming && !isStreaming
      const justFlippedModified = !wasModified && isModified
      if (isModified && (justStoppedStreaming || justFlippedModified)) {
        refresh()
      }
    }
  }
)

// Reset draft when the file changes.
watch(() => props.filePath, () => {
  selectionAnchor.value = null
  commentDraft.value = null
  lastSelectionText = ''
  lastSelectionLines = null
  lastSelectionRange = null
  isEditingText.value = false
  editBuffer.value = ''
  editError.value = ''
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
  font-size: 16px;
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
  color: var(--ok, #4ade80);
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
.pfp-body-excalidraw {
  padding: 0 !important;
  overflow: hidden !important;
}
.pfp-body-csv {
  overflow: hidden !important;
}
.pfp-loading,
.pfp-error {
  padding: 24px;
  text-align: center;
  color: var(--fg2);
}
.pfp-error {
  color: var(--error, #f87171);
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
  white-space: pre;
  overflow-x: auto;
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
  font-size: 14px;
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
  background: var(--bg2, rgba(255, 255, 255, 0.04));
  padding: 10px 12px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 12px;
}
.pfp-md :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.pfp-md :deep(:not(pre) > code) {
  background: var(--bg2, rgba(255, 255, 255, 0.06));
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 0.92em;
}
.pfp-md :deep(:is(h1, h2, h3, h4)) {
  margin-top: 1.2em;
  margin-bottom: 0.4em;
  line-height: 1.3;
}
.pfp-md :deep(h1) { font-size: 1.6em; }
.pfp-md :deep(h2) { font-size: 1.3em; }
.pfp-md :deep(h3) { font-size: 1.1em; }
.pfp-md :deep(a) {
  color: var(--accent);
  text-decoration: underline;
}
.pfp-md :deep(a:hover) {
  color: var(--accent-strong);
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
.pfp-comment-sidebar {
  width: 240px;
  flex-shrink: 0;
  border-left: 1px solid var(--border);
  background: var(--bg2, rgba(255, 255, 255, 0.02));
  display: flex;
  flex-direction: column;
  min-height: 0;
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
  background: var(--danger, #e06c75);
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
  .pfp-comment-sidebar { display: none; }
}
</style>
