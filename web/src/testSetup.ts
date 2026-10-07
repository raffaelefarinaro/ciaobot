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
if (typeof window !== 'undefined') {
  Object.defineProperty(window, 'localStorage', { configurable: true, value: storage })
}
