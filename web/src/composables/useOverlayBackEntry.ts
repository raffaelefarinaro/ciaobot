import { inject, onBeforeUnmount, watch } from 'vue'
import { routerKey, type Router } from 'vue-router'
import { pendingConfirm } from '../lib/confirm'
import { pendingPrompt } from '../lib/prompt'

// Back (the browser's or Android's) closes an overlay the way its close
// button does, instead of changing the route under it.
//
// Nothing pushes a history entry when an overlay opens, so Back would have
// nothing to pop except the page. While an overlay is open this writes ONE
// entry at the SAME address, straight through `router.options.history.push`:
// no navigation runs, so no guard, afterEach or route watcher fires.
//
// - User Back pops that entry. vue-router then runs a same-location
//   navigation; the guard below sees the top overlay's marker gone and awaits
//   that overlay's close (its unsaved-changes confirm included). If the close
//   is refused the guard returns false and vue-router puts the entry back.
// - Closing any other way (x, Escape, backdrop, a link inside it) steps back
//   over the entry silently: `history.go(-1, false)` with the router listeners
//   paused. Until that pop lands the guard holds every navigation, so a
//   `router.push` issued right after a close starts from the page.
// - A close that happens INSIDE a navigation (a route guard closing an
//   overlay) leaves the entry to that navigation: never pop under a push.
// - A `router.replace` that moved the address while the overlay was open: on
//   close the marker is cleared and the entry kept (it is that page now).
// - Forward onto a closed overlay's entry is refused, which bounces back.
//
// Install order matters: the guard is registered on first use, so it runs
// before any guard registered later that can close an overlay.

export const OVERLAY_STATE_KEY = 'ciaoOverlay'
const LAND_TIMEOUT_MS = 1000

interface OverlayEntry {
  id: string
  fullPath: string
  close: () => Promise<boolean>
  live: boolean
  released: boolean
  pushed: Promise<void>
}

interface OverlayHistory {
  router: Router
  stack: OverlayEntry[]
  navigating: boolean
  settling: Promise<void>
}

let installed: OverlayHistory | null = null
let seq = 0

function logFailure(err: unknown): void {
  console.error('[overlay-back]', err)
}

function marker(state: OverlayHistory): string | null {
  const value = state.router.options.history.state?.[OVERLAY_STATE_KEY]
  return typeof value === 'string' ? value : null
}

function clearMarker(state: OverlayHistory): void {
  const h = state.router.options.history
  h.replace(h.location, { [OVERLAY_STATE_KEY]: null })
}

function remove(state: OverlayHistory, entry: OverlayEntry): void {
  const i = state.stack.indexOf(entry)
  if (i !== -1) state.stack.splice(i, 1)
}

function install(router: Router): OverlayHistory {
  if (installed?.router === router) return installed
  const state: OverlayHistory = { router, stack: [], navigating: false, settling: Promise.resolve() }
  installed = state
  if (marker(state) !== null) clearMarker(state) // left by a reload: names no overlay of this page
  router.beforeEach(async (to, from) => {
    await state.settling // a step-back we started must land first
    state.navigating = true
    if (to.fullPath !== from.fullPath) return true
    const top = state.stack[state.stack.length - 1]
    const here = marker(state)
    if (top?.live && here !== top.id) { // Back left the top overlay's entry
      if (pendingConfirm.value || pendingPrompt.value) return false
      return top.close()
    }
    if (here !== null && !state.stack.some(e => e.id === here)) return false // Forward onto a closed overlay
    return true
  })
  const idle = () => { state.navigating = false }
  router.afterEach(idle)
  router.onError(idle)
  return state
}

function claim(state: OverlayHistory, close: () => Promise<boolean>): OverlayEntry {
  const entry: OverlayEntry = {
    id: `${Date.now().toString(36)}-${++seq}`,
    fullPath: '',
    close,
    live: false,
    released: false,
    pushed: Promise.resolve(),
  }
  entry.pushed = state.settling.then(() => {
    if (entry.released) return
    entry.fullPath = state.router.currentRoute.value.fullPath
    state.router.options.history.push(entry.fullPath, { [OVERLAY_STATE_KEY]: entry.id })
    entry.live = true
    state.stack.push(entry)
  }).catch(logFailure) // a failed push costs this overlay its entry, not navigation
  return entry
}

function release(state: OverlayHistory, entry: OverlayEntry): void {
  entry.released = true
  state.settling = state.settling.then(() => entry.pushed).then(async () => {
    remove(state, entry)
    if (!entry.live) return
    entry.live = false
    if (marker(state) !== entry.id) return // Back took it, or a navigation moved on
    if (state.navigating) return // closed inside a navigation: it owns history
    if (state.router.currentRoute.value.fullPath !== entry.fullPath) { clearMarker(state); return }
    state.router.options.history.go(-1, false) // same address both sides: no navigation
    if (marker(state) !== entry.id) return // a history that steps synchronously (memory)
    await new Promise<void>((resolve) => {
      const done = () => { window.removeEventListener('popstate', done); window.clearTimeout(timer); resolve() }
      const timer = window.setTimeout(done, LAND_TIMEOUT_MS)
      window.addEventListener('popstate', done)
    })
  }).catch(logFailure) // a rejected `settling` would block every later navigation
}

export function useOverlayBackEntry(isOpen: () => boolean, close: () => Promise<boolean>): void {
  const router = inject(routerKey, null)
  if (!router) return
  const state = install(router)
  let entry: OverlayEntry | null = null
  // flush: 'sync' is load-bearing: `settling` must be set before the close
  // that triggered this watcher returns.
  const stop = watch(isOpen, (open) => {
    if (open && !entry) entry = claim(state, close)
    else if (!open && entry) { release(state, entry); entry = null }
  }, { immediate: true, flush: 'sync' })
  onBeforeUnmount(() => { stop(); if (entry) { release(state, entry); entry = null } })
}
