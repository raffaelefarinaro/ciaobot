<script setup lang="ts">
/**
 * The browser terminal's pane: the surface a person actually touches.
 *
 * The store owns the wire — the socket, the offsets, the session list, the
 * unlock. This pane owns the screen and the controls around it. The rules
 * that are easy to get wrong are written down here rather than left to the
 * next reader's memory.
 *
 * **One screen, one session.** A renderer is a physical terminal: it is
 * created for the session the pane is showing and disposed when the pane
 * gives it up. It is never re-bound to another session, because a screen
 * that showed two sessions would be showing two histories at once.
 *
 * **Hide is not End.** Hiding closes the socket and leaves the shell running
 * on the engine; ending asks the engine to kill it. They are two buttons,
 * and only one of them stops the process.
 *
 * **The keyboard belongs to the shell while the terminal has focus.**
 * Escape, Tab, the arrows, Ctrl+C and Ctrl+D are the shell's, so the pane
 * does not claim them — and it does not use the app's modal Escape capture
 * (`useModalFocus`), which would swallow the shell's own Escape before the
 * terminal ever saw it. What the pane does instead is *isolate* the app's
 * shortcuts: a keydown that bubbles out of the terminal is stopped at the
 * pane root, so the workspace's 1-9 shortcuts and the chat's Escape never
 * fire while someone is typing into a shell. The isolation is deliberately
 * in the bubble phase: a capture-phase stop would keep the key from the
 * terminal's own listener, which is the whole point of letting the shell
 * have the keyboard.
 *
 * The one exception is the focus escape, Shift+Tab, which is intercepted in
 * the capture phase so it moves the keyboard to this pane's own controls
 * instead of reaching the shell. It has to exist: the shell keeps Tab, so
 * without it the keyboard could not leave the terminal at all, and a pane
 * that cannot be left is a keyboard trap.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useTerminalStore } from '../stores/terminal'
import type { TerminalSessionInfo } from '../lib/terminalProtocol'
import type { TerminalRenderer } from '../lib/terminalRenderer'

const store = useTerminalStore()

/**
 * Whether the pane has room for the dock beside the chat.
 *
 * The layout measures its own space and says so; the pane only draws the
 * shape it is told. It defaults to false — the drawer — because the drawer
 * is always usable, while a dock in a pane too narrow for it would squeeze
 * the conversation to a sliver.
 */
const props = withDefaults(defineProps<{ dockFits?: boolean }>(), { dockFits: false })

/** The element the screen is mounted into. It exists for the pane's whole life. */
const surfaceEl = ref<HTMLElement | null>(null)
/** The control that leaves the terminal, so the focus escape has somewhere to go. */
const hideButtonEl = ref<HTMLButtonElement | null>(null)
/**
 * The screen on show, and the session it belongs to.
 *
 * Shallow on purpose: a deep ref would wrap the screen in a reactive proxy,
 * and the screen's private fields are not readable through a proxy.
 */
const renderer = shallowRef<TerminalRenderer | null>(null)
let rendererSession: string | null = null
/** Bumped for every screen change, so a late load cannot revive an old one. */
let screenToken = 0
/** The last size the screen measured, in cells. */
let measured: { cols: number; rows: number } | null = null
/** The control that opened the pane, so the keyboard can go back to it. */
let opener: HTMLElement | null = null
/** The password, while it is being typed. It is handed over and forgotten. */
const password = ref('')
/** The field itself, so what was typed is cleared the moment it is handed over. */
const passwordEl = ref<HTMLInputElement | null>(null)
/** A phone-sized pane is a drawer; a wide one docks beside the chat. */
const isMobile = ref(false)
/**
 * The shape this pane is in.
 *
 * A phone-sized pane is always a drawer, whatever the room: a shell on a
 * narrow screen wants the whole screen. A wide one docks beside the chat
 * only when the layout says the pane holds both columns.
 */
const isDock = computed(() => !isMobile.value && props.dockFits)

const hasScreen = computed(() => renderer.value !== null)
const canEnd = computed(() => store.activeSession !== null && !store.ending)

