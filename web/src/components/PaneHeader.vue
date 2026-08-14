<template>
  <header class="pane-header" :class="{ 'pane-header--no-center': !hasCenter }">
    <button class="header-hamburger touch-hit" aria-label="Open sidebar" @click="$emit('open-sidebar')">
      <!-- 18px at stroke 2, the size and weight every other icon in this header
           and in the sidebar uses (see the .btn-icon block below). At 22px and
           stroke 2.2 it read as a heavier glyph than the actions it shares the
           row with. -->
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
        <line x1="4" y1="7" x2="20" y2="7"/>
        <line x1="4" y1="12" x2="20" y2="12"/>
        <line x1="4" y1="17" x2="20" y2="17"/>
      </svg>
    </button>
    <div v-if="hasTitle" class="header-title">
      <slot name="title" />
    </div>
    <div class="header-center">
      <BrandMark v-if="brand" />
      <!-- Which view this is. Styled as a quiet marker, never as a heading: it
           answers "where am I", it is not the subject of the page, so it stays at
           --fg3 in an outlined chip. A fill would read as "needs you" (Rule S1).
           It is an <h2> on views that have no title of their own (home, settings,
           the automations list) so those pages keep the one heading their pane
           header used to give them, and a plain <span> where a title already
           names the page. -->
      <component :is="hasTitle ? 'span' : 'h2'" v-if="pageTag" class="page-tag">{{ pageTag }}</component>
    </div>
    <div class="header-trail">
      <div v-if="$slots.actions" class="header-actions" :key="activeBgAgents">
        <slot name="actions" />
      </div>
      <NotificationBell class="header-bell" />
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed, useSlots } from 'vue'
import NotificationBell from './NotificationBell.vue'
import BrandMark from './BrandMark.vue'

const props = withDefaults(defineProps<{
  activeBgAgents?: number
  /** Short marker for the current view: 'home', 'settings', 'automations', … */
  pageTag?: string
  /** Off only where a second mark would duplicate the main pane's (split view). */
  brand?: boolean
}>(), { brand: true })
defineEmits<{ 'open-sidebar': [] }>()

const slots = useSlots()
// An empty `.header-title` would still claim the left grid track and, on mobile,
// a second row. Views with no title of their own (home, settings, the automations
// list) drop the element and let the page tag name them instead.
const hasTitle = computed(() => !!slots.title)
// The middle track only earns its symmetric siblings when it has something
// to centre.
const hasCenter = computed(() => props.brand || !!props.pageTag)
</script>

<style scoped>
.pane-header {
  /* Three columns, so the brand in the middle one is genuinely centred on the
     header rather than centred-by-eye with an absolute offset.
       col 1  minmax(0, 1fr)  the title: may shrink to nothing, so it ellipses
                              instead of pushing the middle column off centre
       col 2  auto            the brand + page tag, sized by content
       col 3  1fr             the actions: `1fr` is minmax(auto, 1fr), so its
                              min is the icons' own width and they never crush
     Both side tracks are `1fr`, so while there is free space they are equal and
     the middle one lands dead centre. Once the actions need more than their
     share they take it from col 1, which is the correct order of sacrifice: a
     long chat title ellipses, no icon is clipped, and the mark drifts off centre
     only in the layout where nothing could have stayed centred anyway. */
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto 1fr;
  align-items: center;
  /* Match the sidebar header: 44px controls + 8px vertical padding + border. */
  height: calc(61px + var(--safe-top));
  padding: calc(var(--space-2) + var(--safe-top)) var(--space-2) var(--space-2);
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  column-gap: var(--space-2);
  flex-shrink: 0;
  box-sizing: border-box;
}

/* Nothing in the middle column to centre, so there is nothing for the symmetric
   side tracks to buy - and they are not free: `1fr` on the trailing track holds
   back half the spare width for actions that only need their own size, while
   the title ellipses in the other half. The chat header is exactly this case
   (`:brand="false"`, no page tag), which is where a title truncated to "Hourly
   skills page m..." sat next to an empty stretch of header. Size the trail to
   its content and give the remainder to the title. */
.pane-header--no-center {
  grid-template-columns: minmax(0, 1fr) auto auto;
}
.header-title {
  grid-column: 1;
  min-width: 0;
  text-align: left;
}
.header-center {
  grid-column: 2;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}
.header-trail {
  grid-column: 3;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-2);
}
/* In the split view the chat pane can be dragged down to its 240px floor, where
   the header cannot hold a centred mark on top of a title and the action icons.
   The middle column is the first thing to go: the mark is decoration plus a
   reload shortcut that is one drag of the splitter away, whereas a clipped icon
   is a lost action. `chat-split` is declared on `.chat-split-main` in
   ChatLayout, so this only applies inside the split view. */
@container chat-split (max-width: 420px) {
  .header-center { display: none; }
}

/* A wide viewport can still leave this pane narrow when the sidebar has been
   dragged near its maximum. The header's action trail is unshrinkable, so
   hide the decorative brand/tag based on the pane width as well as the
   viewport width. */
@container chat-pane (max-width: 460px) {
  .header-center { display: none; }
}

/* Very narrow viewports: drop the mark outright rather than clip it. The trail's
   controls cannot shrink, so something has to go, and the mark is the only thing
   here that is not an action. */
@media (max-width: 460px) {
  .header-center { display: none; }
}
/* Kept in the document, not on the screen. The page now names itself next to
   its own nav icon in the sidebar, which is where it was asked for, so a second
   copy here beside the wordmark was the same fact twice. It stays rendered
   because on home, settings and the automations list it is the pane's only
   heading, and dropping the element would cost those views their document
   outline. Same rule as .sr-only in App.vue. */
