<template>
  <header
    class="pane-header"
    :class="{
      'pane-header--no-center': !hasCenter,
      'pane-header--tag-title': !hasTitle && !!pageTag,
      'pane-header--compact-page': !hasTitle && !!pageTag,
      'pane-header--page-actions': !!$slots.actions,
      'pane-header--overflow': phoneOverflow,
    }"
  >
    <!-- Phones only (the sidebar is a drawer there, so its back / forward
         pair is out of sight): the menu button, then Back when there is
         in-app history to go back to. -->
    <div class="header-lead">
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
      <button
        v-if="historyNav.canBack.value"
        class="header-back touch-hit"
        :aria-label="historyNav.backLabel.value"
        :title="historyNav.backLabel.value"
        @click="historyNav.back"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6"/></svg>
      </button>
    </div>
    <div v-if="hasTitle" class="header-title">
      <slot name="title" />
    </div>
    <div class="header-center" :class="{ 'header-center--with-title': hasTitle }">
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
      <!-- Phones only: the sidebar (and its bell) is a drawer there. The drawer
           is already closed while this header is visible, so a followed row has
           nothing to close. -->
      <NotificationBell class="header-bell" variant="sheet" />
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed, useSlots } from 'vue'
import NotificationBell from './NotificationBell.vue'
import { useHistoryNav } from '../composables/useHistoryNav'

const props = withDefaults(defineProps<{
  activeBgAgents?: number
  /** Short marker for the current view: 'home', 'settings', 'automations', … */
  pageTag?: string
  /** Phones only: Back and the bell move into the view's own overflow menu, so
   *  this header hides them. Other views keep both. */
  phoneOverflow?: boolean
}>(), { phoneOverflow: false })
defineEmits<{ 'open-sidebar': [] }>()

const slots = useSlots()
const historyNav = useHistoryNav()
// An empty `.header-title` would still claim the left grid track and, on mobile,
// a second row. Views with no title of their own (home, settings, the automations
// list) drop the element and let the page tag name them instead.
const hasTitle = computed(() => !!slots.title)
// The middle track only earns its symmetric siblings when it has something
// to centre.
const hasCenter = computed(() => !!props.pageTag)
</script>

<style scoped>
.pane-header {
  /* Keep the page-tag track between the shrinkable title and the actions.
     The trailing 1fr has an automatic minimum so controls never get crushed. */
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto 1fr;
  align-items: center;
  /* Match the sidebar header: 44px controls + 8px vertical padding + border. */
  height: calc(61px + var(--safe-top));
  /* Horizontal padding follows the page grid (App.vue --page-*), so the title
     starts where the page content starts and the actions end where its rail
     ends, instead of both hugging the pane edges. */
  padding: calc(var(--space-2) + var(--safe-top)) var(--page-inset) var(--space-2);
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  column-gap: var(--space-2);
  flex-shrink: 0;
  box-sizing: border-box;
}

/* Nothing in the middle column to centre, so there is nothing for the symmetric
   side tracks to buy - and they are not free: `1fr` on the trailing track holds
   back half the spare width for actions that only need their own size, while
   the title ellipses in the other half. Item headers have no page tag: size
   the trail to its content and give the remainder to the title. */
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
/* A view with no title of its own (Home, Settings, Automations, Memory)
   shows its page tag as that
   title, on the left, where every other page's title sits - not as a pill in
   the middle of the header.

   The viewport condition is load-bearing, not belt-and-braces. This is the
   desktop header: two columns, no lead. The phone header (the `max-width:
   768px` block below) is a different grid whose column 1 already holds the
   hamburger, and it only shows on a phone - but `chat-pane` is the full
   viewport there, because the sidebar is a fixed drawer off the flow. So a
   window between 600 and 768px satisfied both rules at once: the tag became a
   visible left-hand title *into the hamburger's own grid cell*, and the two
   printed on top of each other, at exactly the widths where the phone layout
   had just been given room to breathe. Below 600px the tag is screen-reader
   only. */
@container chat-pane (min-width: 600px) {
  @media (min-width: 769px) {
    .pane-header--tag-title {
      grid-template-columns: minmax(0, 1fr) auto;
    }
    .pane-header--tag-title .header-center {
      grid-column: 1;
      justify-self: start;
    }
    .pane-header--tag-title .header-trail {
      grid-column: 2;
    }
    .pane-header--tag-title .page-tag {
      position: static;
      width: auto;
      height: auto;
      margin: 0;
      overflow: hidden;
      clip: auto;
      color: var(--fg);
      font-family: var(--font);
      font-size: var(--text-lg);
      font-weight: 650;
      letter-spacing: -0.01em;
      text-overflow: ellipsis;
    }
  }
}

