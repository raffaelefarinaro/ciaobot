<template>
  <div id="ciao-app" :data-workspace-color="workspaceColor">
    <div
      v-if="(clientMode || clientStateUnknown) && !onDevicePage"
      class="client-mode-banner"
      :class="{ 'is-offline': hostUnreachable }"
      :role="hostUnreachable ? 'alert' : 'status'"
    >
      <!-- The host can drop while no chat is open, and the per-chat card that
           announces it lives inside ChatPanel. This banner is the only piece of
           chrome present on every screen, so it carries the state too. -->
      <span v-if="projectStore.hostAuthRequired" class="client-mode-banner-text">
        The host needs its password again.
        <a v-if="canUseDeviceControls" class="client-mode-banner-link" :href="contentHref('/login')">Log in again</a>
      </span>
      <span v-else-if="clientStateUnknown" class="client-mode-banner-text">
        Ciaobot can’t tell whether this browser is on the host.
      </span>
      <span v-else-if="projectStore.hostPolicyBlocked" class="client-mode-banner-text">
        A local policy is blocking the connection to the host.
      </span>
      <span v-else-if="hostUnreachable" class="client-mode-banner-text">
        <span class="client-mode-banner-spinner" aria-hidden="true"></span>
        Can’t reach <code>{{ clientHostLabel }}</code>. Reconnecting…
      </span>
      <span v-else class="client-mode-banner-text">
        Client mode. Everything below is on
        <code>{{ clientHostLabel }}</code><template v-if="!clientHasSession"> · host password needed</template>
      </span>
      <div class="client-mode-banner-actions">
        <!-- The one screen that is about this computer, not the host. -->
        <a
          v-if="canUseDeviceControls"
          class="client-mode-banner-link"
          :href="deviceHref('/device')"
        >{{ clientStateUnknown || projectStore.hostPolicyBlocked ? 'Open This device' : 'This device' }}</a>
        <button
          v-if="canUseDeviceControls"
          type="button"
          class="client-mode-banner-btn"
          :disabled="switchingToHost"
          @click="switchBackToHost"
        >
          {{ switchingToHost ? 'Switching…' : 'Switch to host' }}
        </button>
      </div>
    </div>
    <Transition name="fade">
      <StartupView
        v-if="showStartup"
        :phases="phases"
        :overall-ready="overallReady"
        :version="serverVersion"
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
    <PromptDialog />
    <NewChatPicker />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, provide, watch } from 'vue'
import { useRoute } from 'vue-router'
import ConfirmDialog from './components/ConfirmDialog.vue'
import EngineOfflineView from './components/EngineOfflineView.vue'
import InAppToast from './components/InAppToast.vue'
import NewChatPicker from './components/NewChatPicker.vue'
import PromptDialog from './components/PromptDialog.vue'
import RestartNotice from './components/RestartNotice.vue'
import StartupView from './components/StartupView.vue'
import { askConfirm } from './lib/confirm'
import { createEngineMonitor, type EngineState } from './lib/engineStatus'
import { normalizeWorkspaceColor } from './lib/workspaceColors'
import { contentHref, deviceHref, isLoopbackPage, navigateToDevice } from './lib/originNavigation'
import { useProjectStore } from './stores/projects'
import { CONNECTION_ROLE_KEY, type ConnectionRole } from './lib/connectionRole'

interface Phase {
  name: string
  status: string
  message: string
  started_at: string | null
  finished_at: string | null
}

const projectStore = useProjectStore()
const route = useRoute()
const phases = ref<Phase[]>([])
const overallReady = ref(false)
const serverVersion = ref('')
const skipped = ref(false)
const startupDone = ref(false)
/** Set once /api/startup-status has answered at all. Without it a cold launch
 *  with the engine down never learns anything from the poll, and the boot view
 *  would sit there empty instead of the recovery curtain. */
const startupReached = ref(false)
const clientMode = ref(false)
const clientStateUnknown = ref(false)
const canUseDeviceControls = isLoopbackPage()
const clientHostUrl = ref('')
const clientHasSession = ref(false)
const switchingToHost = ref(false)

