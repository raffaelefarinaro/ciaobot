// @vitest-environment jsdom

/**
 * `components/TerminalOfferCard.vue` — the command the assistant suggested.
 *
 * The command is untrusted text the assistant wrote. Ciao does not check it,
 * does not run it and does not vouch for it; the card shows it verbatim and
 * offers three things: copy it, open the terminal for this chat, dismiss it.
 *
 * The tests below hold that line: the command is literal text (never markup),
 * the clipboard receives exactly what is on screen, the copy only claims
 * success when the clipboard said so, Open reuses the existing terminal entry
 * and starts no shell, and a dismissal names the offer it meant to dismiss.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import TerminalOfferCard from '../TerminalOfferCard.vue'
import { useTerminalStore } from '../../stores/terminal'
import type { TerminalOffer } from '../../lib/types'

const writeClipboard = vi.hoisted(() => vi.fn(async (_text: string) => true))
const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())

vi.mock('../../lib/codeCopy', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/codeCopy')>()
  return { ...actual, writeClipboard }
})

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel, postForm: vi.fn() },
}))

const CHAT_ID = 'chat-1'
const TITLE = 'Suggested command — review before running'

function offer(overrides: Partial<TerminalOffer> = {}): TerminalOffer {
  return { id: 'offer-1', command: 'ciao service start --workspace /Users/me/Ciao', ...overrides }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

let wrapper: VueWrapper | null = null

function mountCard(props: { chatId?: string; offer?: TerminalOffer } = {}) {
  // A Pinia of its own per test: the card reads the project store, and a
  // store shared with another test would carry another test's offers.
  const pinia = createPinia()
  setActivePinia(pinia)
  wrapper = mount(TerminalOfferCard, {
    props: { chatId: CHAT_ID, offer: offer(), ...props },
    // In the document: the card moves the focus while a dismissal is out,
    // and focus only exists in a document.
    attachTo: document.body,
    global: { plugins: [pinia] },
  })
  return wrapper
}

/**
 * The command as it is on screen, byte for byte.
 *
 * `textContent`, not VTU's `text()`: the latter trims, so it would pass for a
 * template that silently trimmed the command — while the copy still handed
 * the raw string to the clipboard.
 */
function renderedCommand(card: VueWrapper): string | null {
  return card.get('.terminal-offer-command code').element.textContent
}

