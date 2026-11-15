import { createApp, defineComponent, h } from 'vue'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import HomeTodayStats from '../components/HomeTodayStats.vue'
import { useProjectStore } from '../stores/projects'
import { installStatsBoundary, type StatsState } from './statsBoundary'

/** A separate Vue app prevents fixture plugins leaking into Storybook's app. */
export function mountConnectedStats(host: HTMLElement, state: StatsState, workspace = 'storybook-alpha') {
  const boundary = installStatsBoundary(state)
  const pinia = createPinia()
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: HomeTodayStats },
      { path: '/stats', component: defineComponent({ render: () => h('p', 'Inert Usage destination — no Usage page or bootstrap mounted.') }) },
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
    const projects = useProjectStore(pinia)
    // Seed state only. fetchAll/switchWorkspace are app orchestration, not this
    // owner's dependency. Its real watcher and stats client remain untouched.
    projects.activeWorkspace = workspace
    void router.replace('/')
    app.mount(host)
    return { app, pinia, router, projects, boundary, dispose }
  } catch (error) {
    dispose()
    throw error
  }
}
