import { createApp, type Component, h } from 'vue'
import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView, type RouteRecordRaw } from 'vue-router'
import { installFixtureBoundary, type FixtureBoundary, type FixtureRoute } from './fixtureBoundary'

export type ConnectedFixtureConfig = {
  component: Component
  routes: FixtureRoute[]
  /** Extra vue-router destinations (must be inert: no bootstrap, no fetches). */
  routerRoutes?: RouteRecordRaw[]
  seed?: (pinia: Pinia) => void
}

export type ConnectedFixture = {
  app: ReturnType<typeof createApp>
  pinia: Pinia
  router: ReturnType<typeof createRouter>
  boundary: FixtureBoundary
  dispose: () => void
}

/**
 * A separate Vue app prevents fixture plugins leaking into Storybook's app.
 * The connected owner mounts as the `/` route of a fresh memory router with a
 * fresh Pinia; the fixture boundary answers only the routes declared here and
 * every other transport fails closed.
 */
export function mountConnected(host: HTMLElement, config: ConnectedFixtureConfig): ConnectedFixture {
  const boundary = installFixtureBoundary({ routes: config.routes })
  const pinia = createPinia()
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: config.component },
      ...(config.routerRoutes ?? []),
    ],
  })
  const app = createApp({ render: () => h(RouterView) })
  let disposed = false
  function dispose() {
    if (disposed) return
    disposed = true
    try { app.unmount() } finally {
      try {
        router.options.history.destroy()
        disposePinia(pinia)
      } finally {
        setActivePinia(undefined)
        boundary.dispose()
      }
    }
  }
  try {
    app.use(pinia)
    app.use(router)
    // Seed store state only, never app orchestration; the owner's real
    // watchers and clients stay untouched.
    config.seed?.(pinia)
    void router.replace('/')
    app.mount(host)
    return { app, pinia, router, boundary, dispose }
  } catch (error) {
    dispose()
    throw error
  }
}
