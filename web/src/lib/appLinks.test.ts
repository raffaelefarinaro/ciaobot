// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'

const push = vi.hoisted(() => vi.fn(async (_href: string) => undefined))
vi.mock('../router', () => ({ router: { push } }))

import { handleAppLinkClick, isAppRouteHref } from './appLinks'

const ID = '0123456789abcdef0123456789abcdef'

// The pages an assistant may name in a reply. A link to any of them reads as a
// direction inside the app, so it must not open a second copy in a new tab.
const PAGES = [
  '/',
  '/tasks',
  '/schedules',
  '/automations',
  '/memory',
  '/memory/review',
  '/memory/map',
  '/memory/categories',
  '/memory/import',
  '/memory/retired',
  '/memory/history',
  '/settings',
  '/settings/home',
  '/settings/workspaces',
  '/settings/models',
  '/settings/skills',
  '/settings/subagents',
  '/settings/commands',
  '/settings/mcp',
]

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
  it('knows every page an assistant may link to', () => {
    for (const href of PAGES) {
      expect(isAppRouteHref(href), href).toBe(true)
    }
  })

  it('knows the documented settings anchors', () => {
    for (const href of [
      '/settings/home#install-app',
      // The app's own "How to install" link (`HomeSetupCard.vue`) uses this
      // shorter spelling of the same destination.
      '/settings#install-app',
      '/settings/models#chat-providers',
      '/settings/workspaces#new-workspace',
    ]) {
      expect(isAppRouteHref(href), href).toBe(true)
    }
  })

  it('knows a task address by its full record id only', () => {
    expect(isAppRouteHref(`/tasks/${ID}`)).toBe(true)
    // `9920898d` is how a chat abbreviated ids; it names no task.
    expect(isAppRouteHref('/tasks/9920898d')).toBe(false)
    expect(isAppRouteHref('/tasks')).toBe(true)
    expect(isAppRouteHref(`https://example.com/tasks/${ID}`)).toBe(false)
    expect(isAppRouteHref(`/tasks/${ID}/../../api/x`)).toBe(false)
  })

  it('rejects unsafe, unknown and ambiguous addresses', () => {
    for (const href of [
      '',
      'https://example.com',
      '//example.com/settings',
      '/api/settings',
      '/ws/events',
      '/tasks/0123456789abcdef0123456789abcde',
      '/tasks/0123456789ABCDEF0123456789ABCDEF',
      `/tasks/${ID}/extra`,
      '/tasks/',
      '/settings/',
      '/memory/',
      '/settings/unknown',
      '/settings/providers',
      '/settings?tab=models',
      '/settings/models?effort=high',
      '/settings/workspaces#install-app',
      '/settings/home#new-workspace',
      '/memory/review#top',
      '/settings/../api/x',
      '/tasks/../settings',
    ]) {
      expect(isAppRouteHref(href), href).toBe(false)
    }
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

  it('routes a click on a settings link in place, anchor and all', async () => {
    const a = anchor('/settings/workspaces#new-workspace')
    const event = click(a.querySelector('strong')!)
    expect(handleAppLinkClick(event)).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith('/settings/workspaces#new-workspace'))
  })

  it('handles the click an Enter press on a focused link produces', async () => {
    // A keyboard activation is a click with no pointer detail, not a modified
    // one, so it must route in place exactly like the mouse path.
    const event = click(anchor('/settings/home#install-app'), { detail: 0 })
    expect(handleAppLinkClick(event)).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith('/settings/home#install-app'))
  })

  it('leaves other links to the browser', () => {
    expect(handleAppLinkClick(click(anchor('https://example.com')))).toBe(false)
    expect(handleAppLinkClick(click(anchor('/settings/unknown')))).toBe(false)
    expect(push).not.toHaveBeenCalled()
  })

  it('leaves every modified click and the middle button to the browser', () => {
    for (const init of [
      { metaKey: true },
      { ctrlKey: true },
      { shiftKey: true },
      { altKey: true },
      { button: 1 },
    ]) {
      const event = click(anchor('/settings/workspaces'), init)
      expect(handleAppLinkClick(event), JSON.stringify(init)).toBe(false)
      expect(event.defaultPrevented).toBe(false)
    }
    expect(push).not.toHaveBeenCalled()
  })

  it('stays put when the dialog in the way refuses to close', async () => {
    const before = vi.fn(async () => false)
    expect(handleAppLinkClick(click(anchor(`/tasks/${ID}`)), before)).toBe(true)
    await vi.waitFor(() => expect(before).toHaveBeenCalled())
    await Promise.resolve()
    expect(push).not.toHaveBeenCalled()
  })

  it('navigates from a dialog only once it has closed', async () => {
    const order: string[] = []
    const before = vi.fn(async () => {
      order.push('closed')
      return true
    })
    push.mockImplementationOnce(async (href: string) => {
      order.push(href)
      return undefined
    })
    expect(handleAppLinkClick(click(anchor('/settings/workspaces')), before)).toBe(true)
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith('/settings/workspaces'))
    expect(order).toEqual(['closed', '/settings/workspaces'])
  })
})
