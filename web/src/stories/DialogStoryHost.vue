<script lang="ts">
// Request APIs are singletons: inline Docs canvases must never share a host.
let activeHost = false
</script>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import PromptDialog from '../components/PromptDialog.vue'
import { askConfirm, askConfirmReason, pendingConfirm } from '../lib/confirm'
import { askPrompt, pendingPrompt } from '../lib/prompt'

const props = withDefaults(defineProps<{
  kind: 'confirm' | 'prompt'
  message: string
  title: string
  confirmLabel: string
  destructive?: boolean
  withReason?: boolean
  value?: string
  narrow?: boolean
}>(), { value: '' })

const mounted = ref(false)
const waiting = ref(false)
const result = ref('Not answered')
const opener = ref<HTMLButtonElement | null>(null)
let ownsHost = false

async function open() {
  if (!mounted.value || waiting.value) return
  opener.value?.focus()
  waiting.value = true
  result.value = 'Waiting for an answer'
  const options = { title: props.title, confirmLabel: props.confirmLabel }
  const answer = props.kind === 'prompt'
    ? await askPrompt(props.message, { ...options, value: props.value, placeholder: 'Project name' })
    : await (props.withReason ? askConfirmReason : askConfirm)(props.message, {
      ...options, destructive: props.destructive,
    })
  if (!mounted.value) return
  result.value = JSON.stringify(answer)
  waiting.value = false
}

onMounted(async () => {
  if (activeHost) throw new Error('Dialog stories require one host per canvas; do not render inline Docs.')
  activeHost = ownsHost = true
  mounted.value = true
  // PromptDialog's request watcher must exist before askPrompt seeds the value.
  await nextTick()
  if (mounted.value) void open()
})

onBeforeUnmount(() => {
  mounted.value = false
  if (!ownsHost) return
  if (props.kind === 'confirm') pendingConfirm.value?.resolve(false)
  else pendingPrompt.value?.resolve(null)
  activeHost = false
})
</script>

<template>
  <div :style="narrow ? { maxWidth: '390px', height: 'min(640px, calc(100dvh - 2 * var(--space-4)))', transform: 'translateZ(0)' } : {}">
    <button ref="opener" type="button" class="btn-primary" :disabled="waiting" @click="open">
      Open {{ kind }} dialog
    </button>
    <p role="status">Result: {{ result }}</p>
    <ConfirmDialog v-if="mounted && kind === 'confirm'" />
    <PromptDialog v-if="mounted && kind === 'prompt'" />
  </div>
</template>
