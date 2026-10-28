// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'

const push = vi.hoisted(() => vi.fn(async (_href: string) => undefined))
const currentRoute = vi.hoisted(() => ({ value: { name: 'chat-detail' as string | undefined } }))
vi.mock('../router', () => ({ router: { push, currentRoute } }))
const openTask = vi.hoisted(() => vi.fn((_id: string) => undefined))
vi.mock('../stores/itemPeek', () => ({ useItemPeekStore: () => ({ openTask }) }))

import {
  handleAppLinkClick,
  isAppRouteHref,
  parseChatHref,
  parseTaskHref,
  parseWorkspaceFileAccessHref,
} from './appLinks'

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
  '/settings/workspace',
  '/settings/providers',
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
  openTask.mockClear()
  currentRoute.value = { name: 'chat-detail' }
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
      '/settings/providers#chat-providers',
      '/settings/workspace#new-workspace',
    ]) {
      expect(isAppRouteHref(href), href).toBe(true)
    }
  })

  it('no longer treats /settings/workspaces as an app link', () => {
    expect(isAppRouteHref('/settings/workspaces')).toBe(false)
    expect(isAppRouteHref('/settings/workspaces#new-workspace')).toBe(false)
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
      '/settings/models',
      '/settings/models#chat-providers',
      '/settings?tab=models',
      '/settings/providers?effort=high',
      '/settings/workspace#install-app',
      '/settings/home#new-workspace',
      '/memory/review#top',
      '/settings/../api/x',
      '/tasks/../settings',
    ]) {
      expect(isAppRouteHref(href), href).toBe(false)
    }
  })
})

// The one workspace address an assistant may hand over: it locates a row's
// File access control and never acts on it. Only an exact name the engine
// accepts is a target; every extra parameter or other fragment is not.
function workspaceTarget(name: string): string {
  return `/settings/workspace?workspace=${name}#file-access`
}

describe('a workspace File access link', () => {
  it('parses the exact target with every name the engine accepts', () => {
    for (const name of ['personal', 'Work_2', 'a-b', 'A'.repeat(64)]) {
      expect(parseWorkspaceFileAccessHref(workspaceTarget(name)), name).toBe(name)
      expect(isAppRouteHref(workspaceTarget(name)), name).toBe(true)
    }
  })

  it('rejects names the engine would refuse', () => {
    for (const name of [
      '',
      '-work',
      '_work',
      'work space',
      'work%2F..%2Fapi',
      'a'.repeat(65),
    ]) {
      expect(parseWorkspaceFileAccessHref(workspaceTarget(name)), name).toBeNull()
      expect(isAppRouteHref(workspaceTarget(name)), name).toBe(false)
    }
  })

  it('rejects extra parameters, other fragments and other pages', () => {
    for (const href of [
      '/settings/workspace?workspace=personal&action=save#file-access',
      '/settings/workspace?workspace=personal&workspace=work#file-access',
      '/settings/workspace?workspace=personal#file-access&action=save',
      '/settings/workspace?workspace=personal#file_access',
      '/settings/workspace?workspace=personal#file-access/',
      '/settings/workspace?workspace=personal',
      '/settings/workspace#file-access',
      '/settings/workspace/?workspace=personal#file-access',
      '/settings/providers?workspace=personal#file-access',
      `https://example.com/settings/workspace?workspace=personal#file-access`,
      `//example.com/settings/workspace?workspace=personal#file-access`,
      '/settings/workspace?workspace=../api/x#file-access',
      '/settings/workspaces?workspace=personal#file-access',
    ]) {
      expect(parseWorkspaceFileAccessHref(href), href).toBeNull()
      expect(isAppRouteHref(href), href).toBe(false)
    }
  })
})

describe('parseTaskHref', () => {
  it('names the id of an exact task address only', () => {
    expect(parseTaskHref(`/tasks/${ID}`)).toBe(ID)
    for (const href of [
      '/tasks',
      '/tasks/9920898d',
      '/tasks/0123456789ABCDEF0123456789ABCDEF',
      `/tasks/${ID}/extra`,
      `https://example.com/tasks/${ID}`,
    ]) {
      expect(parseTaskHref(href), href).toBeNull()
    }
  })
})

