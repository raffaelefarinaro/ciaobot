import { ref } from 'vue'

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
  resolve: (confirmed: boolean) => void
}

export const pendingConfirm = ref<ConfirmRequest | null>(null)

export interface ConfirmOptions {
  title?: string
  confirmLabel?: string
  cancelLabel?: string
  destructive?: boolean
}

export function askConfirm(message: string, options: ConfirmOptions = {}): Promise<boolean> {
  // A second request would orphan the first one's promise, leaving its caller
  // awaiting forever. Resolve the outstanding one as cancelled first.
  pendingConfirm.value?.resolve(false)
  return new Promise<boolean>(resolve => {
    let settled = false
    pendingConfirm.value = {
      message,
      title: options.title ?? 'Are you sure?',
      confirmLabel: options.confirmLabel ?? 'Confirm',
      cancelLabel: options.cancelLabel ?? 'Cancel',
      destructive: options.destructive ?? false,
      resolve: (confirmed: boolean) => {
        if (settled) return
        settled = true
        pendingConfirm.value = null
        resolve(confirmed)
      },
    }
  })
}
