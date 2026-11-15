// Vitest 4's jsdom environment does not consistently expose localStorage on
// the test global. Use one browser-shaped store for code and assertions.
const values = new Map<string, string>()
const storage: Storage = {
  get length() { return values.size },
  clear() { values.clear() },
  getItem(key) { return values.get(String(key)) ?? null },
  key(index) { return Array.from(values.keys())[index] ?? null },
  removeItem(key) { values.delete(String(key)) },
  setItem(key, value) { values.set(String(key), String(value)) },
}

Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })
// jsdom does not implement Web Locks. Preserve exclusive, nonblocking behavior.
let locked = false
Object.defineProperty(globalThis.navigator, 'locks', {
  configurable: true,
  value: {
    async request(_name: string, _options: unknown, callback: (lock: object | null) => unknown) {
      if (locked) return callback(null)
      locked = true
      try { return await callback({}) } finally { locked = false }
    },
  },
})
if (typeof window !== 'undefined') {
  Object.defineProperty(window, 'localStorage', { configurable: true, value: storage })
}

// jsdom has no Element.scrollTo. Components that scroll a pane call it, so give
// every jsdom test a no-op; a test that asserts on scrolling installs its own.
if (typeof Element !== 'undefined' && typeof Element.prototype.scrollTo !== 'function') {
  Object.defineProperty(Element.prototype, 'scrollTo', { configurable: true, writable: true, value: () => {} })
}
