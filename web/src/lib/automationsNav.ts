/**
 * The Automations destination's three pages: Schedules (routines, one-offs and
 * Ciaobot's system routines), Loops (chat-owned loops) and Webhooks (triggers).
 * The sidebar's sub-navigation and the phone switcher both read this list.
 *
 * Schedules keeps `/schedules`, the address every existing link uses, and an
 * open automation stays `/schedules/<id>`. Loops and Webhooks live under
 * `/automations/`: a schedule id is a free string an imported schedule file can
 * set, so `/schedules/loops` could name a real automation. Pure, no Vue.
 */
import { isOwnedLoop } from './loops'
import type { Schedule } from './types'

export type AutomationsPage = 'schedules' | 'loops' | 'webhooks'

export interface AutomationsPageLink {
  id: AutomationsPage
  label: string
  to: string
}

export const AUTOMATIONS_PAGES: readonly AutomationsPageLink[] = [
  { id: 'schedules', label: 'Schedules', to: '/schedules' },
  { id: 'loops', label: 'Loops', to: '/automations/loops' },
  { id: 'webhooks', label: 'Webhooks', to: '/automations/webhooks' },
]

/** The address of one of the three pages. */
export function automationsPagePath(page: AutomationsPage): string {
  return AUTOMATIONS_PAGES.find(p => p.id === page)!.to
}

/** Whether a path belongs to the Automations destination. */
export function isAutomationsPath(path: string): boolean {
  return path === '/schedules' || path.startsWith('/schedules/')
    || path === '/automations' || path.startsWith('/automations/')
}

/**
 * The page a path shows. An open automation belongs to the page that lists it:
 * a loop to Loops, everything else to Schedules.
 */
export function automationsPageFor(path: string, openSchedule?: Pick<Schedule, 'owner_chat_id'> | null): AutomationsPage {
  if (path === '/automations/loops') return 'loops'
  if (path === '/automations/webhooks') return 'webhooks'
  if (openSchedule && isOwnedLoop(openSchedule)) return 'loops'
  return 'schedules'
}
