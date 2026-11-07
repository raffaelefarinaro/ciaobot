<script setup lang="ts">
/**
 * The paste-back field for a sign-in that ends on an unreachable loopback
 * page: the user copies that page's address and sends it to the engine, which
 * completes the sign-in. The value is cleared after every try and never kept.
 */
import { computed, ref, useId } from 'vue'
import { callbackRefusalCopy, looksLikeLoopbackCallback } from '../../lib/chatActions'
import { useProjectStore } from '../../stores/projects'

const props = defineProps<{ chatId: string; actionId: string; hint: string }>()
const emit = defineEmits<{ sent: [] }>()

const store = useProjectStore()
const text = ref('')
const busy = ref(false)
const error = ref('')
const inputEl = ref<HTMLInputElement | null>(null)
const inputId = useId()
const errorId = useId()
const ready = computed(() => looksLikeLoopbackCallback(text.value))

async function submit(): Promise<void> {
  if (busy.value || !ready.value) return
  // The button is about to be disabled, and that would drop the focus onto the page.
  inputEl.value?.focus()
  const url = text.value.trim()
  busy.value = true
  error.value = ''
  try {
    const result = await store.submitChatActionCallback(props.chatId, props.actionId, url)
    if (result.ok) emit('sent')
    else error.value = callbackRefusalCopy(result.reason)
  } catch {
    error.value = 'The address could not be sent. Try again.'
  } finally {
    text.value = ''
    busy.value = false
  }
}
</script>

<template>
  <form class="needs-you-paste" @submit.prevent="submit">
    <label :for="inputId" class="needs-you-paste-label">{{ hint }}</label>
    <div class="needs-you-paste-row">
      <input
        :id="inputId"
        ref="inputEl"
        v-model="text"
        class="needs-you-paste-input"
        type="url"
        inputmode="url"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
        placeholder="http://localhost:…/callback?…"
        :aria-describedby="error ? errorId : undefined"
      >
      <button
        type="submit"
        class="needs-you-btn needs-you-btn--primary"
        :disabled="busy || !ready"
      >{{ busy ? 'Sending…' : 'Finish sign-in' }}</button>
    </div>
    <p v-if="error" :id="errorId" class="needs-you-paste-error" role="alert">{{ error }}</p>
  </form>
</template>

<style scoped>
/* The body root is display: contents inside the card's controls row, so the field takes its own line. */
.needs-you-paste {
  flex-basis: 100%;
  display: grid;
  gap: 6px;
  min-width: 0;
}
.needs-you-paste-label {
  font-size: var(--text-sm);
  color: var(--fg2);
}
.needs-you-paste-row {
  display: flex;
  gap: 8px;
  min-width: 0;
}
.needs-you-paste-input {
  flex: 1;
  min-width: 0;
  min-height: 36px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--fg);
  font: inherit;
}
.needs-you-paste-input:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
/* Matches the card's error line, which is never red. */
.needs-you-paste-error {
  margin: 0;
  color: var(--fg);
}
@media (pointer: coarse) {
  .needs-you-paste-input { min-height: var(--touch); }
}
</style>
