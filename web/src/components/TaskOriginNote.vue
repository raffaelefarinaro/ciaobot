<script setup lang="ts">
/**
 * Where a chat's tasks come from: the board task it works on, and the board
 * tasks it is linked to.
 *
 * Rows of Chat details' Origin section: the task title as one row linking to
 * its editor on the board, the agent's state in its detail line. When the
 * agent reported done and the result waits for review, **Approve Done**, the
 * panel's one filled primary action, under that row — the same
 * completion the board's card makes (`POST /api/tasks/{id}/complete` at the
 * revision read), so the user can approve from where they read the result. A
 * task waiting on the user gets no button: the composer is the answer.
 *
 * The completion asks for a note in the field, and the note is optional: one
 * action completes the task, and a blank note completes it without a
 * resolution — the same contract as the board's completion sheet. The form
 * states its consequence and is the confirmation: **Mark done and archive**
 * needs no second modal.
 *
 * A task the chat is only *linked* to (`task link-chat`) is named in its own
 * read-only row (the title, the detail Linked task), with no state and no completion: the chat did not run
 * that task, so it has nothing to report and nothing to close.
 *
 * Read from `taskSignals`, the app-wide view of the active workspace's tasks. A
 * task that store does not hold (not loaded yet, or deleted) still gets the
 * row, from the chat's own `task_delegation` stamp, with no state.
 */
import { computed, nextTick, ref, useId, watch } from 'vue'
import { taskAttemptLabel } from '../lib/taskBoard'
import { approveTask, markTaskDone, taskClosureErrorMessage } from '../lib/taskClosure'
import type { ChatInfo, Task } from '../lib/types'
import { useTaskSignalsStore } from '../stores/taskSignals'
import { useItemPeekStore } from '../stores/itemPeek'
import AppIcon from './AppIcon.vue'
import { isPlainLeftClick } from '../lib/appLinks'

const props = defineProps<{
  chat: Pick<ChatInfo, 'chat_id' | 'helper'>
}>()

const taskSignals = useTaskSignalsStore()

const delegation = computed(() =>
  props.chat.helper?.kind === 'task_delegation' ? props.chat.helper : null,
)
const task = computed<Task | null>(() => taskSignals.taskForChat(props.chat))
const taskId = computed(() => task.value?.id || delegation.value?.task_id || '')

/**
 * The tasks this chat is linked to outside delegation (`task link-chat`).
 *
 * A link is a relation, not a lifecycle: the row below is read-only. The
 * linked task is deliberately not the `task` above, so the state, the
 * Approve Done and the Mark done that follow it stay the delegated chat's.
 * A task the chat is both delegated to and linked to is said once.
 */
const linkedTasks = computed<Task[]>(() =>
  taskSignals.linkedTasksForChat(props.chat).filter(t => t.id !== taskId.value),
)

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

const pillTone = computed(() => {
  if (task.value?.status === 'done') return 'done'
  return superseded.value ? 'muted' : 'warn'
})

const canApprove = computed(() => {
  const t = task.value
  return !!t && !superseded.value && taskSignals.isAwaitingReview(t)
})

/**
 * The user's override: the task is done, whatever the attempt last said
 * (unfinished, waiting on input, interrupted). Only for the chat that holds the
 * task, and never where Approve Done already says the same thing.
 */
const canMarkDone = computed(() => {
  const t = task.value
  return !!t && t.status !== 'done' && !superseded.value && !taskSignals.isAwaitingReview(t)
})

/**
 * The task link opens its editor over this chat, the same as a task link in a
 * message. A modified click is left to the browser, which opens the task page
 * in a new tab.
 */
function onTaskLink(event: MouseEvent, id: string): void {
  if (!isPlainLeftClick(event)) return
  event.preventDefault()
  useItemPeekStore().openTask(id)
}

const emit = defineEmits<{ (e: 'marked-done'): void }>()

const approving = ref(false)
const approveError = ref('')
const noteOpen = ref(false)
const noteDraft = ref('')
const noteField = ref<HTMLTextAreaElement | null>(null)
const resolutionId = useId()
/** The button that opens the prompt — one of the two, never both at once. */
const closureButton = ref<HTMLButtonElement | null>(null)
watch(taskId, () => {
  approveError.value = ''
  noteOpen.value = false
  noteDraft.value = ''
})

/**
 * Open the closure prompt with the keyboard already in it: the button that
 * opened it leaves the DOM, so focus would otherwise fall back to the body
 * and the note — the common case, an empty one — would be typed nowhere.
 */
function beginClosure(): void {
  noteOpen.value = true
  noteDraft.value = ''
  approveError.value = ''
  nextTick(() => noteField.value?.focus())
}

/**
 * Close the prompt and hand the keyboard back to the button that opened it:
 * the note field leaves the DOM with the prompt, and focus would otherwise
 * fall back to the body.
 */
function cancelClosure(): void {
  noteOpen.value = false
  nextTick(() => closureButton.value?.focus())
}

async function markDone(resolution: string): Promise<void> {
  const t = task.value
  const workspace = taskSignals.loadedWorkspace
  if (!t || !workspace || approving.value) return
  approving.value = true
  approveError.value = ''
  let done = false
  try {
    await markTaskDone(t, workspace, resolution)
    done = true
    noteOpen.value = false
  } catch (e) {
    approveError.value = taskClosureErrorMessage(e, 'mark-done')
    // The write disabled the field, and a browser that moves the focus off a
    // disabled control leaves the keyboard nowhere: put it back in the note.
    nextTick(() => noteField.value?.focus())
  } finally {
    approving.value = false
  }
  await taskSignals.reload(workspace)
  if (done) emit('marked-done')
}