// An engine that stops answering (crash, `ciao service stop`, reboot) used to
// leave a loaded tab with no honest state: API calls failed one at a time and
// the WebSocket backed off silently. The monitor reports `unreachable` only
// after two consecutive failed probes, so a single blip never flashes the
// screen, and an announced restart reads as `updating` rather than an outage.
const engineState = ref<EngineState>('ready')
const engineRetrying = ref(false)
const engineHost = window.location.host
const engineMonitor = createEngineMonitor({
  isUpdating: () => projectStore.serverRestarting,
  onChange: (s) => {
    const prev = engineState.value
    engineState.value = s
    const wasDown = prev === 'unreachable' || prev === 'updating'
    if (wasDown && s === 'booting') { startupDone.value = false; skipped.value = false }   // show boot progress again
    if (wasDown && (s === 'ready' || s === 'booting')) projectStore.reconnectNow()
  },
})
const engineUnreachable = computed(() => engineState.value === 'unreachable' || engineState.value === 'updating')

// A cold launch with the engine down never gets a startup answer, so the boot
// view would have nothing to show: hand over to the curtain instead, unless the
// engine has since come back (it then has to boot out loud before we trust it).
const showStartup = computed(() =>
  !startupDone.value && !skipped.value && !(engineUnreachable.value && !startupReached.value),
)
// Over the app, never replacing it: the route, its scroll position and its
// in-memory state have to survive the outage.
const showEngineOffline = computed(() =>
  engineUnreachable.value && (!showStartup.value || !startupReached.value),
)
async function retryEngine() {
  engineRetrying.value = true
  try { await engineMonitor.retry() } finally { engineRetrying.value = false }
}
// The device panel is about this machine, so the "you are seeing the host"
// banner would contradict it.
const onDevicePage = computed(() => route.path.startsWith('/device'))
const workspaceColor = computed(() => {
  const active = projectStore.activeWorkspace
  const ws = projectStore.workspaces.find((item) => item.name === active)
  return normalizeWorkspaceColor(ws?.color)
})
// True only in client mode: the local node proxy reports it cannot reach the
// host (see the `host_unreachable` frame handling in the projects store).
//
// A mounted ChatPanel renders its own `host-connection-card` from the same
// flag, with a richer recovery action, so the banner stands down there rather
// than announcing the same outage twice -- to the eye and to a screen reader.
// Keyed on the panel actually being on screen rather than on the URL: /chat
// with no id and /chat/:id/subagent/:id are both chat paths that mount no
// panel, and those screens need the banner like any other.
const hostUnreachable = computed(
  () => projectStore.hostConnectionUnavailable && projectStore.chatPanelsMounted === 0,
)
const clientHostLabel = computed(() => {
  const raw = clientHostUrl.value
  if (!raw) return 'remote host'
  try {
    return new URL(raw).host || raw
  } catch {
    return raw
  }
})

provide(CONNECTION_ROLE_KEY, computed<ConnectionRole>(() => {
  if (clientStateUnknown.value) return { kind: 'unknown' }
  if (clientMode.value) {
    return {
      kind: 'client',
      hostLabel: clientHostLabel.value,
      reachable: !projectStore.hostConnectionUnavailable,
    }
  }
  return { kind: 'host' }
}))

let pollTimer: ReturnType<typeof setTimeout> | null = null
let nodePollTimer: ReturnType<typeof setInterval> | null = null

async function switchBackToHost() {
  if (!canUseDeviceControls || switchingToHost.value) return
  const confirmed = await askConfirm(
    'Stop client mode and become host on this machine? Changes that exist only on the other host may not be synced.',
    {
      title: 'Become host on this device?',
      confirmLabel: 'Disconnect and become host',
    },
  )
  if (!confirmed) return
  switchingToHost.value = true
  navigateToDevice()
}

async function pollStartup() {
  try {
    const res = await fetch('/api/startup-status', { redirect: 'manual' })
    if (!res.ok) {
      clientStateUnknown.value = true
      return
    }
    const data = await res.json()
    startupReached.value = true
    if (data.version && serverVersion.value !== data.version) {
      serverVersion.value = data.version
    }
    const nextPhases = data.phases || []
    if (JSON.stringify(phases.value) !== JSON.stringify(nextPhases)) {
      phases.value = nextPhases
    }
    const nextReady = data.overall_ready || false
    if (overallReady.value !== nextReady) {
      overallReady.value = nextReady
    }
    if (overallReady.value) {
      startupDone.value = true
    }
    refreshClientBanner(data)
  } catch {
    // A failed role probe is unknown, never evidence of host mode.
    clientStateUnknown.value = true
  }
}

