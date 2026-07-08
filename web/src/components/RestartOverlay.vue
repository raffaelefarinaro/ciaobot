<template>
  <div class="restart-overlay">
    <div class="restart-content">
      <div class="restart-head">
        <span class="wordmark wordmark--lg">ciaobot</span>
        <span class="restart-tag">restart</span>
      </div>

      <div class="restart-body">
        <span class="restart-spinner" aria-hidden="true">{{ spinnerFrame }}</span>
        <p class="restart-message">{{ message || 'Restarting Ciaobot…' }}</p>
      </div>

      <div class="restart-foot">
        <span class="restart-prompt">$</span>
        <span class="restart-prompt-text">waiting for server</span>
        <span class="caret"></span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

defineProps<{
  message?: string
}>()

const FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
const frame = ref(0)
const spinnerFrame = ref(FRAMES[0])
let timer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  timer = setInterval(() => {
    frame.value = (frame.value + 1) % FRAMES.length
    spinnerFrame.value = FRAMES[frame.value]
  }, 90)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.restart-overlay {
  position: fixed;
  inset: 0;
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 300;
  padding: var(--space-4);
  background-image:
    linear-gradient(180deg, rgba(255, 77, 109, 0.04) 0%, transparent 60%),
    repeating-linear-gradient(0deg, rgba(255, 255, 255, 0.012) 0 1px, transparent 1px 3px);
}

.restart-content {
  width: 100%;
  max-width: 520px;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.restart-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  padding-bottom: var(--space-3);
  border-bottom: 1px dashed var(--border);
}
.restart-tag {
  font-size: var(--text-xs);
  color: var(--fg3);
  letter-spacing: 0.5px;
  text-transform: uppercase;
}

.restart-body {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
}
.restart-spinner {
  color: var(--accent);
  font-size: var(--text-lg);
  line-height: 1.4;
  flex-shrink: 0;
}
.restart-message {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-base);
  line-height: 1.5;
}

.restart-foot {
  margin-top: var(--space-2);
  padding-top: var(--space-3);
  border-top: 1px dashed var(--border);
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-base);
  color: var(--fg2);
}
.restart-prompt {
  color: var(--accent);
  font-weight: 700;
}
.restart-prompt-text {
  color: var(--fg2);
}
</style>