async function approve(resolution: string): Promise<void> {
  const t = task.value
  const workspace = taskSignals.loadedWorkspace
  if (!t || !workspace || approving.value) return
  approving.value = true
  approveError.value = ''
  try {
    await approveTask(t, workspace, resolution)
    noteOpen.value = false
  } catch (e) {
    approveError.value = taskClosureErrorMessage(e, 'approve')
    // The write disabled the field, and a browser that moves the focus off a
    // disabled control leaves the keyboard nowhere: put it back in the note.
    nextTick(() => noteField.value?.focus())
  } finally {
    approving.value = false
  }
  // Either way the record is newer than what this note drew: a refusal means it
  // moved on, a success moved it to Done.
  await taskSignals.reload(workspace)
}

function submitNote(): void {
  const resolution = noteDraft.value.trim()
  if (canApprove.value) void approve(resolution)
  else void markDone(resolution)
}
</script>

<template>
  <div v-if="taskId || linkedTasks.length" class="task-origin">
    <template v-if="taskId">
      <a class="cd-row cd-row--link task-origin-link" :href="`/tasks/${taskId}`" @click="onTaskLink($event, taskId)">
        <AppIcon class="cd-icon" name="task" />
        <span class="cd-main">
          <span class="cd-primary cd-primary--wrap task-origin-title">{{ task ? (task.title || taskId) : 'A task on the board' }}</span>
          <span class="cd-detail cd-detail--wrap task-origin-detail">Delegated task<template v-if="statusWords"> · <span class="task-origin-status" :class="pillTone">{{ statusWords }}</span></template></span>
        </span>
        <span class="cd-trail" aria-hidden="true"><AppIcon name="arrow" /></span>
      </a>
      <div v-if="noteOpen || canApprove || canMarkDone || approveError" class="task-origin-controls">
        <template v-if="noteOpen">
          <label class="task-origin-note-label" :for="resolutionId">How was it completed? <span class="task-origin-optional">(optional)</span></label>
          <textarea
            :id="resolutionId"
            ref="noteField"
            v-model="noteDraft"
            class="task-origin-note"
            rows="3"
            placeholder="What worked, and how did you check it?"
            :disabled="approving"
          ></textarea>
          <p class="task-origin-conseq">{{ canApprove ? 'Marks the task done and archives this chat.' : 'Closes the task and archives this chat. The transcript stays in your vault.' }}</p>
          <div class="task-origin-actions">
            <button
              type="button"
              class="btn-small btn-primary"
              :disabled="approving"
              @click="submitNote()"
            >{{ approving ? 'Saving…' : (canApprove ? 'Approve Done' : 'Mark done and archive') }}</button>
            <button type="button" class="btn-small task-origin-cancel" :disabled="approving" @click="cancelClosure">Cancel</button>
          </div>
        </template>
        <button
          v-else-if="canApprove"
          ref="closureButton"
          type="button"
          class="btn-small btn-primary task-origin-approve"
          :disabled="approving"
          :aria-label="`Approve the result of ${task!.title} and mark it done`"
          @click="beginClosure"
        >Approve Done</button>
        <button
          v-else-if="canMarkDone"
          ref="closureButton"
          type="button"
          class="btn-small task-origin-approve"
          :disabled="approving"
          :aria-label="`Mark ${task!.title} done and archive this chat`"
          @click="beginClosure"
        >Mark done…</button>
        <p v-if="approveError" class="task-origin-error" role="alert">{{ approveError }}</p>
      </div>
    </template>
    <a
      v-for="linked in linkedTasks"
      :key="linked.id"
      class="cd-row cd-row--link task-origin-link task-origin-line"
      :href="`/tasks/${linked.id}`"
      @click="onTaskLink($event, linked.id)"
    >
      <AppIcon class="cd-icon" name="task" />
      <span class="cd-main">
        <span class="cd-primary cd-primary--wrap task-origin-title">{{ linked.title || linked.id }}</span>
        <span class="cd-detail task-origin-detail">Linked task</span>
      </span>
      <span class="cd-trail" aria-hidden="true"><AppIcon name="arrow" /></span>
    </a>
  </div>
</template>

<style scoped>
/* Rows of the Origin section; the section heading lives in ChatPanel. */
.task-origin { display: flex; flex-direction: column; min-width: 0; }
.task-origin-status.warn { color: var(--warning); }
.task-origin-status.done { color: var(--success); }
.task-origin-status.muted { color: var(--fg3); }
/* Approve Done / Mark done… and the completion form sit under the task row,
   indented to its text column (16px icon + 10px gap). */
.task-origin-controls { padding: 0 0 var(--space-1) 26px; }
.task-origin-conseq { margin: 6px 0 0; color: var(--fg3); font-size: var(--text-xs); }
.task-origin-optional { color: var(--fg3); }
.task-origin-cancel { margin-left: auto; }
.task-origin-approve { margin-top: 4px; }
.task-origin-note-label {
  display: block;
  margin-top: 8px;
  font-size: var(--text-xs);
}
.task-origin-note {
  width: 100%;
  margin-top: 4px;
  min-height: 44px;
}
.task-origin-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-top: 8px;
}
.task-origin-approve:disabled { opacity: 0.6; cursor: default; }
.task-origin-error {
  margin: 6px 0 0;
  color: var(--fg2);
  font-size: var(--text-sm);
}
@media (pointer: coarse) {
  .task-origin-approve,
  .task-origin-actions .btn-small { min-height: 44px; }
}
</style>
<style scoped src="./chatDetails.css"></style>
