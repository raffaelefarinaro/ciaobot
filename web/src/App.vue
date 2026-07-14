<template>
  <div id="ciao-app">
    <Transition name="fade">
      <StartupView
        v-if="showStartup"
        :phases="phases"
        :overall-ready="overallReady"
        :version="serverVersion"
        @skip="skipped = true"
      />
    </Transition>
    <router-view />
    <InAppToast />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import InAppToast from './components/InAppToast.vue'
import StartupView from './components/StartupView.vue'

interface Phase {
  name: string
  status: string
  message: string
  started_at: string | null
  finished_at: string | null
}

const phases = ref<Phase[]>([])
const overallReady = ref(false)
const serverVersion = ref('')
const skipped = ref(false)
const startupDone = ref(false)

const showStartup = computed(() => !startupDone.value && !skipped.value)

let pollTimer: ReturnType<typeof setTimeout> | null = null

async function pollStartup() {
  try {
    const res = await fetch('/api/startup-status')
    if (!res.ok) return
    const data = await res.json()
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
  } catch {
    // ignore fetch errors during startup
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
})

onUnmounted(() => {
  stopPolling()
})

watch(showStartup, (show) => {
  if (!show) stopPolling()
})
</script>

<style>
:root {
  /* Font scale multiplier */
  --font-scale: 1.0;

  /* Surface */
  --bg: #1a1a2e;        /* page */
  --bg2: #1f2240;       /* cards / panels */
  --bg3: #2a2e54;       /* hover / pressed */
  --bg-elev: #23264a;   /* input bar, modals, popovers */
  /* Text */
  --fg: #e8e8f0;
  --fg2: #b4b4c4;       /* lifted from #a0a0b0 for legibility on small screens */
  --fg3: #7a7a90;
  /* Accent */
  --accent: #ff4d6d;    /* warmer pink for contrast on dark */
  --accent-strong: #ff2e54;
  --accent2: #6a47b8;
  /* Edges */
  --border: #2e3258;
  --border-strong: #3a3f70;
  /* Status */
  --success: #4caf50;
  --warning: #ff9800;
  --error: #f44336;
  /* Geometry */
  --radius: 10px;
  --radius-sm: 6px;
  --radius-lg: 14px;
  --touch: 44px;        /* min hit area on touch devices */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 24px;
  --space-6: 32px;
  /* Safe area passthrough. In browser mode we zero out --safe-bottom because
     the browser's own bottom UI (Safari toolbar) already occupies that zone;
     adding our own safe-inset on top creates dead space below the input bar.
     Only in standalone/fullscreen PWA does the home indicator actually need
     the inset, so we re-enable it via the media query below. */
  --safe-top: env(safe-area-inset-top, 0px);
  --safe-right: env(safe-area-inset-right, 0px);
  --safe-bottom: 0px;
  --safe-left: env(safe-area-inset-left, 0px);
  /* Type */
  --font: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
  --text-xs: calc(11px * var(--font-scale));   /* labels, badges, pills, section titles */
  --text-sm: calc(12px * var(--font-scale));   /* hints, secondary text */
  --text-base: calc(13px * var(--font-scale)); /* body */
  --text-lg: calc(15px * var(--font-scale));   /* headers, titles */
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
  --fg3: #8e90a8;       /* lighter slate */
  /* Accent */
  --accent: #d81b60;    /* crisp pink/crimson for white bg */
  --accent-strong: #b00d46;
  --accent2: #512da8;   /* deep violet secondary */
  /* Edges */
  --border: #d2d4e3;    /* light grey border */
  --border-strong: #b6b8cf;
  /* Status overrides for light-mode readability */
  --success: #2e7d32;
  --warning: #ef6c00;
  --error: #c62828;
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

/* Hide scrollbars globally but keep scroll behavior. Applies to every
   scrollable element in the PWA (chat transcript, sidebar, settings,
   modals, etc.). Chrome/Safari/Edge via ::-webkit-scrollbar, Firefox via
   scrollbar-width, legacy Edge via -ms-overflow-style. */
* {
  scrollbar-width: none;      /* Firefox */
  -ms-overflow-style: none;   /* IE / legacy Edge */
}
*::-webkit-scrollbar {
  width: 0;
  height: 0;
  display: none;              /* WebKit (Chrome, Safari, new Edge) */
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

/* Subtle CRT-style grain. Fixed, behind all content, no pointer events.
   Inline SVG noise tile keeps it zero-asset. */
body::before {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  opacity: 0.025;
  mix-blend-mode: screen;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 0.6 0'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>");
  background-size: 160px 160px;
}
:root.theme-light body::before {
  mix-blend-mode: multiply;
  opacity: 0.015;
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
  font-family: var(--font);
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
a:hover {
  color: var(--accent-strong);
}


.btn-small {
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg3);
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-sm);
  text-decoration: none;
  transition: background 120ms var(--ease), transform 120ms var(--ease);
}

.btn-small:hover { background: var(--border-strong); }
.btn-small:active { transform: scale(0.97); background: var(--bg2); }

.btn-primary {
  padding: 10px 20px;
  border: none;
  border-radius: var(--radius);
  background: var(--accent);
  color: white;
  cursor: pointer;
  font-family: var(--font);
  font-size: calc(14px * var(--font-scale));
  font-weight: 600;
  transition: background 120ms var(--ease), transform 120ms var(--ease);
}

.btn-primary:hover { background: var(--accent-strong); }
.btn-primary:active { transform: scale(0.98); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

/* Compact control (30×30 layout) with a full --touch hit area. Padding expands
   the border-box for taps; negative margin keeps flex/grid spacing tight.
   ::before paints the visible hover surface at 30px so highlights don't bleed
   into the expanded hit target (matches sidebar nav-item icons). */
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

@media (min-width: 769px) {
  /* Tighten typography on desktop where iOS zoom isn't a concern */
  input, textarea, select { font-size: calc(14px * var(--font-scale)); padding: 8px 12px; }
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
  font-size: var(--text-xs);
  color: var(--fg2);
  letter-spacing: 0.5px;
  margin: 0;
  font-weight: 600;
}
.label-eyebrow {
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

/* ── Small bordered icon/text button (chat header, schedule actions) ── */
.btn-chip {
  background: none;
  border: 1px solid var(--border);
  color: var(--fg2);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-base);
  padding: 4px 8px;
  border-radius: var(--radius);
  line-height: 1;
  transition: color 120ms var(--ease), border-color 120ms var(--ease);
}
.btn-chip:hover { color: var(--fg); border-color: var(--fg2); }
.btn-chip.active { color: var(--accent); border-color: var(--accent); }

/* ── Badge / pill (status, context, day-of-week) ─────────────── */
.badge {
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
.badge--accent { background: var(--accent); color: #fff; }
.badge--accent2 { background: var(--accent2); color: var(--fg); }
.badge--muted {
  background: var(--bg3);
  color: var(--fg2);
  border: 1px solid var(--border);
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

@media (max-width: 768px) {
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
