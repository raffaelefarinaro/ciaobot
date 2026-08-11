<template>
  <svg
    class="app-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="square"
    stroke-linejoin="miter"
    :aria-label="label || undefined"
    :aria-hidden="label ? undefined : 'true'"
    :role="label ? 'img' : undefined"
  >
    <!-- Framed picture: rect, sun, horizon. -->
    <template v-if="name === 'image'">
      <rect x="3" y="4" width="18" height="16" />
      <path d="M3 16l5-5 4 4 3-3 6 6" />
      <rect x="15" y="7" width="2" height="2" fill="currentColor" stroke="none" />
    </template>

    <!-- Text document: page with ruled lines. -->
    <template v-else-if="name === 'doc'">
      <rect x="4" y="3" width="16" height="18" />
      <line x1="8" y1="8" x2="16" y2="8" />
      <line x1="8" y1="12" x2="16" y2="12" />
      <line x1="8" y1="16" x2="13" y2="16" />
    </template>

    <!-- Anything else: page with a clipped corner. -->
    <template v-else-if="name === 'file'">
      <path d="M6 3h8l4 4v14H6z" />
      <polyline points="14 3 14 7 18 7" />
    </template>

    <!-- Model: a chip, pins on both sides. -->
    <template v-else-if="name === 'model'">
      <rect x="7" y="7" width="10" height="10" />
      <line x1="3" y1="10" x2="7" y2="10" />
      <line x1="3" y1="14" x2="7" y2="14" />
      <line x1="17" y1="10" x2="21" y2="10" />
      <line x1="17" y1="14" x2="21" y2="14" />
    </template>

    <!-- Open question: framed query. -->
    <template v-else-if="name === 'question'">
      <rect x="3" y="3" width="18" height="18" />
      <path d="M9 9a3 3 0 1 1 3 3v2" />
      <rect x="11" y="17" width="2" height="2" fill="currentColor" stroke="none" />
    </template>

    <!-- Comment: a log window with a tail. -->
    <template v-else-if="name === 'comment'">
      <rect x="3" y="4" width="18" height="13" />
      <line x1="7" y1="9" x2="15" y2="9" />
      <line x1="7" y1="13" x2="17" y2="13" />
      <polyline points="8 17 8 20 11 17" />
    </template>

    <!-- Schedule: clock face with markers, matching the sidebar nav icon. -->
    <template v-else-if="name === 'clock'">
      <rect x="3" y="3" width="18" height="18" />
      <line x1="12" y1="3" x2="12" y2="5" />
      <line x1="12" y1="19" x2="12" y2="21" />
      <line x1="3" y1="12" x2="5" y2="12" />
      <line x1="19" y1="12" x2="21" y2="12" />
      <polyline points="12 8 12 12 15 14" />
    </template>

    <!-- Activity trace: a step signal. -->
    <template v-else-if="name === 'activity'">
      <polyline points="3 16 7 16 9 8 13 20 15 14 21 14" />
    </template>

    <!-- Shield: permission request. -->
    <template v-else-if="name === 'shield'">
      <path d="M12 3l8 3.5v5c0 5-3.4 8.6-8 9.5-4.6-.9-8-4.5-8-9.5v-5L12 3z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <rect x="11" y="15" width="2" height="2" fill="currentColor" stroke="none" />
    </template>
  </svg>
</template>

<script setup lang="ts">
/**
 * The app's icon set, as SVG.
 *
 * Emoji cannot inherit `currentColor`, so they can never carry a workspace hue
 * or dim with a priority tier — they are the one glyph type that cannot join the
 * design system, and they render differently on every platform. This component
 * is the sanctioned replacement; see docs/DESIGN_SYSTEM.md rule S4.
 *
 * House style, matching the sidebar nav icons: 24 viewBox, `stroke-width: 2`,
 * square caps and miter joins, `currentColor`, geometric rather than rounded.
 */
export type AppIconName =
  | 'image'
  | 'doc'
  | 'file'
  | 'model'
  | 'question'
  | 'comment'
  | 'clock'
  | 'activity'
  | 'shield'

withDefaults(defineProps<{
  name: AppIconName
  /** Pixel box. Defaults to the 16px inline size used beside text. */
  size?: number | string
  /** Set only when the icon is the sole label for a control. */
  label?: string
}>(), {
  size: 16,
})
</script>

<style scoped>
.app-icon {
  display: block;
  flex-shrink: 0;
}
</style>
