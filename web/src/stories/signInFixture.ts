import { defineComponent, h, onBeforeUnmount, onMounted, ref, type Component } from 'vue'
import type { Pinia } from 'pinia'
import { mountConnected, type ConnectedFixture } from './mountConnected'
export function signInPanel(component: Component, props: Record<string, unknown>, seed?: (pinia: Pinia) => void) {
  return defineComponent({ setup() {
    const host = ref<HTMLElement>()
    let fixture: ConnectedFixture | undefined
    onMounted(() => { fixture = mountConnected(host.value!, { component: defineComponent({ setup: () => () => h(component, props) }), seed }) })
    onBeforeUnmount(() => fixture?.dispose())
    return () => h('div', { ref: host })
  } })
}
