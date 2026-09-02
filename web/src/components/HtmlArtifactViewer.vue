<script setup lang="ts">
// Renders a self-contained .html artifact: the sandboxed frame plus a
// Preview/Code toggle. Shared by PinnedFilePanel and FileViewerModal so the
// frame attributes and the toggle live in one place instead of being pasted
// into two very large components.
//
// The frame loads /api/workspace-html, which serves the file as text/html
// under an artifact CSP with a small comment bridge script injected. Two
// things must stay in sync with that endpoint:
//
//   1. The `sandbox` attribute below must match the CSP's `sandbox` directive.
//      The effective sandbox is the intersection of the two, so adding a
//      permission here alone silently does nothing.
//   2. `allow-same-origin` must NEVER be added. Without it the document sits
//      in an opaque origin and cannot touch the session cookie, localStorage,
//      or this page. With it, model-authored script would run with the user's
//      full session.
//
// Comments: the bridge script inside the frame watches text selections (and
// Alt+Click for whole elements) and posts anchors over postMessage — the only
// channel an opaque-origin frame has. This component turns those into the
// same pending file comments the markdown viewer produces, so they stage as
// chips above the composer and ride along on the next message. It also
// re-sends durable highlights into the frame so past comments show as marks.
// Frame messages are untrusted (artifact script could forge them); the worst
// case is a note in the user's own composer, which they review before send.
//
// Source is a separate, lazy fetch owned by the parent: an artifact that is
// too large to read as text still renders fine, so a source failure must not
// replace the frame with an error string.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { isArtifactCommentEvent, type ArtifactHighlight } from '../lib/artifactBridge'

const props = defineProps<{
  filePath: string
  reloadToken: number | string
  view: 'preview' | 'code'
  source: string
  sourceLoading?: boolean
  sourceError?: string
}>()

const emit = defineEmits<{
  (e: 'update:view', view: 'preview' | 'code'): void
  // Selection/element comment captured in the frame: the parent panel opens
  // its compose popover at (x, y) and saves through the shared comment path.
  (e: 'compose-comment', anchor: {
    selector: string
    quote: string
    startOffset: number
    endOffset: number
    elementTag?: string
    wholeElement?: boolean
    frameX: number
    frameY: number
  }): void
  // Click on an existing highlight mark inside the frame.
  (e: 'open-comment', payload: { id: string; frameX: number; frameY: number }): void
}>()

const frameEl = ref<HTMLIFrameElement>()

const frameSrc = computed(
  () => `/api/workspace-html?path=${encodeURIComponent(props.filePath)}&t=${props.reloadToken}`,
)

const sourceLines = computed(() => props.source.split('\n'))

function isFrameSource(e: MessageEvent): boolean {
  return frameEl.value !== null && e.source === frameEl.value?.contentWindow
}

function onMessage(e: MessageEvent): void {
  if (!isFrameSource(e)) return
  if (!isArtifactCommentEvent(e.data)) return
  if (e.data.action === 'compose') {
    emit('compose-comment', {
      selector: e.data.selector,
      quote: e.data.quote,
      startOffset: e.data.startOffset,
      endOffset: e.data.endOffset,
      elementTag: e.data.elementTag,
      wholeElement: e.data.wholeElement,
      frameX: e.data.x,
      frameY: e.data.y,
    })
  } else if (e.data.action === 'open') {
    emit('open-comment', { id: e.data.id, frameX: e.data.x, frameY: e.data.y })
  }
}

// Push the durable comment list into the frame so the bridge re-draws marks.
// Called on load and whenever the parent's comment list changes.
function sendHighlights(highlights: ArtifactHighlight[]): void {
  const win = frameEl.value?.contentWindow
  if (!win) return
  win.postMessage(
    { frame: 'ciao-artifact', type: 'ciao:apply-comments', comments: highlights },
    '*',
  )
}

onMounted(() => window.addEventListener('message', onMessage))
onBeforeUnmount(() => window.removeEventListener('message', onMessage))

defineExpose({ sendHighlights, frameEl })
</script>

<template>
  <div class="hav">
    <div class="hav-toolbar" role="group" aria-label="Artifact view">
      <button
        type="button"
        class="hav-tab"
        :class="{ active: view === 'preview' }"
        :aria-pressed="view === 'preview'"
        @click="emit('update:view', 'preview')"
      >Preview</button>
      <button
        type="button"
        class="hav-tab"
        :class="{ active: view === 'code' }"
        :aria-pressed="view === 'code'"
        @click="emit('update:view', 'code')"
      >Code</button>
    </div>

    <!-- `v-if` rather than `v-show`: leaving the frame mounted keeps the
         artifact's script running (timers, listeners) behind the Code view. -->
    <iframe
      v-if="view === 'preview'"
      ref="frameEl"
      class="hav-frame"
      :src="frameSrc"
      :title="`Artifact preview: ${filePath}`"
      sandbox="allow-scripts"
      referrerpolicy="no-referrer"
      loading="lazy"
    ></iframe>

    <template v-else>
      <div v-if="sourceLoading" class="hav-note">Loading source…</div>
      <div v-else-if="sourceError" class="hav-note hav-note-error">{{ sourceError }}</div>
      <pre v-else class="hav-code"><code><span
        v-for="(line, i) in sourceLines"
        :key="i"
        class="hav-code-line"
      >{{ line || ' ' }}</span></code></pre>
    </template>
  </div>
</template>

<style scoped>
.hav {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  gap: 6px;
}
.hav-toolbar {
  display: flex;
  gap: 4px;
  flex: 0 0 auto;
}
.hav-tab {
  padding: 4px 10px;
  border: 1px solid var(--border, #2e3258);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg3, var(--bg2, #2a2e54));
  color: var(--fg2);
  font-size: var(--text-xs, 11px);
  cursor: pointer;
}
.hav-tab:hover {
  border-color: var(--border-strong, #3a3f70);
  background: var(--bg-elev, #23264a);
}
.hav-tab.active {
  color: var(--fg, #e8e8f0);
  border-color: var(--border-strong, #3a3f70);
  background: var(--bg-elev, #23264a);
}
/* White background because artifacts are ordinary web pages: a transparent
   frame over the dark PWA shell makes unstyled text unreadable. Artifacts that
   honour prefers-color-scheme paint over this anyway. */
.hav-frame {
  flex: 1;
  min-height: 320px;
  width: 100%;
  border: 1px solid var(--border, #2e3258);
  border-radius: var(--radius-sm, 6px);
  background: #fff;
  display: block;
}
.hav-code {
  margin: 0;
  flex: 1;
  min-height: 0;
  overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre;
  color: var(--fg, #e8e8f0);
}
.hav-code code {
  display: block;
}
.hav-code-line {
  display: block;
  padding: 0 4px;
}
.hav-note {
  color: var(--fg2);
  font-size: var(--text-sm, 12px);
  padding: 8px 4px;
}
.hav-note-error {
  color: var(--error, #f44336);
}
</style>