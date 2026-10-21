// @vitest-environment jsdom

/**
 * The one mount point for item peeks. It shows the peek for the target it holds
 * and closes it on any route change, so a peek never outlives the view it was
 * opened over.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { defineComponent, h, nextTick, reactive } from 'vue'
import { routeLocationKey } from 'vue-router'
import ItemPeekHost from '../ItemPeekHost.vue'
import { useItemPeekStore } from '../../stores/itemPeek'

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
vi.mock('../../stores/projects', () => ({
  useProjectStore: () => ({ activeWorkspace: 'personal' }),
}))

const TaskPeekStub = defineComponent({
  name: 'TaskPeek',
  setup: () => () => h('div', { class: 'probe-peek' }),
})

function render(route: { fullPath: string }) {
  return mount(ItemPeekHost, {
    global: {
      stubs: { TaskPeek: TaskPeekStub },
      provide: { [routeLocationKey as symbol]: route },
    },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('ItemPeekHost', () => {
  it('renders the task peek for a task target and nothing without one', async () => {
    const peek = useItemPeekStore()
    const wrapper = render(reactive({ fullPath: '/chat/a' }))
    expect(wrapper.find('.probe-peek').exists()).toBe(false)
    peek.target = { kind: 'task', id: 'ship', workspace: 'personal' }
    await nextTick()
    expect(wrapper.find('.probe-peek').exists()).toBe(true)
  })

  it('closes the peek when the route changes', async () => {
    const peek = useItemPeekStore()
    const route = reactive({ fullPath: '/chat/a' })
    render(route)
    peek.target = { kind: 'task', id: 'ship', workspace: 'personal' }
    const close = vi.spyOn(peek, 'close')
    await nextTick()

    route.fullPath = '/chat/b'
    await nextTick()
    expect(close).toHaveBeenCalled()
    expect(peek.isOpen).toBe(false)
  })
})
