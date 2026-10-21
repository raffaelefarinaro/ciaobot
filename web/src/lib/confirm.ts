import { ref } from 'vue'
import { cleanReason } from './decisionReasons'

/**
 * Promise-based replacement for `window.confirm`.
 *
 * The native dialog cannot be used inside the macOS app: wry's `WKUIDelegate`
 * implements neither `runJavaScriptConfirmPanelWithMessage:` nor its alert and
 * prompt siblings, so WebKit shows nothing and `confirm()` resolves to `false`.
 * A guard written as `if (!confirm(...)) return` therefore silently blocks the
 * action in the desktop app while working fine in a browser.
 *
 * This cannot be a drop-in polyfill of `window.confirm`, because that call is
 * synchronous and no in-page dialog can block the JS thread. Callers await it.
 */
export interface ConfirmRequest {
  message: string
  title: string
  confirmLabel: string
  cancelLabel: string
  destructive: boolean
  /** Show the optional "Why?" field (#1730); `reason` is what the dialog collected. */
  withReason: boolean
  reason: string
  resolve: (confirmed: boolean) => void
}

export const pendingConfirm = ref<ConfirmRequest | null>(null)

export interface ConfirmOptions {
  title?: string
  confirmLabel?: string
  cancelLabel?: string
  destructive?: boolean
}

function openConfirm(message: string, options: ConfirmOptions, withReason: boolean): { promise: Promise<boolean>, request: ConfirmRequest } {
  // A second request would orphan the first one's promise, leaving its caller
  // awaiting forever. Resolve the outstanding one as cancelled first.
  pendingConfirm.value?.resolve(false)
  let request!: ConfirmRequest
  const promise = new Promise<boolean>(resolve => {
    let settled = false
    request = {
      message,
      title: options.title ?? 'Are you sure?',
      confirmLabel: options.confirmLabel ?? 'Confirm',
      cancelLabel: options.cancelLabel ?? 'Cancel',
      destructive: options.destructive ?? false,
      withReason,
      reason: '',
      resolve: (confirmed: boolean) => {
        if (settled) return
        settled = true
        pendingConfirm.value = null
        resolve(confirmed)
      },
    }
    pendingConfirm.value = request
  })
  return { promise, request }
}

export function askConfirm(message: string, options: ConfirmOptions = {}): Promise<boolean> {
  return openConfirm(message, options, false).promise
}

export interface ConfirmReasonResult {
  confirmed: boolean
  reason: string
}

/** A confirm carrying an optional "Why?" field. Skipping it costs no extra click. */
export async function askConfirmReason(message: string, options: ConfirmOptions = {}): Promise<ConfirmReasonResult> {
  const { promise, request } = openConfirm(message, options, true)
  const confirmed = await promise
  return { confirmed, reason: confirmed ? cleanReason(request.reason) : '' }
}
