<script setup lang="ts">
/**
 * The browser terminal's pane: the surface a person actually touches.
 *
 * The store owns the wire — the socket, the offsets, the session list, the
 * grant. This pane owns the screen and the controls around it. The rules
 * that are easy to get wrong are written down here rather than left to the
 * next reader's memory.
 *
 * **There is no unlock form.** The terminal used to ask for the password
 * again before it showed a shell. It does not any more: the signed-in
 * session is the terminal's trust boundary, and the store asks the engine
 * for the grant by itself. The pane has nothing to ask for, so it shows
 * the shell.
 *
 * **One screen, one session.** A renderer is a physical terminal: it is
 * created for the session the pane is showing and disposed when the pane
 * gives it up. It is never re-bound to another session, because a screen
 * that showed two sessions would be showing two histories at once.
 *
 * **Close is not End.** Closing the pane leaves every shell running on the
 * engine; closing an individual session tab ends only that shell.
 *
 * **The terminal belongs to the chat on screen.** The layout names the chat
 * (`chatId`, `''` off a chat route) and the store's `followChat` hides the
 * terminal when it changes and shows it again on return, if it was open when
 * the person left. A switch moves no focus.
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
 *
 * **A soft keyboard has no Escape.** A phone or tablet cannot produce
 * Escape, Tab, the arrows, Ctrl+C or Ctrl+D, so on a coarse pointer the
 * pane shows a row of on-screen keys under the screen (#1894). The row is
 * `TerminalKeyRow.vue`; the bytes come from `lib/terminalKeys.ts`, and the
 * arrows follow the screen's cursor-key mode. Each key goes through the same
 * input path as a keystroke, and none of them takes the focus from the
 * shell: a tap that moved the focus would also dismiss the soft keyboard.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useTerminalStore } from '../stores/terminal'
import type { TerminalSessionInfo } from '../lib/terminalProtocol'
import type { TerminalFolderChoice, TerminalLocation } from '../stores/terminal'
import type { TerminalRenderer } from '../lib/terminalRenderer'
import { terminalKeyBytes, type TerminalKey } from '../lib/terminalKeys'
import TerminalKeyRow from './TerminalKeyRow.vue'

const store = useTerminalStore()

/**
 * Whether the pane is in the slot beside the chat.
 *
 * The layout owns the slot: a pinned file takes it, and the terminal takes
 * it when no file has and the pane has room for both columns. The pane only
 * draws the shape it is told — the tile when it is in the slot, the drawer
 * over the pane when it is not.
 *
 * `chatId` is the chat on screen, as the layout names it (`''` off a chat
 * route); the terminal follows it.
 */
const props = withDefaults(defineProps<{ inTile?: boolean; chatId?: string }>(), { inTile: false, chatId: '' })
const emit = defineEmits<{ open: [boolean] }>()

/** The element the screen is mounted into. It exists for the pane's whole life. */
const surfaceEl = ref<HTMLElement | null>(null)
/** The control that leaves the terminal, so the focus escape has somewhere to go. */
const closeButtonEl = ref<HTMLButtonElement | null>(null)
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
/** A phone-sized pane is a drawer; a wide one docks beside the chat. */
const isMobile = ref(false)
/**
 * The shape this pane is in.
 *
 * The tile is the slot the pinned file uses too: the same window, the same
 * divider, the same remembered ratio. Everything else — a phone, a pane too
 * narrow for both columns, a pinned file that owns the slot — is the drawer
 * over the pane, which is always usable.
 */
const isTile = computed(() => props.inTile && !isMobile.value)

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
  if (store.visible && !store.chatSwitch) created.focus()
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

// ── On-screen keys ──────────────────────────────────────────────────────

const keyEncoder = new TextEncoder()

/** Only a screen that may type may press a key for the user. */
const keysEnabled = computed(() => renderer.value !== null && store.unlocked && store.phase === 'live')

