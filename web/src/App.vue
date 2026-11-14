<template>
  <div id="ciao-app" :data-workspace-color="workspaceColor">
    <Transition name="fade">
      <StartupView
        v-if="showStartup"
        :phases="phases"
        :overall-ready="overallReady"
        @skip="skipped = true"
      />
    </Transition>
    <RestartNotice
      v-if="projectStore.serverRestarting"
      :message="projectStore.serverRestartMessage"
    />
    <EngineOfflineView
      v-if="showEngineOffline"
      :state="engineState === 'updating' ? 'updating' : 'unreachable'"
      :loopback="canUseDeviceControls"
      :host="engineHost"
      :retrying="engineRetrying"
      @retry="retryEngine"
    />
    <router-view />
    <InAppToast />
    <ConfirmDialog />
    <ArchiveTaskDialog />
    <PromptDialog />
    <NewChatPicker />
    <div class="status-bar-tint" aria-hidden="true" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import ArchiveTaskDialog from './components/ArchiveTaskDialog.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import EngineOfflineView from './components/EngineOfflineView.vue'
import InAppToast from './components/InAppToast.vue'
import NewChatPicker from './components/NewChatPicker.vue'
import PromptDialog from './components/PromptDialog.vue'
import RestartNotice from './components/RestartNotice.vue'
import StartupView from './components/StartupView.vue'
import { createEngineMonitor, requestStartupStatus, type EngineState } from './lib/engineStatus'
import { activeWorkspaceAccent, rememberWorkspaceColor } from './lib/workspaceColors'
import { isLoopbackPage } from './lib/loopback'
import { useProjectStore } from './stores/projects'

interface Phase {
  name: string
  status: string
  message: string
  started_at: string | null
  finished_at: string | null
}

const projectStore = useProjectStore()
const phases = ref<Phase[]>([])
const overallReady = ref(false)
const skipped = ref(false)
const startupDone = ref(false)
const startupDelayElapsed = ref(false)
const STARTUP_CURTAIN_DELAY_MS = 180
// The engine is either on this machine (a loopback origin) or on another one
// the user reached over the network; the offline curtain names one or the other.
const canUseDeviceControls = isLoopbackPage()

// An engine that stops answering (crash, `ciao service stop`, reboot) used to
// leave a loaded tab with no honest state: API calls failed one at a time and
// the WebSocket backed off silently. The monitor reports `unreachable` only
// after the engine has gone unanswered for a sustained stretch, and only when
// the awareness socket cannot vouch for it either: a remote origin (a phone
// or a laptop reaching the engine over a VPN or a relay) can leave a probe
// unanswered for seconds while the engine is perfectly healthy, and the
// curtain that says "your computer is off" must not appear for that. An
// announced restart reads as `updating` rather than an outage.
const engineState = ref<EngineState>('ready')
const engineRetrying = ref(false)
const engineHost = window.location.host
const engineMonitor = createEngineMonitor({
  isUpdating: () => projectStore.serverRestarting,
  isAlive: () => projectStore.engineSeenAliveWithin(),
  onChange: (s) => {
    const prev = engineState.value
    engineState.value = s
    const wasDown = prev === 'unreachable' || prev === 'updating'
    if (wasDown && s === 'booting') {
      startupDone.value = false
      skipped.value = false
      startupDelayElapsed.value = true
    }
    if (wasDown && (s === 'ready' || s === 'booting')) projectStore.reconnectNow()
  },
})
const engineUnreachable = computed(() => engineState.value === 'unreachable' || engineState.value === 'updating')
const shouldPollStartup = computed(() => !startupDone.value && !skipped.value
  && !engineUnreachable.value && engineState.value !== 'auth_required')

// A cold launch with the engine down never gets a startup answer, so the boot
// view would have nothing to show: the curtain replaces it whenever the engine
// is unreachable (it then has to boot out loud before we trust it), and the two
// are never on screen together.
const showStartup = computed(() => startupDelayElapsed.value && !startupDone.value && !skipped.value
  && !engineUnreachable.value && engineState.value !== 'auth_required')
// Over the app, never replacing it: the route, its scroll position and its
// in-memory state have to survive the outage.
const showEngineOffline = computed(() => engineUnreachable.value)
async function retryEngine() {
  engineRetrying.value = true
  try { await engineMonitor.retry() } finally { engineRetrying.value = false }
}
const workspaceColor = computed(() =>
  activeWorkspaceAccent(projectStore.workspaces, projectStore.activeWorkspace))
watch(workspaceColor, (color) => { if (color !== 'neutral') rememberWorkspaceColor(color) }, { immediate: true })

let pollTimer: ReturnType<typeof setTimeout> | null = null
let startupDelayTimer: ReturnType<typeof setTimeout> | null = null
let polling = false
let pollGeneration = 0
let appMounted = false

async function pollStartup(generation: number) {
  const response = await requestStartupStatus()
  if (!appMounted || generation !== pollGeneration) return
  if (response.status === 401 || response.status === 403) {
    // Move the monitor's own state too, so its next healthy probe reports
    // recovery and lets startup status resume without a parallel poll loop.
    void engineMonitor.retry().catch(() => {})
    return
  }
  // Status 0 means no answer arrived at all (the request failed or ran out
  // of time): there is no status to read a phase list from.
  if (response.status < 200 || response.status >= 300) return
  const data = response.body as { phases?: Phase[]; overall_ready?: boolean } | null
  const nextPhases = data?.phases || []
  if (JSON.stringify(phases.value) !== JSON.stringify(nextPhases)) phases.value = nextPhases
  const nextReady = data?.overall_ready || false
  if (overallReady.value !== nextReady) overallReady.value = nextReady
  if (overallReady.value) startupDone.value = true
}

function startPolling() {
  if (!appMounted || !shouldPollStartup.value || polling || pollTimer) return
  polling = true
  const generation = pollGeneration
  void pollStartup(generation).finally(() => {
    polling = false
    if (generation !== pollGeneration) {
      if (appMounted && shouldPollStartup.value) startPolling()
      return
    }
    if (appMounted && shouldPollStartup.value) {
      pollTimer = setTimeout(() => {
        pollTimer = null
        startPolling()
      }, 1500)
    }
  })
}

function stopPolling() {
  pollGeneration++
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

onMounted(() => {
  appMounted = true
  startupDelayTimer = setTimeout(() => {
    startupDelayTimer = null
    startupDelayElapsed.value = true
  }, STARTUP_CURTAIN_DELAY_MS)
  startPolling()
  engineMonitor.start()
})

onUnmounted(() => {
  appMounted = false
  stopPolling()
  if (startupDelayTimer) clearTimeout(startupDelayTimer)
  engineMonitor.stop()
})

watch(shouldPollStartup, (shouldPoll) => {
  if (!shouldPoll) stopPolling()
  else startPolling()
})
</script>

<style src="./styles/app.css"></style>
