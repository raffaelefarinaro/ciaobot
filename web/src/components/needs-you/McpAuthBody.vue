<script setup lang="ts">
/**
 * The body of an `mcp_auth` needs-you card: the sign-in's primary control and
 * its steps. The card owns Not now and Continue the chat / Done; this body
 * never renders them.
 */
import { computed, ref, watch } from 'vue'
import { actionCopy, chatActionErrorReason, MCP_AUTH_STEPS, safeExternalUrl } from '../../lib/chatActions'
import { useProjectStore } from '../../stores/projects'
import type { ChatAction } from '../../lib/types'
import HelperTerminalButton from './HelperTerminalButton.vue'

const props = defineProps<{ chatId: string; action: ChatAction }>()
const emit = defineEmits<{ error: [message: string]; notice: [message: string] }>()

const store = useProjectStore()
const starting = ref(false)
const copy = computed(() => actionCopy(props.action))
const url = computed(() => safeExternalUrl(props.action.params.url))
const isClaude = computed(() => props.action.params.provider === 'claude')
const sessionId = computed(() => props.action.params.session_id ?? '')
/** This body's root, so a focus inside it can be seen before it leaves. */
const bodyEl = ref<HTMLElement | null>(null)

// The focus rests on the card while the sign-in moves on. A disabled button or
// a link that is about to be removed would otherwise drop it onto the page.
function focusCard(): void {
  (document.activeElement as HTMLElement | null)?.closest<HTMLElement>('.needs-you-card')?.focus()
}

// Runs before the DOM is patched, so the link still holds the focus here.
watch(() => props.action.state, () => {
  if (bodyEl.value?.contains(document.activeElement)) focusCard()
})

async function start(): Promise<void> {
  if (starting.value) return
  focusCard()
  starting.value = true
  emit('error', '')
  emit('notice', '')
  try {
    const next = await store.startChatAction(props.chatId, props.action.id)
    if (next === null) {
      emit('notice', 'This card changed on another device. The newer state is shown.')
    } else if (next.state === 'waiting') {
      const target = safeExternalUrl(next.params.url)
      if (target) window.open(target, '_blank', 'noopener,noreferrer')
    }
  } catch (e) {
    if (chatActionErrorReason(e) === 'invalid_state') {
      emit('notice', 'This card changed on another device. The newer state is shown.')
    } else {
      emit('error', 'The sign-in could not be started. Try again.')
    }
  } finally {
    starting.value = false
  }
}
</script>

<template>
  <div ref="bodyEl" class="needs-you-mcp">
    <ol v-if="action.state === 'waiting'" class="needs-you-steps">
      <li
        v-for="(step, i) in MCP_AUTH_STEPS"
        :key="step"
        :class="{ 'is-done': i === 0, 'is-now': i === 1 }"
        :aria-current="i === 1 ? 'step' : undefined"
      >{{ step }}</li>
    </ol>

    <button
      v-if="action.state === 'open' || action.state === 'failed'"
      type="button"
      class="needs-you-btn needs-you-btn--primary"
      :disabled="starting"
      @click="start"
    >{{ starting ? 'Starting…' : copy?.primaryLabel }}</button>

    <a
      v-else-if="action.state === 'waiting' && url"
      class="needs-you-btn"
      :href="url"
      target="_blank"
      rel="noopener noreferrer"
    >{{ copy?.primaryLabel }}</a>

    <HelperTerminalButton
      v-if="isClaude && (action.state === 'working' || action.state === 'waiting')"
      :chat-id="chatId"
      :session-id="sessionId"
    />
  </div>
</template>

<style scoped>
/* The root has no box: its button is a flex item of the card's controls row. */
.needs-you-mcp { display: contents; }
.needs-you-steps {
  display: grid;
  flex-basis: 100%;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: var(--text-sm);
  counter-reset: needs-you-step;
}
.needs-you-steps li {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--fg2);
  counter-increment: needs-you-step;
}
.needs-you-steps li::before {
  content: counter(needs-you-step);
  display: inline-grid;
  place-items: center;
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  border: 1px solid var(--border-strong);
  border-radius: 50%;
  color: var(--accent);
  font-size: var(--text-xs);
}
.needs-you-steps .is-done::before { content: '\2713'; }
.needs-you-steps .is-now { color: var(--fg); }
.needs-you-steps .is-now::before { border-color: var(--accent); }
</style>