/**
 * Send one on-screen key, exactly as a keystroke on the screen would.
 *
 * The bytes go through `store.sendInput`, the path the renderer's own
 * keystrokes take, so they obey the same size and back-pressure rules and
 * never bypass the connection's acknowledgement. The arrows follow the
 * screen's cursor-key mode, read at the moment of the press.
 *
 * A tap is not supposed to move the focus (the row prevents it), but if a
 * browser moved it onto the key anyway, it goes back to the screen so the
 * soft keyboard stays with the shell. A keyboard activation keeps the
 * focus on the key, so a keyboard user can press it again.
 */
function pressKey(key: TerminalKey, pointer: boolean): void {
  const current = renderer.value
  if (!current || !keysEnabled.value) return
  store.sendInput(keyEncoder.encode(terminalKeyBytes(key, current.applicationCursorKeys)))
  if (pointer && keyRowHasFocus()) focusScreen()
}

/** True when the focus sits on one of the on-screen keys. */
function keyRowHasFocus(): boolean {
  const active = document.activeElement
  return active instanceof HTMLElement && active.closest('.tp-keys') !== null
}

// ── Actions ─────────────────────────────────────────────────────────────

function closePane(): void {
  // Closing detaches; it never ends. The shell keeps running on the engine.
  store.hide()
}

async function end(session: TerminalSessionInfo): Promise<void> {
  await store.endSession(session.session_id)
}

/**
 * The "where" question, asked when a scope offers two folders.
 *
 * A chat with a linked repository can start a shell in the workspace or in
 * the repository, and neither is the obvious one, so the pane asks: on its
 * own when no shell is running, and after "+" when one is.
 */
const choosing = ref(false)
const offersChoice = computed(() => store.locations.length > 1)
/** Phases with a live grant and a listed scope: a pick can be acted on. */
const CHOOSER_PHASES = new Set<string>(['ready', 'connecting', 'live', 'reconnecting', 'ended'])
const showChooser = computed(() => offersChoice.value
  && CHOOSER_PHASES.has(store.phase)
  && (choosing.value || (!store.activeSession && !store.creating)))

const LOCATION_LABELS: Record<TerminalLocation, string> = {
  workspace: 'Workspace',
  repo: 'Repository',
}

function folderName(folder: string): string {
  return folder.split(/[\\/]/).filter(Boolean).at(-1) || folder
}

async function create(): Promise<void> {
  if (offersChoice.value) {
    choosing.value = !choosing.value
    return
  }
  await store.createSession()
}

async function createIn(choice: TerminalFolderChoice): Promise<void> {
  choosing.value = false
  await store.createSession(choice.location)
}

function openSession(session: TerminalSessionInfo): void {
  store.selectSession(session.session_id)
}

function onTabKeydown(event: KeyboardEvent, index: number): void {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  const tablist = (event.currentTarget as HTMLElement).closest('[role="tablist"]')
  const tabs = [...(tablist?.querySelectorAll<HTMLButtonElement>('[role="tab"]') ?? [])]
  if (!tabs.length) return
  event.preventDefault()
  const next = event.key === 'Home' ? 0
    : event.key === 'End' ? tabs.length - 1
      : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
  tabs[next].focus()
  const session = store.sessions[next]
  if (session) openSession(session)
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
  closeButtonEl.value?.focus()
}

// ── Lifecycle ───────────────────────────────────────────────────────────

watch(() => store.target, () => { choosing.value = false })

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
  // The layout owns the slot and needs to know whether the terminal is on
  // screen: a hidden terminal gives the slot back.
  emit('open', open)
  // A chat switch showed or hid the terminal: the keyboard stays where the
  // navigation left it, and the old opener is forgotten.
  if (store.chatSwitch) { opener = null; return }
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

watch(() => props.chatId, (chatId) => { store.followChat(chatId) }, { immediate: true })

function onViewport(): void {
  isMobile.value = window.innerWidth < 768
}

