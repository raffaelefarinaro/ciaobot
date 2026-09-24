import { nextTick, onBeforeUnmount, onMounted, watch, type Ref } from 'vue'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

interface ElementState {
  inert: boolean
  ariaHidden: string | null
}

export interface ModalFocusOptions {
  /** Focus target for this particular dialog, preferred over the first control. */
  initialFocus?: Ref<HTMLElement | null>
  onEscape: () => void
  /** Disable when the surface already owns a more specific focus-return path. */
  restoreFocus?: boolean
}

/**
 * Shared modal focus contract: focus enters the dialog, Tab stays inside it,
 * Escape is claimed before global shortcuts, background siblings are inert,
 * and the opener gets focus back when the dialog closes.
 */
export function useModalFocus(
  container: Ref<HTMLElement | null>,
  active: Ref<boolean>,
  options: ModalFocusOptions,
) {
  let opener: HTMLElement | null = null
  const backgroundState = new Map<HTMLElement, ElementState>()

  function focusableElements(): HTMLElement[] {
    const root = container.value
    if (!root) return []
    return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      .filter((element) => !element.hidden && element.getAttribute('aria-hidden') !== 'true')
  }

  function setBackgroundInert() {
    backgroundState.clear()
    let branch: HTMLElement | null = container.value
    while (branch && branch !== document.body) {
      const parent = branch.parentElement
      if (!parent) break
      for (const sibling of Array.from(parent.children)) {
        if (!(sibling instanceof HTMLElement) || sibling === branch || sibling.contains(branch)) continue
        if (backgroundState.has(sibling)) continue
        backgroundState.set(sibling, {
          inert: sibling.inert,
          ariaHidden: sibling.getAttribute('aria-hidden'),
        })
        sibling.inert = true
        sibling.setAttribute('aria-hidden', 'true')
      }
      branch = parent
    }
  }

  function restoreBackground() {
    for (const [element, state] of backgroundState) {
      element.inert = state.inert
      if (state.ariaHidden === null) element.removeAttribute('aria-hidden')
      else element.setAttribute('aria-hidden', state.ariaHidden)
    }
    backgroundState.clear()
  }

  function focusDialog() {
    const target = options.initialFocus?.value ?? focusableElements()[0]
    target?.focus()
  }

  function onKeydown(event: KeyboardEvent) {
    if (!active.value) return
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopImmediatePropagation()
      options.onEscape()
      return
    }
    if (event.key !== 'Tab') return

    const focusable = focusableElements()
    if (!focusable.length) {
      event.preventDefault()
      container.value?.focus()
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    const current = document.activeElement
    if (event.shiftKey && (current === first || !container.value?.contains(current))) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && current === last) {
      event.preventDefault()
      first.focus()
    }
  }

  watch(active, async (open) => {
    if (open) {
      opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
      await nextTick()
      setBackgroundInert()
      focusDialog()
      return
    }
    restoreBackground()
    const target = opener
    opener = null
    await nextTick()
    if (options.restoreFocus !== false && target?.isConnected) target.focus()
  }, { immediate: true })

  onMounted(() => window.addEventListener('keydown', onKeydown, true))
  onBeforeUnmount(() => {
    window.removeEventListener('keydown', onKeydown, true)
    restoreBackground()
  })
}
