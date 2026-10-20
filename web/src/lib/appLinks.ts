import { SETTINGS_NAV } from './settingsNav'

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
 * page is an exact path — no query strings, no arbitrary fragments, and no
 * `/api`, `/ws` or protocol-relative addresses. The single exception is a
 * workspace's File access target, whose one query parameter and fixed fragment
 * are parsed by `parseWorkspaceFileAccessHref`. Adding one is a deliberate
 * decision: an unknown or malformed address stays an ordinary browser link.
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

// Settings tabs come from the sidebar's own rail, so a renamed tab cannot
// leave this list behind. `/settings/home` is the General tab by name; the
// shipped app links to `/settings#install-app` and both spellings land there.
const SETTINGS_HREFS = [
  ...SETTINGS_NAV.map(item => item.to),
  '/settings/home',
  '/settings/home#install-app',
  '/settings#install-app',
  '/settings/models#chat-providers',
  '/settings/workspaces#new-workspace',
]

const APP_ROUTE_HREFS = new Set([...PAGE_HREFS, ...SETTINGS_HREFS])
const TASK_HREF = /^\/tasks\/[0-9a-f]{32}$/
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

export function isAppRouteHref(href: string): boolean {
  const raw = (href || '').trim()
  return APP_ROUTE_HREFS.has(raw)
    || TASK_HREF.test(raw)
    || parseWorkspaceFileAccessHref(raw) !== null
}

/**
 * Route a click on an in-app link without a page load.
 *
 * Returns true when the click was a recognised link and has been handled, so
 * a delegated handler can stop there. A modified click (new tab, new window)
 * is left to the browser, which opens the same address. `before` runs first
 * and can veto the navigation — a dialog that has to close out of the way
 * and may be refused by its own unsaved-changes confirm.
 */
export function handleAppLinkClick(event: MouseEvent, before?: () => Promise<boolean>): boolean {
  const target = event.target as HTMLElement | null
  const anchor = target?.closest('a[href]') as HTMLAnchorElement | null
  if (!anchor) return false
  const href = anchor.getAttribute('href') || ''
  if (!isAppRouteHref(href)) return false
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return false
  event.preventDefault()
  event.stopPropagation()
  void (async () => {
    if (before && !(await before())) return
    const { router } = await import('../router')
    await router.push(href)
  })()
  return true
}
