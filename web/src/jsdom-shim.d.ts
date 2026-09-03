// jsdom is a test-only dependency (it is the vitest DOM environment) and ships
// no type declarations. `src/lib/artifactBridgeScript.test.ts` constructs one
// directly to run the artifact bridge script in a second, isolated document,
// so declare just that sliver rather than pulling @types/jsdom into the app's
// type graph.
declare module 'jsdom' {
  export class JSDOM {
    constructor(html?: string, options?: Record<string, unknown>)
    readonly window: Window & typeof globalThis & { eval(code: string): unknown }
  }
}
