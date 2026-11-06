<script setup lang="ts">
/**
 * The body of a `permission_mode` needs-you card: the mode now, the mode asked
 * for, the agent's reason and the Start control. The card owns Not now and
 * Continue the chat / Done; this body never renders them. Start sends an empty
 * body: the engine applies the mode the request carried, never one sent here.
 */
import { computed, ref, watch } from 'vue'
import { actionCopy, chatActionErrorReason } from '../../lib/chatActions'
import { isBridgeMode, modeLabel } from '../../lib/permissionModes'
import { useProjectStore } from '../../stores/projects'
import type { ChatAction } from '../../lib/types'

const props = defineProps<{ chatId: string; action: ChatAction }>()
const emit = defineEmits<{ error: [message: string]; notice: [message: string] }>()

const store = useProjectStore()
const starting = ref(false)
const copy = computed(() => actionCopy(props.action))
const chat = computed(() => store.chats.find(c => c.chat_id === props.chatId))
/** This body's root, so a focus inside it can be seen before it leaves. */
const bodyEl = ref<HTMLElement | null>(null)

function labelFor(mode: unknown): string {
  return isBridgeMode(mode) ? modeLabel(mode) : 'unknown'
}
const nowLabel = computed(() => labelFor(chat.value?.mode))
const requestedLabel = computed(() => labelFor(props.action.params.mode))
const isBypass = computed(() => props.action.params.mode === 'bypass')
const note = computed(() => props.action.params.note ?? '')

// The focus rests on the card while the change moves on. A disabled button
// would otherwise drop it onto the page.
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
  try {
    const next = await store.startChatAction(props.chatId, props.action.id, {})
    if (next === null) emit('notice', 'This card changed on another device. The newer state is shown.')
  } catch (e) {
    if (chatActionErrorReason(e) === 'invalid_state') {
      emit('notice', 'This card changed on another device. The newer state is shown.')
    } else {
      emit('error', 'The mode could not be changed. Try again.')
    }
  } finally {
    starting.value = false
  }
}
</script>

<template>
  <div ref="bodyEl" class="needs-you-perm">
    <template v-if="action.state !== 'done'">
      <p class="needs-you-perm-line"><span class="needs-you-perm-label">Now</span> {{ nowLabel }}</p>
      <p class="needs-you-perm-line"><span class="needs-you-perm-label">Requested</span> {{ requestedLabel }}</p>
      <p v-if="note" class="needs-you-perm-note">Agent's reason: {{ note }}</p>
      <p v-if="isBypass" class="needs-you-perm-warn">Every command and file edit runs without asking you first.</p>

      <button
        v-if="copy?.primaryLabel"
        type="button"
        class="needs-you-btn needs-you-btn--primary"
        :disabled="starting"
        @click="start"
      >{{ copy.primaryLabel }}</button>
    </template>
  </div>
</template>

<style scoped>
.needs-you-perm:empty { display: none; }
.needs-you-perm {
  display: grid;
  flex-basis: 100%;
  gap: 6px;
  justify-items: start;
}
.needs-you-perm-line,
.needs-you-perm-note,
.needs-you-perm-warn {
  margin: 0;
  line-height: 1.5;
}
.needs-you-perm-line { color: var(--fg2); }
.needs-you-perm-label {
  display: inline-block;
  min-width: 7em;
  margin-right: 0.5em;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}
.needs-you-perm-note { color: var(--fg2); }
.needs-you-perm-warn { color: var(--warning); font-weight: 500; }
</style>
