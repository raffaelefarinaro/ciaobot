<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useHostTerminal } from '../composables/useHostTerminal'
import { apiErrorMessage } from '../lib/errorMessage'
import type { HostTerminalAction } from '../lib/types'

const props = withDefaults(
  defineProps<{
    action?: HostTerminalAction
    chatId?: string
    projectId?: string
    label?: string
    ariaLabel?: string
    variant?: 'link' | 'small'
  }>(),
  { variant: 'link' },
)

const { available, canRun, ensureCapabilities, runAction, openFolder } = useHostTerminal()

const pending = ref(false)
const done = ref('')
const error = ref('')

const visible = computed(() => (props.action ? canRun(props.action) : available.value))
const resolvedLabel = computed(() => props.label ?? (props.action ? 'Run in Terminal' : 'Open in Terminal'))

onMounted(ensureCapabilities)

async function launch() {
  error.value = ''
  done.value = ''
  pending.value = true
  try {
    const result = props.action
      ? await runAction(props.action)
      : await openFolder({ chatId: props.chatId, projectId: props.projectId })
    done.value = result.launched
  } catch (e) {
    error.value = apiErrorMessage(e, 'Could not open a terminal.')
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <span v-if="visible" class="host-terminal">
    <button
      type="button"
      :class="variant === 'small' ? 'btn-small' : 'host-terminal-link'"
      :disabled="pending"
      :aria-busy="pending"
      :aria-label="ariaLabel"
      @click="launch"
    >{{ pending ? 'Opening…' : resolvedLabel }}</button>
    <span v-if="done" class="host-terminal-status" role="status">Opened in {{ done }}</span>
    <span v-if="error" class="host-terminal-error" role="alert">{{ error }}</span>
  </span>
</template>

<style scoped>
.host-terminal-link {
  display: inline;
  padding: 0;
  border: 0;
  background: none;
  color: var(--fg);
  font: inherit;
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 2px;
  cursor: pointer;
}
.host-terminal-link:hover { text-decoration-color: currentColor; }
.host-terminal-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-sm); }
.host-terminal-status,
.host-terminal-error { display: block; font-size: var(--text-sm); }
.host-terminal-status { color: var(--fg3); }
.host-terminal-error { color: var(--error); }
@media (pointer: coarse) {
  .host-terminal button { min-height: var(--touch); min-width: var(--touch); }
}
</style>
