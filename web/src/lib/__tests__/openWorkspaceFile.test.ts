// @vitest-environment jsdom

// The openable dead link: a `file:` path the sanitizer refuses, rendered as a
// button on the engine's own machine (lib/safeMarkdown.ts). The button lives
// inside markdown injected with v-html, so it cannot be a Vue component: the
// click is handled by delegation, the same way the code-block copy button is
// (lib/codeCopy.ts). The delegation is what survives the innerHTML swap that
// happens on every streamed token.
import { afterEach, describe, expect, it, vi } from 'vitest'

import { handleOpenLocalFileClick } from '../openWorkspaceFile'

const BUTTON_HTML =
  '<button type="button" class="markdown-dead-link markdown-dead-link--openable"'
  + ' data-local-path="/tmp/notes.md">label (/tmp/notes.md)</button>'

function host(): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = BUTTON_HTML
  document.body.appendChild(el)
  return el
}

function okResponse(): Response {
  return { ok: true, status: 200, json: async () => ({}) } as unknown as Response
}

describe('the openable dead link', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    document.body.innerHTML = ''
  })

  it('asks the engine to open the path when the button is clicked', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse())
    vi.stubGlobal('fetch', fetchMock)
    const el = host()
    const button = el.querySelector('button') as HTMLButtonElement

    let handled: boolean | null = null
    button.addEventListener('click', (e) => {
      handled = handleOpenLocalFileClick(e, () => {})
    })
    button.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    await Promise.resolve()

    expect(handled).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/workspace-open')
    expect(init.method).toBe('POST')
    // The engine gets the path, never a `file:` URL to navigate to.
    expect(init.body).toBe('{"path":"/tmp/notes.md"}')
  })

  it('ignores a click that did not hit the button', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const el = host()
    const other = document.createElement('span')
    el.appendChild(other)

    let handled: boolean | null = null
    other.addEventListener('click', (e) => {
      handled = handleOpenLocalFileClick(e, () => {})
    })
    other.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    await Promise.resolve()

    expect(handled).toBe(false)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('reports a refusal from the engine', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({ error: 'Opening files is only available on this computer.' }),
    }))
    const el = host()
    const button = el.querySelector('button') as HTMLButtonElement
    const onFail = vi.fn()

    button.addEventListener('click', (e) => {
      handleOpenLocalFileClick(e, onFail)
    })
    button.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))

    await vi.waitFor(() => {
      expect(onFail).toHaveBeenCalledWith('Opening files is only available on this computer.')
    })
  })

  it('swallows the click, so the browser follows nothing', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse()))
    const el = host()
    const button = el.querySelector('button') as HTMLButtonElement
    const event = new MouseEvent('click', { bubbles: true, cancelable: true })
    const prevent = vi.spyOn(event, 'preventDefault')
    const stop = vi.spyOn(event, 'stopPropagation')

    button.addEventListener('click', (e) => {
      handleOpenLocalFileClick(e, () => {})
    })
    button.dispatchEvent(event)
    await Promise.resolve()

    expect(prevent).toHaveBeenCalled()
    expect(stop).toHaveBeenCalled()
  })
})
