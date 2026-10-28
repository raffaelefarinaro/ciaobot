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
  it('redirects the renamed models tab to providers, keeping query and hash', async () => {
    const router = makeRouter()
    await router.push('/settings/models?x=1#chat-providers')
    const route = router.currentRoute.value
    expect(route.path).toBe('/settings/providers')
    expect(route.hash).toBe('#chat-providers')
    expect(route.query).toEqual({ x: '1' })
    expect(route.params.tab).toBe('providers')
    expect(route.name).toBe('settings-tab')
  })

  it('leaves the other settings tabs on the generic tab route', async () => {
    const router = makeRouter()
    await router.push('/settings/skills')
    expect(router.currentRoute.value.name).toBe('settings-tab')
    expect(router.currentRoute.value.params.tab).toBe('skills')
  })
  it('routes each memory section and sends the old proposals address to To decide', async () => {
    const router = makeRouter()
    await router.push('/memory/review?show=revisit')
    expect(router.currentRoute.value.name).toBe('memory')
    expect(router.currentRoute.value.params.section).toBe('review')
    expect(router.currentRoute.value.query.show).toBe('revisit')
    await router.push('/memory')
    expect(router.currentRoute.value.name).toBe('memory')
    expect(router.currentRoute.value.params.section ?? '').toBe('')
    await router.push('/proposals')
    expect(router.currentRoute.value.path).toBe('/memory/review')
    expect(router.currentRoute.value.query.show).toBe('suggested')
  })

  it('addresses one task on the board', async () => {
    const router = makeRouter()
    await router.push('/tasks/0123456789abcdef0123456789abcdef')
    expect(router.currentRoute.value.name).toBe('task-detail')
    expect(router.currentRoute.value.params.taskId).toBe('0123456789abcdef0123456789abcdef')
    await router.push('/tasks')
    expect(router.currentRoute.value.name).toBe('tasks')
  })

  it('notifications_path_is_not_a_route', () => {
    expect(routes.find((r) => r.path === '/notifications')).toBeUndefined()
  })
})