function refreshClientBanner(data: Record<string, unknown>) {
  const role = String(data.node_role || '')
  clientStateUnknown.value = data.state_valid !== true || !['host', 'client', 'active', 'standby'].includes(role)
  clientMode.value = !clientStateUnknown.value && (role === 'client' || role === 'standby')
  clientHostUrl.value = String(data.host_url || data.active_peer_url || '')
  clientHasSession.value = Boolean(data.has_host_session)
}

async function pollClientBanner() {
  try {
    const res = await fetch('/api/startup-status', { redirect: 'manual' })
    if (!res.ok) {
      clientStateUnknown.value = true
      return
    }
    refreshClientBanner(await res.json())
  } catch {
    clientStateUnknown.value = true
  }
}

function scheduleNextPoll() {
  if (!showStartup.value) return
  pollTimer = setTimeout(async () => {
    await pollStartup()
    scheduleNextPoll()
  }, 1500)
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

onMounted(() => {
  pollStartup().then(scheduleNextPoll)
  void pollClientBanner()
  nodePollTimer = setInterval(() => { void pollClientBanner() }, 5000)
  engineMonitor.start()
})

onUnmounted(() => {
  stopPolling()
  if (nodePollTimer) {
    clearInterval(nodePollTimer)
    nodePollTimer = null
  }
  engineMonitor.stop()
})

watch(showStartup, (show) => {
  if (!show) stopPolling()
  else if (!pollTimer) void pollStartup().then(scheduleNextPoll)
})
</script>

<style>
.client-mode-banner {
  --banner-tone: var(--warning);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  padding: 6px 16px;
  background: color-mix(in srgb, var(--banner-tone) 8%, var(--bg));
  border-bottom: 1px solid color-mix(in srgb, var(--banner-tone) 35%, var(--border));
  color: var(--fg);
  font-family: var(--font-sans, -apple-system, BlinkMacSystemFont, sans-serif);
  font-size: var(--text-sm);
  line-height: 1.45;
}
.client-mode-banner.is-offline { --banner-tone: var(--error); }
.client-mode-banner-text { flex: 1 1 16rem; min-width: 0; }
/* The state dot rides the sentence so it stays beside the first word when the
   banner wraps on a phone. */
.client-mode-banner-text::before {
  content: "";
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-right: 8px;
  vertical-align: 1px;
  border-radius: var(--radius-pill);
  background: var(--banner-tone);
}
.client-mode-banner-spinner {
  display: inline-block;
  width: 10px;
  height: 10px;
  margin-right: var(--space-2);
  vertical-align: -1px;
  border: 2px solid color-mix(in srgb, var(--fg) 30%, transparent);
  border-top-color: var(--fg);
  border-radius: var(--radius-pill);
  animation: client-mode-banner-spin 0.9s linear infinite;
}
@keyframes client-mode-banner-spin {
  to { transform: rotate(360deg); }
}
@media (prefers-reduced-motion: reduce) {
  .client-mode-banner-spinner { animation: none; }
}
.client-mode-banner code {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 0.92em;
  color: var(--fg);
}
.client-mode-banner-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: auto;
  flex-shrink: 0;
}
.client-mode-banner-link {
  color: var(--accent);
  font-weight: 600;
  text-decoration: none;
  white-space: nowrap;
}
.client-mode-banner-link:hover { text-decoration: underline; text-underline-offset: 3px; }
.client-mode-banner-btn {
  min-height: 30px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg-elev, var(--bg2));
  color: var(--fg);
  font: inherit;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
}
.client-mode-banner-btn:hover:not(:disabled) { border-color: var(--border-strong, var(--border)); }
.client-mode-banner-btn:disabled { opacity: 0.6; cursor: wait; }
@media (pointer: coarse) {
  .client-mode-banner-btn, .client-mode-banner-link {
    min-height: var(--touch, 44px);
    display: inline-flex;
    align-items: center;
  }
}

