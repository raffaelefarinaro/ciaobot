// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import FolderBrowser from '../FolderBrowser.vue'
import type { WorkspaceDirListing } from '../../lib/folderBrowserDriver'

// The real VueFinder is a large chunk; the component's contract is its props and handlers.
vi.mock('vuefinder', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    VueFinder: defineComponent({
      name: 'VueFinderStub',
      props: ['id', 'driver', 'config', 'features', 'contextMenuItems', 'selectionMode', 'onFileDclick', 'onSelect', 'onPathChange'],
      setup: () => () => h('div', { class: 'vf-stub' }),
    }),
    contextMenuItems: [{ id: 'open' }, { id: 'refresh' }, { id: 'delete' }],
    ContextMenuIds: { open: 'open', refresh: 'refresh' },
  }
})
vi.mock('vuefinder/dist/vuefinder.css', () => ({}))

const KEY = 'vuefinder_config_ciao-folder-browser'
const root = { name: 'workspace', path: '/ws' }

function listing(overrides: Partial<WorkspaceDirListing> = {}): WorkspaceDirListing {
  return {
    path: '/ws/exports',
    root,
    roots: [root],
    parent: '/ws',
    entries: [{ name: 'a.md', path: '/ws/exports/a.md', type: 'file', size: 3, mtime: 1, ext: 'md' }],
    truncated: false,
    ...overrides,
  }
}

const fileItem = { path: 'workspace://exports/a.md', type: 'file', basename: 'a.md' }
const dirItem = { path: 'workspace://exports/sub', type: 'dir', basename: 'sub' }

const live: VueWrapper[] = []

function open(initialPath = '/ws/exports'): VueWrapper {
  const wrapper = mount(FolderBrowser, { props: { initialPath, chatId: 'chat-1' } })
  live.push(wrapper)
  return wrapper
}

function close(wrapper: VueWrapper): void {
  wrapper.unmount()
  live.splice(live.indexOf(wrapper), 1)
}

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(listing()), { status: 200 })))
})

afterEach(() => {
  live.splice(0).forEach(w => w.unmount())
  vi.unstubAllGlobals()
  delete (window as any).matchMedia
})

describe('FolderBrowser', () => {
  it('mounts VueFinder at the requested folder with the read-only setup', async () => {
    const wrapper = open()
    await flushPromises()

    const vf = wrapper.findComponent({ name: 'VueFinderStub' })
    expect(vf.props('config').initialPath).toBe('workspace://exports')
    expect(vf.props('features').search).toBe(false)
    expect(vf.props('contextMenuItems').map((i: { id: string }) => i.id)).toEqual(['open', 'refresh'])
    expect(JSON.parse(localStorage.getItem(KEY)!).view).toBe('list')
    expect(wrapper.emitted('navigate')).toEqual([['/ws/exports']])
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.fbr-note').exists()).toBe(false)
  })

  it('says when a folder is cut short', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(listing({
      truncated: true,
      entries: [
        { name: 'a.md', path: '/ws/exports/a.md', type: 'file', size: 1, mtime: 1, ext: 'md' },
        { name: 'b.md', path: '/ws/exports/b.md', type: 'file', size: 1, mtime: 1, ext: 'md' },
        { name: 'c.md', path: '/ws/exports/c.md', type: 'file', size: 1, mtime: 1, ext: 'md' },
      ],
    })), { status: 200 })))
    const wrapper = open()
    await flushPromises()

    expect(wrapper.find('.fbr-note').text()).toBe('Showing the first 3 items.')
  })

  it('a double-clicked file opens by absolute path', async () => {
    const wrapper = open()
    await flushPromises()
    const spy = vi.fn()

    wrapper.findComponent({ name: 'VueFinderStub' }).props('onFileDclick')({
      item: fileItem, defaultPrevented: false, preventDefault: spy,
    })

    expect(spy).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('open-file')).toEqual([['/ws/exports/a.md']])
  })

  it('moving to another folder reports its absolute path', async () => {
    const wrapper = open()
    await flushPromises()

    wrapper.findComponent({ name: 'VueFinderStub' }).props('onPathChange')('workspace://exports/sub')

    const navigate = wrapper.emitted('navigate')!
    expect(navigate[navigate.length - 1]).toEqual(['/ws/exports/sub'])
  })

  it('a refused folder shows the reason instead of the browser', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: 'outside_roots' }), { status: 403 })))
    const wrapper = open()
    await flushPromises()

    expect(wrapper.find('.fbr-error').text()).toContain('outside')
    expect(wrapper.find('.vf-stub').exists()).toBe(false)
  })

  it('a tap opens a file only on a coarse pointer', async () => {
    window.matchMedia = vi.fn(() => ({ matches: true })) as any
    const coarse = open()
    await flushPromises()
    const coarseVf = coarse.findComponent({ name: 'VueFinderStub' })

    coarseVf.props('onSelect')([dirItem])
    expect(coarse.emitted('open-file')).toBeUndefined()
    coarseVf.props('onSelect')([fileItem])
    expect(coarse.emitted('open-file')).toEqual([['/ws/exports/a.md']])
    close(coarse)

    window.matchMedia = vi.fn(() => ({ matches: false })) as any
    const fine = open()
    await flushPromises()

    fine.findComponent({ name: 'VueFinderStub' }).props('onSelect')([fileItem])
    expect(fine.emitted('open-file')).toBeUndefined()
  })

  it('an unmount during the first load stops there', async () => {
    let answer!: (resp: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { answer = resolve })))
    const wrapper = open()
    close(wrapper)

    answer(new Response(JSON.stringify(listing()), { status: 200 }))
    await flushPromises()

    expect(localStorage.getItem(KEY)).toBeNull()
    expect(wrapper.emitted('navigate')).toBeUndefined()
  })
})
