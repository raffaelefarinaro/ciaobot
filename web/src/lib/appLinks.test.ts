// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'

const push = vi.hoisted(() => vi.fn(async () => undefined))
vi.mock('../router', () => ({ router: { push } }))

import { handleAppLinkClick, isAppRouteHref } from './appLinks'

const ID = '0123456789abcdef0123456789abcdef'

function anchor(href: string): HTMLAnchorElement {
  const a = document.createElement('a')
  a.setAttribute('href', href)
  a.innerHTML = '<strong>Reply to Ivo</strong>'
  document.body.appendChild(a)
  return a
}

function click(target: Element, init: MouseEventInit = {}): MouseEvent {
  const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0, ...init })
  Object.defineProperty(event, 'target', { value: target })
  return event
}

afterEach(() => {
  document.body.innerHTML = ''
  push.mockClear()
})

describe('isAppRouteHref', () => {
  it('knows a task address by its full record id only', () => {
    expect(isAppRouteHref(`/tasks/${ID}`)).toBe(true)
    // `9920898d` is how a chat abbreviated ids; it names no task.
    expect(isAppRouteHref('/tasks/9920898d')).toBe(false)
    expect(isAppRouteHref('/tasks')).toBe(false)
    expect(isAppRouteHref(`https://example.com/tasks/${ID}`)).toBe(false)
    expect(isAppRouteHref(`/tasks/${ID}/../../api/x`)).toBe(false)
  })
})

describe('handleAppLinkClick', () => {
  it('routes a click on a task link in place, from inside the label', async () => {
    const a = anchor(`/tasks/${ID}`)
    const event = click(a.querySelector('strong')!)
    expect(handleAppLinkClick(event)).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith(`/tasks/${ID}`))
  })

  it('leaves other links and modified clicks to the browser', () => {
    expect(handleAppLinkClick(click(anchor('https://example.com')))).toBe(false)
    const event = click(anchor(`/tasks/${ID}`), { metaKey: true })
    expect(handleAppLinkClick(event)).toBe(false)
    expect(event.defaultPrevented).toBe(false)
    expect(push).not.toHaveBeenCalled()
  })

  it('stays put when the dialog in the way refuses to close', async () => {
    const before = vi.fn(async () => false)
    expect(handleAppLinkClick(click(anchor(`/tasks/${ID}`)), before)).toBe(true)
    await vi.waitFor(() => expect(before).toHaveBeenCalled())
    await Promise.resolve()
    expect(push).not.toHaveBeenCalled()
  })
})
