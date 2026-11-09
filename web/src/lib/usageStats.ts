// Client and formatters for GET /api/stats (the Usage page and Home's Today rows).
// Every numeric field may be null, so every response goes through normalizeUsageStats.
import { api } from './api'
import { modelDisplayName } from './modelLabels'

export const STATS_RANGES = [7, 30, 90, 365] as const
export type StatsRange = (typeof STATS_RANGES)[number]

export interface UsageCounts {
  chats_opened: number | null
  messages_sent: number | null
  chats_archived: number | null
  memories_saved: number | null
  tasks_completed: number | null
  projects_worked_on: number | null
  automation_runs: number | null
  tokens: number | null
}

export interface UsageToday {
  chats_opened: number | null
  messages_sent: number | null
  chats_archived: number | null
  memories_saved: number | null
  projects_worked_on: number | null
  tasks_completed: number | null
  avg_chats_opened_30d: number | null
  avg_messages_sent_30d: number | null
}

export interface UsageDay { date: string; count: number | null }
export interface UsageProject { project_id: string; name: string; messages: number | null }
export interface UsagePipeline { proposed: number | null; accepted: number | null; notes_updated: number | null }
export interface UsageModel { model: string; turns: number | null }

export interface UsageStats {
  range_days: number
  generated_at: string
  today: UsageToday
  totals: UsageCounts
  previous: UsageCounts
  daily_messages: UsageDay[]
  projects: UsageProject[]
  memory_pipeline: UsagePipeline
  hours: Array<number | null> // always length 24
  models: UsageModel[]
}

const COUNT_KEYS = ['chats_opened', 'messages_sent', 'chats_archived', 'memories_saved', 'tasks_completed', 'projects_worked_on', 'automation_runs', 'tokens'] as const
const TODAY_KEYS = ['chats_opened', 'messages_sent', 'chats_archived', 'memories_saved', 'projects_worked_on', 'tasks_completed', 'avg_chats_opened_30d', 'avg_messages_sent_30d'] as const
const PIPELINE_KEYS = ['proposed', 'accepted', 'notes_updated'] as const
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/

export const DASH = '—'

function num(v: unknown): number | null { return typeof v === 'number' && Number.isFinite(v) ? v : null }
function obj(v: unknown): Record<string, unknown> { return v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {} }
function str(v: unknown): string { return typeof v === 'string' ? v : '' }
function list(v: unknown): unknown[] { return Array.isArray(v) ? v : [] }
function pick<K extends string>(src: unknown, keys: readonly K[]): Record<K, number | null> {
  const o = obj(src)
  return Object.fromEntries(keys.map(k => [k, num(o[k])])) as Record<K, number | null>
}

export function normalizeUsageStats(raw: unknown, range: StatsRange): UsageStats {
  const o = obj(raw)
  const hours = list(o.hours).slice(0, 24).map(num)
  while (hours.length < 24) hours.push(null)
  return {
    range_days: num(o.range_days) ?? range,
    generated_at: str(o.generated_at),
    today: pick(o.today, TODAY_KEYS),
    totals: pick(o.totals, COUNT_KEYS),
    previous: pick(o.previous, COUNT_KEYS),
    daily_messages: list(o.daily_messages).map(obj)
      .filter(d => typeof d.date === 'string' && DATE_RE.test(d.date))
      .map(d => ({ date: d.date as string, count: num(d.count) })),
    projects: list(o.projects).map(obj)
      .filter(p => str(p.project_id) || str(p.name))
      .map(p => ({ project_id: str(p.project_id), name: str(p.name) || str(p.project_id), messages: num(p.messages) })),
    memory_pipeline: pick(o.memory_pipeline, PIPELINE_KEYS),
    hours,
    models: list(o.models).map(obj).filter(m => str(m.model))
      .map(m => ({ model: str(m.model), turns: num(m.turns) })),
  }
}

