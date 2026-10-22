<script setup lang="ts">
/**
 * The command the assistant suggested for this chat.
 *
 * The command is untrusted text the assistant wrote: Ciao has not checked it,
 * does not run it and cannot vouch for it. The card shows it verbatim and
 * offers three things — copy it, open the terminal for this chat, dismiss it
 * — and nothing else. It never runs the command and never pastes it into the
 * shell: *Open in Terminal* starts no shell, asks for no grant and asks the
 * engine to run nothing — an already-live session attaches and shows the
 * screen it already holds, exactly like the rail's button. The user reviews
 * the command and decides.
 *
 * The offer is transient and engine-owned. The browser holds a copy of it in
 * memory, and a replacement (a new id) replaces what is on screen. The card
 * is therefore *not* keyed by the offer id: a 409 that swaps the command
 * patches this component in place, so the notice explaining the swap survives
 * it. The id is watched instead, and only the copy's own state follows it.
 */
import { onBeforeUnmount, ref, useId, watch, nextTick } from 'vue'
import { COPY_FEEDBACK_MS, writeClipboard } from '../lib/codeCopy'
import { shouldRestoreFocus } from '../lib/moveToBackground'
import { useProjectStore } from '../stores/projects'
import BrowserTerminalButton from './BrowserTerminalButton.vue'
import type { TerminalOffer } from '../lib/types'

const props = defineProps<{
  /** The chat that owns the offer. The terminal opens for this chat only. */
  chatId: string
  /** The engine's offer: an id and the command it names. */
  offer: TerminalOffer
}>()

const store = useProjectStore()
const titleId = useId()
/** The card itself, so the focus can rest on it while a dismissal is out. */
const rootEl = ref<HTMLElement | null>(null)
/** The dismissal control, so the focus can go back to it. */
const dismissEl = ref<HTMLButtonElement | null>(null)

/** The copy button's own state: idle, or the confirmation of a copy. */
const copyState = ref<'idle' | 'copied'>('idle')
/** A retryable failure, in the user's words. */
const errorMessage = ref('')
/** The suggestion changed under the dismissal, so a newer one is on screen. */
const conflictNotice = ref('')
/** True while the dismissal is in flight. */
const dismissing = ref(false)

let copyTimer: ReturnType<typeof setTimeout> | null = null
/** True once the card is gone. A late answer must not touch it. */
let disposed = false
/**
 * The newest copy attempt. An older one that resolves later may not write
 * its outcome: two overlapping clicks would otherwise cross their states.
 */
let copyAttempt = 0

function clearCopyTimer(): void {
  if (copyTimer === null) return
  clearTimeout(copyTimer)
  copyTimer = null
}

// A new offer is a new command, so the confirmation of the old one is about
// something that is no longer on screen and goes. The conflict notice stays:
// it explains the swap, and the swap is what the user is looking at now.
watch(() => props.offer.id, () => {
  clearCopyTimer()
  copyState.value = 'idle'
  errorMessage.value = ''
})

/**
 * Copy the command exactly as shown.
 *
 * The id and the text are captured before the clipboard is written, because
 * the answer arrives later: by then the engine may have replaced the offer,
 * and the clipboard would hold the command that was on screen while the card
 * shows a different one. Claiming "Copied" then would be a lie about what
 * the user is about to paste.
 */
async function copy(): Promise<void> {
  const attempt = ++copyAttempt
  const id = props.offer.id
  const command = props.offer.command
  // Every attempt starts from scratch. A click after a confirmed copy must
  // not leave "Copied" on screen next to the error of a failing attempt.
  copyState.value = 'idle'
  errorMessage.value = ''
  const copied = await writeClipboard(command)
  // The card is gone, or a later click owns the outcome: this answer is not
  // the card's to show.
  if (disposed || attempt !== copyAttempt) return
  if (props.offer.id !== id) {
    errorMessage.value = 'The suggestion changed before the copy finished. Copy the newer command.'
    return
  }
  if (!copied) {
    // The clipboard refused it. The command is still on screen and the
    // button is still there: the copy is the user's to try again.
    errorMessage.value = 'The command could not be copied. Try again.'
    return
  }
  copyState.value = 'copied'
  clearCopyTimer()
  copyTimer = setTimeout(() => {
    copyTimer = null
    if (disposed) return
    copyState.value = 'idle'
  }, COPY_FEEDBACK_MS)
}

/**
 * Dismiss the suggestion.
 *
 * The dismissal names the offer it means, so a suggestion that arrived while
 * the request was in flight is not dismissed by mistake: the engine answers
 * with a conflict and its own newer offer, which is adopted and shown. A
 * failure is the user's to retry — nothing is resubmitted here.
 */