:root {
  /* Font scale multiplier. The reference is the original (pre-rescale) UI;
     the default 1.2 corresponds to "100%" in the Settings display, so the
     slider's display math (scale / 1.2 * 100) yields 100 at this value. */
  --font-scale: 1.2;

  /* Surface */
  --bg: #1a1a2e;        /* page */
  --bg2: #1f2240;       /* cards / panels */
  --bg3: #2a2e54;       /* hover / pressed */
  --bg-elev: #23264a;   /* input bar, modals, popovers */
  /* Text */
  --fg: #e8e8f0;
  --fg2: #b4b4c4;       /* lifted from #a0a0b0 for legibility on small screens */
  --fg3: #8f90a8;
  /* Accent */
  --accent: #ff4d6d;    /* warmer pink for contrast on dark */
  --accent-strong: #ff2e54;
  --accent2: #6a47b8;
  /* Label colour on a filled accent surface (.btn-primary, active pills,
     accent badges). The dark-theme accents are bright, so white on them sits
     at 2-3.6:1; the canvas indigo clears WCAG AA (4.5:1) on every dark accent
     and its hover shade. */
  --on-accent: #1a1a2e;
  /* Edges */
  --border: #2e3258;
  --border-strong: #3a3f70;
  /* Status */
  --success: #4caf50;
  --warning: #ff9800;
  --error: #f44336;
  /* Geometry */
  --radius-xs: 4px;     /* small squared tags: state chips, key badges */
  --radius: 10px;
  --radius-sm: 6px;
  --radius-lg: 14px;
  --radius-pill: 999px; /* fully rounded ends: pills, count badges, chips */
  --touch: 44px;        /* min hit area on touch devices */
  /* Width of the home screen's content column. Shared because the status row
     and the lanes under it must start on the same left edge, and when this
     lived as a number in each component they drifted (1040 vs 1320) and the
     row sat neither centred nor aligned. */
  --home-max: 1320px;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 24px;
  --space-6: 32px;
  /* Page grid shared by every pane (header, body, rail, composer):
     content is capped at --page-max, centred, with --page-gutter inside it,
     and an optional --page-rail column on the right. --page-inset is the
     resulting horizontal padding for a full-width row (e.g. a header) so its
     edges land on the content's edges. */
  --page-max: 1180px;
  --page-gutter: 32px;
  --page-rail: 280px;
  --page-inset: max(var(--page-gutter), calc((100% - var(--page-max)) / 2 + var(--page-gutter)));
  /* Safe area passthrough. In browser mode we zero out --safe-bottom because
     the browser's own bottom UI (Safari toolbar) already occupies that zone;
     adding our own safe-inset on top creates dead space below the input bar.
     Only in standalone/fullscreen PWA does the home indicator actually need
     the inset, so we re-enable it via the media query below. */
  --safe-top: env(safe-area-inset-top, 0px);
  --safe-right: env(safe-area-inset-right, 0px);
  --safe-bottom: 0px;
  --safe-left: env(safe-area-inset-left, 0px);
  /* Type: Hybrid typography model. Sans-serif for prose & UI, monospace for code & developer tokens. */
  --font-mono: ui-monospace, 'SF Mono', 'Fira Code', 'Cascadia Code', 'Segoe UI Mono', 'Roboto Mono', Menlo, Consolas, monospace;
  --font-sans: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'SF Pro Display', 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  --font: var(--font-sans);
  --text-xs: calc(11px * var(--font-scale));   /* labels, badges, pills, section titles */
  --text-sm: calc(12px * var(--font-scale));   /* hints, secondary text */
  --text-base: calc(14px * var(--font-scale)); /* body */
  --text-lg: calc(16px * var(--font-scale));   /* headers, titles */
  /* Motion */
  --ease: cubic-bezier(0.2, 0.8, 0.2, 1);
}

:root.theme-light {
  /* Surface */
  --bg: #f4f4fa;        /* soft light lavender page */
  --bg2: #ffffff;       /* clean white card / panel */
  --bg3: #e6e8f4;       /* hover / pressed */
  --bg-elev: #fcfcfd;   /* elevated popover, modal, inputs */
  /* Text */
  --fg: #1a1a2e;        /* dark slate text matching dark bg */
  --fg2: #5f607d;       /* medium-dark slate */
  --fg3: #66687f;       /* readable secondary metadata */
  /* Accent */
  --accent: #d81b60;    /* crisp pink/crimson for white bg */
  --accent-strong: #b00d46;
  --accent2: #512da8;   /* deep violet secondary */
  --on-accent: #ffffff; /* light accents are deep enough for white labels */
  /* Edges */
  --border: #d2d4e3;    /* light grey border */
  --border-strong: #b6b8cf;
  /* Status overrides for light-mode readability */
  --success: #2e7d32;
  --warning: #ef6c00;
  --error: #c62828;
}

