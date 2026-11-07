<script setup lang="ts">
/**
 * The body of a `restart` needs-you card: the agent's note and the one control
 * that opens the Settings place where the restart or update is started. The
 * card owns Not now; this body never starts a restart itself.
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { actionCopy, restartTarget } from '../../lib/chatActions'
import type { ChatAction } from '../../lib/types'

const props = defineProps<{ chatId: string; action: ChatAction }>()
defineEmits<{ error: [message: string]; notice: [message: string] }>()

const router = useRouter()
const copy = computed(() => actionCopy(props.action))
const note = computed(() => props.action.params.note ?? '')

function openSettings(): void {
  void router.push(restartTarget(props.action.params.reason ?? ''))
}
</script>

<template>
  <div class="needs-you-restart">
    <p v-if="note" class="needs-you-restart-note"><span>Agent's reason:</span> {{ note }}</p>
    <button type="button" class="needs-you-btn needs-you-btn--primary" @click="openSettings">{{ copy?.primaryLabel }}</button>
  </div>
</template>

<style scoped>
.needs-you-restart {
  display: grid;
  gap: 10px;
  justify-items: start;
}
.needs-you-restart-note {
  margin: 0;
  line-height: 1.5;
  color: var(--fg2);
  font-size: var(--text-sm);
}
.needs-you-restart-note span {
  color: var(--fg);
  font-weight: 600;
}
</style>