export async function fetchUsageStats(workspace: string, range: StatsRange): Promise<UsageStats> {
  return normalizeUsageStats(await api.get<unknown>(`/api/stats?workspace=${encodeURIComponent(workspace)}&range=${range}`), range)
}

export function formatCount(v: number | null): string { return v === null ? DASH : Math.round(v).toLocaleString('en-US') }

export function formatCompact(v: number | null): string {
  if (v === null) return DASH
  for (const [size, suffix] of [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']] as const) {
    if (Math.abs(v) >= size) {
      const scaled = v / size
      return `${scaled >= 100 ? Math.round(scaled) : scaled.toFixed(1).replace(/\.0$/, '')}${suffix}`
    }
  }
  return formatCount(v)
}

export function formatDelta(current: number | null, previous: number | null): string | null {
  if (current === null || previous === null || previous === 0) return null
  const pct = Math.round(((current - previous) / previous) * 100)
  return pct > 0 ? `+${pct}%` : `${pct === 0 ? 0 : pct}%` // never "-0%"
}

export function periodLabel(rangeDays: number): string { return rangeDays === 365 ? 'previous year' : `previous ${rangeDays} days` }
export function rangeLabel(range: StatsRange): string { return range === 365 ? 'Year' : `${range} days` }

export type HeatLevel = 0 | 1 | 2 | 3 | 4
export interface HeatCell { date: string | null; count: number | null; level: HeatLevel }
export interface ModelShare { model: string; label: string; turns: number | null; percent: number }

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

export function heatLevel(count: number | null, max: number): HeatLevel {
  if (count === null || count <= 0 || max <= 0) return 0
  return Math.min(4, Math.max(1, Math.ceil((count / max) * 4))) as HeatLevel
}

export function weekdayIndex(date: string): number {
  const [y, m, d] = date.split('-').map(Number)
  return (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7
}

export function formatDay(date: string): string {
  const [y, m, d] = date.split('-').map(Number)
  return `${WEEKDAYS[weekdayIndex(date)]} ${d} ${MONTHS[m - 1]} ${y}`
}

export function heatmapCells(days: UsageDay[]): HeatCell[] {
  if (!days.length) return []
  const max = Math.max(0, ...days.map(d => d.count ?? 0))
  const pad: HeatCell[] = Array.from({ length: weekdayIndex(days[0].date) }, () => ({ date: null, count: null, level: 0 }))
  return [...pad, ...days.map(d => ({ date: d.date, count: d.count, level: heatLevel(d.count, max) }))]
}

export function heatCellLabel(cell: HeatCell): string {
  if (!cell.date) return ''
  const what = cell.count === null ? 'no data' : `${formatCount(cell.count)} message${cell.count === 1 ? '' : 's'}`
  return `${formatDay(cell.date)}: ${what}`
}

export function barPercent(value: number | null, max: number): string {
  if (value === null || max <= 0) return '0%'
  return `${Math.round((Math.max(0, value) / max) * 1000) / 10}%`
}

export function busiestHour(hours: Array<number | null>): number | null {
  let best: number | null = null
  hours.forEach((v, h) => { if (v !== null && v > 0 && (best === null || v > (hours[best] ?? 0))) best = h })
  return best
}

export function topProjects(projects: UsageProject[], limit = 8): UsageProject[] {
  return [...projects].sort((a, b) => (b.messages ?? -1) - (a.messages ?? -1)).slice(0, limit)
}

export function modelShares(models: UsageModel[]): ModelShare[] {
  const total = models.reduce((sum, m) => sum + Math.max(0, m.turns ?? 0), 0)
  return [...models].sort((a, b) => (b.turns ?? 0) - (a.turns ?? 0)).map(m => ({
    model: m.model, label: modelDisplayName(m.model) || m.model, turns: m.turns,
    percent: total > 0 ? Math.round((Math.max(0, m.turns ?? 0) / total) * 100) : 0,
  }))
}

export function periodText(rangeDays: number): string { return rangeDays === 365 ? 'year' : `${rangeDays} days` }
