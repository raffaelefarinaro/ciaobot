<template>
  <!-- One automation on an Automations page: the shared page row (ListRow),
       its title over one muted line (cadence and where it runs; for a loop, its
       chat, cadence and end), and the end column: the last run (a loop's next
       one) with its state as a shape and, where it wants the owner, a word. A
       paused one says so with a tag and shows a state only when it wants a look
       (the engine turns an automation off when its chat is deleted). -->
  <ListRow
    :to="`/schedules/${schedule.schedule_id}`"
    class="ov-item automation-row"
    :class="{ 'automation-row--paused': !schedule.enabled }"
  >
    <template #heading>
      <span class="list-row-title ov-title">{{ title }}</span>
    </template>
    <template #sub>{{ sub }}</template>
    <template #end>
      <span v-if="!schedule.enabled" class="automation-tag">paused</span>
      <span v-else-if="schedule.frequency === 'once'" class="automation-tag">once</span>
      <template v-if="schedule.enabled || alert">
        <span class="list-row-when automation-when" :class="{ 'automation-when--alert': alert }">
          <span v-if="state !== 'none'" class="automation-state" :title="stateLabel">
            <ActivityGlyph v-if="alert" kind="alert" />
            <ActivityGlyph v-else-if="state === 'working'" kind="working" />
            <CareGlyph v-else kind="done" :size="14" />
            <span v-if="word" class="automation-state-word">{{ word }}</span>
            <span class="sr-only">{{ stateLabel }}.</span>
          </span>
          <span class="automation-when-text">{{ when }}</span>
        </span>
      </template>
    </template>
  </ListRow>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import ListRow from './ListRow.vue'
import ActivityGlyph from './ActivityGlyph.vue'
import CareGlyph from './CareGlyph.vue'
import type { Schedule } from '../lib/types'
import { cadenceSummary } from '../lib/scheduleClock'
import { isOwnedLoop, loopCadenceLabel, loopEndsLabel } from '../lib/loops'
import { lastRunLabel, lastRunState, lastRunWord, relativeWhen, untilNext } from '../lib/scheduleList'
import { useProjectStore } from '../stores/projects'

const props = defineProps<{ schedule: Schedule }>()
const projects = useProjectStore()

const isLoop = computed(() => isOwnedLoop(props.schedule))

const title = computed(() => {
  const s = props.schedule
  if (s.title) return s.title
  const first = (s.prompt || '').split('\n')[0].trim()
  return first.length > 60 ? `${first.slice(0, 57)}...` : first
})

function chatTitle(chatId: string | null | undefined, fallback: string): string {
  const chat = projects.chats.find(c => c.chat_id === chatId)
  return chat ? chat.title || 'Untitled chat' : fallback
}

// Where each run lands, in the sub-line's words.
function delivery(s: Schedule): string {
  if (s.web_chat_id) {
    const chat = projects.chats.find(c => c.chat_id === s.web_chat_id)
    return chat ? `continues “${chat.title || 'Untitled chat'}”` : 'continues one chat'
  }
  if (s.web_project_id) {
    const project = projects.projects.find(p => p.project_id === s.web_project_id)
    if (project) return `new chat in ${project.name}`
  }
  return s.context_label ? `new chat in ${s.context_label}` : 'new chat each run'
}

const sub = computed(() => {
  const s = props.schedule
  if (isLoop.value) {
    // Not in the loaded chats yet: the engine's label is the owner's title.
    const owner = chatTitle(s.owner_chat_id, s.context_label || 'a chat that no longer exists')
    return `In “${owner}” · ${loopCadenceLabel(s)} · ${loopEndsLabel(s, Date.now())}`
  }
  // Ciaobot's own routines run where the engine puts them; their cadence is the whole story.
  if (s.scope === 'system') return cadenceSummary(s)
  return `${cadenceSummary(s)} · ${delivery(s)}`
})

const state = computed(() => lastRunState(props.schedule, id => projects.isChatStreaming(id)))
const alert = computed(() => state.value === 'missed' || state.value === 'failed')
const word = computed(() => lastRunWord(props.schedule, state.value))
const stateLabel = computed(() => lastRunLabel(props.schedule, state.value))

const when = computed(() => {
  const s = props.schedule
  // A loop is about what comes next, not what already ran.
  if (isLoop.value) {
    const next = untilNext(s.next_run)
    if (!next) return 'ends soon'
    return next.startsWith('in ') ? `next ${next}` : `next run ${next}`
  }
  // A missed slot is dated by when it was due.
  if (state.value === 'missed' && s.last_expected_run) return relativeWhen(s.last_expected_run)
  // A one-off that has not run yet is dated by its run.
  if (s.frequency === 'once' && s.next_run && !s.last_dispatched_at) return relativeWhen(s.next_run)
  if (s.last_dispatched_at) return relativeWhen(s.last_dispatched_at)
  // An older record keeps only the day it last ran.
  if (s.last_triggered_on) {
    const day = new Date(`${s.last_triggered_on}T12:00:00`)
    if (!Number.isNaN(day.getTime())) return day.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
  }
  return s.frequency === 'manual' ? 'runs when you ask' : 'not run yet'
})
</script>

<style scoped>
/* A paused automation only changes the title's register, as on Home. */
.automation-row--paused :deep(.list-row-title) {
  color: var(--fg3);
  font-weight: 500;
}

.automation-when {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.automation-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

/* The word beside an alert says what happened; it carries the meaning with the
   triangle, so it takes the full-strength text, not a colour. */
.automation-when--alert .automation-state-word {
  color: var(--fg);
  font-weight: 600;
}

/* A compact squared tag, not a count. */
.automation-tag {
  padding: 0 6px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.6;
}

/* A narrow pane puts the end column under the text instead of squeezing the
   title: the last run is read after the name, not instead of it. */
@container chat-pane (max-width: 560px) {
  .automation-row {
    flex-wrap: wrap;
    row-gap: 6px;
  }
  .automation-row :deep(.list-row-main) {
    flex-basis: 100%;
  }
  .automation-row :deep(.list-row-end) {
    flex-direction: row;
    align-items: center;
    gap: 8px;
  }
}
</style>
