<script setup lang="ts">
/**
 * A board task, read over the chat it was opened from. Read-only: the title
 * bar says what the task is, the body says what it holds, and one text action
 * goes to the full task page where it can be edited.
 *
 * Task links inside the body (a note that names another task) open in this
 * same peek, through the shared app-link handler.
 */
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import OverlaySheet from './OverlaySheet.vue'
import TaskNoteFeed from './TaskNoteFeed.vue'
import { handleAppLinkClick } from '../lib/appLinks'
import { renderUserMarkdown } from '../lib/safeMarkdown'
import {
  formatCardDue,
  formatCompletedAt,
  isOverdue,
  localDateKey,
  splitTaskLog,
  taskAttemptLabel,
  taskStatusLabel,
} from '../lib/taskBoard'
import { useItemPeekStore } from '../stores/itemPeek'
import { useProjectStore } from '../stores/projects'

const peek = useItemPeekStore()
const projectStore = useProjectStore()
const today = localDateKey()

const task = computed(() => peek.task)
const taskId = computed(() => (peek.target?.kind === 'task' ? peek.target.id : ''))

/** The project's name, or General for a task that belongs to none. */
const projectName = computed(() => {
  const t = task.value
  if (!t) return ''
  const general = projectStore.generalProject(projectStore.activeWorkspace)?.project_id ?? ''
  if (!t.project_id || t.project_id === general) return 'General'
  return projectStore.workspaceProjects.find((p) => p.project_id === t.project_id)?.name || t.project_id
})

const title = computed(() => task.value?.title || 'Task')

const subtitle = computed(() => {
  const t = task.value
  if (!t) return ''
  const due = formatCardDue(t.due, today)
  const late = isOverdue(t, today) ? 'Overdue' : ''
  return [taskStatusLabel(t.status), projectName.value, due, late].filter(Boolean).join(' · ')
})

const description = computed(() => (task.value ? splitTaskLog(task.value.body).description : ''))

function onBodyClick(event: MouseEvent): void {
  handleAppLinkClick(event)
}
</script>

<template>
  <OverlaySheet :open="peek.isOpen" :title="title" :subtitle="subtitle" @close="peek.close()">
    <template #actions>
      <RouterLink
        v-if="taskId"
        class="task-peek-open"
        :to="{ name: 'task-detail', params: { taskId } }"
      >Open task page</RouterLink>
    </template>

    <p v-if="peek.loading" class="hint" role="status">Loading task…</p>

    <div v-else-if="peek.notFound" class="task-peek-state">
      <p class="hint">This task isn't on the {{ projectStore.activeWorkspace }} board.</p>
      <p><RouterLink :to="{ name: 'tasks' }">Open the board</RouterLink></p>
    </div>

    <div v-else-if="peek.error" class="task-peek-state">
      <p class="task-action-error" role="alert">{{ peek.error }}</p>
      <button type="button" class="btn-chip task-chip" @click="peek.refresh()">Retry</button>
    </div>

    <div v-else-if="task" class="task-peek" @click="onBodyClick">
      <p v-if="task.attempt_state" class="task-peek-attempt">
        <span class="badge badge--muted">{{ taskAttemptLabel(task.attempt_state, task.attempt_outcome, task.attempt_detail) }}</span>
      </p>

      <section v-if="task.attempt_summary" class="task-peek-section" aria-label="Agent report">
        <!-- eslint-disable-next-line vue/no-v-html — rendered via DOMPurify -->
        <div class="task-history-summary markdown" v-html="renderUserMarkdown(task.attempt_summary)"></div>
      </section>

      <section class="task-peek-section">
        <h3 class="task-peek-heading">Description</h3>
        <!-- eslint-disable-next-line vue/no-v-html — rendered via DOMPurify -->
        <div v-if="description" class="task-peek-description markdown" v-html="renderUserMarkdown(description)"></div>
        <p v-else class="hint">No description.</p>
      </section>

      <section class="task-peek-section" aria-labelledby="task-peek-activity">
        <h3 id="task-peek-activity" class="task-peek-heading">Activity</h3>
        <TaskNoteFeed v-if="task.updates.length" :updates="task.updates" />
        <p v-else class="hint">No progress notes yet.</p>
      </section>

      <section v-if="task.status === 'done'" class="task-peek-section" aria-label="Resolution">
        <p class="hint">Completed{{ formatCompletedAt(task.completed_at) ? ` ${formatCompletedAt(task.completed_at)}` : ' (no time recorded)' }}.</p>
        <p v-if="task.resolution" class="task-resolution-text">{{ task.resolution }}</p>
        <p v-else class="hint">No resolution recorded.</p>
      </section>
    </div>
  </OverlaySheet>
</template>

<style scoped>
.task-peek {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.task-peek-attempt { margin: 0; }
.task-peek-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
}
.task-peek-heading {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  font-weight: 600;
}
.task-peek-description,
.task-history-summary,
.task-resolution-text {
  color: var(--fg);
  font-size: var(--text-sm);
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.task-resolution-text { margin: 0; white-space: pre-wrap; }
.task-peek-state {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
}
.task-peek-state p { margin: 0; }
.task-peek-open {
  color: var(--accent);
  font-size: var(--text-sm);
  text-decoration: none;
  white-space: nowrap;
}
.task-peek-open:hover { text-decoration: underline; }
.task-peek-open:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
@media (pointer: coarse) {
  .task-peek-open,
  .task-peek-state button {
    display: inline-flex;
    align-items: center;
    min-height: var(--touch);
  }
}
</style>