/* Per-workspace accent overrides (Option A: accents only).
   Applied on #ciao-app for the active workspace, and on individual
   controls (home new-chat buttons, workspace pills, chat badges) that
   belong to another workspace. Pink is explicit so a pink-target control
   inside a non-pink active workspace does not inherit the parent accent.
   Every pair keeps --on-accent at WCAG AA on both --accent and --accent-strong:
   dark presets hover to a shade still >= 4.5:1 against the indigo label, light
   presets are deep enough for white labels (and for accent text on white). */
[data-workspace-color="pink"] {
  --accent: #ff4d6d;
  --accent-strong: #ff2e54;
}
[data-workspace-color="cyan"] {
  --accent: #38bdf8;
  --accent-strong: #0ea5e9;
}
[data-workspace-color="amber"] {
  --accent: #fb923c;
  --accent-strong: #ea580c;
}
[data-workspace-color="emerald"] {
  --accent: #34d399;
  --accent-strong: #059669;
}
[data-workspace-color="violet"] {
  --accent: #a78bfa;
  --accent-strong: #9670f7;
}
:root.theme-light [data-workspace-color="pink"] {
  --accent: #d81b60;
  --accent-strong: #b00d46;
}
:root.theme-light [data-workspace-color="cyan"] {
  --accent: #0369a1;
  --accent-strong: #075985;
}
:root.theme-light [data-workspace-color="amber"] {
  --accent: #c2410c;
  --accent-strong: #9a3412;
}
:root.theme-light [data-workspace-color="emerald"] {
  --accent: #047857;
  --accent-strong: #065f46;
}
:root.theme-light [data-workspace-color="violet"] {
  --accent: #7c3aed;
  --accent-strong: #6d28d9;
}

/* In standalone/fullscreen PWA the home indicator is live, so restore the
   safe-area inset. Browser mode stays at 0 (set in :root above) to avoid
   double-counting with Safari's own bottom toolbar. */
@media (display-mode: standalone), (display-mode: fullscreen), (display-mode: minimal-ui) {
  :root {
    --safe-bottom: env(safe-area-inset-bottom, 0px);
  }
}

/* When the on-screen keyboard is open, it covers the home indicator,
   so collapse the bottom safe-area inset to avoid an empty gap
   between the input bar and the keyboard. */
html.keyboard-open {
  --safe-bottom: 0px;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

/* Keep scroll affordances visible. Quiet by default, stronger at the edges of
   bounded data regions, but never mistaken for disabled content. */
* {
  scrollbar-width: thin;
  scrollbar-color: var(--border-strong) transparent;
  -ms-overflow-style: scrollbar;
}
*::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}
*::-webkit-scrollbar-track {
  background: transparent;
}
*::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border: 2px solid transparent;
  border-radius: var(--radius-pill);
  background-clip: padding-box;
}
*::-webkit-scrollbar-thumb:hover {
  background: var(--fg3);
  border: 2px solid transparent;
  background-clip: padding-box;
}

html, body {
  height: 100%;
  overflow: hidden;
  overscroll-behavior: none;
}

body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--fg);
  font-size: calc(14px * var(--font-scale));
  -webkit-font-smoothing: antialiased;
  position: relative;
  /* Keep browser zoom available for accessibility. Individual controls use
     touch-action: manipulation to avoid delayed/double activation. */
  touch-action: auto;
}

#ciao-app {
  height: var(--app-h, 100dvh);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
  z-index: 1;
}

/* ── Wordmark ────────────────────────────────────────────────── */
.wordmark {
  font-family: var(--font-mono);
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--fg);
  display: inline-flex;
  align-items: baseline;
  gap: 0.18em;
  line-height: 1;
  user-select: none;
}
.wordmark::before {
  content: "›";
  color: var(--accent);
  font-weight: 400;
  font-size: 1.1em;
}
.wordmark--lg { font-size: 32px; }
.wordmark--md { font-size: 20px; }
.wordmark--sm { font-size: 14px; }

/* Blinking terminal caret. */
.caret {
  display: inline-block;
  width: 0.5em;
  height: 1em;
  background: var(--accent);
  vertical-align: text-bottom;
  margin-left: 0.15em;
  animation: caret-blink 1.1s steps(2, end) infinite;
}
@keyframes caret-blink {
  0%, 49.9% { opacity: 1; }
  50%, 100% { opacity: 0; }
}