async function dismiss(): Promise<void> {
  if (dismissing.value) return
  const expectedId = props.offer.id
  // The button is about to be disabled, and a disabled button drops the
  // focus onto the page. Move it to the card first: the focus belongs to
  // the card until the card goes, and the panel hands it to the composer
  // then. A dismissal must not leave the keyboard on the page.
  if (rootEl.value?.contains(document.activeElement)) rootEl.value.focus()
  dismissing.value = true
  errorMessage.value = ''
  try {
    const result = await store.dismissTerminalOffer(props.chatId, expectedId)
    // The card is gone: there is nothing left to report on.
    if (disposed) return
    if (result === 'conflict') {
      conflictNotice.value = 'Not dismissed: the suggestion changed — the newer one is shown.'
    }
  } catch {
    if (disposed) return
    // The request never landed, or the engine refused it. The offer on
    // screen is unchanged, and the dismissal belongs to the user to repeat.
    errorMessage.value = 'The suggestion could not be dismissed. Try again.'
  }
  if (disposed) return
  dismissing.value = false
  // The card is still here — a conflict, or a failure — and the button is
  // live again. The focus goes back to the control the user pressed, but
  // only while it is still the card's: while the request was out the user
  // may have moved the focus, most likely into the draft to write the next
  // message, and taking it back would interrupt them and could turn their
  // next Enter into a control they did not choose.
  await nextTick()
  if (shouldRestoreFocus(rootEl.value, document.activeElement)) {
    dismissEl.value?.focus()
  }
}

onBeforeUnmount(() => {
  disposed = true
  clearCopyTimer()
})
</script>

<template>
  <section
    ref="rootEl"
    class="terminal-offer"
    role="group"
    :aria-labelledby="titleId"
    tabindex="-1"
  >
    <p :id="titleId" class="terminal-offer-title">Suggested command — review before running</p>
    <p class="terminal-offer-hint">
      The assistant wrote this. Ciao has not checked it and will not run it.
      The shell it opens is on the engine's computer.
    </p>

    <!-- The command as literal text: no markup, no markdown, no links. -->
    <pre
      class="terminal-offer-command"
      tabindex="0"
      role="region"
      aria-label="Suggested command"
    ><code>{{ offer.command }}</code></pre>

    <p v-if="conflictNotice" class="terminal-offer-notice" role="status">{{ conflictNotice }}</p>
    <p v-if="errorMessage" class="terminal-offer-error" role="alert">{{ errorMessage }}</p>
    <span class="sr-only terminal-offer-status" role="status">{{ copyState === 'copied' ? 'Command copied to the clipboard' : '' }}</span>

    <div class="terminal-offer-actions">
      <!-- Never disabled: a failed copy is the user's to repeat. -->
      <button
        type="button"
        class="terminal-offer-action terminal-offer-copy"
        @click="copy"
      >{{ copyState === 'copied' ? 'Copied' : 'Copy' }}</button>

      <!-- The one way into the browser terminal. It opens the panel for this
           chat and starts no shell: that stays an explicit act. -->
      <span class="terminal-offer-open"><BrowserTerminalButton :chat-id="chatId" /></span>

      <button
        ref="dismissEl"
        type="button"
        class="terminal-offer-action terminal-offer-dismiss"
        :disabled="dismissing"
        @click="dismiss"
      >{{ dismissing ? 'Dismissing…' : 'Dismiss' }}</button>
    </div>
  </section>
</template>

<style scoped>
.terminal-offer {
  /* A flex child with unbreakable content needs min-width: 0, or a long
     command widens the chat column instead of wrapping inside the card.
     It must also keep its own height: a short viewport may squeeze the
     cards above it, never this one — a suggestion waiting to be reviewed
     stays readable. */
  min-width: 0;
  max-width: 100%;
  flex-shrink: 0;
  margin: 8px 0;
  padding: 12px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--fg2);
  font-size: var(--text-sm);
  text-align: left;
}
.terminal-offer-title {
  margin: 0;
  color: var(--fg);
  font-weight: 600;
}
/* The card holds the focus while a dismissal is out, so the focus has to be
   visible on the card and not only on the button that pressed it. Only for a
   keyboard: a mouse dismissal must not flash a ring around the card. */
.terminal-offer:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.terminal-offer-hint {
  margin: 4px 0 0;
  line-height: 1.5;
}
.terminal-offer-command {
  /* The command is data, not prose: it keeps its own line breaks and wraps
     anywhere, inside a bounded box the user can scroll. */
  margin: 10px 0 0;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 240px;
  max-width: 100%;
  overflow: auto;
}
.terminal-offer-command:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.terminal-offer-notice,
.terminal-offer-error {
  margin: 8px 0 0;
  line-height: 1.5;
}
.terminal-offer-notice { color: var(--fg2); }
.terminal-offer-error { color: var(--error); }
.terminal-offer-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  margin-top: 12px;
}
.terminal-offer-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 32px;
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--fg);
  font-family: inherit;
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease);
}
.terminal-offer-action:hover:not(:disabled) { background: var(--bg3); border-color: var(--fg2); }
.terminal-offer-action:disabled { opacity: 0.5; cursor: not-allowed; }
.terminal-offer-action:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
/* The terminal's own button is a text action; inside this card it is one of
   three, so it keeps its voice and gains the card's touch target. */
.terminal-offer-open :deep(.terminal-button) {
  display: inline-flex;
  align-items: center;
  min-height: 32px;
}
.terminal-offer-open :deep(.terminal-button:focus-visible) {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
@media (pointer: coarse) {
  .terminal-offer-action { min-height: var(--touch); }
  .terminal-offer-open :deep(.terminal-button) { min-height: var(--touch); }
}
</style>
