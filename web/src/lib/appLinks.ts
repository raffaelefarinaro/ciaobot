import { SETTINGS_HIDDEN_PAGES, SETTINGS_NAV } from './settingsNav'

/**
 * Links that point into the app itself rather than out of it.
 *
 * An agent writes `[Reply to Ivo](/tasks/<id>)` or `[Workspaces](/settings/workspaces)`
 * in a chat or a vault note, and the reader expects the task or page to open.
 * Rendered markdown gives every other anchor `target="_blank"`, and a vault
 * note's links are not intercepted at all, so without this an app link either
 * opened a second copy of the app in a new tab or reloaded this one.
 * Recognised hrefs keep no `target` and are routed in place by the click
 * handlers that already own rendered markdown.
 *
 * Only addresses with a stable, shareable meaning are listed. A task id is the
 * 32-hex record id `ciao task list` returns; a short prefix is not an id. A
 * conversation is named by its own id, `chat-` plus eight lowercase hex
 * characters. A page is an exact path — no query strings, no arbitrary
 * fragments, and no `/api`, `/ws` or protocol-relative addresses. The single
 * exception is a workspace's File access target, whose one query parameter and
 * fixed fragment are parsed by `parseWorkspaceFileAccessHref`. Adding one is a
 * deliberate decision: an unknown or malformed address stays an ordinary
 * browser link.
 */
const PAGE_HREFS = [
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
]

// Settings tabs come from the sidebar's own rail (plus the pages with no rail
// item), so a renamed tab cannot leave this list behind. `/settings/home` is the General tab by name; the
// shipped app links to `/settings#install-app` and both spellings land there.
const SETTINGS_HREFS = [
  ...SETTINGS_NAV.map(item => item.to),
  ...SETTINGS_HIDDEN_PAGES.map(item => item.to),
  '/settings/home',
  '/settings/home#install-app',
  '/settings#install-app',
  '/settings/providers#chat-providers',
  '/settings/workspaces#new-workspace',
]

const APP_ROUTE_HREFS = new Set([...PAGE_HREFS, ...SETTINGS_HREFS])
const TASK_HREF = /^\/tasks\/([0-9a-f]{32})$/
// A conversation reference. The id is the shape the engine mints: `chat-`
// plus eight lowercase hex characters (ciao/web/chat_service.py `_uuid8`).
// The address is exact: no extra path, no query, no fragment.
const CHAT_HREF = /^\/chat\/(chat-[0-9a-f]{8})$/
// One workspace row's File access control. The name is the engine's own rule
// (ciao/workspaces.py), and the address is exact: no extra parameter, no other
// fragment, no other tab. The link only locates the control; it never chooses
// a value, saves, or switches the active workspace.
const WORKSPACE_FILE_ACCESS_HREF =
  /^\/settings\/workspaces\?workspace=([A-Za-z0-9][A-Za-z0-9_-]{0,63})#file-access$/

/** The workspace an exact File access link names, or null for anything else. */
export function parseWorkspaceFileAccessHref(href: string): string | null {
  const match = WORKSPACE_FILE_ACCESS_HREF.exec((href || '').trim())
  return match ? match[1]! : null
}

/** The task id an exact `/tasks/<id>` address names, or null for anything else. */
export function parseTaskHref(href: string): string | null {
  const match = TASK_HREF.exec((href || '').trim())
  return match ? match[1]! : null
}

/** The chat id an exact `/chat/<id>` address names, or null for anything else. */
export function parseChatHref(href: string): string | null {
  const match = CHAT_HREF.exec((href || '').trim())
  return match ? match[1]! : null
}

export function isAppRouteHref(href: string): boolean {
  const raw = (href || '').trim()
  return APP_ROUTE_HREFS.has(raw)
    || parseTaskHref(raw) !== null
    || parseChatHref(raw) !== null
    || parseWorkspaceFileAccessHref(raw) !== null
}

/**
 * Whether a click is a plain primary-button click, with no modifier that asks
 * the browser for a new tab or window. Keyboard activation is a plain click too.
 */
export function isPlainLeftClick(event: MouseEvent): boolean {
  return !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0)
}

/**
 * Route a click on an in-app link without a page load.
 *
 * Returns true when the click was a recognised link and has been handled, so
 * a delegated handler can stop there. A modified click (new tab, new window)
 * is left to the browser, which opens the same address. `before` runs first
 * and can veto the navigation — a dialog that has to close out of the way
 * and may be refused by its own unsaved-changes confirm.
 *
 * A task link opens as a peek over the current view, with the URL unchanged,
 * so the chat stays put. On the board's own routes it navigates as before,
 * since the board already shows that task in its editor.
 */
export function handleAppLinkClick(event: MouseEvent, before?: () => Promise<boolean>): boolean {
  const target = event.target as HTMLElement | null
  const anchor = target?.closest('a[href]') as HTMLAnchorElement | null
  if (!anchor) return false
  const href = anchor.getAttribute('href') || ''
  if (!isAppRouteHref(href)) return false
  if (!isPlainLeftClick(event)) return false
  event.preventDefault()
  event.stopPropagation()
  void (async () => {
    if (before && !(await before())) return
    const { router } = await import('../router')
    const taskId = parseTaskHref(href)
    const name = router.currentRoute.value.name
    if (taskId && name !== 'tasks' && name !== 'task-detail') {
      const { useItemPeekStore } = await import('../stores/itemPeek')
      useItemPeekStore().openTask(taskId)
      return
    }
    await router.push(href)
  })()
  return true
}
