import { defineComponent, h, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, type Component } from 'vue'
import { mountConnected, type ConnectedFixture } from './mountConnected'
import { seedInbox } from './notificationInboxFixture'

// The production navigation composable reads browser history edges while the
// isolated app uses a memory router. Preserve the workshop's history entry and
// give the fixture a real two-entry memory route for the back action.
export function headerHistory(owner: Component) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ConnectedFixture>()
      const ready = ref(false)
      const originalState = history.state
      onMounted(async () => {
        history.replaceState({ ...originalState, back: '/' }, '')
        fixture.value = mountConnected(host.value!, { component: owner, routerRoutes: [{ path: '/story', component: owner }], seed: pinia => seedInbox(pinia) })
        await fixture.value.router.isReady()
        await fixture.value.router.push('/story')
        await nextTick()
        ready.value = true
      })
      onBeforeUnmount(() => {
        fixture.value?.dispose()
        history.replaceState(originalState, '')
        window.dispatchEvent(new PopStateEvent('popstate'))
      })
      return () => h('div', [h('div', { ref: host }), h('p', { 'data-testid': 'history-ready' }, ready.value ? 'ready' : 'mounting'), h('p', { 'data-testid': 'history-route' }, fixture.value?.router.currentRoute.value.fullPath ?? '')])
    },
  })
}