.page-tag {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
:deep(.header-left) {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  flex: 1;
  text-align: left;
}
:deep(.pane-title) {
  font-weight: 600;
  /* Token, not 16px: the breadcrumb beside this uses --text-lg, so a fixed
     pixel value drifted apart from it as soon as the font scale moved. */
  font-size: var(--text-lg);
  display: block;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  flex-shrink: 0;
}
.header-hamburger {
  display: none;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  background: none;
  border: none;
  color: var(--fg);
  cursor: pointer;
  border-radius: var(--radius-sm);
  flex-shrink: 0;
}
.header-hamburger:active { transform: scale(0.96); }
.header-bell {
  flex-shrink: 0;
  display: none;
}
/* Unify header icon sizes with the sidebar (30px containers, 18px content).
   ::before keeps hover/active fills at the 30px visual footprint. */
.pane-header :deep(.btn-icon),
.pane-header :deep(.model-picker-btn),
.pane-header :deep(.archive-btn) {
  box-sizing: content-box;
  width: 30px;
  height: 30px;
  min-width: 30px;
  min-height: 30px;
  padding: calc((var(--touch) - 30px) / 2);
  margin: calc((30px - var(--touch)) / 2);
  border-radius: var(--radius-sm);
  position: relative;
  isolation: isolate;
}
.pane-header :deep(.btn-icon::before),
.pane-header :deep(.model-picker-btn::before),
.pane-header :deep(.archive-btn::before),
.pane-header .header-bell :deep(.bell-btn::before) {
  content: '';
  position: absolute;
  inset: calc((var(--touch) - 30px) / 2);
  z-index: -1;
  border-radius: var(--radius-sm);
  background: transparent;
  pointer-events: none;
  transition: background 120ms var(--ease);
}
.pane-header :deep(.btn-icon:hover),
.pane-header :deep(.model-picker-btn:hover),
.pane-header :deep(.archive-btn:hover),
.pane-header .header-bell :deep(.bell-btn:hover) {
  background: transparent;
}
.pane-header :deep(.btn-icon:hover::before),
.pane-header :deep(.model-picker-btn:hover::before),
.pane-header :deep(.archive-btn:hover::before),
.pane-header .header-bell :deep(.bell-btn:hover::before) {
  background: var(--bg3);
}
.pane-header :deep(.btn-icon.active::before),
.pane-header :deep(.btn-icon[aria-pressed="true"]::before) {
  background: var(--bg3);
  border: 1px solid var(--border);
}
.pane-header :deep(.bell-btn) {
  box-sizing: content-box;
  width: 30px;
  height: 30px;
  padding: calc((var(--touch) - 30px) / 2);
  margin: calc((30px - var(--touch)) / 2);
}
.pane-header :deep(.bell-btn) svg {
  width: 18px;
  height: 18px;
}
.pane-header .header-bell :deep(.bell-btn) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--radius-sm);
  background: transparent;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  transition: color 120ms var(--ease), transform 120ms var(--ease);
}
.pane-header .header-bell :deep(.bell-btn:hover) {
  color: var(--fg);
}
.pane-header .header-bell :deep(.bell-btn:active) { transform: scale(0.96); }
.pane-header .header-bell :deep(.bell-btn.has-unread) { color: var(--accent); }
.pane-header :deep(.btn-icon:active),
.pane-header :deep(.model-picker-btn:active),
.pane-header :deep(.archive-btn:active) {
  transform: scale(0.96);
  background: transparent;
}
@media (max-width: 768px) {
  /* Narrow: the hamburger joins the row, so a single row would have to fit
     hamburger + title + brand + actions + bell across ~360px and the title is
     what would lose. Instead the chrome keeps row 1 - hamburger, brand, actions -
     and the title gets row 2 to itself, full width, where its two-line clamp has
     somewhere to go. Row 2 only exists when there is a title, because
     `.header-title` is not rendered otherwise.
     The columns are content-sized here rather than equal `1fr` tracks: with the
     actions unable to shrink, equal tracks would let them overrun the middle
     column and sit on top of the mark. The mark centres in the space left
     between the hamburger and the actions. */
  .pane-header {
    height: auto;
    grid-template-columns: auto minmax(0, 1fr) auto;
    padding-left: calc(var(--space-3) + var(--safe-left));
    padding-right: calc(var(--space-3) + var(--safe-right));
    row-gap: var(--space-1);
  }
  .header-hamburger {
    display: flex;
    grid-column: 1;
    grid-row: 1;
    justify-self: start;
  }
  .header-center {
    grid-column: 2;
    grid-row: 1;
    justify-content: center;
    /* The track shrinks but the wordmark does not, so on the tightest headers -
       an automation detail at 375px, where the trail already holds Run now,
       overflow and the bell, and worse again at a raised font scale - it
       overran the hamburger and the actions. Let it disappear instead: it is
       decoration plus a reload shortcut, and a clipped control is a lost
       action. Same trade the split-view rule above makes. */
    min-width: 0;
    overflow: hidden;
  }
  .header-trail {
    grid-column: 3;
    grid-row: 1;
  }
  .header-title {
    grid-column: 1 / -1;
    grid-row: 2;
    text-align: left;
    min-width: 0;
  }
  .header-bell { display: flex; }
  :deep(.header-left) { min-width: 0; }
  :deep(.header-actions) {
    flex-shrink: 0;
    gap: var(--space-1);
  }
  :deep(.pane-title) {
    flex: 1 1 100%;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    /* Token, not 12px: a literal here does not answer the Appearance font-scale
       setting, so raising the scale grew every other string but this one. */
    font-size: var(--text-sm);
    line-height: 1.2;
    white-space: normal;
  }
}
</style>
