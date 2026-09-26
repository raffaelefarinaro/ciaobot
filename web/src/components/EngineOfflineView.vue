<template>
  <!-- A curtain, not a route: the plan's route, scroll position and in-memory
       state have to survive an outage, so this sits over the app and leaves
       `router-view` mounted. Recovery only removes the overlay -- nothing
       reloads. Statically imported (not lazy) so it is part of the entry chunk
       the service worker caches; a lazy chunk would 404 exactly when it is
       needed. -->
  <div
    ref="rootEl"
    class="engine-offline"
    role="alertdialog"
    aria-modal="true"
    aria-live="assertive"
    aria-labelledby="engine-offline-title"
  >
    <div class="engine-offline-content">
      <h1 id="engine-offline-title" class="engine-offline-title">{{ title }}</h1>
      <p class="engine-offline-text">{{ text }}</p>
      <template v-if="state === 'unreachable' && loopback">
        <div v-for="cmd in commands" :key="cmd" class="engine-offline-cmd">
          <code>{{ cmd }}</code>
          <button class="btn-small" type="button" @click="copy(cmd)">{{ copied === cmd ? 'Copied' : 'Copy' }}</button>
        </div>
        <p class="hint">Logs: <code>.runtime/ciao.stderr.log</code> in your Ciaobot workspace.</p>
      </template>
      <div class="engine-offline-actions">
        <button ref="retryButton" class="btn-primary" type="button" :disabled="retrying" @click="emit('retry')">{{ retrying ? 'Checking…' : 'Retry' }}</button>
        <span class="hint">Reconnecting automatically…</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps<{
  state: 'updating' | 'unreachable'
  loopback: boolean
  host: string
  retrying: boolean
}>()

const emit = defineEmits<{ (e: 'retry'): void }>()

const commands = ['ciao service start', 'ciao service status']
const copied = ref('')
const retryButton = ref<HTMLButtonElement | null>(null)
const rootEl = ref<HTMLElement | null>(null)

// Nothing behind the curtain unmounts, so focus would stay wherever it was and
// the first Tab would reach a control the user cannot see. Retry is the one
// thing to do here, so it takes focus when the curtain appears.
onMounted(() => {
  retryButton.value?.focus()
  // Modal means modal: ChatLayout's bare 1-9, Esc and modifier shortcuts are
  // still live behind the curtain, and they would switch workspace under a view
  // the user cannot see. Capture phase so they are stopped before the app's own
  // bubble-phase listeners ever see the key.
  window.addEventListener('keydown', onKeydown, true)
})
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown, true))

function onKeydown(event: KeyboardEvent) {
  const root = rootEl.value
  if (!root) return
  if (event.key === 'Tab') {
    const focusables = Array.from(root.querySelectorAll<HTMLElement>('button:not([disabled])'))
    if (focusables.length === 0) return
    const first = focusables[0], last = focusables[focusables.length - 1]
    const active = document.activeElement as HTMLElement | null
    if (event.shiftKey && (active === first || !root.contains(active))) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && (active === last || !root.contains(active))) { event.preventDefault(); first.focus() }
    return
  }
  // Our own buttons keep their native Enter/Space activation (a default
  // action, unaffected by stopping propagation), but no app listener may see the key.
  if (root.contains(event.target as Node)) { event.stopImmediatePropagation(); return }
  event.preventDefault()
  event.stopImmediatePropagation()
}

async function copy(cmd: string) {
  if (!navigator.clipboard) return
  try {
    await navigator.clipboard.writeText(cmd)
    // Only claim a copy that actually happened: outside a secure context
    // `navigator.clipboard` is missing and `writeText` can be denied. The
    // command stays selectable text, so the hint still does its job.
    copied.value = cmd
  } catch {
    // Clipboard access can be denied (no permission, no secure context).
  }
}

// Two different failures with two different ways out. On the loopback origin
// the fix is a command on this computer, so it is shown verbatim and
// copyable; anywhere else the only honest thing to name is the host.
const title = computed(() => {
  if (props.state === 'updating') return 'Ciaobot is restarting'
  if (props.loopback) return "Ciaobot isn't running"
  return `Ciaobot on ${props.host} isn't reachable`
})
const text = computed(() => {
  // `serverRestarting` also covers plain Settings and drain restarts, so this
  // must not promise a new version.
  if (props.state === 'updating') return 'This page reconnects by itself.'
  if (props.loopback) return 'The engine on this computer stopped responding. Start it from a terminal:'
  return 'Check that the computer running Ciaobot is on and connected. Your open chat and drafts are kept.'
})
</script>

<style scoped>
.engine-offline {
  position: fixed;
  inset: 0;
  /* Above the dialogs (1000): while the engine is down they cannot be acted
     on, so a confirm or prompt left open must not float over the curtain. */
  z-index: 1100;
  display: grid;
  place-items: center;
  background: var(--bg);
  padding: max(var(--page-gutter, 16px), env(safe-area-inset-top, 0px)) max(var(--page-gutter, 16px), env(safe-area-inset-right, 0px)) max(var(--page-gutter, 16px), env(safe-area-inset-bottom, 0px)) max(var(--page-gutter, 16px), env(safe-area-inset-left, 0px));
}
.engine-offline-content {
  max-width: 34rem;
  width: 100%;
}
.engine-offline-title {
  margin: 0 0 8px;
  color: var(--fg);
  font-size: calc(20px * var(--font-scale));
  font-weight: 700;
  letter-spacing: -0.01em;
}
.engine-offline-text {
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-base);
  line-height: 1.5;
}
.engine-offline-cmd {
  display: flex;
  gap: 8px;
  align-items: center;
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  padding: 8px 12px;
  margin-block: 8px;
  background: var(--bg2);
}
.engine-offline-cmd code {
  flex: 1;
  overflow-x: auto;
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  color: var(--fg);
  white-space: nowrap;
}
.engine-offline-actions {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-top: 20px;
  flex-wrap: wrap;
}
</style>
