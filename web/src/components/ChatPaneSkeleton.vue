<template>
  <div
    class="chat-pane-skeleton"
    role="status"
    aria-live="polite"
    aria-label="Loading conversation"
    aria-busy="true"
  >
    <!-- The pane header this surface will grow into: a title bar with a rule
         under it, so the placeholder occupies the same box as the header that
         replaces it. -->
    <div class="pane-skeleton-header" aria-hidden="true">
      <span class="pane-skeleton-line pane-skeleton-line--title"></span>
    </div>
    <div class="pane-skeleton-body">
      <!-- The transcript's own shapes: a left-aligned assistant bubble, a
           right-aligned user bubble, and a collapsed activity row between
           them, at the widths and radii the real rows use. -->
      <div class="pane-skeleton-msg pane-skeleton-msg--assistant" aria-hidden="true">
        <span class="pane-skeleton-line pane-skeleton-line--long"></span>
        <span class="pane-skeleton-line pane-skeleton-line--medium"></span>
      </div>
      <div class="pane-skeleton-msg pane-skeleton-msg--user" aria-hidden="true">
        <span class="pane-skeleton-line pane-skeleton-line--wide"></span>
        <span class="pane-skeleton-line pane-skeleton-line--short"></span>
      </div>
      <div class="pane-skeleton-trace" aria-hidden="true">
        <span class="pane-skeleton-chevron">&#9656;</span>
        <span class="pane-skeleton-line pane-skeleton-line--trace"></span>
      </div>
      <div class="pane-skeleton-msg pane-skeleton-msg--assistant" aria-hidden="true">
        <span class="pane-skeleton-line pane-skeleton-line--long"></span>
        <span class="pane-skeleton-line pane-skeleton-line--long"></span>
        <span class="pane-skeleton-line pane-skeleton-line--medium"></span>
        <span class="pane-skeleton-line pane-skeleton-line--short"></span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// The placeholder for a chat pane that is on its way but not here yet.
//
// Two gaps used to leave the pane blank (#1651): the lazy `ChatPanel` chunk
// renders nothing while it is being fetched, and a chat route whose catalog
// entry has not arrived yet falls through every branch in ChatLayout. This is
// the surface that covers both: a static, prop-less, async-free component the
// layout can mount the moment it knows a conversation is coming.
//
// Its geometry is copied from `ChatPanel.vue`'s own transcript skeleton
// (`.history-skeleton-stack`): the same radii, heights and shimmer, so the
// handover from this placeholder to the real transcript is a fill-in rather
// than a jump. It deliberately imports nothing from ChatPanel - that file is
// the chunk this placeholder stands in for.
//
// It carries no interactive element: no button, no link, no focusable node.
// The keyboard order and the 44px touch targets of the pane are exactly what
// they are with the transcript on screen.
</script>

<style scoped>
.chat-pane-skeleton {
  /* The conversation's reading measure, in the placeholder's own words: the
     chat panel declares it on its own root, and the placeholder must not
     depend on a token that lives in the chunk it stands in for. */
  --pane-measure: 768px;
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

/* The header bar: the same height and rule as the pane header it stands in
   for, so the transcript does not jump when the real one arrives. */
.pane-skeleton-header {
  flex-shrink: 0;
  min-height: calc(61px + var(--safe-top));
  display: flex;
  align-items: center;
  /* The chat header's own inset: the title starts on the reading column's
     left edge, not at the pane's edge. */
  padding: var(--space-2) max(var(--page-gutter), calc((100% - var(--pane-measure)) / 2));
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}

/* The phone header is a different grid (the real one puts the menu button
   and the title on their own rows), and its inset is the phone gutter. */
@media (max-width: 768px) {
  .pane-skeleton-header {
    padding: var(--space-3) var(--space-3);
  }
}

.pane-skeleton-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 14px;
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  padding: 28px 26px 24px;
  /* The same reading measure the transcript keeps, centred in the pane. */
  max-width: var(--pane-measure);
  margin: 0 auto;
}

/* Geometry copied from ChatPanel's .message.assistant / .message.user on
   purpose: the whole point is that the placeholder occupies the same box as
   the row that replaces it. */
.pane-skeleton-msg {
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 12px 14px;
  min-width: 0;
}

.pane-skeleton-msg--assistant {
  align-self: stretch;
  margin-right: 48px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-left: 3px solid color-mix(in srgb, var(--accent) 45%, transparent);
  border-radius: 4px 14px 14px 14px;
}

.pane-skeleton-msg--user {
  align-self: flex-end;
  width: min(62%, calc(100% - 48px));
  margin-left: 48px;
  background: color-mix(in srgb, var(--accent2) 12%, var(--bg3));
  border: 1px solid var(--border-strong);
  border-radius: 14px 14px 2px 14px;
}

:root.theme-light .pane-skeleton-msg--user {
  background: color-mix(in srgb, var(--accent2) 8%, var(--bg3));
  border-color: var(--border);
}

/* Matches a collapsed trace row: dashed, 98% wide, accent2 edge. */
.pane-skeleton-trace {
  align-self: flex-start;
  display: flex;
  align-items: center;
  gap: 8px;
  width: 98%;
  box-sizing: border-box;
  padding: 9px 12px;
  border: 1px dashed var(--border);
  border-left: 3px solid color-mix(in srgb, var(--accent2) 45%, transparent);
  border-radius: var(--radius);
  opacity: 0.85;
}

.pane-skeleton-chevron {
  color: var(--fg3);
  font-size: 9px;
  line-height: 1;
}

@media (hover: hover) and (pointer: fine) {
  .pane-skeleton-msg--assistant { margin-right: 32px; }
  .pane-skeleton-msg--user { margin-left: 32px; }
}

.pane-skeleton-line {
  display: block;
  height: 10px;
  border-radius: var(--radius-pill);
  background: linear-gradient(90deg, var(--bg2) 0%, var(--bg3) 50%, var(--bg2) 100%);
  background-size: 200% 100%;
  animation: pane-skeleton-sweep 1.4s ease-in-out infinite;
}

.pane-skeleton-line--title { width: 34%; height: 13px; }
.pane-skeleton-line--wide { width: 84%; }
.pane-skeleton-line--long { width: 92%; }
.pane-skeleton-line--medium { width: 68%; }
.pane-skeleton-line--short { width: 42%; }
.pane-skeleton-line--trace { width: 96px; height: 9px; }

@keyframes pane-skeleton-sweep {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}

@media (prefers-reduced-motion: reduce) {
  .pane-skeleton-line {
    animation: none;
  }
  .chat-pane-skeleton { opacity: 0.9; }
}
</style>
