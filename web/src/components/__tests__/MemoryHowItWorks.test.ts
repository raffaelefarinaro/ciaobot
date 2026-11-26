// @vitest-environment jsdom

/**
 * The "How memory works" disclosure under the Memory header.
 *
 * What a mount can decide: that it starts collapsed and says so to assistive
 * tech, that the toggle is a native button wired to its body, that the open
 * choice is remembered per browser and a blocked store never throws, that the
 * copy names only
 * the things it is allowed to name, and that it makes no request at all.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import MemoryHowItWorks from '../MemoryHowItWorks.vue'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: vi.fn() },
}))

const STORAGE_KEY = 'ciao.memory.howOpen'

async function mountCard() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/memory/:section?', component: { template: '<div />' } }],
  })
  await router.push('/memory/unsure')
  await router.isReady()
  const wrapper = mount(MemoryHowItWorks, {
    attachTo: document.body,
    global: { plugins: [router] },
  })
  await flushPromises()
  return wrapper
}

function toggle(wrapper: Awaited<ReturnType<typeof mountCard>>) {
  return wrapper.find('button.mhw-toggle')
}

beforeEach(() => {
  localStorage.clear()
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
})

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('MemoryHowItWorks disclosure', () => {
  it('starts collapsed and hides its body from the tab order', async () => {
    const wrapper = await mountCard()
    const button = toggle(wrapper)

    expect(button.exists()).toBe(true)
    expect(button.text()).toBe('How memory works')
    expect(button.attributes('aria-expanded')).toBe('false')
    expect((wrapper.find('#mhw-body').element as HTMLElement).style.display).toBe('none')
  })

  it('uses a native button whose aria-controls names the body', async () => {
    const wrapper = await mountCard()
    const button = toggle(wrapper)

    expect(button.element.tagName).toBe('BUTTON')
    expect(button.attributes('type')).toBe('button')
    expect(button.attributes('aria-controls')).toBe('mhw-body')
    expect(wrapper.find('#mhw-body').exists()).toBe(true)
  })

  it('opens and closes with the toggle', async () => {
    const wrapper = await mountCard()
    const button = toggle(wrapper)

    await button.trigger('click')
    expect(button.attributes('aria-expanded')).toBe('true')
    expect((wrapper.find('#mhw-body').element as HTMLElement).style.display).not.toBe('none')

    await button.trigger('click')
    expect(button.attributes('aria-expanded')).toBe('false')
    expect((wrapper.find('#mhw-body').element as HTMLElement).style.display).toBe('none')
  })

  it('persists the open choice per browser and reads it back on mount', async () => {
    const first = await mountCard()
    await toggle(first).trigger('click')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('1')
    first.unmount()

    const second = await mountCard()
    expect(toggle(second).attributes('aria-expanded')).toBe('true')

    await toggle(second).trigger('click')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('0')
  })

  it('survives blocked storage: mounts collapsed and still toggles', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })

    const wrapper = await mountCard()
    const button = toggle(wrapper)
    expect(button.attributes('aria-expanded')).toBe('false')

    await button.trigger('click')
    expect(button.attributes('aria-expanded')).toBe('true')
  })

  it('says unsure items go on the Unsure list and that Git is the undo', async () => {
    const wrapper = await mountCard()
    const text = wrapper.text()

    expect(text).toContain('Unsure list')
    expect(text).toContain('Git repository')
    expect(text).toContain('Workspace/Proposals')
    expect(wrapper.find('table').exists()).toBe(false)
  })

  it('states what is not saved', async () => {
    const wrapper = await mountCard()
    const text = wrapper.text()

    expect(text).toContain('fewer than three chats')
    expect(text).toContain('automated run')
  })

  it('links to the Categories tab through the router', async () => {
    const wrapper = await mountCard()
    const link = wrapper.findAll('a').find(a => a.text() === 'Categories tab')

    expect(link).toBeDefined()
    expect(link!.attributes('href')).toBe('/memory/categories')
  })

  it('links to the site page in a new tab with noopener', async () => {
    const wrapper = await mountCard()
    const link = wrapper.find('a[target="_blank"]')

    expect(link.attributes('href')).toBe('https://www.raffaelefarinaro.com/ciaobot/memory.html#control')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toContain('noopener')
    expect(link.attributes('rel')).toContain('noreferrer')
  })

  it('mentions the two rules notes written by the weekly pass', async () => {
    const wrapper = await mountCard()
    const text = wrapper.text()

    expect(text).toContain('Memory-Review-Rules')
    expect(text).toContain('Task-Rules')
  })

  it('makes no requests', async () => {
    await mountCard()

    expect(apiGet).not.toHaveBeenCalled()
    expect(apiPost).not.toHaveBeenCalled()
    expect(apiPatch).not.toHaveBeenCalled()
  })
})
