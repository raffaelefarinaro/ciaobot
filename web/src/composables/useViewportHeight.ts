import { onScopeDispose, ref, type Ref } from 'vue'

import { onViewportChange, viewportHeight } from '../lib/viewport'

/**
 * Reactive height of the visible viewport. Shrinks when the on-screen keyboard
 * opens and grows back when it closes.
 *
 * Use this in any `position: fixed` popover that clamps itself on screen. A
 * plain `viewportHeight()` read inside a computed is not enough: nothing
 * invalidates it, so a popover that opens and then focuses an input stays where
 * the pre-keyboard viewport put it, which on a phone means behind the keyboard.
 */
export function useViewportHeight(): Ref<number> {
  const height = ref(viewportHeight())
  const stop = onViewportChange((h) => { height.value = h })
  onScopeDispose(stop)
  return height
}
