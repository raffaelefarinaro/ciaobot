/** A `className` assignment observed on one element. */
export interface ClassWrite {
  el: Element
  value: string
}

/**
 * Records every `className` assignment on any element until `restore()`.
 * Vue's runtime-dom writes `class` with `el.className =` on first insertion and
 * on every class change, so this sees transient writes that a DOM read after a
 * flush cannot. Test-only: restore in a `finally`.
 */
export function recordClassWrites(): { writes: ClassWrite[]; restore: () => void } {
  const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'className')!
  const writes: ClassWrite[] = []
  Object.defineProperty(Element.prototype, 'className', {
    configurable: true,
    get: descriptor.get,
    set(this: Element, value: string) {
      writes.push({ el: this, value: String(value) })
      descriptor.set!.call(this, value)
    },
  })
  return {
    writes,
    restore: () => Object.defineProperty(Element.prototype, 'className', descriptor),
  }
}
