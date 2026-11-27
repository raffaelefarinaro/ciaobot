/* ------------------------------------------------------------------ *
 * What a chat is running right now: its agents (subagent cards) and its
 * background shells (shell cards), read from the cards the activity trace
 * already keeps. Only running cards count; a settled card never does.
 *
 * Live cards (the turn in progress) win over history rows for the same
 * tool_use_id, so a card that settled live is not counted again from a
 * reloaded row that still says running. Pure helpers, no Vue imports.
 * ------------------------------------------------------------------ */

import type { ShellCard } from './shellCard'
import type { SubagentCard } from './subagentCard'

/** One card as the store holds it: live in the streaming timeline, or from a history row. */
export type WorkCard =
  | { kind: 'subagent'; card: SubagentCard; started_at?: number }
  | { kind: 'shell'; card: ShellCard; started_at?: number }

export interface RunningWorkRow {
  tool_use_id: string
  label: string
  /** The agent type of a subagent row; empty for a shell. */
  detail: string
  /** Epoch ms the live card was created; undefined for a card from history. */
  started_at?: number
}

export interface RunningWork {
  agents: RunningWorkRow[]
  shells: RunningWorkRow[]
}

export const NO_RUNNING_WORK: RunningWork = { agents: [], shells: [] }

/** Running work from the live cards, then the history cards. A card with an id already seen is skipped. */
export function runningWorkFrom(live: WorkCard[], history: WorkCard[] = []): RunningWork {
  const seen = new Set<string>()
  const work: RunningWork = { agents: [], shells: [] }
  for (const entry of [...live, ...history]) {
    const id = entry.card.tool_use_id
    if (seen.has(id)) continue
    seen.add(id)
    if (entry.kind === 'subagent') {
      if (entry.card.state !== 'running') continue
      work.agents.push({
        tool_use_id: id,
        label: entry.card.description || 'Subagent',
        detail: entry.card.agent_type,
        started_at: entry.started_at,
      })
    } else {
      // A foreground shell runs inside its turn, which the chat already shows as busy.
      if (entry.card.state !== 'running' || !entry.card.background) continue
      work.shells.push({
        tool_use_id: id,
        label: entry.card.command || 'Shell',
        detail: '',
        started_at: entry.started_at,
      })
    }
  }
  return work
}

/** A chat row's words for its running background shells: "1 shell running", "3 shells running". */
export function runningShellsLabel(count: number): string {
  return count === 1 ? '1 shell running' : `${count} shells running`
}

/** Elapsed time since a live card started: "12s", "3m 4s", "1h 2m". Empty when the start is unknown. */
export function elapsedLabel(startedAt: number | undefined, now: number): string {
  if (startedAt === undefined || !Number.isFinite(startedAt)) return ''
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}
