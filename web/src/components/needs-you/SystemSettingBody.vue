<script setup lang="ts">
/**
 * The body of a `system_setting` needs-you card: the one control that opens the
 * macOS privacy pane on the Mac, or, off the Mac, the words that say where to go.
 * Done on Full Disk Access also links to the Settings restart control. The card
 * owns Not now and Continue the chat / Done; this body never renders them, and it
 * never restarts anything itself.
 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { actionCopy, chatActionErrorReason, restartTarget, SYSTEM_SETTING_TITLES } from '../../lib/chatActions'
import { isLoopbackPage } from '../../lib/loopback'
import { useProjectStore } from '../../stores/projects'
import type { ChatAction } from '../../lib/types'

const props = defineProps<{ chatId: string; action: ChatAction }>()
const emit = defineEmits<{ error: [message: string]; notice: [message: string] }>()

const CHANGED_NOTICE = 'This card changed on another device. The newer state is shown.'
const NOT_LOCAL_NOTICE = 'Open this on your Mac: System Settings → Privacy & Security.'

const store = useProjectStore()
const router = useRouter()
const starting = ref(false)
const local = isLoopbackPage()
const copy = computed(() => actionCopy(props.action))
const title = computed(() => SYSTEM_SETTING_TITLES[props.action.params.setting ?? ''])
const canOpen = computed(() => props.action.state === 'open' || props.action.state === 'failed')
const showOpen = computed(() => canOpen.value && local && !!copy.value?.primaryLabel)
const showHint = computed(() => canOpen.value && !local)
const showRestart = computed(() => props.action.state === 'done' && props.action.params.setting === 'full_disk_access')
/** This body's root, so a focus inside it can be seen before it leaves. */
const bodyEl = ref<HTMLElement | null>(null)

// The focus rests on the card while the pane opens. A disabled button or a
// button that is about to be removed would otherwise drop it onto the page.
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
    if (next === null) emit('notice', CHANGED_NOTICE)
  } catch (e) {
    const reason = chatActionErrorReason(e)
    if (reason === 'not_local') {
      emit('notice', NOT_LOCAL_NOTICE)
    } else if (reason === 'invalid_state') {
      emit('notice', CHANGED_NOTICE)
    } else {
      emit('error', 'System Settings could not be opened. Try again.')
    }
  } finally {
    starting.value = false
  }
}

function openSettings(): void {
  void router.push(restartTarget('config'))
}
</script>

<template>
  <div ref="bodyEl" class="needs-you-system-setting">
    <button
      v-if="showOpen"
      type="button"
      class="needs-you-btn needs-you-btn--primary"
      :disabled="starting"
      @click="start"
    >{{ starting ? 'Opening…' : copy?.primaryLabel }}</button>

    <p v-else-if="showHint" class="needs-you-system-setting-hint">
      Open this on your Mac: System Settings → Privacy & Security → {{ title ?? 'the setting the agent named' }}.
    </p>

    <button
      v-if="showRestart"
      type="button"
      class="needs-you-btn"
      @click="openSettings"
    >Restart Ciaobot in Settings</button>
  </div>
</template>

<style scoped>
.needs-you-system-setting {
  display: grid;
  gap: 10px;
  justify-items: start;
}
.needs-you-system-setting-hint {
  margin: 0;
  line-height: 1.5;
  color: var(--fg2);
  font-size: var(--text-sm);
}
</style>
