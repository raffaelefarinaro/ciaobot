<template>
  <div class="update-progress-overlay" role="status" aria-live="polite">
    <div class="update-progress-content">
      <div class="update-brand" aria-label="Ciaobot">
        <div class="update-wordmark"><span>›</span>ciao</div>
        <p class="update-eyebrow">Update in progress</p>
      </div>

      <div class="update-status">
        <div class="update-spinner" aria-hidden="true"></div>
        <h2>Updating Ciaobot&hellip;</h2>
        <p class="update-subtitle">
          <template v-if="version">Moving to {{ version }}. </template>
          This may take a few minutes. You can keep an eye on the details below.
        </p>

        <div class="update-progress-summary">
          <span class="update-progress-value">{{ progress }}%</span>
          <span class="update-progress-message">{{ message }}</span>
        </div>
        <div
          class="update-progress-track"
          role="progressbar"
          aria-label="Update progress"
          aria-valuemin="0"
          aria-valuemax="100"
          :aria-valuenow="progress"
        >
          <span class="update-progress-fill" :style="{ width: `${progress}%` }"></span>
        </div>

        <p class="update-language-line">
          <span aria-hidden="true">✦</span>
          <strong>{{ greeting[0] }}</strong>
          <span>in {{ greeting[1] }}</span>
          <span class="update-language-extra">· {{ greeting[2] }}</span>
        </p>
      </div>

      <div class="update-details">
        <button
          class="update-details-toggle"
          type="button"
          :aria-expanded="detailsOpen"
          aria-controls="update-terminal"
          @click="detailsOpen = !detailsOpen"
        >
          {{ detailsOpen ? 'Hide' : 'Show' }} update details
          <span aria-hidden="true">⌃</span>
        </button>
        <div v-if="detailsOpen" id="update-terminal" class="update-terminal">
          <div class="update-terminal-head">
            <span>terminal</span>
            <span>● live output</span>
          </div>
          <pre role="log" aria-live="polite">{{ logLines.join('\n') }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps<{
  version?: string
  finishing?: boolean
}>()

const progress = ref(8)
const message = ref('checking the current Ciaobot version')
const logLines = ref([
  '[ciao] opening the update flow ..................... ok',
  '[ciao] checking the current Ciaobot version ........ in progress',
])
const detailsOpen = ref(true)
const languageIndex = ref(0)
let progressTimer: number | null = null
let languageTimer: number | null = null

const stages = [
  [8, 'checking the current Ciaobot version', 'checking the current Ciaobot version ........ in progress'],
  [22, 'preparing the local engine', 'preparing the local engine ..................... in progress'],
  [42, 'checking the signed release', 'checking the signed release ..................... in progress'],
  [58, 'downloading the next hello', 'downloading the signed Ciaobot release .......... in progress'],
  [76, 'installing the updated runtime', 'installing the updated runtime .................. in progress'],
  [88, 'getting ready to restart', 'getting ready to restart ........................ in progress'],
] as const

const languages = [
  ['ciao', 'Italian', 'buongiorno'],
  ['hello', 'English', 'good morning'],
  ['hola', 'Spanish', 'buenos días'],
  ['salut', 'French', 'bonjour'],
  ['hallo', 'German', 'guten Morgen'],
  ['olá', 'Portuguese', 'bom dia'],
  ['こんにちは', 'Japanese', 'おはよう'],
  ['안녕하세요', 'Korean', '좋은 아침'],
  ['مرحبا', 'Arabic', 'صباح الخير'],
] as const

const greeting = computed(() => languages[languageIndex.value])

function advanceStage(index: number) {
  if (props.finishing) {
    progress.value = 100
    message.value = 'restarting Ciaobot with the latest version'
    logLines.value = [
      '[ciao] updated runtime ............................ ok',
      '[ciao] restarting Ciaobot .......................... in progress',
    ]
    return
  }
  const stage = stages[index]
  if (!stage) return
  progress.value = stage[0]
  message.value = stage[1]
  logLines.value = [
    '[ciao] opening the update flow ..................... ok',
    `[ciao] ${stage[2]}`,
  ]
  progressTimer = window.setTimeout(() => advanceStage(index + 1), 900)
}

watch(() => props.finishing, (finishing) => {
  if (finishing) {
    if (progressTimer) window.clearTimeout(progressTimer)
    advanceStage(0)
  }
})

onMounted(() => {
  advanceStage(1)
  languageTimer = window.setInterval(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    languageIndex.value = (languageIndex.value + 1) % languages.length
  }, 2200)
})

