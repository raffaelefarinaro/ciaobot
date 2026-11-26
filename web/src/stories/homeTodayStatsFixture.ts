import { defineComponent, h } from 'vue'
import type { RouteRecordRaw } from 'vue-router'
import type { FixtureRoute } from './fixtureBoundary'

/** The four rendered HomeTodayStats fixture states: loaded, held loading, HTTP failure, partial normalization. */
export type StatsState = 'default' | 'loading' | 'failed' | 'partial'

/** Synthetic workspaces only: no real workspace names, paths or tokens. */
export const statsWorkspaces = ['storybook-alpha', 'storybook-beta'] as const

const today = {
  chats_opened: 12, messages_sent: 24, chats_archived: 2,
  projects_worked_on: 2, tasks_completed: 1,
  avg_chats_opened_30d: 1.2, avg_messages_sent_30d: 8.4, avg_chats_archived_30d: 0.5,
  avg_projects_worked_on_30d: 1, avg_tasks_completed_30d: 0.2,
}

function respondStats(state: StatsState, workspace: string): Response | Promise<never> {
  if (state === 'loading') return new Promise<never>(() => {})
  const payload = state === 'failed' ? { error: 'Synthetic stats unavailable' }
    : state === 'partial' ? { today: { chats_opened: 0, messages_sent: 'invalid', projects_worked_on: 3, avg_chats_opened_30d: 0.4 } }
      : { today: { ...today, chats_opened: workspace === statsWorkspaces[1] ? 4 : 12 } }
  return new Response(JSON.stringify(payload), {
    status: state === 'failed' ? 503 : 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Exact declared reads: `/api/stats?workspace=<synthetic>&range=30`, one per synthetic workspace. */
export function statsRoutes(state: StatsState): FixtureRoute[] {
  return (statsWorkspaces as readonly string[]).map(workspace => ({
    method: 'GET' as const,
    path: `/api/stats?workspace=${workspace}&range=30`,
    respond: () => respondStats(state, workspace),
  }))
}

/** `/stats` is an inert destination: not the Usage page, no bootstrap mounted. */
export function usageRoute(): RouteRecordRaw {
  return {
    path: '/stats',
    component: defineComponent({ render: () => h('p', 'Inert Usage destination — no Usage page or bootstrap mounted.') }),
  }
}
