// @vitest-environment jsdom

import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import HtmlArtifactViewer from '../HtmlArtifactViewer.vue'

function mountViewer(props: Partial<InstanceType<typeof HtmlArtifactViewer>['$props']> = {}) {
  return mount(HtmlArtifactViewer, {
    props: {
      filePath: 'Workspace/dashboard.html',
      reloadToken: 42,
      view: 'preview',
      source: '<h1>hi</h1>',
      ...props,
    },
  })
}

describe('HtmlArtifactViewer', () => {
  it('renders the artifact in a frame by default', () => {
    const frame = mountViewer().get('iframe')
    expect(frame.attributes('src')).toBe(
      '/api/workspace-html?path=Workspace%2Fdashboard.html&t=42',
    )
  })

  it('sandboxes the frame without same-origin access', () => {
    // The one attribute that must never appear: with allow-same-origin the
    // artifact's script would run with the user's session and full access to
    // the embedding app. Keep this assertion even if the list grows.
    const sandbox = mountViewer().get('iframe').attributes('sandbox')
    expect(sandbox).toBe('allow-scripts')
    expect(sandbox).not.toContain('allow-same-origin')
  })

  it('unmounts the frame in Code view so artifact script stops running', () => {
    const wrapper = mountViewer({ view: 'code' })
    expect(wrapper.find('iframe').exists()).toBe(false)
    expect(wrapper.get('.hav-code').text()).toContain('<h1>hi</h1>')
  })

  it('emits the requested view when a tab is clicked', async () => {
    const wrapper = mountViewer()
    await wrapper.findAll('.hav-tab')[1].trigger('click')
    expect(wrapper.emitted('update:view')).toEqual([['code']])
  })

  it('shows a source error without hiding the toggle', () => {
    const wrapper = mountViewer({ view: 'code', source: '', sourceError: 'Source is too large to show (>2 MB).' })
    expect(wrapper.get('.hav-note-error').text()).toContain('too large')
    expect(wrapper.findAll('.hav-tab')).toHaveLength(2)
  })

  it('emits compose-comment for a bridge compose message from the frame', async () => {
    const wrapper = mountViewer()
    const frame = wrapper.get('iframe')
    const fakeWindow = { postMessage: vi.fn() } as unknown as Window
    Object.defineProperty(frame.element, 'contentWindow', { value: fakeWindow, configurable: true })
    window.dispatchEvent(new MessageEvent('message', {
      source: fakeWindow as unknown as MessageEventSource,
      data: {
        frame: 'ciao-artifact',
        type: 'ciao:artifact-comment',
        action: 'compose',
        selector: 'div:nth-of-type(1) > p:nth-of-type(1)',
        quote: 'Revenue rose',
        startOffset: 3,
        endOffset: 15,
        elementTag: 'p',
        x: 12,
        y: 34,
      },
    }))
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('compose-comment')).toEqual([[
      {
        selector: 'div:nth-of-type(1) > p:nth-of-type(1)',
        quote: 'Revenue rose',
        startOffset: 3,
        endOffset: 15,
        elementTag: 'p',
        wholeElement: undefined,
        frameX: 12,
        frameY: 34,
      },
    ]])
  })

  it('ignores bridge messages from other windows or with wrong markers', async () => {
    const wrapper = mountViewer()
    const frame = wrapper.get('iframe')
    const fakeWindow = { postMessage: vi.fn() } as unknown as Window
    Object.defineProperty(frame.element, 'contentWindow', { value: fakeWindow, configurable: true })
    window.dispatchEvent(new MessageEvent('message', {
      source: null,
      data: { frame: 'ciao-artifact', type: 'ciao:artifact-comment', action: 'compose', selector: 'p', quote: 'x', startOffset: 0, endOffset: 1, x: 0, y: 0 },
    }))
    window.dispatchEvent(new MessageEvent('message', {
      source: fakeWindow as unknown as MessageEventSource,
      data: { frame: 'ciao-artifact', type: 'other', action: 'compose' },
    }))
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('compose-comment')).toBeUndefined()
  })

  it('emits bridge-ready so the parent can push highlights after a frame load', async () => {
    // The load-time handshake. Without it the parent's only push came from a
    // watcher that fires before the new document exists, so a reopened
    // artifact showed none of its stored marks.
    const wrapper = mountViewer()
    const frame = wrapper.get('iframe')
    const fakeWindow = { postMessage: vi.fn() } as unknown as Window
    Object.defineProperty(frame.element, 'contentWindow', { value: fakeWindow, configurable: true })
    window.dispatchEvent(new MessageEvent('message', {
      source: fakeWindow as unknown as MessageEventSource,
      data: { frame: 'ciao-artifact', type: 'ciao:artifact-comment', action: 'ready' },
    }))
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('bridge-ready')).toHaveLength(1)
    expect(wrapper.emitted('compose-comment')).toBeUndefined()
  })

  it('unlocks the in-frame compose half on ready', async () => {
    // The bridge keeps its pill inert until a parent speaks. Relying on the
    // highlight push to double as that signal would leave compose dead
    // wherever the parent skips the push (outside Preview, non-artifact
    // files), so the viewer says it explicitly.
    const wrapper = mountViewer()
    const frame = wrapper.get('iframe')
    const postMessage = vi.fn()
    const fakeWindow = { postMessage } as unknown as Window
    Object.defineProperty(frame.element, 'contentWindow', { value: fakeWindow, configurable: true })
    window.dispatchEvent(new MessageEvent('message', {
      source: fakeWindow as unknown as MessageEventSource,
      data: { frame: 'ciao-artifact', type: 'ciao:artifact-comment', action: 'ready' },
    }))
    await wrapper.vm.$nextTick()

    expect(postMessage).toHaveBeenCalledWith(
      { frame: 'ciao-artifact', type: 'ciao:comments-enable' },
      '*',
    )
  })

  it('rejects frame messages while no frame is mounted', async () => {
    // The guard used to read `frameEl.value !== null`, which is always true
    // for an unmounted template ref (`undefined`), leaving only
    // `e.source === undefined` behind it.
    const wrapper = mountViewer({ view: 'code' })
    window.dispatchEvent(new MessageEvent('message', {
      source: undefined,
      data: { frame: 'ciao-artifact', type: 'ciao:artifact-comment', action: 'ready' },
    }))
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('bridge-ready')).toBeUndefined()
  })

  it('sendHighlights posts the comment list into the frame', () => {
    const wrapper = mountViewer()
    const postMessage = vi.fn()
    const frame = wrapper.get('iframe')
    Object.defineProperty(frame.element, 'contentWindow', {
      value: { postMessage } as unknown as Window,
      configurable: true,
    })
    wrapper.vm.sendHighlights([{ id: 'c1', selector: 'h1', quote: 'hi' }])
    expect(postMessage).toHaveBeenCalledWith(
      {
        frame: 'ciao-artifact',
        type: 'ciao:apply-comments',
        comments: [{ id: 'c1', selector: 'h1', quote: 'hi' }],
      },
      '*',
    )
  })
})
