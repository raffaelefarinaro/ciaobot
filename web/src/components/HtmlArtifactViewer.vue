<script setup lang="ts">
// Renders a self-contained .html artifact: the sandboxed frame plus a
// Preview/Code toggle. Shared by PinnedFilePanel and FileViewerModal so the
// frame attributes and the toggle live in one place instead of being pasted
// into two very large components.
//
// The frame loads /api/workspace-html, which serves the file as text/html
// under an artifact CSP. Two things must stay in sync with that endpoint:
//
//   1. The `sandbox` attribute below must match the CSP's `sandbox` directive.
//      The effective sandbox is the intersection of the two, so adding a
//      permission here alone silently does nothing.
//   2. `allow-same-origin` must NEVER be added. Without it the document sits
//      in an opaque origin and cannot touch the session cookie, localStorage,
//      or this page. With it, model-authored script would run with the user's
//      full session.
//
// Source is a separate, lazy fetch owned by the parent: an artifact that is
// too large to read as text still renders fine, so a source failure must not
// replace the frame with an error string.
import { computed } from 'vue'

const props = defineProps<{
  filePath: string
  reloadToken: number | string
  view: 'preview' | 'code'
  source: string
  sourceLoading?: boolean
  sourceError?: string
}>()

const emit = defineEmits<{ (e: 'update:view', view: 'preview' | 'code'): void }>()

const frameSrc = computed(
  () => `/api/workspace-html?path=${encodeURIComponent(props.filePath)}&t=${props.reloadToken}`,
)

const sourceLines = computed(() => props.source.split('\n'))
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
