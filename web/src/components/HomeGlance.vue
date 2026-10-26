<template>
  <section class="home-glance" aria-labelledby="home-glance-title">
    <div class="home-glance-head">
      <h2 id="home-glance-title">At a glance</h2>
      <span class="home-glance-status">{{ status }}</span>
    </div>

    <!-- Wide panes show the inbox itself; narrow ones a single line that opens
         it through the visible bell. Both render and a pane container query
         picks one, so the switch lands on the same 940px as Home's rail. -->
    <div class="home-glance-list">
      <NotificationInboxList density="popover" :show-summary="false" />
    </div>
    <button
      type="button"
      class="home-glance-summary"
      aria-haspopup="dialog"
      @click="requestInboxOpen"
    >{{ summary }}</button>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useNotificationInboxStore } from '../stores/notificationInbox'
import { requestInboxOpen } from '../composables/useInboxPopover'
import NotificationInboxList from './NotificationInboxList.vue'

const inbox = useNotificationInboxStore()

const status = computed(() => {
  if (inbox.count > 0) return `${inbox.count} need a look`
  return inbox.allClear ? 'Nothing to review.' : ''
})

const summary = computed(() => {
  const parts = inbox.sections
    .filter((s) => s.rows.length > 0)
    .map((s) => `${s.title} ${s.rows.length}`)
  if (parts.length > 0) return parts.join(' · ')
  if (inbox.allClear) return 'Nothing to review.'
  return inbox.overall === 'checking' ? 'Checking your sources...' : 'Some sources could not be checked.'
})
</script>

<style scoped>
.home-glance {
  width: 100%;
  min-width: 0;
}

.home-glance-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin: 0 0 12px;
}

.home-glance h2 {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.02em;
}

.home-glance-status {
  color: var(--fg3);
  font-size: var(--text-sm);
}

.home-glance-summary {
  display: none;
  width: 100%;
  min-height: var(--touch);
  padding: 13px 0;
  border: 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
}

.home-glance-summary:hover {
  color: var(--accent);
}

.home-glance-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}

/* The same pane width Home's rail leaves the side column at (ChatLayout's
   HOME_RAIL_MIN_PANE_PX): keep them in sync. */
@container chat-pane (max-width: 940px) {
  .home-glance-list,
  .home-glance-status {
    display: none;
  }

  .home-glance-summary {
    display: block;
  }
}
</style>
