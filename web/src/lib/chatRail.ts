import type { HomeTierKey } from './homeLanes'

export const RAIL_MAX_DOTS = 12 // above this the dots row becomes a mono "3 / 40" count
export const RAIL_SETTLE_MS = 120 // debounce after the last scroll event
export const RAIL_SLIDE_MS = 220 // a keyboard step slides the title this long before switching

export interface ChatRailItem {
  chatId: string
  title: string
  project: string
  tier: HomeTierKey
  titlePending: boolean
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

const STATE_WORDS: Partial<Record<HomeTierKey, string>> = {
  needsYou: 'needs you',
  working: 'working',
  unread: 'unread',
}

/** Accessible name: "<title>, <project>" plus ", needs you" | ", working" | ", unread"; pending title → "Naming conversation, <project>". Empty project omitted. */
export function railItemLabel(item: Pick<ChatRailItem, 'title' | 'project' | 'tier' | 'titlePending'>): string {
  const parts = [item.titlePending ? 'Naming conversation' : item.title]
  if (item.project) parts.push(item.project)
  const state = STATE_WORDS[item.tier]
  if (state) parts.push(state)
  return parts.join(', ')
}
