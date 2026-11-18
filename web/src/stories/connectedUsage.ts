import { createApp, defineComponent, h } from 'vue'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import UsageView from '../components/UsageView.vue'
import { useProjectStore } from '../stores/projects'
import { installUsageBoundary, type UsageFixtureState } from './usageBoundary'

/** Separate Vue app so fixture plugins do not leak into Storybook's app. */
export function mountConnectedUsage(host: HTMLElement, state: UsageFixtureState, workspace = 'storybook-alpha') {
  const boundary = installUsageBoundary(state)
  const pinia = createPinia()
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: UsageView },
      { path: '/stats', component: defineComponent({ render: () => h('p', 'Inert stats destination for workshop routing only.') }) },
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
    projects.activeWorkspace = workspace
    void router.replace('/')
    app.mount(host)
    return { app, pinia, router, projects, boundary, dispose }
  } catch (error) {
    dispose()
    throw error
  }
}