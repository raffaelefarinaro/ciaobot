<template>
  <div class="voice-recorder">
    <!-- Idle: show mic button -->
    <button v-if="state === 'idle'" class="voice-btn" @click="startRecording" title="Record voice">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><line x1="12" y1="19" x2="12" y2="22"/></svg>
    </button>

    <!-- Recording: show stop button + timer -->
    <button v-if="state === 'recording'" class="voice-btn recording" @click="stopRecording" title="Stop recording">
      <span class="rec-dot"></span>
      <span class="rec-time">{{ formattedTime }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import { ref, onUnmounted } from 'vue'
import {
  isDesktopApp,
  queryDesktopPermission,
  requestDesktopPermission,
} from '../lib/desktop'

const emit = defineEmits<{
  recorded: [blob: Blob]
  error: [message: string]
}>()

const state = ref<'idle' | 'recording'>('idle')
const duration = ref(0)
const formattedTime = ref('0:00')
let mediaRecorder: MediaRecorder | null = null
let chunks: Blob[] = []
let timer: ReturnType<typeof setInterval> | null = null

function updateTime() {
  duration.value++
  const m = Math.floor(duration.value / 60)
  const s = duration.value % 60
  formattedTime.value = `${m}:${s.toString().padStart(2, '0')}`
}

async function startRecording() {
  if (state.value !== 'idle') return

  if (isDesktopApp()) {
    const current = await queryDesktopPermission('microphone')
    if (current === 'denied' || current === 'restricted') {
      emit(
        'error',
        'Microphone access is blocked. Open System Settings > Microphone, allow Ciaobot, then try again.',
      )
      return
    }
    if (current === 'not_determined') {
      const requested = await requestDesktopPermission('microphone')
      if (requested !== 'authorized') {
        emit(
          'error',
          'Microphone access was denied. Open System Settings > Microphone, allow Ciaobot, then try again.',
        )
        return
      }
    }
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : MediaRecorder.isTypeSupported('audio/ogg;codecs=opus')
        ? 'audio/ogg;codecs=opus'
        : ''
    mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
    chunks = []
    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunks.push(e.data)
    }
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach(t => t.stop())
      if (chunks.length > 0) {
        const blob = new Blob(chunks, { type: mimeType || 'audio/webm' })
        emit('recorded', blob)
      }
      state.value = 'idle'
    }
    mediaRecorder.start()
    state.value = 'recording'
    duration.value = 0
    formattedTime.value = '0:00'
    timer = setInterval(updateTime, 1000)
  } catch (e) {
    console.error('Could not start voice recording:', e)
    const denied = e instanceof DOMException && e.name === 'NotAllowedError'
    if (denied) {
      const desktop = isDesktopApp()
      emit(
        'error',
        desktop
          ? 'Microphone access was denied. Open System Settings > Microphone, allow Ciaobot, then try again. If Ciaobot is not listed, quit it, run "tccutil reset Microphone local.ciaobot.app" in Terminal, reopen Ciaobot, and click Allow when prompted.'
          : 'Microphone access was denied. Ciaobot is running in a browser, so the block is in the browser, not in System Settings. Click the lock icon next to the address bar, open Site settings, set Microphone to Allow, then try again. If Ciaobot is installed as a browser app, open Chrome Settings > Privacy and security > Site settings > Microphone, find this site, and allow it. If your browser itself is denied at the OS level, allow the browser in System Settings first.',
      )
    } else {
      emit('error', 'Could not start voice recording. Check that a microphone is available, then try again.')
    }
  }
}

function stopRecording() {
  if (state.value !== 'recording' || !mediaRecorder) return
  mediaRecorder.stop()
  mediaRecorder = null
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

// Allow the parent (and the Cmd+D global shortcut) to kick off recording.
// Toggles: a second call while recording stops it, mirroring the on-screen
// stop button so Cmd+D is press-once-to-start, press-again-to-stop.
function toggleRecording() {
  if (state.value === 'recording') stopRecording()
  else startRecording()
}

defineExpose({ toggleRecording })

onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (mediaRecorder && state.value === 'recording') {
    mediaRecorder.stop()
  }
})
</script>

<style scoped>
.voice-recorder {
  display: flex;
  align-items: center;
}

.voice-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  background: none;
  border: 1px solid var(--border);
  cursor: pointer;
  padding: 0;
  min-width: var(--touch);
  min-height: var(--touch);
  border-radius: var(--radius);
  color: var(--fg2);
  transition: background 120ms var(--ease), color 120ms var(--ease), border-color 120ms var(--ease);
  user-select: none;
  -webkit-user-select: none;
}

.voice-btn:hover {
  background: var(--bg3);
  color: var(--fg);
  border-color: var(--fg2);
}

.voice-btn:active {
  background: var(--bg2);
}

.voice-btn.recording {
  background: rgba(244, 67, 54, 0.15);
  color: var(--error);
}

.rec-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--error);
  animation: pulse 1s infinite;
}

.rec-time {
  font-size: 12px;
  font-family: var(--font);
  min-width: 32px;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
</style>
