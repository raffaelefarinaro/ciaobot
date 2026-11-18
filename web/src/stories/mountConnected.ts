import { createApp, type Component, h } from 'vue'
import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView, type RouteRecordRaw } from 'vue-router'
import { activeFixtureBoundary, type FixtureBoundary } from './fixtureBoundary'

export type ConnectedFixtureConfig = {
  component: Component
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
 * fresh Pinia. It never installs a boundary: the preview decorator fences every
 * story from `parameters.fixture`, and the routes that story declared there are
 * the only ones answered. Mounting with no active boundary is a story bug.
 */
export function mountConnected(host: HTMLElement, config: ConnectedFixtureConfig): ConnectedFixture {
  const boundary = activeFixtureBoundary()
  if (!boundary) throw new Error('mountConnected needs the fixture boundary the preview installs from parameters.fixture')
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
