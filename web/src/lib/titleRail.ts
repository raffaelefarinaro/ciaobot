import type { HomeTierKey } from './homeLanes'

export const RAIL_SETTLE_MS = 120 // debounce after the last scroll event
export const RAIL_SLIDE_MS = 220 // a keyboard step slides the title this long before switching

/** The state mark a rail segment carries, drawn with the app's activity glyphs in miniature. */
export type RailMark = 'needs' | 'working' | 'unread'

/**
 * One entry on a title rail: an open chat or an open automation. The rail shows
 * `title` with `subtitle` under it, `mark` on the item's track segment, and
 * `label` as the item's accessible name.
 */
export interface RailItem {
  id: string
  title: string
  subtitle: string
  mark: RailMark | null
  titlePending: boolean
  label: string
}

/**
 * The sidebar's project order, top to bottom: the pinned projects (General and
 * the other auto-managed ones) as the server orders them, then the user's
 * projects in their drag order. `projects` is the workspace's list as the store
 * sorts it. The chat rail walks each project's chats in this order; the project
 * rail walks the projects themselves.
 */
export function sidebarProjectOrder<T extends { is_auto?: boolean }>(projects: readonly T[]): T[] {
  return [...projects.filter(p => p.is_auto), ...projects.filter(p => !p.is_auto)]
}

/** Snapshot ids that still exist, in snapshot order, then new ids in fresh order. previous=null → fresh. */
export function stableRailOrder(fresh: readonly string[], previous: readonly string[] | null): string[] {
  if (previous === null) return [...fresh]
  const live = new Set(fresh)
  const kept = previous.filter(id => live.has(id))
  const seen = new Set(kept)
  return [...kept, ...fresh.filter(id => !seen.has(id))]
}

/** No wrap-around: previous/next are null at the ends; index -1 and both null when currentId is absent. */
export function railNeighbours(
  order: readonly string[],
  currentId: string,
): { index: number; previous: string | null; next: string | null } {
  const index = order.indexOf(currentId)
  if (index === -1) return { index: -1, previous: null, next: null }
  return {
    index,
    previous: index > 0 ? order[index - 1] : null,
    next: index < order.length - 1 ? order[index + 1] : null,
  }
}

/** Index of the centre nearest viewportCentre (ties → lower index); -1 for an empty list. */
export function settledIndex(viewportCentre: number, centres: readonly number[]): number {
  let best = -1
  let bestDistance = Infinity
  centres.forEach((centre, i) => {
    const distance = Math.abs(centre - viewportCentre)
    if (distance < bestDistance) {
      best = i
      bestDistance = distance
    }
  })
  return best
}

/** Inline style for the body under a rail mid-swipe: it leans a quarter of the way and fades. */
export function railLeanStyle(lean: number): { transform: string; opacity: string } | undefined {
  if (lean === 0) return undefined
  return { transform: `translateX(${-lean * 25}%)`, opacity: String(1 - Math.min(1, Math.abs(lean)) * 0.5) }
}

/** Classes for the body under a rail (railMotion.css): leaning mid-swipe, or arriving from a side. */
export function railMotionClass(lean: number, arrival: -1 | 1 | 0): Record<string, boolean> {
  return {
    'rail-leaning': lean !== 0,
    'rail-arrive-next': arrival > 0,
    'rail-arrive-prev': arrival < 0,
  }
}

const TIER_MARKS: Partial<Record<HomeTierKey, RailMark>> = {
  needsYou: 'needs',
  working: 'working',
  unread: 'unread',
}

/** A chat's Home tier as a rail mark; a quiet chat has none. */
export function chatTierMark(tier: HomeTierKey): RailMark | null {
  return TIER_MARKS[tier] ?? null
}

const STATE_WORDS: Partial<Record<HomeTierKey, string>> = {
  needsYou: 'needs you',
  working: 'working',
  unread: 'unread',
}

/** Accessible name: "<title>, <project>" plus ", needs you" | ", working" | ", unread"; pending title → "Naming conversation, <project>". Empty project omitted. */
export function railItemLabel(item: { title: string; project: string; tier: HomeTierKey; titlePending: boolean }): string {
  const parts = [item.titlePending ? 'Naming conversation' : item.title]
  if (item.project) parts.push(item.project)
  const state = STATE_WORDS[item.tier]
  if (state) parts.push(state)
  return parts.join(', ')
}