/* The expanded desktop sidebar already names these pages. Keep their h2 in
   the accessibility tree, but spend header space only on real actions. */
@media (min-width: 769px) {
  :global(.chat-layout.sidebar-open .pane-header--compact-page) {
    height: 0;
    min-height: 0;
    padding: 0;
    border: 0;
    gap: 0;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-rows: 0;
  }
  :global(.chat-layout.sidebar-open .pane-header--compact-page .page-tag) {
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
  :global(.chat-layout.sidebar-open .pane-header--compact-page.pane-header--page-actions) {
    height: auto;
    grid-template-rows: auto;
    padding: var(--space-2) var(--page-inset);
  }
  :global(.chat-layout.sidebar-open .pane-header--compact-page .header-trail) {
    grid-column: 2;
    grid-row: 1;
  }
}

/* Kept in the document, not on the screen. The page now names itself next to
   its own nav icon in the sidebar, which is where it was asked for, so a second
   copy here would say the same fact twice. It stays rendered
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
.header-lead { display: none; }
/* Matches ChatLayout's `isMobile` (innerWidth < 768). The class lands on the
   bell's button, a child root, so it is reached through :deep. */
.header-trail :deep(.header-bell) { display: none; }
@media (max-width: 767px) {
  .header-trail :deep(.header-bell) { display: inline-flex; }
  /* Phones, opted in by the view (ChatPanel): Back and the bell live in the
     view's ⋯ menu instead. Hidden, not removed, so the DOM and the bell's
     anchoring stay as they are. */
  .pane-header.pane-header--overflow .header-back { display: none; }
  .pane-header--overflow .header-trail :deep(.header-bell) { display: none; }
}
.header-back,
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
.header-back:active,
.header-hamburger:active { transform: scale(0.96); }
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
.pane-header :deep(.archive-btn::before) {
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
.pane-header :deep(.archive-btn:hover) {
  background: transparent;
}
.pane-header :deep(.btn-icon:hover::before),
.pane-header :deep(.model-picker-btn:hover::before),
.pane-header :deep(.archive-btn:hover::before) {
  background: var(--bg3);
}
.pane-header :deep(.btn-icon.active::before),
.pane-header :deep(.btn-icon[aria-pressed="true"]::before) {
  background: var(--bg3);
  border: 1px solid var(--border);
}
.pane-header :deep(.btn-icon:active),
.pane-header :deep(.model-picker-btn:active),
.pane-header :deep(.archive-btn:active) {
  transform: scale(0.96);
  background: transparent;
}
@media (max-width: 768px) {
  /* Narrow: keep lead, page tag and actions in row 1. A tagged view's title
     gets row 2; untagged item headers use the single-row override below. */
  .pane-header {
    height: auto;
    grid-template-columns: minmax(0, 1fr) auto 1fr;
    /* Top padding matches the desktop/home 61px header's centring (12px here
       vs the 30px icon half-height) rather than --space-2: with 8px the
       touch-hit icons' -7px margin pulled their visual box to ~1px below the
       header edge. The extra 4px is touch-icon clearance, not room for a
       status-bar material. */
    padding-top: calc(var(--space-3) + var(--safe-top));
    padding-left: calc(var(--space-3) + var(--safe-left));
    padding-right: calc(var(--space-3) + var(--safe-right));
    row-gap: var(--space-1);
  }
  .header-lead {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    grid-column: 1;
    grid-row: 1;
    justify-self: start;
  }
  .header-back,
  .header-hamburger { display: flex; }
  .header-center {
    grid-column: 2;
    grid-row: 1;
    justify-content: center;
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
  /* Nothing to centre (the chat, a subagent, a pinned file): row 1 would hold
     only the menu, Back and the actions around an empty middle, spending a
     whole row of a phone screen on chrome. One row instead - lead, title,
     actions - with the title taking the slack between them. */
  .pane-header--no-center:has(.header-title) {
    grid-template-columns: auto minmax(0, 1fr) auto;
    /* Same as the top inset, so the two-line title block sits centred. */
    padding-bottom: var(--space-3);
  }
  .pane-header--no-center .header-center { display: none; }
  .pane-header--no-center .header-title {
    grid-column: 2;
    grid-row: 1;
  }
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