beforeEach(() => {
  writeClipboard.mockReset().mockResolvedValue(true)
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDel.mockReset()
  apiGet.mockImplementation((path: string) => {
    if (path === '/api/terminal/status') {
      return Promise.resolve({
        available: true,
        reason: '',
        platform: 'macos',
        host: 'engine.local',
        unlock_expires_in: 900,
        detached_ttl_seconds: 600,
      })
    }
    if (path.startsWith('/api/terminal/sessions')) return Promise.resolve({ sessions: [] })
    return Promise.resolve({})
  })
  apiPatch.mockResolvedValue({})
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
  useTerminalStore().dispose()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('TerminalOfferCard', () => {
  it('carries the fixed warning and says who wrote the command', () => {
    const card = mountCard()

    expect(card.get('.terminal-offer-title').text()).toBe(TITLE)
    const hint = card.get('.terminal-offer-hint').text()
    // The four things a reader has to know before they run anything.
    expect(hint).toContain('The assistant wrote this')
    expect(hint).toContain('has not checked it')
    expect(hint).toContain('will not run it')
    expect(hint).toContain("engine's computer")
  })

  it('renders the command as literal text: no element, no link, no markdown', () => {
    const command = [
      '<img src=x onerror="alert(1)">',
      '[run me](javascript:alert(1))',
      'https://example.invalid/run-now',
      '`rm -rf /`',
    ].join('\n')
    const card = mountCard({ offer: offer({ command }) })

    // The text is the text: nothing in it became an element.
    expect(renderedCommand(card)).toBe(command)
    expect(card.find('.terminal-offer-command img').exists()).toBe(false)
    expect(card.find('.terminal-offer-command a').exists()).toBe(false)
    expect(card.find('.terminal-offer-command script').exists()).toBe(false)
    expect(card.findAll('a')).toHaveLength(0)
    expect(card.findAll('img')).toHaveLength(0)
  })

  it('shows the command exactly as the engine sent it, spaces and newlines included', () => {
    // Leading and trailing whitespace, and a trailing newline: a template
    // that trimmed the command would still pass a `.text()` comparison.
    const command = '  ciao service start \\\n  --workspace /Users/me/Ciao  \n'
    const card = mountCard({ offer: offer({ command }) })

    expect(renderedCommand(card)).toBe(command)
  })

  it('makes the command a named, keyboard-reachable region', () => {
    const card = mountCard()
    const region = card.get('.terminal-offer-command')

    // A long command scrolls, and a scrollable region that cannot be
    // reached with the keyboard cannot be read by one.
    expect(region.attributes('tabindex')).toBe('0')
    expect(region.attributes('role')).toBe('region')
    expect(region.attributes('aria-label')).toBe('Suggested command')
  })

  it('copies exactly what is on screen, newlines and trailing spaces included', async () => {
    const command = 'ciao service start \\\n  --workspace /Users/me/Ciao  '
    const card = mountCard({ offer: offer({ command }) })

    await card.get('.terminal-offer-copy').trigger('click')
    await flushPromises()

    expect(writeClipboard).toHaveBeenCalledTimes(1)
    expect(writeClipboard).toHaveBeenCalledWith(command)
    // The claim follows the clipboard, not the click.
    expect(card.get('.terminal-offer-copy').text()).toBe('Copied')
  })

  it('claims Copied only after the clipboard answers', async () => {
    const pending = deferred<boolean>()
    writeClipboard.mockReturnValueOnce(pending.promise)
    const card = mountCard()

    await card.get('.terminal-offer-copy').trigger('click')
    await flushPromises()

    // The clipboard has not answered yet: nothing is claimed, and nothing is
    // announced.
    expect(card.get('.terminal-offer-copy').text()).toBe('Copy')
    expect(card.get('.terminal-offer-status').text()).toBe('')

    pending.resolve(true)
    await flushPromises()
    expect(card.get('.terminal-offer-copy').text()).toBe('Copied')
    expect(card.get('.terminal-offer-status').text()).toContain('copied to the clipboard')
  })

  it('reports a failed copy and lets the user try again', async () => {
    writeClipboard.mockResolvedValue(false)
    const card = mountCard()

    await card.get('.terminal-offer-copy').trigger('click')
    await flushPromises()

    expect(card.get('.terminal-offer-copy').text()).toBe('Copy')
    const error = card.find('.terminal-offer-error')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('could not be copied')

    // Retryable: the same button tries again, and a success is claimed.
    writeClipboard.mockResolvedValue(true)
    await card.get('.terminal-offer-copy').trigger('click')
    await flushPromises()
    expect(card.get('.terminal-offer-copy').text()).toBe('Copied')
    expect(card.findAll('.terminal-offer-error')).toHaveLength(0)
  })

  it('a second click after Copied that fails shows only the error', async () => {
    const card = mountCard()

    await card.get('.terminal-offer-copy').trigger('click')
    await flushPromises()
    expect(card.get('.terminal-offer-copy').text()).toBe('Copied')

    // The next attempt starts from scratch: the confirmation of the last one
    // must not sit next to the error of this one.
    writeClipboard.mockResolvedValue(false)
    await card.get('.terminal-offer-copy').trigger('click')
    await flushPromises()

    expect(card.get('.terminal-offer-copy').text()).toBe('Copy')
    expect(card.findAll('.terminal-offer-error')).toHaveLength(1)
    expect(card.get('.terminal-offer-error').text()).toContain('could not be copied')
  })

  it('lets the latest of two overlapping copies own the outcome', async () => {
    const first = deferred<boolean>()
    const second = deferred<boolean>()
    writeClipboard.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const card = mountCard()

    await card.get('.terminal-offer-copy').trigger('click')
    await card.get('.terminal-offer-copy').trigger('click')

    // The second copy answers first and succeeds; the first answers last and
    // fails. The stale answer may not claim anything.
    second.resolve(true)
    first.resolve(false)
    await flushPromises()

    expect(card.get('.terminal-offer-copy').text()).toBe('Copied')
    expect(card.findAll('.terminal-offer-error')).toHaveLength(0)
  })

  it('does not claim the newer command was copied when the offer changed mid-copy', async () => {
    const pending = deferred<boolean>()
    writeClipboard.mockReturnValueOnce(pending.promise)
    const card = mountCard()

    await card.get('.terminal-offer-copy').trigger('click')
    // The engine replaced the suggestion while the clipboard was being written.
    await card.setProps({ offer: offer({ id: 'offer-2', command: 'ciao service start' }) })
    pending.resolve(true)
    await flushPromises()

    // The clipboard holds the command that was on screen, not the one now
    // shown: claiming "Copied" would be a lie about what would be pasted.
    expect(card.get('.terminal-offer-copy').text()).toBe('Copy')
    expect(renderedCommand(card)).toBe('ciao service start')
  })

  it('forgets a failed copy when the suggestion changes', async () => {
    writeClipboard.mockResolvedValue(false)
    const card = mountCard()

    await card.get('.terminal-offer-copy').trigger('click')
    await flushPromises()
    expect(card.findAll('.terminal-offer-error')).toHaveLength(1)

    // A new command: the error belonged to the old one.
    await card.setProps({ offer: offer({ id: 'offer-2', command: 'ciao service start' }) })
    expect(card.findAll('.terminal-offer-error')).toHaveLength(0)
  })

  it('drops the copy confirmation when a replacement arrives, without waiting for the timer', async () => {
    vi.useFakeTimers()
    const card = mountCard()

    await card.get('.terminal-offer-copy').trigger('click')
    await flushPromises()
    expect(card.get('.terminal-offer-copy').text()).toBe('Copied')

    // The command on screen is a different one now: the confirmation is
    // about a command that is gone. No timer has to run for that.
    await card.setProps({ offer: offer({ id: 'offer-2', command: 'ciao service start' }) })
    expect(card.get('.terminal-offer-copy').text()).toBe('Copy')
  })

  it('opens the terminal for this chat and starts nothing', async () => {
    const card = mountCard()
    const terminal = useTerminalStore()
    const sendInput = vi.spyOn(terminal, 'sendInput')
    const unlock = vi.spyOn(terminal, 'unlock')
    const createSession = vi.spyOn(terminal, 'createSession')

    await card.get('button.terminal-button').trigger('click')
    await flushPromises()

    expect(terminal.target).toEqual({ chatId: CHAT_ID })
    // No shell, no grant, no keystroke: opening the panel starts nothing.
    expect(apiPost).not.toHaveBeenCalled()
    expect(terminal.sessions).toHaveLength(0)
    expect(unlock).not.toHaveBeenCalled()
    expect(createSession).not.toHaveBeenCalled()
    expect(sendInput).not.toHaveBeenCalled()
  })

  it('does nothing at all on mount', async () => {
    const terminal = useTerminalStore()
    const open = vi.spyOn(terminal, 'open')
    const unlock = vi.spyOn(terminal, 'unlock')
    const sendInput = vi.spyOn(terminal, 'sendInput')

    mountCard()
    // A mount effect that is deferred by a microtask must not fire either.
    await flushPromises()
    await nextTick()

    expect(apiGet).not.toHaveBeenCalled()
    expect(apiPost).not.toHaveBeenCalled()
    expect(apiPatch).not.toHaveBeenCalled()
    expect(open).not.toHaveBeenCalled()
    expect(unlock).not.toHaveBeenCalled()
    expect(sendInput).not.toHaveBeenCalled()
    expect(terminal.visible).toBe(false)
  })

  it('dismisses the offer it is showing, by its id', async () => {
    const card = mountCard()

    await card.get('.terminal-offer-dismiss').trigger('click')
    await flushPromises()

    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(apiPatch).toHaveBeenCalledWith(`/api/chats/${CHAT_ID}`, {
      terminal_offer: { expected_id: 'offer-1' },
    })
  })

  it('disables the dismissal while it is in flight', async () => {
    const pending = deferred<Record<string, never>>()
    apiPatch.mockReturnValueOnce(pending.promise)
    const card = mountCard()

    await card.get('.terminal-offer-dismiss').trigger('click')
    expect(card.get('.terminal-offer-dismiss').attributes('disabled')).toBeDefined()

    pending.resolve({})
    await flushPromises()
    expect(card.get('.terminal-offer-dismiss').attributes('disabled')).toBeUndefined()
  })

  it('shows the newer command and says so when the dismissal conflicts', async () => {
    apiPatch.mockRejectedValueOnce({
      status: 409,
      payload: {
        error: 'terminal_offer_conflict',
        terminal_offer: { id: 'offer-2', command: 'ciao service start' },
      },
    })
    const card = mountCard()

    await card.get('.terminal-offer-dismiss').trigger('click')
    await flushPromises()

    // A conflict is not a failure and is not retried behind the user's back.
    expect(apiPatch).toHaveBeenCalledTimes(1)
    const notice = card.find('.terminal-offer-notice')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('the suggestion changed — the newer one is shown')

    // The engine's replacement arrives as a new offer: the card is patched in
    // place, so the notice that explains the swap survives it.
    await card.setProps({ offer: offer({ id: 'offer-2', command: 'ciao service start' }) })
    expect(renderedCommand(card)).toBe('ciao service start')
    expect(card.get('.terminal-offer-notice').text()).toContain('the newer one is shown')
  })

  it('dismisses the newer offer, by the newer id, after a conflict', async () => {
    apiPatch.mockRejectedValueOnce({
      status: 409,
      payload: {
        error: 'terminal_offer_conflict',
        terminal_offer: { id: 'offer-2', command: 'ciao service start' },
      },
    })
    const card = mountCard()

    await card.get('.terminal-offer-dismiss').trigger('click')
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledTimes(1)

    // The engine's replacement arrives as a new offer.
    await card.setProps({ offer: offer({ id: 'offer-2', command: 'ciao service start' }) })

    apiPatch.mockResolvedValueOnce({})
    await card.get('.terminal-offer-dismiss').trigger('click')
    await flushPromises()

    // The second dismissal names the offer on screen, and nothing was
    // replayed behind the user's back.
    expect(apiPatch).toHaveBeenCalledTimes(2)
    expect(apiPatch).toHaveBeenLastCalledWith(`/api/chats/${CHAT_ID}`, {
      terminal_offer: { expected_id: 'offer-2' },
    })
  })

  it('reports a failed dismissal and leaves it to the user to retry', async () => {
    apiPatch.mockRejectedValueOnce(new Error('the engine is unreachable'))
    const card = mountCard()

    await card.get('.terminal-offer-dismiss').trigger('click')
    await flushPromises()

    const error = card.find('.terminal-offer-error')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('could not be dismissed')
    // Retryable, and never retried on its own.
    expect(card.get('.terminal-offer-dismiss').attributes('disabled')).toBeUndefined()
    expect(apiPatch).toHaveBeenCalledTimes(1)

    apiPatch.mockResolvedValueOnce({})
    await card.get('.terminal-offer-dismiss').trigger('click')
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledTimes(2)
    expect(card.findAll('.terminal-offer-error')).toHaveLength(0)
  })

  it('a failed dismissal with the focus still in the card returns it to Dismiss', async () => {
    apiPatch.mockRejectedValueOnce(new Error('the engine is unreachable'))
    const card = mountCard()

    const dismiss = card.get('.terminal-offer-dismiss')
    ;(dismiss.element as HTMLElement).focus()
    await dismiss.trigger('click')
    await flushPromises()

    expect(card.get('.terminal-offer-error').text()).toContain('could not be dismissed')
    // The card still holds the focus, so the control the user pressed is
    // where the focus goes back to.
    expect(document.activeElement).toBe(dismiss.element)
  })

  it('focus moved into the composer while the dismissal was in flight stays there', async () => {
    const pending = deferred<Record<string, never>>()
    apiPatch.mockReturnValueOnce(pending.promise)
    const card = mountCard()

    const dismiss = card.get('.terminal-offer-dismiss')
    ;(dismiss.element as HTMLElement).focus()
    await dismiss.trigger('click')
    // While the request is out, the card holds the focus: the button is
    // disabled, and a disabled button drops the focus onto the page.
    expect(document.activeElement).toBe(card.get('.terminal-offer').element)

    // The user moves the focus into the composer to write the next message.
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    composer.focus()
    expect(document.activeElement).toBe(composer)

    pending.reject(new Error('the engine is unreachable'))
    await flushPromises()

    expect(card.get('.terminal-offer-error').text()).toContain('could not be dismissed')
    // The focus is the user's now: taking it back would interrupt them, and
    // could turn their next Enter into a control they did not choose.
    expect(document.activeElement).toBe(composer)
  })

  it('reverts the confirmation after the feedback window', async () => {
    vi.useFakeTimers()
    const card = mountCard()

    await card.get('.terminal-offer-copy').trigger('click')
    await flushPromises()
    expect(card.get('.terminal-offer-copy').text()).toBe('Copied')

    vi.advanceTimersByTime(2000)
    await nextTick()
    expect(card.get('.terminal-offer-copy').text()).toBe('Copy')
  })

  it('leaves no timer behind when the card is unmounted mid-confirmation', async () => {
    vi.useFakeTimers()
    const card = mountCard()
    // The store owns a timer of its own; the card's confirmation is the one
    // that must go with it.
    const baseline = vi.getTimerCount()

    await card.get('.terminal-offer-copy').trigger('click')
    await flushPromises()
    expect(card.get('.terminal-offer-copy').text()).toBe('Copied')
    expect(vi.getTimerCount()).toBe(baseline + 1)

    card.unmount()
    wrapper = null

    // The pending confirmation must not fire into a card that is gone.
    expect(vi.getTimerCount()).toBe(baseline)
    expect(() => vi.advanceTimersByTime(5000)).not.toThrow()
    expect(vi.getTimerCount()).toBe(baseline)
  })

  it('does nothing when the clipboard answers after the card is gone', async () => {
    vi.useFakeTimers()
    const pending = deferred<boolean>()
    writeClipboard.mockReturnValueOnce(pending.promise)
    const card = mountCard()
    const baseline = vi.getTimerCount()

    await card.get('.terminal-offer-copy').trigger('click')
    card.unmount()
    wrapper = null

    pending.resolve(true)
    await flushPromises()

    // The card is gone: the late answer claims nothing and schedules no
    // confirmation timer.
    expect(vi.getTimerCount()).toBe(baseline)
  })
})