const hostLabel = computed(() => store.status?.host || 'unknown host')
const platformLabel = computed(() => store.status?.platform || 'unknown platform')
const folderLabel = computed(() => store.activeSession?.folder || 'no folder')
/**
 * The truncation notice says what actually happened: the engine's ring of
 * output lost its beginning, so the screen was cleared and the replay
 * started again from the first byte the engine still had. A truncated ring
 * cannot rebuild a screen it no longer holds.
 */
const truncatedMessage = computed(() => (store.truncated
  ? 'The engine\'s output buffer was truncated, so the terminal was cleared and replayed from the start of what is left. A truncated buffer cannot rebuild the full screen.'
  : ''))

// ── The screen ──────────────────────────────────────────────────────────

/**
 * Give up the current screen.
 *
 * The screen is detached before it is disposed: the store's surface belongs
 * to the renderer, and a disposal that arrives while the store still points
 * at it would fail a write the store is waiting on.
 */
function releaseScreen(): void {
  screenToken += 1
  const current = renderer.value
  renderer.value = null
  rendererSession = null
  if (!current) return
  store.detachRenderer(current)
  current.dispose()
}

/**
 * Open a screen for a session and hand it to the store.
 *
 * The renderer module is loaded on demand: it carries xterm and its
 * stylesheet, and a page that never opens a terminal must not pay for it.
 */
async function openScreen(sessionId: string): Promise<void> {
  const element = surfaceEl.value
  if (!element) return
  releaseScreen()
  const token = screenToken
  const module = await import('../lib/terminalRenderer')
  // The user may have picked another session, or closed the pane, while the
  // module was loading.
  if (token !== screenToken) return
  if (store.activeSession?.session_id !== sessionId) return
  const created = new module.TerminalRenderer({
    sessionId,
    element,
    onInput: (bytes) => { store.sendInput(bytes) },
    onResize: (cols, rows) => {
      measured = { cols, rows }
      store.resize(cols, rows)
    },
  })
  renderer.value = created
  rendererSession = sessionId
  store.attachRenderer(created)
  applyInputState()
  if (store.visible) created.focus()
}

/**
 * Only a live, unlocked session that this screen owns may send keystrokes.
 * A screen that nobody has claimed sends nothing.
 */
function applyInputState(): void {
  const current = renderer.value
  if (!current) return
  const mine = rendererSession === store.activeSession?.session_id
  current.setInputEnabled(mine && store.unlocked && store.phase === 'live')
}

/** Put the keyboard on the screen. */
function focusScreen(): void {
  renderer.value?.focus()
}

// ── Actions ─────────────────────────────────────────────────────────────

function hide(): void {
  // Hiding detaches; it never ends. The shell keeps running on the engine.
  store.hide()
}

async function end(): Promise<void> {
  await store.endSession()
}

async function create(): Promise<void> {
  await store.createSession()
}

function openSession(session: TerminalSessionInfo): void {
  store.selectSession(session.session_id)
}

async function unlock(): Promise<void> {
  const entered = password.value
  // The password goes to one request and is not kept: the field and the
  // component's own copy are cleared before the answer comes back.
  password.value = ''
  if (passwordEl.value) passwordEl.value.value = ''
  const ok = await store.unlock(entered)
  if (ok) void nextTick(() => focusScreen())
}

async function takeover(): Promise<void> {
  await store.reconnect(true)
}

function sessionState(session: TerminalSessionInfo): string {
  if (session.exited) {
    return session.exit_code === null ? 'ended' : `ended (${session.exit_code})`
  }
  if (session.attached) return 'attached'
  return 'running'
}

// ── Keyboard ────────────────────────────────────────────────────────────

/**
 * The focus escape: Shift+Tab leaves the terminal for this pane's controls.
 *
 * It is intercepted in the capture phase, which is the only place a gesture
 * may stop a key before the terminal's own listener sees it. The shell never
 * sees this one, so it sends no bytes and moves no focus of its own — it
 * only hands the keyboard back to the pane.
 *
 * Only while the terminal holds the focus: on the pane's own controls,
 * Shift+Tab is the browser's ordinary "back" and is left alone.
 */
