<script setup lang="ts">
/**
 * Where a delegated chat comes from: the board task it works on.
 *
 * One sentence naming the task, linked to its editor on the board, then the
 * agent's state in words. When the agent reported done and the result waits for
 * review, one neutral **Approve Done** — the same completion the board's card
 * makes (`POST /api/tasks/{id}/complete` at the revision read), so the user can
 * approve from where they read the result. A task waiting on the user gets no
 * button: the composer is the answer.
 *
 * Read from `taskSignals`, the app-wide view of the active workspace's tasks. A
 * task that store does not hold (not loaded yet, or deleted) still gets the
 * sentence, from the chat's own `task_delegation` stamp, with no state.
 *
 * `variant` picks the rail's treatment or the flat note above the transcript;
 * the sentence is the same in both, never two different sentences.
 */
import { computed, ref, watch } from 'vue'
import { api } from '../lib/api'
import { taskApiErrorMessage, taskAttemptLabel } from '../lib/taskBoard'
import type { ChatInfo, Task } from '../lib/types'
import { useTaskSignalsStore } from '../stores/taskSignals'
import AppIcon from './AppIcon.vue'

const props = defineProps<{
  chat: Pick<ChatInfo, 'chat_id' | 'helper'>
  variant: 'rail' | 'note'
}>()

const taskSignals = useTaskSignalsStore()

const delegation = computed(() =>
  props.chat.helper?.kind === 'task_delegation' ? props.chat.helper : null,
)
const task = computed<Task | null>(() => taskSignals.taskForChat(props.chat))
const taskId = computed(() => task.value?.id || delegation.value?.task_id || '')

/**
 * Whether a later attempt holds the task: this chat's result is history, and
 * approving here would approve another chat's work.
 */
const superseded = computed(() => {
  const own = delegation.value?.attempt_id
  const holder = task.value?.attempt_id
  return !!own && !!holder && own !== holder
})

const statusWords = computed(() => {
  const t = task.value
  if (!t) return ''
  if (t.status === 'done') return 'Done'
  if (superseded.value) return 'A newer attempt holds this task'
  return taskAttemptLabel(t.attempt_state, t.attempt_outcome, t.attempt_detail)
})

const canApprove = computed(() => {
  const t = task.value
  return !!t && !superseded.value && taskSignals.isAwaitingReview(t)
})

const approving = ref(false)
const approveError = ref('')
watch(taskId, () => { approveError.value = '' })

async function approve(): Promise<void> {
  const t = task.value
  const workspace = taskSignals.loadedWorkspace
  if (!t || !workspace || approving.value) return
  approving.value = true
  approveError.value = ''
  try {
    await api.post(`/api/tasks/${encodeURIComponent(t.id)}/complete`, {
      workspace,
      expected_revision: t.revision,
    })
  } catch (e) {
    approveError.value = (e as { status?: number } | null)?.status === 409
      ? 'This task changed since it was read. Check it and approve again.'
      : taskApiErrorMessage(e, 'Could not approve the task')
  } finally {
    approving.value = false
  }
  // Either way the record is newer than what this note drew: a refusal means it
  // moved on, a success moved it to Done.
  await taskSignals.reload(workspace)
}
</script>

<template>
  <div v-if="taskId" class="task-origin" :class="`task-origin--${variant}`">
    <AppIcon class="task-origin-icon" name="activity" :size="16" />
    <div class="task-origin-body">
      <p class="task-origin-line">
        <template v-if="task">This chat works on the task <router-link class="task-origin-link" :to="{ path: '/tasks', query: { task: taskId } }">{{ task.title || taskId }}</router-link>.</template>
        <template v-else>This chat works on <router-link class="task-origin-link" :to="{ path: '/tasks', query: { task: taskId } }">a task on the board</router-link>.</template>
      </p>
      <p v-if="statusWords" class="task-origin-status">{{ statusWords }}</p>
      <button
        v-if="canApprove"
        type="button"
        class="btn-small task-origin-approve"
        :disabled="approving"
        :aria-label="`Approve the result of ${task!.title} and mark it done`"
        @click="approve"
      >{{ approving ? 'Approving…' : 'Approve Done' }}</button>
      <p v-if="approveError" class="task-origin-error" role="alert">{{ approveError }}</p>
    </div>
  </div>
</template>

<style scoped>
/* The same shape as the automation and memory-pass origin notes in ChatPanel. */
.task-origin {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg2);
  line-height: 1.45;
}
.task-origin--rail {
  margin: 0 0 18px;
  padding: 10px 12px;
}
.task-origin--note {
  /* Clear of the header rule, and of the Work details tab that sits over the
     note's top-right corner (34px, 44px on touch) while the rail is hidden. */
  margin: 12px 0;
  padding: 9px calc(34px + 12px) 9px 12px;
  font-size: var(--text-sm);
}
.task-origin-icon { flex: none; margin-top: 2px; color: var(--fg3); }
.task-origin-body { min-width: 0; }
.task-origin-line { margin: 0; overflow-wrap: anywhere; }
.task-origin-link {
  color: var(--fg);
  font-weight: 650;
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 3px;
}
.task-origin-link:hover { color: var(--fg); text-decoration-color: currentColor; }
.task-origin-link:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}
.task-origin-status {
  margin: 2px 0 0;
  color: var(--fg3);
  font-size: var(--text-sm);
}
.task-origin-approve { margin-top: 8px; }
.task-origin-approve:disabled { opacity: 0.6; cursor: default; }
.task-origin-error {
  margin: 6px 0 0;
  color: var(--fg2);
  font-size: var(--text-sm);
}
/* The link is inline in a sentence, which the target-size rule exempts. */
@media (pointer: coarse) {
  .task-origin-approve { min-height: 44px; }
  .task-origin--note { padding-right: calc(var(--touch) + 12px); }
}
</style>
