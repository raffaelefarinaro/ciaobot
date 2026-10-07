/**
 * Links that point into the app itself rather than out of it.
 *
 * An agent writes `[Reply to Ivo](/tasks/<id>)` in a chat or a vault note, and
 * the reader expects the task to open. Rendered markdown gives every other
 * anchor `target="_blank"`, and a vault note's links are not intercepted at
 * all, so without this a task link either opened a second copy of the app in
 * a new tab or reloaded this one. Recognised hrefs keep no `target` and are
 * routed in place by the click handlers that already own rendered markdown.
 *
 * Only addresses with a stable, shareable meaning are listed. A task id is the
 * 32-hex record id `ciao task list` returns; a short prefix is not an id.
 */
const APP_ROUTE_HREFS = [/^\/tasks\/[0-9a-f]{32}$/]

export function isAppRouteHref(href: string): boolean {
  const raw = (href || '').trim()
  return APP_ROUTE_HREFS.some(pattern => pattern.test(raw))
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
