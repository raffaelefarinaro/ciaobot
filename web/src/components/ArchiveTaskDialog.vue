<template>
  <DialogRoot
    :open="request !== null"
    modal
    @update:open="onOpenChange"
  >
    <DialogOverlay as-child>
      <div class="confirm-backdrop" @click.self="cancel">
        <DialogContent
          class="confirm-card"
          aria-modal="true"
          @open-auto-focus="onOpenAutoFocus"
          @escape-key-down="onEscapeKeyDown"
        >
          <DialogTitle as="p" class="confirm-title">
            Archive chat
          </DialogTitle>
          <DialogDescription as="p" class="confirm-message">
            <template v-if="step === 'note'">{{ request?.awaitingReview ? 'Approving' : 'Marking' }} "{{ request?.task.title }}"{{ request?.awaitingReview ? '' : ' done' }} completes the task and archives this chat.</template>
            <template v-else-if="request?.awaitingReview">The task "{{ request.task.title }}" is waiting for your review. Approve it and archive this chat, or archive and keep the task open?</template>
            <template v-else>This chat works on the task "{{ request?.task.title }}", which is not done. Complete it and archive this chat, or archive and keep the task open?</template>
          </DialogDescription>
          <template v-if="step === 'choose'">
            <div class="confirm-actions">
              <button
                ref="cancelButton"
                class="confirm-action confirm-action--cancel"
                type="button"
                @click="cancel"
              >
                Cancel
              </button>
              <button
                class="confirm-action confirm-action--cancel"
                type="button"
                @click="request?.resolve('keep')"
              >
                Keep task open
              </button>
              <button
                class="confirm-action confirm-action--primary"
                type="button"
                @click="beginNote"
              >
                Complete task…
              </button>
            </div>
          </template>
          <template v-else>
            <label class="archive-task-label" for="archive-task-resolution">How was this completed? (optional)</label>
            <textarea
              id="archive-task-resolution"
              ref="noteField"
              v-model="draft"
              class="archive-task-note"
              rows="3"
              :disabled="busy"
            ></textarea>
            <p v-if="error" class="archive-task-error" role="alert">{{ error }}</p>
            <div class="confirm-actions">
              <button
                class="confirm-action confirm-action--cancel"
                type="button"
                :disabled="busy"
                @click="back"
              >
                Back
              </button>
              <button
                class="confirm-action confirm-action--primary"
                type="button"
                :disabled="busy"
                @click="submit()"
              >
                {{ busy ? 'Saving…' : (request?.awaitingReview ? 'Approve Done' : 'Mark done') }}
              </button>
            </div>
          </template>
        </DialogContent>
      </div>
    </DialogOverlay>
  </DialogRoot>
</template>

<script setup lang="ts">
/**
 * The three-way question when archiving a chat that holds an open board task:
 * complete the task (closure note, then archive), keep it open, or cancel.
 * Rendered once in App.vue and driven by `askArchiveTask`, like ConfirmDialog.
 * The completion calls are the rail note's own (`lib/taskClosure`).
 *
 * The closure note is optional and one action completes: a blank note
 * completes the task without a resolution.
 */
import {
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogRoot,
  DialogTitle,
} from 'reka-ui'
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { pendingArchiveTask } from '../lib/archiveTaskChoice'
import { approveTask, markTaskDone, taskClosureErrorMessage } from '../lib/taskClosure'
import { useTaskSignalsStore } from '../stores/taskSignals'

const request = pendingArchiveTask
const taskSignals = useTaskSignalsStore()
const cancelButton = ref<HTMLButtonElement | null>(null)
const noteField = ref<HTMLTextAreaElement | null>(null)
const step = ref<'choose' | 'note'>('choose')
const draft = ref('')
const busy = ref(false)
const error = ref('')

watch(request, () => {
  step.value = 'choose'
  draft.value = ''
  busy.value = false
  error.value = ''
})

function cancel() {
  if (busy.value) return
  request.value?.resolve('cancel')
}

function onOpenChange(open: boolean) {
  if (!open) cancel()
}

function onOpenAutoFocus(event: Event) {
  event.preventDefault()
  nextTick(() => {
    if (request.value) cancelButton.value?.focus()
  })
}

function onEscapeKeyDown(event: KeyboardEvent) {
  event.preventDefault()
  cancel()
}

function beginNote() {
  step.value = 'note'
  error.value = ''
  nextTick(() => noteField.value?.focus())
}

function back() {
  step.value = 'choose'
  error.value = ''
  nextTick(() => cancelButton.value?.focus())
}

async function submit() {
  const req = request.value
  if (!req || busy.value) return
  const resolution = draft.value.trim()
  const kind = req.awaitingReview ? 'approve' : 'mark-done'
  busy.value = true
  error.value = ''
  let ok = false
  try {
    if (kind === 'approve') await approveTask(req.task, req.workspace, resolution)
    else await markTaskDone(req.task, req.workspace, resolution)
    ok = true
  } catch (e) {
    error.value = taskClosureErrorMessage(e, kind)
  }
  // Either way the record is newer than what the caller read: a refusal means
  // it moved on, a success moved it to Done.
  await taskSignals.reload(req.workspace)
  busy.value = false
  if (ok) req.resolve(kind === 'approve' ? 'approved' : 'marked-done')
  else nextTick(() => noteField.value?.focus())
}

onBeforeUnmount(() => {
  request.value?.resolve('cancel')
})
</script>

<style scoped>
.confirm-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  background: rgb(0 0 0 / 55%);
}
.confirm-card {
  width: 100%;
  max-width: 30rem;
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--fg);
  box-shadow: 0 1.5rem 3rem rgb(0 0 0 / 45%);
}
.confirm-title {
  margin: 0 0 var(--space-2);
  font-weight: 600;
}
.confirm-message {
  margin: 0 0 var(--space-4);
  color: var(--fg2);
  white-space: pre-line;
}
.confirm-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--space-2);
}
.confirm-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 6rem;
  min-height: 2.75rem;
  padding: 0 var(--space-3);
  border: 1px solid transparent;
  border-radius: var(--radius);
  color: var(--fg);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
  transition:
    background 120ms var(--ease),
    border-color 120ms var(--ease),
    color 120ms var(--ease),
    transform 120ms var(--ease);
}
.confirm-action:active {
  transform: scale(0.98);
}
.confirm-action:disabled {
  opacity: 0.5;
  cursor: default;
}
.confirm-action--cancel {
  border-color: var(--border-strong);
  background: var(--bg3);
}
.confirm-action--cancel:hover:not(:disabled) {
  background: var(--border-strong);
}
.confirm-action--primary {
  border-color: var(--accent);
  background: var(--accent);
  color: var(--on-accent);
}
.confirm-action--primary:hover:not(:disabled) {
  border-color: var(--accent-strong);
  background: var(--accent-strong);
}
.archive-task-label {
  display: block;
  margin-bottom: var(--space-1);
  color: var(--fg2);
  font-size: 0.85em;
}
.archive-task-note {
  display: block;
  width: 100%;
  min-height: 2.75rem;
  margin-bottom: var(--space-3);
  padding: var(--space-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg);
  font: inherit;
  resize: vertical;
  box-sizing: border-box;
}
.archive-task-error {
  margin: 0 0 var(--space-3);
  color: var(--error);
}
</style>
