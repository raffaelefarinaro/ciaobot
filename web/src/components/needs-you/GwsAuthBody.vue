<script setup lang="ts">
/**
 * The body of a `gws_auth` needs-you card: the Google sign-in's primary control
 * and, off the engine's computer, the consent link and the paste-back field.
 * The card owns Not now and Continue the chat / Done; this body never renders them.
 */
import { computed, ref, watch } from 'vue'
import { actionCopy, chatActionErrorReason, isGoogleConsentUrl } from '../../lib/chatActions'
import { isLoopbackPage } from '../../lib/loopback'
import { useProjectStore } from '../../stores/projects'
import type { ChatAction } from '../../lib/types'
import CallbackPasteField from './CallbackPasteField.vue'

const props = defineProps<{ chatId: string; action: ChatAction }>()
const emit = defineEmits<{ error: [message: string]; notice: [message: string] }>()

const store = useProjectStore()
const starting = ref(false)
const copy = computed(() => actionCopy(props.action))
/** The engine's computer: the consent page can open here, and only here is the redirect reachable. */
const onHost = isLoopbackPage()
const url = computed(() => (isGoogleConsentUrl(props.action.params.url) ? props.action.params.url : null))
/** This body's root, so a focus inside it can be seen before it leaves. */
const bodyEl = ref<HTMLElement | null>(null)
const PASTE_HINT = 'After you approve, your browser shows a page that cannot load. Copy its full address and paste it here.'

// The focus rests on the card while the sign-in moves on. A disabled button or
// a link that is about to be removed would otherwise drop it onto the page.
function focusCard(): void {
  (document.activeElement as HTMLElement | null)?.closest<HTMLElement>('.needs-you-card')?.focus()
}

// Runs before the DOM is patched, so the button still holds the focus here.
watch(() => props.action.state, () => {
  if (bodyEl.value?.contains(document.activeElement)) focusCard()
})

async function start(): Promise<void> {
  if (starting.value) return
  focusCard()
  starting.value = true
  emit('error', '')
  emit('notice', '')
  // A pop-up opened after an await is blocked, so the placeholder tab opens first.
  const tab = onHost ? window.open('', '_blank') : null
  try {
    const next = await store.startChatAction(props.chatId, props.action.id)
    if (next === null) {
      tab?.close()
      emit('notice', 'This card changed on another device. The newer state is shown.')
    } else if (next.state === 'waiting' && isGoogleConsentUrl(next.params.url) && tab) {
      tab.opener = null
      tab.location.href = next.params.url
    } else {
      tab?.close()
    }
  } catch (e) {
    tab?.close()
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
  <div ref="bodyEl" class="needs-you-gws">
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

    <CallbackPasteField
      v-if="action.state === 'waiting' && !onHost"
      :chat-id="chatId"
      :action-id="action.id"
      :hint="PASTE_HINT"
    />

    <router-link
      v-if="action.state === 'failed' && /web app/i.test(action.error)"
      class="needs-you-btn"
      to="/settings/workspace"
    >Use manual connect in Settings</router-link>
  </div>
</template>

<style scoped>
/* The root has no box: its button is a flex item of the card's controls row. */
.needs-you-gws { display: contents; }
</style>
