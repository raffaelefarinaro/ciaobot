// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter, type RouteRecordRaw } from 'vue-router'
import { defineComponent } from 'vue'
import { routes } from './router'

const Stub = defineComponent({ render: () => null })

// The app routes with every lazy view swapped for a stub, so navigation
// resolves without importing the whole app.
function makeRouter() {
  const stubbed = routes.map((r) => ('component' in r && r.component ? { ...r, component: Stub } : r)) as RouteRecordRaw[]
  return createRouter({ history: createMemoryHistory(), routes: stubbed })
}

describe('router', () => {
  it('redirects the retired providers tab to the chat providers card on models', async () => {
    const router = makeRouter()
    await router.push('/settings/providers')
    expect(router.currentRoute.value.path).toBe('/settings/models')
    expect(router.currentRoute.value.hash).toBe('#chat-providers')
    expect(router.currentRoute.value.params.tab).toBe('models')
  })

  it('leaves the other settings tabs on the generic tab route', async () => {
    const router = makeRouter()
    await router.push('/settings/workspaces')
    expect(router.currentRoute.value.name).toBe('settings-tab')
    expect(router.currentRoute.value.params.tab).toBe('workspaces')
  })
  it('routes each memory section and sends the old proposals address to Suggested', async () => {
    const router = makeRouter()
    await router.push('/memory/revisit')
    expect(router.currentRoute.value.name).toBe('memory')
    expect(router.currentRoute.value.params.section).toBe('revisit')
    await router.push('/memory')
    expect(router.currentRoute.value.name).toBe('memory')
    expect(router.currentRoute.value.params.section ?? '').toBe('')
    await router.push('/proposals')
    expect(router.currentRoute.value.path).toBe('/memory/suggested')
  })
})