function onPaneKeydownCapture(event: KeyboardEvent): void {
  if (event.key !== 'Tab' || !event.shiftKey) return
  if (!terminalHasFocus()) return
  // A composition in progress owns the keyboard: moving the focus now would
  // tear the IME's own state apart.
  if (event.isComposing) return
  event.preventDefault()
  event.stopPropagation()
  focusControls()
}

/** True while the keyboard is on the terminal rather than on the pane. */
function terminalHasFocus(): boolean {
  const surface = surfaceEl.value
  if (!surface) return false
  return surface.contains(document.activeElement)
}

/**
 * Isolate the app's shortcuts from the terminal.
 *
 * The handler sits on the pane root in the bubble phase, so the terminal's
 * own listener has already run and the key has already reached the shell.
 * Stopping it here keeps the workspace's 1-9 shortcuts, the chat's Escape
 * and every other app chord from firing while the pane is on screen.
 */
function onPaneKeydown(event: KeyboardEvent): void {
  event.stopPropagation()
}

/** Move the keyboard to the pane's own controls. */
function focusControls(): void {
  hideButtonEl.value?.focus()
}

// ── Lifecycle ───────────────────────────────────────────────────────────

watch(() => store.activeSession?.session_id, (sessionId) => {
  if (!sessionId) {
    releaseScreen()
    return
  }
  if (rendererSession === sessionId) return
  void openScreen(sessionId)
}, { immediate: true })

watch(() => store.phase, () => {
  applyInputState()
  // The engine needs the size once the connection is live. The screen
  // measures itself when it is first laid out, which is often before the
  // handshake finished, so the size is sent again here.
  if (store.phase === 'live' && measured) store.resize(measured.cols, measured.rows)
})

watch(() => store.visible, (open) => {
  if (open) {
    opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    void nextTick(() => { if (store.visible) focusScreen() })
    return
  }
  // The pane is gone: the keyboard goes back to whatever opened it.
  const target = opener
  opener = null
  void nextTick(() => { if (target?.isConnected) target.focus() })
}, { flush: 'sync', immediate: true })

function onViewport(): void {
  isMobile.value = window.innerWidth < 768
}

onMounted(() => {
  onViewport()
  window.addEventListener('resize', onViewport)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onViewport)
  releaseScreen()
})
</script>