onMounted(() => {
  onViewport()
  window.addEventListener('resize', onViewport)
  // The screen draws into an element that does not exist yet when the setup
  // runs, so the watcher above did nothing for a session that was already
  // open. A pane that mounts again — it moves between the slot and the
  // drawer — finds its session here.
  const session = store.activeSession
  if (session) void openScreen(session.session_id)
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
      'terminal-pane--drawer': !isTile,
      'terminal-pane--tile': isTile,
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
      <!-- Close sits top right, where the pinned file tile keeps its own. -->
      <button ref="closeButtonEl" type="button" class="btn-icon tp-close" title="Close terminal" aria-label="Close" @click="closePane">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>
      </button>
    </header>

    <p class="tp-warning">
      This is a shell on the engine's machine, not a sandbox: what runs here
      acts on the engine's files as the engine's user.
    </p>
    <p v-if="truncatedMessage" class="tp-truncated" role="status">{{ truncatedMessage }}</p>
    <p v-if="store.error" class="tp-error" role="alert">{{ store.error }}</p>

    <template v-if="store.status?.available !== false">
      <nav class="tp-sessions" aria-label="Terminal sessions">
        <div class="tp-tabs" role="tablist" aria-label="Terminal sessions">
          <div v-for="session in store.sessions" :key="session.session_id" class="tp-tab-wrap">
            <button type="button" class="tp-tab" role="tab"
              :aria-selected="session.session_id === store.activeSession?.session_id"
              :tabindex="session.session_id === store.activeSession?.session_id ? 0 : -1"
              :title="`${session.folder} · ${sessionState(session)}`"
              @keydown="onTabKeydown($event, store.sessions.indexOf(session))" @click="openSession(session)">
              <span class="tp-session-folder">{{ folderName(session.folder) }}</span>
              <span class="tp-session-state">{{ sessionState(session) }}</span>
            </button>
            <button type="button" class="btn-icon tp-session-end" :aria-label="`End terminal session ${session.folder}`" title="End session" :disabled="store.ending" @click="end(session)">×</button>
          </div>
          <button type="button" class="btn-icon tp-new" aria-label="New terminal session" :title="store.creating ? 'Starting…' : 'New session'" :aria-expanded="offersChoice ? choosing : undefined" :disabled="store.creating || store.sessions.length >= 4" @click="create">+</button>
        </div>
        <div v-if="store.phase === 'takeover'" class="tp-takeover" role="alert">
          <p class="tp-takeover-text">Another connection took over this terminal session.</p>
          <button type="button" class="btn-small tp-takeover-button" @click="takeover">Take over</button>
        </div>
      </nav>
      <div class="tp-body">
      <div class="tp-surface-wrap">
        <div ref="surfaceEl" class="tp-surface" @click="focusScreen"></div>
        <p v-if="store.phase === 'connecting' || store.phase === 'reconnecting'" class="tp-phase" role="status">
          Connecting to the terminal…
        </p>
        <p v-else-if="store.phase === 'ended'" class="tp-phase" role="status">
          This session has ended. Its output stays on the engine until the session is removed.
        </p>
        <div v-if="showChooser" class="tp-chooser" role="group" aria-label="Where to open the shell">
          <p class="tp-chooser-title">Open a shell in</p>
          <div class="tp-chooser-options">
            <button
              v-for="choice in store.locations"
              :key="choice.location"
              type="button"
              class="tp-chooser-option"
              :title="choice.folder"
              :disabled="store.creating || store.sessions.length >= 4"
              @click="createIn(choice)"
            >
              <span class="tp-chooser-label">{{ LOCATION_LABELS[choice.location] }}</span>
              <span class="tp-chooser-folder">{{ folderName(choice.folder) }}</span>
            </button>
          </div>
          <button v-if="store.activeSession" type="button" class="btn-small tp-chooser-cancel" @click="choosing = false">Cancel</button>
        </div>
        <p v-else-if="!store.activeSession && store.creating" class="tp-phase" role="status">
          Starting a shell…
        </p>
        <p v-else-if="!store.activeSession" class="tp-phase" role="status">
          No shell is open for this {{ store.target && 'projectId' in store.target ? 'project' : 'chat' }} yet.
        </p>
      </div>

      <!-- The keys a soft keyboard cannot produce. Rendered only with a
           screen, and shown only under a coarse pointer (see TerminalKeyRow). -->
      <TerminalKeyRow v-if="renderer" :disabled="!keysEnabled" @press="pressKey" />

      </div>
    </template>

    <p class="tp-hint">
      The terminal keeps the keyboard while it has focus: Escape, Tab, the
      arrows, Ctrl+C and Ctrl+D go to the shell.
      <span class="tp-hint-touch">On a touch screen, the keys under the terminal send them.</span>
      Press
      <kbd>Shift</kbd>+<kbd>Tab</kbd> to come back to these controls, or use
      <strong>Close</strong> to leave the terminal — the shell keeps running.
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

/* The tile: the slot the pinned file uses too. The tile draws the window —
   the inset, the radius, the shadow — and the terminal is its content, so
   the pane draws no frame of its own and fills the tile. */
.terminal-pane--tile.terminal-pane--open {
  flex: 1 1 auto;
  height: 100%;
  min-height: 0;
  background: transparent;
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
.tp-close { flex: 0 0 auto; }
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
.tp-hint-touch { display: none; }
.tp-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
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
.tp-chooser {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  padding: var(--space-3);
  background: color-mix(in srgb, var(--bg) 94%, transparent);
  border-radius: var(--radius-sm);
}
.tp-chooser-title {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--fg2);
}
.tp-chooser-options {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: var(--space-2);
}
.tp-chooser-option {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  min-width: 160px;
  max-width: 240px;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.tp-chooser-option:hover:not(:disabled) { border-color: var(--border-strong); background: var(--bg3); }
.tp-chooser-option:disabled { opacity: 0.5; cursor: default; }
.tp-chooser-option:focus-visible,
.tp-chooser-cancel:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tp-chooser-label { font-size: var(--text-sm); font-weight: 600; }
.tp-chooser-folder {
  max-width: 100%;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--fg3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tp-sessions {
  flex: 0 0 auto;
  min-width: 0;
  overflow: hidden;
}
.tp-tabs {
  display: flex;
  align-items: center;
  gap: 2px;
  overflow-x: auto;
  padding: 0 var(--space-3) var(--space-1);
  border-bottom: 1px solid var(--border);
}
.tp-tab-wrap {
  display: flex;
  align-items: center;
  flex: 0 0 auto;
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  background: var(--bg3);
}
.tp-tab-wrap:has(.tp-tab[aria-selected="true"]) {
  background: var(--bg);
  border-bottom-color: var(--bg);
}
.tp-tab {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  max-width: 220px;
  padding: 6px 8px;
  border: 0;
  background: transparent;
  color: var(--fg);
  cursor: pointer;
}
.tp-tab:focus-visible,
.tp-session-end:focus-visible,
.tp-new:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.tp-session-folder {
  min-width: 0;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tp-session-state {
  flex: 0 0 auto;
  font-size: var(--text-xs);
  color: var(--fg3);
}
.tp-session-end { flex: 0 0 auto; }
.tp-new { flex: 0 0 auto; }
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

/* The drawer: the terminal covers the pane it is in, because a shell needs
   the whole screen to be useful, and a narrow column beside a chat is not.
   The layout decides the shape — it measured the room and told the pane
   whether it could have the slot — and the viewport has the last word: a
   phone-sized pane is a drawer whatever the layout says. */
.terminal-pane--drawer.terminal-pane--open {
  position: absolute;
  inset: 0;
  z-index: 60;
  height: auto;
  min-height: 0;
  padding-bottom: var(--safe-bottom);
}
@media (pointer: coarse) {
  .tp-hint-touch { display: inline; }
  .tp-close,
  .tp-session-end,
  .tp-takeover-button,
  .tp-new {
    min-height: var(--touch);
    min-width: var(--touch);
  }
  .tp-tab-wrap { min-height: var(--touch); }
  .tp-chooser-option,
  .tp-chooser-cancel { min-height: var(--touch); }
  .tp-tab { min-height: var(--touch); }
}
</style>