button {
  font-family: var(--font);
  -webkit-tap-highlight-color: transparent;
  touch-action: manipulation;
}

:where(button, a, input, textarea, select, [role="button"], [role="link"]):focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

a {
  color: var(--accent);
  text-decoration: underline;
  text-decoration-thickness: 1px;
  transition: color 120ms var(--ease);
}
/* Anchors styled as buttons keep their button text colour on hover. A bare
   a:hover (0,1,1) outranks .btn-primary (0,1,0), so it used to repaint the
   label accent-strong — the same colour .btn-primary:hover paints the
   background — and the text vanished. */
a:not(.btn-small, .btn-primary, .btn-chip, .btn-icon):hover {
  color: var(--accent-strong);
}


.btn-small {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 8px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg3);
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-sm);
  font-weight: 600;
  text-decoration: none;
  transition: background 120ms var(--ease), transform 120ms var(--ease), border-color 120ms var(--ease);
}

.btn-small:hover { background: var(--border-strong); border-color: var(--border-strong); }
.btn-small:active { transform: scale(0.97); background: var(--bg2); }

.btn-primary {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 10px 20px;
  border: none;
  border-radius: var(--radius);
  background: var(--accent);
  color: var(--on-accent);
  cursor: pointer;
  font-family: var(--font);
  font-size: calc(14px * var(--font-scale));
  font-weight: 600;
  text-decoration: none;
  transition: background 120ms var(--ease), transform 120ms var(--ease);
}

.btn-primary:hover { background: var(--accent-strong); }
.btn-primary:active { transform: scale(0.98); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

/* Compact control (30×30 layout) with a full --touch hit area. Padding expands
   the border-box for taps; negative margin keeps flex/grid spacing tight.
   ::before paints the visible hover surface at 30px so highlights don't bleed
   into the expanded hit target (matches sidebar nav-item icons). */
/* ── Page grid ─────────────────────────────────────────────────────────
   One layout for every pane body: a main column and an optional right rail,
   capped at --page-max and centred, so switching pages never moves the
   content's edges. Collapses to one column when the pane (not the window)
   is narrow; chat-pane is the container ChatLayout declares on .chat-main. */
.page-grid {
  box-sizing: border-box;
  width: 100%;
  max-width: var(--page-max);
  margin: 0 auto;
  padding-inline: var(--page-gutter);
  display: grid;
  grid-template-columns: minmax(0, 1fr) var(--page-rail);
  align-items: start;
  gap: 48px;
}
.page-grid--single { grid-template-columns: minmax(0, 1fr); }
.page-main { min-width: 0; }
.page-rail {
  position: sticky;
  /* 0, not a gap: inside a padded scroll body a non-zero sticky offset pushes
     the rail below the main column's first heading before any scrolling. */
  top: 0;
  min-width: 0;
  font-size: var(--text-sm);
}
@container chat-pane (max-width: 940px) {
  .page-grid { grid-template-columns: minmax(0, 1fr); gap: var(--space-6); }
  .page-rail { position: static; }
}
@media (max-width: 700px) {
  :root { --page-gutter: 16px; }
}

/* Rail vocabulary: a small heading, hairline key/value rows, hairline link
   rows, and a muted note. Shared so every page's rail reads the same. */
.rail-title {
  margin: 0 0 10px;
  color: var(--fg);
  font-size: calc(15px * var(--font-scale));
  font-weight: 650;
  letter-spacing: -0.01em;
}
.rail-title + .rail-kvs, .rail-title + .rail-list { margin-top: 0; }
.rail-section + .rail-section { margin-top: var(--space-5); }
.rail-label { margin: 0 0 4px; color: var(--fg3); font-size: var(--text-sm); }
.rail-note { margin: 8px 0 0; color: var(--fg3); font-size: var(--text-sm); line-height: 1.45; }
.rail-kvs, .rail-list { border-top: 1px solid var(--border); }
.rail-kv {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  min-height: 36px;
  border-bottom: 1px solid var(--border);
  color: var(--fg2);
}
.rail-kv strong { color: var(--fg); font-weight: 600; text-align: right; }
.rail-kv .rail-attention { color: var(--warning); }
.rail-item {
  display: flex;
  flex-direction: column;
  width: 100%;
  min-height: var(--touch);
  justify-content: center;
  padding: 6px 0;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: none;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
  text-decoration: none;
  cursor: pointer;
}
.rail-item:hover { color: var(--accent); }
.rail-item small { color: var(--fg3); font-size: var(--text-xs); }
.rail-item .rail-attention { color: var(--warning); }

.touch-hit {
  box-sizing: content-box;
  --touch-hit-visual: 30px;
  padding: calc((var(--touch) - var(--touch-hit-visual)) / 2);
  margin: calc((var(--touch-hit-visual) - var(--touch)) / 2);
  position: relative;
  isolation: isolate;
}
.touch-hit::before {
  content: '';
  position: absolute;
  inset: calc((var(--touch) - var(--touch-hit-visual)) / 2);
  z-index: -1;
  border-radius: var(--radius-sm, 6px);
  background: transparent;
  pointer-events: none;
  transition: background 120ms var(--ease);
}
.touch-hit:hover::before {
  background: var(--bg3);
}

/* Icon-only / round buttons need full touch targets */
.btn-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: var(--touch);
  min-height: var(--touch);
  padding: 8px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--fg);
  border-radius: var(--radius);
  cursor: pointer;
  transition: background 120ms var(--ease), transform 120ms var(--ease);
}
.btn-icon:hover { background: var(--bg3); }
.btn-icon:active { transform: scale(0.94); background: var(--bg2); }
.btn-icon[aria-pressed="true"] { background: var(--bg3); border-color: var(--border); }

