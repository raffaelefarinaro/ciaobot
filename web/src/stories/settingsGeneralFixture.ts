import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef, type Component } from 'vue'
import type { Pinia } from 'pinia'
import { mountConnected, type ConnectedFixture } from './mountConnected'

/** A synthetic JSON response for a declared route. */
export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

/** A read or write the story never answers: it stays pending until teardown. */
export function neverSettles(): Promise<never> {
  return new Promise<never>(() => {})
}

export interface ConnectedStory {
  component: Component
  seed?: (pinia: Pinia) => void
  /** Clears per-mount response counters so every render starts from the first answer. */
  reset?: () => void
  /** Wraps the owner in a container of this width in px (the narrow state). */
  width?: number
}

/**
 * The story render for a connected owner: mounts the production component
 * through `mountConnected` each time the story renders, and disposes it on
 * teardown. Only the routes the story declared in `parameters.fixture` answer.
 */
export function connectedRender(story: ConnectedStory) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ConnectedFixture>()
      onMounted(() => {
        story.reset?.()
        fixture.value = mountConnected(host.value!, { component: story.component, seed: story.seed })
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', { style: story.width ? `max-width: ${story.width}px` : undefined }, [h('div', { ref: host })])
    },
  })
}
