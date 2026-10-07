// Node 22.23 exposes an experimental global localStorage accessor that is
// undefined unless Node starts with --localstorage-file. Point tests at the
// storage provided by their jsdom window so the browser API remains available
// without a Node-specific launch flag.
if (typeof window !== 'undefined') {
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: window.localStorage,
  })
}
