/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  // The three slots are props / raw bindings / data. `object` rather than `{}`
  // because `{}` also accepts primitives, and `unknown` for data so a consumer
  // has to narrow before use.
  const component: DefineComponent<object, object, unknown>
  export default component
}
