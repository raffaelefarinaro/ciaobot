<template>
  <div class="page-grid page-grid--single automations-page">
    <div class="page-main">
      <section class="ov-section" aria-labelledby="ov-loops-title">
        <div class="ov-head ov-head--stacked">
          <h2 id="ov-loops-title" tabindex="-1" data-page-heading>Loops</h2>
          <p class="ov-intro">A loop runs inside one chat and ends on its own. Ask a chat: “check the build every 5 minutes for the next hour”.</p>
        </div>
        <AutomationListRow v-for="s in loops" :key="s.schedule_id" :schedule="s" />
        <div v-if="!loops.length" class="ov-empty automations-empty">
          <p class="automations-empty-title">No loops running</p>
          <p>
            Loops are made by a chat when you ask it to keep checking something:
            it repeats inside that chat on a cadence and stops when its time or its
            runs are used up. To end one early, open it and press Stop loop.
          </p>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
/*
 * The Automations Loops page: the workspace's chat-owned loops (lib/loops.ts),
 * soonest next run first. A loop is model-made and bounded and is never
 * paused, so there is no create action here; a chat makes one.
 */
import { computed } from 'vue'
import AutomationListRow from './AutomationListRow.vue'
import { useWorkspaceSchedules } from '../composables/useWorkspaceSchedules'
import { groupAutomations } from '../lib/scheduleList'

const schedules = useWorkspaceSchedules()
const loops = computed(() => groupAutomations(schedules.value).loops)
</script>

<style scoped src="./overviewSections.css"></style>
<style scoped>
.ov-head h2:focus { outline: none; }

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
</style>
