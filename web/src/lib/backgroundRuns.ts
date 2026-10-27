import type { BackgroundRunSummary } from './types'
import { formatDuration } from './time'

// The agent names most runs ("clone rizzo-flow"); an unnamed one falls back to
// its command, which is long, so the caller's CSS truncates it.
export function backgroundRunLabel(run: BackgroundRunSummary): string {
  return run.label.trim() || run.cmd.join(' ') || 'Background run'
}

// Whole seconds: a run's age ticks once a second, and "4.3s" would jitter.
export function backgroundRunElapsed(run: BackgroundRunSummary, now: number): string {
  const started = Date.parse(run.started_at)
  if (!Number.isFinite(started)) return ''
  const ms = Math.max(0, now - started)
  return ms < 60_000 ? `${Math.floor(ms / 1000)}s` : formatDuration(Math.floor(ms / 1000) * 1000)
}

// Whole minutes for the composer bar, where a per-second tick would make the
// part's width jitter. Under a minute reads "<1m"; an hour or more "1h 2m".
export function minutesSince(startedAt: string, now: number): string {
  const started = Date.parse(startedAt)
  if (!Number.isFinite(started)) return ''
  const minutes = Math.floor(Math.max(0, now - started) / 60_000)
  if (minutes < 1) return '<1m'
  if (minutes < 60) return `${minutes}m`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

export function backgroundRunMinutes(run: BackgroundRunSummary, now: number): string {
  return minutesSince(run.started_at, now)
}

// How a finished run ended, for the composer bar's outcome part.
export function backgroundRunOutcome(run: BackgroundRunSummary): { text: string; tone: 'ok' | 'error' | 'muted' } {
  const label = backgroundRunLabel(run)
  if (run.status === 'ok') return { text: `${label} finished`, tone: 'ok' }
  if (run.status === 'cancelled') return { text: `${label} stopped`, tone: 'muted' }
  const code = run.exit_code == null ? '' : ` (exit ${run.exit_code})`
  return { text: `${label} failed${code}`, tone: 'error' }
}