<template>
  <section
    class="terminal-pane"
    :class="{
      'terminal-pane--open': store.visible,
      'terminal-pane--drawer': !isDock,
      'terminal-pane--dock': isDock,
    }"
    aria-label="Terminal"
    @keydown.capture="onPaneKeydownCapture"
    @keydown="onPaneKeydown"
  >
    <header class="tp-head">
      <div class="tp-titles">
        <h2 class="tp-title">Terminal</h2>
        <p class="tp-status">
          <span class="tp-status-item">{{ hostLabel }}</span>
          <span class="tp-status-item tp-status-folder" :title="folderLabel">{{ folderLabel }}</span>
          <span class="tp-status-item">{{ platformLabel }}</span>
        </p>
      </div>
      <div class="tp-actions">
        <button
          type="button"
          class="btn-small tp-focus"
          :disabled="!hasScreen"
          title="Put the keyboard back in the terminal"
          @click="focusScreen"
        >Focus terminal</button>
        <button
          ref="hideButtonEl"
          type="button"
          class="btn-small tp-hide"
          title="Hide the terminal. The shell keeps running."
          @click="hide"
        >Hide</button>
        <button
          type="button"
          class="btn-small tp-end"
          :disabled="!canEnd"
          title="End this session and stop the shell"
          @click="end"
        >{{ store.ending ? 'Ending…' : 'End' }}</button>
      </div>
    </header>

    <p class="tp-warning">
      This is a shell on the engine's machine, not a sandbox: what runs here
      acts on the engine's files as the engine's user.
    </p>
    <p v-if="truncatedMessage" class="tp-truncated" role="status">{{ truncatedMessage }}</p>
    <p v-if="store.error" class="tp-error" role="alert">{{ store.error }}</p>

    <form v-if="store.phase === 'locked' && store.status?.available !== false" class="tp-unlock" @submit.prevent="unlock">
      <label class="tp-unlock-label" for="tp-terminal-password">Terminal password</label>
      <input
        id="tp-terminal-password"
        ref="passwordEl"
        v-model="password"
        class="tp-unlock-input"
        type="password"
        autocomplete="current-password"
        :disabled="store.unlocking"
      />
      <button type="submit" class="btn-primary tp-unlock-submit" :disabled="store.unlocking">
        {{ store.unlocking ? 'Unlocking…' : 'Unlock' }}
      </button>
      <p class="tp-unlock-hint">
        The terminal is locked until the engine's password is entered. The
        password is handed over once and not kept.
      </p>
    </form>

    <div v-else-if="store.status?.available !== false" class="tp-body">
      <div class="tp-surface-wrap">
        <div ref="surfaceEl" class="tp-surface" @click="focusScreen"></div>
        <p v-if="store.phase === 'connecting' || store.phase === 'reconnecting'" class="tp-phase" role="status">
          Connecting to the terminal…
        </p>
        <p v-else-if="store.phase === 'ended'" class="tp-phase" role="status">
          This session has ended. Its output stays on the engine until the session is removed.
        </p>
        <p v-else-if="!store.activeSession" class="tp-phase" role="status">
          No shell is open for this {{ store.target && 'projectId' in store.target ? 'project' : 'chat' }} yet.
        </p>
      </div>

      <aside class="tp-sessions" aria-label="Terminal sessions">
        <div class="tp-sessions-head">
          <h3 class="tp-sessions-title">Sessions</h3>
          <button type="button" class="btn-small tp-new" :disabled="store.creating" @click="create">
            {{ store.creating ? 'Starting…' : 'New session' }}
          </button>
        </div>
        <ul v-if="store.sessions.length" class="tp-session-list">
          <li v-for="session in store.sessions" :key="session.session_id" class="tp-session">
            <span class="tp-session-folder" :title="session.folder">{{ session.folder }}</span>
            <span class="tp-session-state">{{ sessionState(session) }}</span>
            <button
              v-if="session.session_id !== store.activeSession?.session_id"
              type="button"
              class="btn-small tp-session-open"
              @click="openSession(session)"
            >Open</button>
            <span v-else class="tp-session-current">Showing</span>
          </li>
        </ul>
        <p v-else class="tp-sessions-empty">No shell is running here yet.</p>
        <div v-if="store.phase === 'takeover'" class="tp-takeover" role="alert">
          <p class="tp-takeover-text">Another connection took over this terminal session.</p>
          <button type="button" class="btn-small tp-takeover-button" @click="takeover">Take over</button>
        </div>
      </aside>
    </div>

    <p class="tp-hint">
      The terminal keeps the keyboard while it has focus: Escape, Tab, the
      arrows, Ctrl+C and Ctrl+D go to the shell. Press
      <kbd>Shift</kbd>+<kbd>Tab</kbd> to come back to these controls, or use
      <strong>Hide</strong> to leave the terminal — the shell keeps running.
    </p>
  </section>
</template>

<style scoped>
.terminal-pane {
  display: none;
}
.terminal-pane--open {
  display: flex;
  flex-direction: column;
  background: var(--bg2);
  color: var(--fg);
}

/* The dock: a column beside the chat, the way the pinned file panel is one.
   A shell is something people keep open while they read, so it takes the
   pane's height and its own width, and the conversation keeps its measure
   instead of being squeezed by a strip underneath it. */
.terminal-pane--dock.terminal-pane--open {
  flex: 0 0 auto;
  width: clamp(360px, 38%, 620px);
  min-height: 0;
  border-left: 1px solid var(--border);
}
/* A dock is narrow, so the body stacks: the screen takes the room and the
   session list keeps a slice of the height under it. */