onUnmounted(() => {
  if (progressTimer) window.clearTimeout(progressTimer)
  if (languageTimer) window.clearInterval(languageTimer)
})
</script>

<style scoped>
.update-progress-overlay {
  position: fixed;
  z-index: 300;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 32px 20px;
  overflow-y: auto;
  background: #101010;
  color: #f2ede8;
}

.update-progress-content {
  display: grid;
  justify-items: center;
  width: min(720px, 100%);
  text-align: center;
}

.update-brand { display: grid; justify-items: center; gap: 12px; }
.update-wordmark {
  color: #f2ede8;
  font: 700 clamp(42px, 6vw, 64px)/1 var(--font-mono);
  letter-spacing: -.08em;
}
.update-wordmark span { margin-right: 8px; color: #d85a35; }
.update-eyebrow {
  margin: 0;
  color: #f0a084;
  font: 600 11px/1.3 var(--font-mono);
  letter-spacing: .14em;
  text-transform: uppercase;
}

.update-status { width: min(600px, 100%); margin-top: 52px; }
.update-spinner {
  width: 28px;
  height: 28px;
  margin: 0 auto 18px;
  border: 3px solid #4b403a;
  border-top-color: #d85a35;
  border-radius: 50%;
  animation: update-spin 1s linear infinite;
}
@keyframes update-spin { to { transform: rotate(360deg); } }
.update-status h2 { margin: 0; color: #f2ede8; font-size: clamp(20px, 3vw, 26px); }
.update-subtitle { margin: 10px auto 0; color: #9e9690; font-size: 15px; }

.update-progress-summary {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin-top: 30px;
  color: #9e9690;
  font: 11px/1.3 var(--font-mono);
  text-align: left;
}
.update-progress-value { color: #f2ede8; font-size: 16px; font-weight: 700; }
.update-progress-message { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.update-progress-track {
  height: 7px;
  margin-top: 10px;
  overflow: hidden;
  border-radius: 99px;
  background: #3a302b;
}
.update-progress-fill {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, #d85a35, #f0a084);
  transition: width 320ms ease;
}
.update-language-line { min-height: 22px; margin: 20px 0 0; color: #9e9690; font-size: 13px; }
.update-language-line > span:first-child { color: #f0a084; }
.update-language-line strong { margin: 0 4px; color: #f2ede8; font-weight: 650; }
.update-language-extra { color: #6f6964; font: 11px/1.3 var(--font-mono); }

.update-details { width: 100%; margin-top: 32px; }
.update-details-toggle {
  min-height: 44px;
  padding: 8px 12px;
  border: 0;
  background: transparent;
  color: #9e9690;
  cursor: pointer;
  font-size: 13px;
}
.update-details-toggle:hover { color: #f2ede8; }
.update-details-toggle span { margin-left: 7px; color: #f0a084; }
.update-terminal {
  overflow: hidden;
  border: 1px solid #34312e;
  border-radius: 12px;
  background: #0d0d0d;
  text-align: left;
}
.update-terminal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid #292622;
  color: #6f6964;
  font: 600 10px/1.2 var(--font-mono);
  letter-spacing: .08em;
  text-transform: uppercase;
}
.update-terminal-head span:last-child { color: #77c08a; }
.update-terminal pre {
  min-height: 154px;
  max-height: 190px;
  margin: 0;
  overflow: auto;
  padding: 16px;
  color: #bcb5ae;
  font: 12px/1.75 var(--font-mono);
  white-space: pre-wrap;
}

@media (max-width: 620px) {
  .update-progress-overlay { padding: 24px 16px; }
  .update-status { margin-top: 40px; }
}
@media (prefers-reduced-motion: reduce) {
  .update-spinner, .update-progress-fill { animation: none; transition: none; }
}
</style>
