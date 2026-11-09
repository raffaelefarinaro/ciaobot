<script setup lang="ts">
/**
 * The body of an `mcp_auth` needs-you card: the sign-in's primary control and
 * its steps. The card owns Not now and Continue the chat / Done; this body
 * never renders them. It also owns Turn off, which turns the server off for this
 * workspace and removes the card, and the line that says where Turn off applies.
 */
import { computed, ref, useId, watch } from 'vue'
import {
  actionCopy,
  chatActionErrorReason,
  MCP_AUTH_STEPS,
  mcpTurnOffErrorText,
  mcpTurnOffScopeText,
  safeExternalUrl,
} from '../../lib/chatActions'
import { isLoopbackPage } from '../../lib/loopback'
import { useProjectStore } from '../../stores/projects'
import type { ChatAction } from '../../lib/types'
import CallbackPasteField from './CallbackPasteField.vue'
import HelperTerminalButton from './HelperTerminalButton.vue'

const props = defineProps<{ chatId: string; action: ChatAction }>()
const emit = defineEmits<{ error: [message: string]; notice: [message: string] }>()

const store = useProjectStore()
const starting = ref(false)
const turningOff = ref(false)
const copy = computed(() => actionCopy(props.action))
const server = computed(() => props.action.params.server ?? '')
const scopeText = computed(() => mcpTurnOffScopeText(props.action.params.provider ?? ''))
const scopeId = useId()
const url = computed(() => safeExternalUrl(props.action.params.url))
const isClaude = computed(() => props.action.params.provider === 'claude')
const sessionId = computed(() => props.action.params.session_id ?? '')
/** This body's root, so a focus inside it can be seen before it leaves. */
const bodyEl = ref<HTMLElement | null>(null)
const PASTE_HINT = 'Signing in on this phone? After you approve, the page fails to load. Copy its address and paste it here.'
/** A sign-in that ends on an unreachable loopback page: this device is not the engine's computer. */
const showPaste = computed(() => props.action.state === 'waiting'
  && !!props.action.params.url
  && props.action.params.callback_used !== '1'
  && !isLoopbackPage())

// The focus rests on the card while the sign-in moves on. A disabled button or
// a link that is about to be removed would otherwise drop it onto the page.
function focusCard(): void {
  (document.activeElement as HTMLElement | null)?.closest<HTMLElement>('.needs-you-card')?.focus()
}

// Runs before the DOM is patched, so the link still holds the focus here.
watch([() => props.action.state, showPaste], () => {
  if (bodyEl.value?.contains(document.activeElement)) focusCard()
})

async function turnOff(): Promise<void> {
  if (turningOff.value || starting.value) return
  focusCard()
  turningOff.value = true
  emit('error', '')
  emit('notice', '')
  try {
    const result = await store.turnOffChatActionServer(props.chatId, props.action.id)
    if (result === 'conflict') emit('notice', 'This card changed on another device. The newer state is shown.')
  } catch (e) {
    emit('error', mcpTurnOffErrorText(chatActionErrorReason(e), server.value))
  } finally {
    turningOff.value = false
  }
}

function onSent(): void {
  emit('notice', 'Address sent. This card updates when the sign-in finishes.')
}

async function start(): Promise<void> {
  if (starting.value || turningOff.value) return
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

    <button
      v-if="action.state !== 'done'"
      type="button"
      class="needs-you-btn needs-you-btn--quiet needs-you-turn-off"
      :disabled="turningOff || starting"
      :aria-busy="turningOff || undefined"
      :aria-label="`Turn off ${server}`"
      :aria-describedby="scopeId"
      @click="turnOff"
    >{{ turningOff ? 'Turning off…' : 'Turn off' }}</button>

    <HelperTerminalButton
      v-if="isClaude && (action.state === 'working' || action.state === 'waiting')"
      :chat-id="chatId"
      :session-id="sessionId"
    />

    <CallbackPasteField
      v-if="showPaste"
      :chat-id="chatId"
      :action-id="action.id"
      :hint="PASTE_HINT"
      @sent="onSent"
    />

    <p v-if="action.state !== 'done'" :id="scopeId" class="needs-you-turn-off-scope">{{ scopeText }}</p>
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
/* Sits on its own line after the card's Not now and meta in the controls row. */
.needs-you-turn-off-scope {
  flex-basis: 100%;
  order: 10;
  margin: 0;
  font-size: var(--text-xs);
  color: var(--fg3);
}
</style>
