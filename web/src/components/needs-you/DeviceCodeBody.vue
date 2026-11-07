<script setup lang="ts">
/**
 * The body of a `device_code` needs-you card (gh or az sign-in): the start
 * control, the one-time code with Copy, the tool's sign-in link, and the helper
 * terminal. The card owns Not now and Continue the chat / Done; this body
 * never renders them.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { actionCopy, chatActionErrorReason, safeExternalUrl } from '../../lib/chatActions'
import { writeClipboard } from '../../lib/codeCopy'
import { useProjectStore } from '../../stores/projects'
import type { ChatAction } from '../../lib/types'
import HelperTerminalButton from './HelperTerminalButton.vue'

/** The engine's own pattern for the one-time code (`_DEVICE_CODE_RE`). */
const CODE_RE = /^[A-Z0-9-]{4,16}$/
const COPIED_MS = 2000

const props = defineProps<{ chatId: string; action: ChatAction }>()
const emit = defineEmits<{ error: [message: string]; notice: [message: string] }>()

const store = useProjectStore()
const starting = ref(false)
const copied = ref(false)
let copiedTimer: ReturnType<typeof setTimeout> | undefined
/** True once the card is gone. A late answer must not touch it. */
let disposed = false

const copy = computed(() => actionCopy(props.action))
const code = computed(() => {
  const raw = props.action.params.code ?? ''
  return CODE_RE.test(raw) ? raw : ''
})
const url = computed(() => safeExternalUrl(props.action.params.url))
const host = computed(() => (url.value ? new URL(url.value).host : ''))
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
    const next = await store.startChatAction(props.chatId, props.action.id, {})
    if (next === null) {
      emit('notice', 'This card changed on another device. The newer state is shown.')
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

async function copyCode(): Promise<void> {
  const ok = await writeClipboard(code.value)
  if (disposed) return
  if (ok) {
    copied.value = true
    clearTimeout(copiedTimer)
    copiedTimer = setTimeout(() => {
      copied.value = false
    }, COPIED_MS)
  } else {
    emit('error', 'The code could not be copied. Select it and copy it by hand.')
  }
}

onBeforeUnmount(() => {
  disposed = true
  clearTimeout(copiedTimer)
})
</script>

<template>
  <div ref="bodyEl" class="needs-you-device">
    <div
      v-if="action.state === 'waiting' && code"
      class="needs-you-device-code"
      role="group"
      aria-label="One-time code"
    ><code>{{ code }}</code></div>

    <button
      v-if="action.state === 'open' || action.state === 'failed'"
      type="button"
      class="needs-you-btn needs-you-btn--primary"
      :disabled="starting"
      @click="start"
    >{{ starting ? 'Starting…' : copy?.primaryLabel }}</button>

    <template v-if="action.state === 'waiting'">
      <button v-if="code" type="button" class="needs-you-btn" @click="copyCode">{{ copied ? 'Copied' : 'Copy' }}</button>
      <a
        v-if="url"
        class="needs-you-btn"
        :href="url"
        target="_blank"
        rel="noopener noreferrer"
      >Open {{ host }}</a>
    </template>

    <HelperTerminalButton
      v-if="action.state === 'working' || action.state === 'waiting'"
      :chat-id="chatId"
      :session-id="sessionId"
    />
  </div>
</template>

<style scoped>
/* The root has no box: its controls are flex items of the card's controls row. */
.needs-you-device { display: contents; }
/* The code is the one thing the user types elsewhere, so it is large and on its own line. */
.needs-you-device-code {
  flex-basis: 100%;
  margin: 0;
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: calc(var(--text-lg) * 1.5);
  font-weight: 600;
  letter-spacing: 0.08em;
  overflow-wrap: anywhere;
  user-select: all;
}
.needs-you-device-code code {
  font: inherit;
  letter-spacing: inherit;
}
</style>