.terminal-pane--dock .tp-body {
  flex-direction: column;
}
.terminal-pane--dock .tp-sessions {
  flex: 0 0 auto;
  max-height: 30%;
}
.tp-head {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--border);
  flex: 0 0 auto;
}
.tp-titles { flex: 1; min-width: 0; }
.tp-title {
  margin: 0;
  font-size: var(--text-base);
  font-weight: 650;
}
.tp-status {
  margin: 2px 0 0;
  display: flex;
  flex-wrap: wrap;
  gap: 2px var(--space-2);
  font-size: var(--text-xs);
  color: var(--fg3);
  min-width: 0;
}
.tp-status-item { overflow-wrap: anywhere; }
/* The folder is a path: it is shown, and it is never wrapped into a
   column. A phone's drawer has room for a line of it, and the whole path
   is in the tooltip. */
.tp-status-folder {
  font-family: var(--font-mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
  min-width: 0;
}
.tp-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  flex: 0 0 auto;
  align-items: center;
}
.tp-warning,
.tp-truncated,
.tp-error,
.tp-hint {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-xs);
  color: var(--fg3);
  flex: 0 0 auto;
}
.tp-warning { color: var(--fg2); }
.tp-truncated { color: var(--warning); }
.tp-error { color: var(--error); }
.tp-hint { border-top: 1px solid var(--border); }
.tp-hint kbd {
  font-family: var(--font-mono);
  padding: 0 3px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
}
.tp-body {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
}
.tp-surface-wrap {
  position: relative;
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
}
.tp-surface {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.tp-phase {
  position: absolute;
  inset: auto var(--space-2) var(--space-2) var(--space-2);
  margin: 0;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--bg-elev) 92%, transparent);
  color: var(--fg2);
  font-size: var(--text-xs);
  pointer-events: none;
}
.tp-sessions {
  flex: 0 0 240px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  overflow: auto;
}
.tp-sessions-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.tp-sessions-title {
  margin: 0;
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--fg3);
}
.tp-session-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.tp-session {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 4px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg3);
}
.tp-session-folder {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tp-session-state,
.tp-session-current {
  flex: 0 0 auto;
  font-size: var(--text-xs);
  color: var(--fg3);
}
.tp-sessions-empty {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--fg3);
}
.tp-takeover {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2);
  border: 1px solid var(--warning);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--warning) 12%, transparent);
}
.tp-takeover-text {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--fg);
}
.tp-unlock {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-3);
  overflow: auto;
}
.tp-unlock-label { font-size: var(--text-sm); }
.tp-unlock-input {
  min-width: 220px;
  padding: 6px 8px;
  background: var(--bg-elev);
  color: var(--fg);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  font: inherit;
}
.tp-unlock-input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tp-unlock-hint {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--fg3);
  max-width: 46ch;
}

/* A phone-sized pane is a drawer: it covers the chat column, because a shell
   needs the whole screen to be useful, and a narrow column beside a chat is
   not. The class is set from the same width the layout uses, so the two
   cannot disagree about which shape the pane is in. */
.terminal-pane--drawer.terminal-pane--open {
  position: absolute;
  inset: 0;
  z-index: 60;
  height: auto;
  min-height: 0;
  padding-bottom: var(--safe-bottom);
}
.terminal-pane--drawer .tp-body {
  flex-direction: column;
}
.terminal-pane--drawer .tp-sessions {
  flex: 0 0 auto;
  max-height: 34%;
}
/* On a phone the header stacks: the buttons take their own row, so the
   status line gets the full width and the path is a line of it rather
   than a column. A dock stacks it for the same reason: it is a narrow
   column, and a row of three buttons leaves the title no room. */
.terminal-pane--drawer .tp-head,
.terminal-pane--dock .tp-head {
  flex-direction: column;
  align-items: stretch;
}
.terminal-pane--drawer .tp-actions,
.terminal-pane--dock .tp-actions {
  justify-content: flex-start;
}
@media (pointer: coarse) {
  .tp-actions :deep(button),
  .tp-session-open,
  .tp-takeover-button,
  .tp-new,
  .tp-unlock-submit {
    min-height: var(--touch);
    min-width: var(--touch);
  }
  .tp-session { min-height: var(--touch); }
}
</style>