// A reference to another conversation is a link into the app: it opens the
// chat in this tab, in the same window the reader is already in.
describe('a chat address', () => {
  const CHAT = 'chat-0123abcd'

  it('knows a chat by its exact id', () => {
    expect(isAppRouteHref(`/chat/${CHAT}`)).toBe(true)
    expect(parseChatHref(`/chat/${CHAT}`)).toBe(CHAT)
  })

  it('rejects a malformed or extended chat address', () => {
    for (const href of [
      '/chat/chat-0123abcde',
      '/chat/chat-0123abcd1',
      '/chat/chat-0123ABCD',
      '/chat/chat-1',
      '/chat/chat-0123abcd/extra',
      '/chat/chat-0123abcd?tab=1',
      '/chat/chat-0123abcd#top',
      '/chat',
      '/chat/',
      `https://example.com/chat/${CHAT}`,
      `//example.com/chat/${CHAT}`,
    ]) {
      expect(parseChatHref(href), href).toBeNull()
      expect(isAppRouteHref(href), href).toBe(false)
    }
  })

  it('routes a plain click to the chat in place', async () => {
    const a = anchor(`/chat/${CHAT}`)
    const event = click(a.querySelector('strong')!)
    expect(handleAppLinkClick(event)).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith(`/chat/${CHAT}`))
    // A chat is not a task: no peek, just the conversation.
    expect(openTask).not.toHaveBeenCalled()
  })

  it('leaves a modified click and the middle button to the browser', () => {
    for (const init of [
      { metaKey: true },
      { ctrlKey: true },
      { shiftKey: true },
      { altKey: true },
      { button: 1 },
    ]) {
      const event = click(anchor(`/chat/${CHAT}`), init)
      expect(handleAppLinkClick(event), JSON.stringify(init)).toBe(false)
      expect(event.defaultPrevented).toBe(false)
    }
    expect(push).not.toHaveBeenCalled()
  })
})

describe('handleAppLinkClick', () => {
  it('opens a task link as a peek over the chat, from inside the label', async () => {
    const a = anchor(`/tasks/${ID}`)
    const event = click(a.querySelector('strong')!)
    expect(handleAppLinkClick(event)).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    await vi.waitFor(() => expect(openTask).toHaveBeenCalledWith(ID))
    expect(push).not.toHaveBeenCalled()
  })

  it('sends a task link on the board to the board\'s own editor', async () => {
    for (const name of ['tasks', 'task-detail']) {
      currentRoute.value = { name }
      const a = anchor(`/tasks/${ID}`)
      expect(handleAppLinkClick(click(a.querySelector('strong')!)), name).toBe(true)
      await vi.waitFor(() => expect(push).toHaveBeenCalledWith(`/tasks/${ID}`))
    }
    expect(openTask).not.toHaveBeenCalled()
  })

  it('routes a click on a settings link in place, anchor and all', async () => {
    const a = anchor('/settings/workspace#new-workspace')
    const event = click(a.querySelector('strong')!)
    expect(handleAppLinkClick(event)).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith('/settings/workspace#new-workspace'))
  })

  it('routes a click on a workspace File access link in place', async () => {
    const href = workspaceTarget('work')
    const a = anchor(href)
    const event = click(a.querySelector('strong')!)
    expect(handleAppLinkClick(event)).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith(href))
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
      const event = click(anchor('/settings/workspace'), init)
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
    expect(openTask).not.toHaveBeenCalled()
  })

  it('opens the peek only once the dialog in the way has closed', async () => {
    const order: string[] = []
    const before = vi.fn(async () => {
      order.push('closed')
      return true
    })
    openTask.mockImplementationOnce((id: string) => {
      order.push(`peek ${id}`)
    })
    expect(handleAppLinkClick(click(anchor(`/tasks/${ID}`)), before)).toBe(true)
    await vi.waitFor(() => expect(openTask).toHaveBeenCalledWith(ID))
    expect(order).toEqual(['closed', `peek ${ID}`])
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
    expect(handleAppLinkClick(click(anchor('/settings/workspace')), before)).toBe(true)
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith('/settings/workspace'))
    expect(order).toEqual(['closed', '/settings/workspace'])
  })
})
