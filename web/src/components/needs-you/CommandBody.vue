<script setup lang="ts">
/**
 * The body of a `command` needs-you card: the suggested command as literal
 * text, and the controls that type it into this chat's terminal. Nothing here
 * runs a command. Typing puts it at the prompt without Enter, and the user
 * presses Enter. The card owns Continue (done) and Not now.
 */
import { onBeforeUnmount, ref } from 'vue'
import { useRunInTerminal } from '../../composables/useRunInTerminal'
import { writeClipboard } from '../../lib/codeCopy'
import { useProjectStore } from '../../stores/projects'
import type { ChatAction } from '../../lib/types'

const props = defineProps<{ chatId: string; action: ChatAction }>()

const store = useProjectStore()
const terminal = useRunInTerminal()
const busy = terminal.busy
const message = ref('')
const error = ref('')
/** Set once the command has been copied in this mount, which offers I ran it. */
const copiedOnce = ref(false)
const typing = ref(false)
const copying = ref(false)
const marking = ref(false)
/** True once the card is gone. A late answer must not touch it. */
let disposed = false

onBeforeUnmount(() => {
  disposed = true
})

function commandText(): string {
  return props.action.params.command ?? ''
}

async function typeIt(): Promise<void> {
  error.value = ''
  message.value = ''
  const id = props.action.id
  typing.value = true
  try {
    const result = await terminal.run(commandText(), { chatId: props.chatId, paste: true })
    if (disposed || props.action.id !== id) return
    if (result === 'typed') {
      try {
        await store.startChatAction(props.chatId, id, { step: 'typed' })
      } catch {
        error.value = 'The command was typed, but the card could not be updated. Press Enter in the terminal when you are ready.'
      }
    } else if (result === 'copied') {
      copiedOnce.value = true
      message.value = 'The terminal was not ready, so the command was copied. Paste it into a terminal, review it, then press Enter.'
    } else if (result === 'copied_multiline') {
      copiedOnce.value = true
      message.value = 'This shell would run a multi-line command line by line as it is pasted, so it was copied instead of typed.'
    } else {
      error.value = 'The command could not be typed or copied. Select it and copy it by hand.'
    }
  } finally {
    typing.value = false
  }
}

async function copy(): Promise<void> {
  error.value = ''
  message.value = ''
  copying.value = true
  try {
    const ok = await writeClipboard(commandText())
    if (disposed) return
    if (ok) {
      copiedOnce.value = true
      message.value = 'Copied.'
    } else {
      error.value = 'The command could not be copied. Try again.'
    }
  } finally {
    copying.value = false
  }
}

async function ran(): Promise<void> {
  error.value = ''
  const id = props.action.id
  marking.value = true
  try {
    await store.startChatAction(props.chatId, id, { step: 'ran' })
  } catch {
    if (!disposed) error.value = 'Could not update the card. Try again.'
  } finally {
    marking.value = false
  }
}
</script>

<template>
  <div class="needs-you-command">
    <pre
      class="needs-you-command-text"
      tabindex="0"
      role="region"
      aria-label="Suggested command"
    ><code>{{ action.params.command }}</code></pre>

    <p v-if="action.state === 'open'" class="needs-you-command-hint">
      Opens this chat's terminal with the command at the prompt. Nothing runs until you press Enter.
    </p>

    <div v-if="action.state !== 'done'" class="needs-you-command-actions">
      <button
        v-if="action.state !== 'working'"
        type="button"
        class="needs-you-btn needs-you-btn--primary"
        :disabled="typing || busy"
        @click="typeIt"
      >Type it in a terminal</button>
      <button
        v-else
        type="button"
        class="needs-you-btn needs-you-btn--primary"
        :disabled="marking"
        @click="ran"
      >I ran it</button>
      <button
        type="button"
        class="needs-you-btn"
        :disabled="copying"
        @click="copy"
      >Copy</button>
      <button
        v-if="action.state !== 'working' && copiedOnce"
        type="button"
        class="needs-you-btn"
        :disabled="marking"
        @click="ran"
      >I ran it</button>
    </div>

    <p v-if="message" class="needs-you-command-message" role="status">{{ message }}</p>
    <p v-if="error" class="needs-you-command-error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.needs-you-command {
  display: grid;
  gap: 10px;
  justify-items: stretch;
}
.needs-you-command-text {
  /* The command is data, not prose: it keeps its own line breaks and wraps
     anywhere, inside a bounded box the user can scroll. */
  margin: 0;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 240px;
  max-width: 100%;
  overflow: auto;
}
.needs-you-command-text:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.needs-you-command-hint,
.needs-you-command-message,
.needs-you-command-error {
  margin: 0;
  line-height: 1.5;
}
.needs-you-command-hint,
.needs-you-command-message { color: var(--fg2); }
.needs-you-command-error { color: var(--error); }
.needs-you-command-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
</style>
