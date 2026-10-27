<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useNotificationInboxStore } from '../stores/notificationInbox'
import { notificationPath } from '../lib/notificationInbox'
import type { NotificationRow } from '../lib/notificationInbox'
import { formatRelative } from '../lib/time'

withDefaults(defineProps<{ density?: 'popover' | 'sheet' }>(), { density: 'popover' })
const emit = defineEmits<{ navigate: [] }>()

const inbox = useNotificationInboxStore()
const router = useRouter()

const unavailable = ref(false)
const opening = ref('')
const noticeEl = ref<HTMLElement | null>(null)

const EXACT_TARGETS = new Set(['chat', 'task', 'schedule'])

// A row for one exact chat, task or schedule is re-read before it is opened, so
// a target that vanished since the projection never opens a dead route. The read
// is the owner's own coalesced refresh: no POST, no markRead. A failed read keeps
// the row projected, so the click navigates and the destination reports its own
// error; "gone" is only ever claimed from a successful read.
async function open(row: NotificationRow, ev: MouseEvent) {
  if (ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return
  if (!EXACT_TARGETS.has(row.destination.kind)) {
    emit('navigate')
    return
  }
  ev.preventDefault()
  if (opening.value) return
  opening.value = row.key
  unavailable.value = false
  try {
    await inbox.refresh([row.source])
    if (inbox.rows.some((r) => r.key === row.key)) {
      await router.push(notificationPath(row.destination))
      emit('navigate')
    } else {
      unavailable.value = true
      await nextTick()
      noticeEl.value?.focus()
    }
  } finally {
    opening.value = ''
  }
}
</script>

<template>
  <div class="notif-inbox" :class="`notif-inbox--${density}`">
    <p
      v-if="unavailable"
      ref="noticeEl"
      tabindex="-1"
      role="status"
      data-testid="inbox-unavailable"
      class="notif-unavailable"
    >That item is no longer available. The list has been refreshed.</p>

    <p role="status" data-testid="inbox-summary" class="notif-summary">
      <template v-if="inbox.count > 0">
        {{ inbox.count }} {{ inbox.count === 1 ? 'item needs' : 'items need' }} a look.<template
          v-if="inbox.overall !== 'ready'"
        > Some sources could not be fully checked.</template>
      </template>
      <template v-else-if="inbox.allClear">
        <span data-testid="inbox-empty-clear">You're all caught up.</span>
      </template>
      <template v-else-if="inbox.overall === 'checking'">
        <span data-testid="inbox-empty-checking">Checking your sources...</span>
      </template>
      <template v-else-if="inbox.overall === 'unavailable'">
        <span data-testid="inbox-empty-unavailable">Some sources could not be checked, so nothing is confirmed clear.</span>
      </template>
      <template v-else>
        <span data-testid="inbox-empty-stale">Nothing was found at the last check, but some sources are out of date.</span>
      </template>
    </p>

    <ul v-if="inbox.sources.some(s => s.status !== 'ready')" aria-label="Source status" class="notif-sources">
      <template v-for="source in inbox.sources" :key="source.source">
        <li v-if="source.status !== 'ready'" :data-testid="`source-${source.source}`" class="notif-source">
          <span v-if="source.status === 'checking'">{{ source.label }}: checking...</span>
          <span v-else-if="source.status === 'stale'">{{ source.label }}: showing the last successful check{{
            source.lastSuccessAt ? ' ' + formatRelative(source.lastSuccessAt) : ''
          }}. Could not refresh.</span>
          <span v-else>{{ source.label }}: could not be checked.</span>
          <button
            v-if="source.status !== 'checking'"
            type="button"
            class="notif-retry"
            :aria-label="`Retry checking ${source.label}`"
            :disabled="inbox.refreshing"
            @click="inbox.retry(source.source)"
          >Retry</button>
        </li>
      </template>
    </ul>

    <template v-for="section in inbox.sections" :key="section.section">
      <section v-if="section.rows.length > 0" :aria-labelledby="`notif-h-${section.section}`" class="notif-section">
        <h3 :id="`notif-h-${section.section}`">
          {{ section.title }} <span class="notif-section-count">{{ section.rows.length }}</span>
        </h3>
        <ul class="notif-list">
          <li v-for="row in section.rows" :key="row.key" :data-testid="`row-${row.key}`">
            <RouterLink
              class="notif-row"
              :class="{ 'notif-row--attention': row.section === 'needsYou' }"
              :to="notificationPath(row.destination)"
              @click.capture="open(row, $event)"
            >
              <span class="notif-title">{{ row.title }}</span>
              <span class="notif-reason">{{ row.reason }}</span>
              <span class="notif-meta">
                <template v-if="row.unread">
                  <span class="notif-unread" aria-hidden="true"></span>
                  <span class="sr-only">Unread</span>
                </template>
                <time v-if="row.timestamp" :datetime="row.timestamp">{{ formatRelative(row.timestamp) }}</time>
                <span v-if="row.freshness === 'stale'" class="notif-stale">May be out of date</span>
              </span>
            </RouterLink>
          </li>
        </ul>
      </section>
    </template>
  </div>
</template>

<style scoped>
.notif-inbox {
  min-width: 0;
}

.notif-summary {
  margin: 0 0 12px;
  padding-top: var(--space-3);
  color: var(--fg2);
  font-size: var(--text-sm);
}

.notif-unavailable {
  margin: 0 0 12px;
  padding: 8px 12px;
  border: 1px solid var(--warning);
  border-radius: var(--radius-xs);
  color: var(--warning);
  font-size: var(--text-sm);
}

.notif-unavailable:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.notif-sources {
  margin: 0 0 16px;
  padding: 0;
  list-style: none;
}

.notif-source {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  color: var(--fg2);
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
}

.notif-retry {
  min-height: var(--touch);
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--fg);
  font: inherit;
  cursor: pointer;
}

.notif-retry:disabled {
  color: var(--fg3);
  cursor: default;
}

.notif-retry:focus-visible,
.notif-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}

.notif-section h3 {
  margin: 16px 0 8px;
  color: var(--fg);
  font-size: var(--text-sm);
  font-weight: 650;
}

.notif-section-count {
  color: var(--fg3);
  font-weight: 500;
}

.notif-list {
  margin: 0;
  padding: 0;
  list-style: none;
  border-top: 1px solid var(--border);
}

.notif-list li {
  border-bottom: 1px solid var(--border);
}

.notif-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-height: var(--touch);
  padding: 13px 0;
  color: var(--fg);
  text-decoration: none;
}

.notif-title {
  font-size: calc(13px * var(--font-scale));
  font-weight: 650;
  overflow-wrap: anywhere;
  transition: color 120ms var(--ease);
}

.notif-row--attention .notif-title {
  color: var(--warning);
}

.notif-row:hover .notif-title {
  color: var(--accent);
}

.notif-reason {
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.notif-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--fg3);
  font-size: var(--text-sm);
}

.notif-unread {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
}

.notif-stale {
  color: var(--warning);
}

/* Phones get the larger target the sheet is designed around. */
.notif-inbox--sheet .notif-row {
  min-height: 52px;
}

@media (prefers-reduced-motion: reduce) {
  .notif-title {
    transition: none;
  }
}
</style>
