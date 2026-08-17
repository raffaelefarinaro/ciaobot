import { ref } from 'vue'

/**
 * Promise-based replacement for `window.prompt`.
 *
 * Same constraint as `askConfirm` in ./confirm: wry's `WKUIDelegate` does not
 * implement `runJavaScriptTextInputPanelWithPrompt:`, so inside the macOS app
 * WebKit shows no dialog and `prompt()` returns null immediately. A caller
 * written as `const name = prompt(...); if (!name) return` therefore turns into
 * a button that does nothing at all in the desktop app while working fine in a
 * browser.
 *
 * Like `askConfirm`, this cannot be a drop-in polyfill: `window.prompt` is
 * synchronous and no in-page dialog can block the JS thread. Callers await it.
 * Resolves to the trimmed text, or null when cancelled.
 */
export interface PromptRequest {
  message: string
  title: string
  value: string
  placeholder: string
  confirmLabel: string
  cancelLabel: string
  resolve: (value: string | null) => void
}

export const pendingPrompt = ref<PromptRequest | null>(null)

export interface PromptOptions {
  title?: string
  value?: string
  placeholder?: string
  confirmLabel?: string
  cancelLabel?: string
}

export function askPrompt(message: string, options: PromptOptions = {}): Promise<string | null> {
  // A second request would orphan the first one's promise, leaving its caller
  // awaiting forever. Resolve the outstanding one as cancelled first.
  pendingPrompt.value?.resolve(null)
  return new Promise<string | null>(resolve => {
    let settled = false
    pendingPrompt.value = {
      message,
      title: options.title ?? message,
      value: options.value ?? '',
      placeholder: options.placeholder ?? '',
      confirmLabel: options.confirmLabel ?? 'Create',
      cancelLabel: options.cancelLabel ?? 'Cancel',
      resolve: (value: string | null) => {
        if (settled) return
        settled = true
        pendingPrompt.value = null
        resolve(value)
      },
    }
  })
}
