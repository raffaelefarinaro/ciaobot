<template>
  <div class="page-grid automations-page">
    <div class="page-main">
      <h2 class="sr-only" tabindex="-1" data-page-heading>Schedules</h2>

      <!-- A missed routine keeps its row (the alert and "missed" on it); this
           short notice says so before the lists and runs them all at once. -->
      <section v-if="missed.length" class="ov-section automations-missed" aria-labelledby="ov-missed-title">
        <div class="ov-head">
          <h2 id="ov-missed-title"><ActivityGlyph kind="alert" />Missed</h2>
          <button
            type="button"
            class="btn-small ov-run-all"
            :disabled="runningAllMissed"
            :aria-busy="runningAllMissed"
            @click.stop="emit('run-all-missed')"
          >{{ runningAllMissed ? 'Starting…' : 'Run all' }}</button>
        </div>
        <p class="ov-intro automations-missed-text">
          {{ missedSentence }} Time-of-day runs are caught up on the next launch, or run them now.
        </p>
      </section>

      <section v-if="groups.system.length" class="ov-section" aria-labelledby="ov-system-title">
        <div class="ov-head ov-head--stacked">
          <h2 id="ov-system-title">System routines</h2>
          <p class="ov-intro">Ciaobot's own upkeep. They run on their own and never need you.</p>
        </div>
        <AutomationListRow v-for="s in groups.system" :key="s.schedule_id" :schedule="s" />
      </section>

      <section class="ov-section" aria-labelledby="ov-routines-title">
        <div class="ov-head ov-head--stacked ov-head--action">
          <div class="ov-head-text">
            <h2 id="ov-routines-title">Your routines</h2>
            <p v-if="groups.routines.length" class="ov-intro">Sorted by next run. Paused ones last.</p>
          </div>
          <button type="button" class="btn-primary automations-new" @click="emit('new')">New routine</button>
        </div>
        <AutomationListRow v-for="s in groups.routines" :key="s.schedule_id" :schedule="s" />
        <div v-if="!groups.routines.length" class="ov-empty automations-empty">
          <p class="automations-empty-title">No routines yet</p>
          <p>
            A routine runs a prompt for you on a schedule: at a time of day, every
            few minutes, or once. Each run opens a new chat in a project, or
            continues one chat you pick. Start one with New routine.
          </p>
        </div>
      </section>
    </div>

    <aside class="page-rail">
      <DockedWindow id="ov-recent-title" title="Recent runs">
        <div v-if="recentRuns.length" class="docked-list docked-list--end">
          <RouterLink
            v-for="r in recentRuns"
            :key="r.id"
            :to="`/chat/${r.chatId}`"
            class="docked-row"
          >
            <span class="docked-row-title">{{ r.title }}</span>
            <span class="docked-row-detail automations-run-when">{{ relativeWhen(r.lastRunAt) }}<template v-if="projects.isChatStreaming(r.chatId)"> · running</template></span>
          </RouterLink>
        </div>
        <p v-else class="automations-rail-empty">No runs yet. Each run's chat shows here once it starts.</p>
      </DockedWindow>
    </aside>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import ActivityGlyph from './ActivityGlyph.vue'
import AutomationListRow from './AutomationListRow.vue'
import DockedWindow from './DockedWindow.vue'
import { useWorkspaceSchedules } from '../composables/useWorkspaceSchedules'
import { groupAutomations, relativeWhen, scheduleTitle } from '../lib/scheduleList'
import { isOwnedLoop } from '../lib/loops'
import { useProjectStore } from '../stores/projects'

defineProps<{ runningAllMissed: boolean }>()
const emit = defineEmits<{ (e: 'run-all-missed'): void; (e: 'new'): void }>()

const projects = useProjectStore()
const schedules = useWorkspaceSchedules()
const groups = computed(() => groupAutomations(schedules.value))

// Missed is a wall-clock slot that passed with no run (the server's `missed`);
// a loop never has one.
const missed = computed(() => schedules.value.filter(s => s.missed && !isOwnedLoop(s)))
const missedSentence = computed(() => {
  const names = missed.value.map(scheduleTitle)
  if (names.length === 1) return `${names[0]} did not run when it was due.`
  if (names.length === 2) return `${names[0]} and ${names[1]} did not run when they were due.`
  return `${names.length} routines did not run when they were due.`
})

const recentRuns = computed(() =>
  schedules.value
    .filter(s => s.last_dispatched_at && s.last_run_chat_id)
    .map(s => ({
      id: s.schedule_id,
      title: scheduleTitle(s),
      lastRunAt: s.last_dispatched_at!,
      chatId: s.last_run_chat_id!,
    }))
    // Compared as instants, not as strings. `last_dispatched_at` is written in
    // two offsets (UTC from the interval and run-now paths, the entry's own
    // timezone from the wall-clock tick and catch-up), and ISO strings only sort
    // correctly at a fixed offset.
    .sort((a, b) => Date.parse(b.lastRunAt) - Date.parse(a.lastRunAt))
    .slice(0, 5),
)
</script>

<style scoped src="./overviewSections.css"></style>
<style scoped>
.page-main {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.automations-missed .ov-head h2 {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}

.automations-missed-text {
  margin: 0;
  color: var(--fg2);
}

.automations-new {
  flex: none;
  min-height: 36px;
}

@media (pointer: coarse) {
  .automations-new { min-height: var(--touch); }
}

.automations-empty-title {
  margin: 0 0 4px;
  color: var(--fg);
  font-size: var(--text-base);
  font-weight: 600;
}

.automations-empty p:last-child {
  max-width: 62ch;
  margin: 0;
  line-height: 1.5;
}

.automations-run-when {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}

.automations-rail-empty {
  margin: 0;
  padding: 13px 0 0;
  color: var(--fg3);
  font-size: var(--text-sm);
  line-height: 1.45;
}
</style>