input, textarea, select {
  background: var(--bg);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 12px;
  font-family: var(--font);
  /* 16px prevents iOS auto-zoom on focus */
  font-size: calc(16px * var(--font-scale));
  outline: none;
  transition: border-color 120ms var(--ease), box-shadow 120ms var(--ease);
}

input:focus, textarea:focus, select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px rgba(255, 77, 109, 0.2);
}

/* Tighten typography only when the user has a fine pointer (mouse / trackpad).
   Gating on viewport width alone used to override inputs back to <16px on
   wide touch devices (iPad portrait/landscape, iPhone landscape, Android
   tablets), which makes iOS Safari auto-zoom the page on focus. Pointer
   type is the actual signal: iOS only auto-zooms on touch input, so a
   coarse pointer always wants the 16px default regardless of width. */
@media (pointer: fine) {
  input, textarea, select { font-size: calc(14px * var(--font-scale)); padding: 8px 12px; }
}

/* Touch devices: keep inputs at >= 16px so iOS Safari does not auto-zoom the
   viewport on focus. Some components (markdown editor, comment compose)
   override the global input rule with smaller text tokens; this carve-out
   pins every input/textarea/select to 16px when the pointer is coarse, no
   matter what scoped styles say. Pairs with the (pointer: fine) tightening
   above: trackpad/desktop stays tight, touch stays zoom-stable. */
@media (pointer: coarse) {
  input, textarea, select { font-size: 16px !important; }
}

/* ── Shared page layout (schedules, settings, login) ─────────── */
.page {
  padding: calc(16px + var(--safe-top)) calc(16px + var(--safe-right))
           calc(16px + var(--safe-bottom)) calc(16px + var(--safe-left));
  max-width: 600px;
  margin: 0 auto;
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: contain;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.page-header h2 {
  font-size: calc(16px * var(--font-scale));
  font-weight: 700;
}

.card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.section-title {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--fg2);
  letter-spacing: 0.5px;
  margin: 0;
  font-weight: 600;
}
.label-eyebrow {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 0;
  font-weight: 600;
}
.subsection-title {
  font-size: var(--text-xs);
  color: var(--fg2);
  margin: var(--space-2) 0 0 0;
  font-weight: 500;
}

@media (max-width: 600px) {
  .page {
    padding: calc(12px + var(--safe-top)) calc(12px + var(--safe-right))
             calc(12px + var(--safe-bottom)) calc(12px + var(--safe-left));
  }
}

/* ── Shared form patterns ────────────────────────────────────── */
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
@media (max-width: 600px) {
  .form-grid { grid-template-columns: 1fr; }
}

