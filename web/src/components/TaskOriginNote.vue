<script setup lang="ts">
/**
 * Where a chat's tasks come from: the board task it works on, and the board
 * tasks it is linked to.
 *
 * One sentence naming the task, linked to its editor on the board, then the
 * agent's state in words. When the agent reported done and the result waits for
 * review, one neutral **Approve Done** — the same completion the board's card
 * makes (`POST /api/tasks/{id}/complete` at the revision read), so the user can
 * approve from where they read the result. A task waiting on the user gets no
 * button: the composer is the answer.
 *
 * The completion asks for a note in the field, and the note is optional: one
 * action completes the task, and a blank note completes it without a
 * resolution — the same contract as the board's completion sheet.
 *
 * A task the chat is only *linked* to (`task link-chat`) is named in its own
 * read-only sentence, with no state and no completion: the chat did not run
 * that task, so it has nothing to report and nothing to close.
 *
 * Read from `taskSignals`, the app-wide view of the active workspace's tasks. A
 * task that store does not hold (not loaded yet, or deleted) still gets the
 * sentence, from the chat's own `task_delegation` stamp, with no state.
 */
import { computed, nextTick, ref, watch } from 'vue'
import { taskAttemptLabel } from '../lib/taskBoard'
import { approveTask, markTaskDone, taskClosureErrorMessage } from '../lib/taskClosure'
import type { ChatInfo, Task } from '../lib/types'
import { useTaskSignalsStore } from '../stores/taskSignals'
import { useItemPeekStore } from '../stores/itemPeek'
import AppIcon from './AppIcon.vue'
import { askConfirm } from '../lib/confirm'
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
 * The task link opens its peek over this chat, the same as a task link in a
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
  const ok = await askConfirm(
    `Mark "${t.title}" done? Its attempt is detached, the task is closed, and this chat is archived. The transcript stays in your vault.`,
    { title: 'Mark done', confirmLabel: 'Mark done and archive' },
  )
  if (!ok) return
  approving.value = true
  approveError.value = ''
  let done = false
  try {
    await markTaskDone(t, workspace, resolution)
    done = true
    noteOpen.value = false
  } catch (e) {
    approveError.value = taskClosureErrorMessage(e, 'mark-done')
    // The write disabled the field, and a browser drops the focus of an
    // element it disables: put the keyboard back in the note.
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
    // The write disabled the field, and a browser drops the focus of an
    // element it disables: put the keyboard back in the note.
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
    <AppIcon class="task-origin-icon" name="activity" :size="16" />
    <div class="task-origin-body">
      <template v-if="taskId">
        <p class="task-origin-line">
          <template v-if="task">This chat works on the task <a class="task-origin-link" :href="`/tasks/${taskId}`" @click="onTaskLink($event, taskId)">{{ task.title || taskId }}</a>.</template>
          <template v-else>This chat works on <a class="task-origin-link" :href="`/tasks/${taskId}`" @click="onTaskLink($event, taskId)">a task on the board</a>.</template>
        </p>
        <p v-if="statusWords" class="task-origin-status">{{ statusWords }}</p>
        <template v-if="noteOpen">
          <label class="task-origin-note-label" for="task-origin-resolution">How was this completed? (optional)</label>
          <textarea
            id="task-origin-resolution"
            ref="noteField"
            v-model="noteDraft"
            class="task-origin-note"
            rows="3"
            :disabled="approving"
          ></textarea>
          <div class="task-origin-actions">
            <button
              type="button"
              class="btn-small task-origin-approve"
              :disabled="approving"
              @click="submitNote()"
            >{{ approving ? 'Saving…' : (canApprove ? 'Approve Done' : 'Mark done') }}</button>
            <button type="button" class="btn-small" :disabled="approving" @click="cancelClosure">Cancel</button>
          </div>
        </template>
        <button
          v-else-if="canApprove"
          ref="closureButton"
          type="button"
          class="btn-small task-origin-approve"
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
        >Mark done</button>
        <p v-if="approveError" class="task-origin-error" role="alert">{{ approveError }}</p>
      </template>
      <p v-for="linked in linkedTasks" :key="linked.id" class="task-origin-line">This chat is linked to the task <a class="task-origin-link" :href="`/tasks/${linked.id}`" @click="onTaskLink($event, linked.id)">{{ linked.title || linked.id }}</a>.</p>
    </div>
  </div>
</template>

<style scoped>
/* The same shape as the automation and memory-pass origin notes in ChatPanel. */
.task-origin {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin: 0 0 18px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg2);
  line-height: 1.45;
}
.task-origin-icon { flex: none; margin-top: 2px; color: var(--fg3); }
.task-origin-body { min-width: 0; }
.task-origin-line { margin: 0; overflow-wrap: anywhere; }
/* Two sentences in a row — a delegated task and a linked one, or two links —
   read as two lines, not one block. */
.task-origin-line + .task-origin-line { margin-top: 2px; }
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
  margin-top: 8px;
}
.task-origin-approve:disabled { opacity: 0.6; cursor: default; }
.task-origin-error {
  margin: 6px 0 0;
  color: var(--fg2);
  font-size: var(--text-sm);
}
/* The link is inline in a sentence, which the target-size rule exempts. */
@media (pointer: coarse) {
  .task-origin-approve,
  .task-origin-actions .btn-small { min-height: 44px; }
}
</style>
