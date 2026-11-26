import { STATS_RANGES, normalizeUsageStats } from '../lib/usageStats'
import type { StatsRange } from '../lib/usageStats'

export const usageWorkspaces = ['storybook-alpha', 'storybook-beta'] as const

const DAY_ROWS = Array.from({ length: 364 }, (_, i) => {
  const d = new Date(Date.UTC(2026, 10, 9 - (363 - i)))
  return { date: d.toISOString().slice(0, 10), count: (i + 3) % 5 }
})

/** Synthetic full usage body for workshop stories (no live workspace paths). */
export function syntheticUsageRaw(workspace: string, range: StatsRange) {
  const totalsScale = workspace === 'storybook-beta' ? 0.6 : 1
  return {
    range_days: range,
    generated_at: '2026-11-09T10:00:00Z',
    totals: {
      chats_opened: Math.round(248 * totalsScale),
      messages_sent: Math.round(4517 * totalsScale),
      chats_archived: Math.round(171 * totalsScale),
     
      tasks_completed: Math.round(37 * totalsScale),
      projects_worked_on: Math.round(16 * totalsScale),
      automation_runs: Math.round(180 * totalsScale),
      tokens: Math.round(71500000 * totalsScale),
    },
    previous: {
      chats_opened: Math.round(210 * totalsScale),
      messages_sent: Math.round(3702 * totalsScale),
      chats_archived: Math.round(157 * totalsScale),
     
      tasks_completed: Math.round(31 * totalsScale),
      projects_worked_on: Math.round(13 * totalsScale),
      automation_runs: Math.round(173 * totalsScale),
      tokens: null,
    },
    daily_messages: DAY_ROWS,
    projects: [
      { project_id: 'sb-alpha-general', name: 'storybook-alpha', messages: 412 },
      { project_id: 'sb-alpha-notes', name: 'storybook-alpha-notes', messages: 260 },
      { project_id: 'sb-beta-general', name: 'storybook-beta', messages: 198 },
    ],
    hours: [2, 1, 0, 0, 0, 0, 1, 4, 9, 14, 16, 12, 7, 11, 15, 13, 10, 8, 6, 9, 14, 17, 12, 6],
    models: [
      { model: 'storybook-model-a', turns: 680 },
      { model: 'storybook-model-b', turns: 220 },
      { model: 'storybook-model-c', turns: 100 },
    ],
  }
}

export function syntheticUsageStats(workspace: string, range: StatsRange) {
  return normalizeUsageStats(syntheticUsageRaw(workspace, range), range)
}

export function declaredUsagePath(workspace: string, range: StatsRange): string {
  return `/api/stats?workspace=${encodeURIComponent(workspace)}&range=${range}`
}

export const declaredUsagePaths = usageWorkspaces.flatMap(workspace =>
  STATS_RANGES.map(range => declaredUsagePath(workspace, range)),
)