.form-group { display: flex; flex-direction: column; gap: 4px; }
.form-group > label {
  font-size: var(--text-xs);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.form-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

/* ── Ghost / tertiary button — transparent, used for "talk about it" ── */
.btn-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: 1px solid var(--border);
  color: var(--fg2);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-sm);
  font-weight: 500;
  padding: 8px 16px;
  border-radius: var(--radius);
  line-height: 1.2;
  transition: color 120ms var(--ease), border-color 120ms var(--ease), background 120ms var(--ease);
}
.btn-chip:hover { color: var(--fg); border-color: var(--fg2); background: var(--bg3); }
.btn-chip.active { color: var(--accent); border-color: var(--accent); }

/* ── Badge / pill (status, context, day-of-week) ─────────────── */
.badge {
  font-family: var(--font-mono);
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: var(--text-xs);
  font-weight: 600;
  white-space: nowrap;
  line-height: 1.3;
  letter-spacing: 0.3px;
}
.badge--accent { background: var(--accent); color: var(--on-accent); }
.badge--accent2 { background: var(--accent2); color: var(--fg); }
.badge--muted {
  background: var(--bg3);
  color: var(--fg2);
  border: 1px solid var(--border);
}
.badge--builtin {
  background: color-mix(in srgb, var(--accent2) 14%, var(--bg3));
  color: var(--fg2);
  border: 1px solid color-mix(in srgb, var(--accent2) 40%, var(--border));
}
.theme-light .badge--builtin {
  background: color-mix(in srgb, var(--light-secondary, #512da8) 10%, var(--light-surface-interactive, #e6e8f4));
  color: var(--light-text-muted, #5f607d);
  border-color: color-mix(in srgb, var(--light-secondary, #512da8) 28%, var(--light-border, #d2d4e3));
}
.badge--success { background: rgba(76, 175, 80, 0.15); color: var(--success); }
.badge--warn { background: rgba(255, 152, 0, 0.15); color: var(--warning); }
.badge--error { background: rgba(244, 67, 54, 0.15); color: var(--error); }

/* Compact dot-style pill for day-of-week markers */
.badge--dot {
  padding: 2px 5px;
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  letter-spacing: 0.3px;
  text-transform: uppercase;
  background: transparent;
  color: var(--fg2);
  opacity: 0.35;
  font-weight: 600;
}
.badge--dot.active {
  background: var(--accent2);
  color: var(--fg);
  opacity: 1;
}

/* ── Hint text ───────────────────────────────────────────────── */
.hint {
  color: var(--fg2);
  font-size: var(--text-sm);
  margin: 0;
  line-height: 1.5;
}
.hint--info {
  display: block;
  color: var(--fg2);
  background: color-mix(in srgb, var(--accent2) 6%, var(--bg2));
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--accent2);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
}
.hint--warn {
  display: block;
  color: var(--fg2);
  background: color-mix(in srgb, var(--warning) 6%, var(--bg2));
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--warning);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
}

/* ── Multi-select toggle pill (day-of-week selectors) ────────── */
.checkbox-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 40px;
  height: 30px;
  padding: 0 8px;
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.3px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  color: var(--fg2);
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease), color 120ms var(--ease);
  user-select: none;
}
.checkbox-pill:hover { border-color: var(--fg2); }
.checkbox-pill.active {
  background: var(--accent2);
  border-color: var(--accent2);
  color: var(--fg);
}

/* ── Danger button variant ───────────────────────────────────── */
.btn-danger {
  border-color: var(--error) !important;
  color: var(--error) !important;
}

@media (pointer: coarse) {
  .btn-small,
  .btn-primary,
  .btn-chip,
  .checkbox-pill {
    min-height: var(--touch);
  }
}

/* ── Mobile sheet modal ───────────────────────────────────────── */
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
}

.modal-sheet {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  width: 100%;
  max-width: 520px;
  max-height: 90dvh;
  overflow: auto;
  display: flex;
  flex-direction: column;
}

@media (max-width: 600px) {
  .modal-backdrop { padding: 0; align-items: stretch; }
  .modal-sheet {
    max-width: none;
    max-height: 100dvh;
    border-radius: 0;
    border: none;
    padding-top: var(--safe-top);
    padding-bottom: var(--safe-bottom);
  }
}

/* Visually-hidden helper for icon-only button labels */
.sr-only {
  position: absolute;
  width: 1px; height: 1px;
  padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0 0 0 0);
  white-space: nowrap; border: 0;
}

/* ── Startup view transitions ─────────────────────────────────── */
.fade-enter-active,
.fade-leave-active {
  transition: opacity 400ms var(--ease);
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
