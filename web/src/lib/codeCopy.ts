/**
 * Copy affordance for fenced code blocks in rendered chat markdown.
 *
 * Chat markdown reaches the DOM through `v-html` (see `lib/safeMarkdown.ts`),
 * so this button cannot be a Vue component: the markdown renderer emits it as
 * plain markup and one delegated click listener on the chat panel drives every
 * instance. Delegation is also what keeps the button alive during streaming —
 * a bubble's innerHTML is replaced on every token, so a listener bound to an
 * individual button node would die with the node it was attached to.
 */

export const CODE_BLOCK_CLASS = 'code-block'
export const CODE_COPY_CLASS = 'code-copy-btn'

type CopyState = 'idle' | 'copied' | 'failed'

const STATE_TEXT: Record<CopyState, { label: string; aria: string }> = {
  idle: { label: 'Copy', aria: 'Copy code' },
  copied: { label: 'Copied!', aria: 'Code copied' },
  failed: { label: 'Failed', aria: 'Copy failed' },
}

/** How long the confirmation (or failure) label stays before reverting. */
export const COPY_FEEDBACK_MS = 1500

const revertTimers = new WeakMap<HTMLButtonElement, number>()

/** Markup for the per-block copy button, emitted by the markdown renderer. */
export function codeCopyButtonHtml(): string {
  const { label, aria } = STATE_TEXT.idle
  return (
    `<button type="button" class="${CODE_COPY_CLASS}" data-copy-state="idle"`
    + ` aria-label="${aria}" title="${aria}">${label}</button>`
  )
}

/**
 * The block's raw text: `textContent` deliberately, so syntax markup (and the
 * language, which lives in a `class`, never in the text) stays out of it.
 */
export function codeBlockText(block: Element | null | undefined): string {
  const source = block?.querySelector('pre code') ?? block?.querySelector('pre')
  // marked always terminates a fenced block with a newline; the user did not
  // select it, so do not paste it either.
  return (source?.textContent ?? '').replace(/\n$/, '')
}

function setState(button: HTMLButtonElement, state: CopyState): void {
  const { label, aria } = STATE_TEXT[state]
  button.dataset.copyState = state
  button.textContent = label
  button.setAttribute('aria-label', aria)
  button.setAttribute('title', aria)
}

function scheduleRevert(button: HTMLButtonElement): void {
  const pending = revertTimers.get(button)
  if (pending !== undefined) clearTimeout(pending)
  const timer = setTimeout(() => {
    revertTimers.delete(button)
    // The button may have been replaced by a re-render in the meantime; that
    // node is already back in the idle state, so touching this one is inert.
    setState(button, 'idle')
  }, COPY_FEEDBACK_MS) as unknown as number
  revertTimers.set(button, timer)
}

/**
 * Legacy path for insecure origins (Ciaobot is routinely opened over plain
 * http on the LAN), where `navigator.clipboard` is simply absent.
 */
function legacyCopy(text: string): boolean {
  if (typeof document === 'undefined' || typeof document.execCommand !== 'function') return false
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.top = '-1000px'
  area.style.opacity = '0'
  document.body.appendChild(area)
  try {
    area.select()
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    area.remove()
  }
}

export async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // Permission denied or an insecure context — fall through to the legacy path.
  }
  return legacyCopy(text)
}

/** Copy one block's text and flash the result on its button. */
export async function copyCodeBlock(button: HTMLButtonElement): Promise<boolean> {
  const text = codeBlockText(button.closest(`.${CODE_BLOCK_CLASS}`))
  if (!text) return false
  const copied = await writeClipboard(text)
  setState(button, copied ? 'copied' : 'failed')
  scheduleRevert(button)
  return copied
}

/**
 * Delegated click handler. Returns true when the event hit a copy button, so
 * the caller can skip its other click handling for that event.
 */
export function handleCodeCopyClick(event: Event): boolean {
  const target = event.target
  if (!(target instanceof Element)) return false
  const button = target.closest(`button.${CODE_COPY_CLASS}`)
  if (!(button instanceof HTMLButtonElement)) return false
  event.preventDefault()
  event.stopPropagation()
  void copyCodeBlock(button)
  return true
}
