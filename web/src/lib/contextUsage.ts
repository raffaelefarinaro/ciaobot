// Context-window occupancy for the Chat details meter. No Vue imports.

/** The slice of a render item this helper reads. */
export interface ContextUsageItem {
  kind: string
  msg?: { origin?: string | null; timestamp?: string }
  meta?: { usage?: Record<string, unknown> | null } | null
}

/**
 * The newest occupancy the live stream reported for a chat, newer than any
 * reply it rode on when the turn left no reply (a compaction). `usage` is
 * null after a compaction that has not reported the new figure yet.
 */
export interface LiveContextReport {
  usage: Record<string, unknown> | null
  at: string
}

export interface ContextUsage {
  /** Percent of the window in use, or null when it is unknown. */
  pct: number | null
  /** A compaction is newer than the last reported usage. */
  compacted: boolean
  /** Tokens in use and the window's size, when the provider reported both. */
  used: number | null
  size: number | null
}

const COMPACTED_PREFIX = /^compacted context\b/i

/** True for the stream's status that a compaction finished. */
export function isCompactedStatus(message: string): boolean {
  return COMPACTED_PREFIX.test(message)
}

/**
 * A stamp for a live report that is newer than every message already held,
 * whatever the browser clock says: replies carry the server's clock, so a
 * browser running behind would otherwise date a compaction before the reply
 * it supersedes and bring back the stale figure.
 */
export function stampAfter(messages: readonly { timestamp?: string }[], now: number = Date.now()): string {
  let newest = Number.NEGATIVE_INFINITY
  for (const m of messages) {
    const t = Date.parse(m.timestamp ?? '')
    if (t > newest) newest = t
  }
  return new Date(Math.max(now, newest + 1)).toISOString()
}

const NONE: ContextUsage = { pct: null, compacted: false, used: null, size: null }

function tokenCount(value: unknown): number | null {
  const n = Number.parseFloat(String(value ?? ''))
  return Number.isFinite(n) && n >= 0 ? n : null
}

/**
 * One usage report read as occupancy. Providers report the percentage as text
 * ("13.2%"), so parseFloat, not Number. Null when it carries none.
 */
function fromUsage(usage: Record<string, unknown>): ContextUsage | null {
  const pct = Number.parseFloat(String(usage.context_pct ?? usage.contextPct ?? ''))
  if (!Number.isFinite(pct) || pct <= 0) return null
  const used = tokenCount(usage.context_used)
  const size = tokenCount(usage.context_size)
  return { pct, compacted: false, used, size: size && size > 0 ? size : null }
}

function itemTime(item: ContextUsageItem): number {
  return Date.parse(item.msg?.timestamp ?? '')
}

/**
 * The context occupancy as of the newest item, or of the live report when it
 * is newer. A compaction newer than the last usage report makes that report
 * stale, so the figure is hidden until the provider reports a new one.
 */
export function latestContextUsage(
  items: readonly ContextUsageItem[],
  live: LiveContextReport | null = null,
): ContextUsage {
  const liveAt = live ? Date.parse(live.at) : Number.NaN
  let compacted = false
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i]
    if (live && itemTime(item) < liveAt) break
    if (item.kind === 'system' && item.msg?.origin === 'compaction') {
      compacted = true
      continue
    }
    if (item.kind !== 'assistant' || !item.meta?.usage) continue
    const report = fromUsage(item.meta.usage)
    if (!report) return { ...NONE, compacted }
    return compacted ? { ...NONE, compacted: true } : report
  }
  if (!live || compacted) return { ...NONE, compacted }
  const report = live.usage ? fromUsage(live.usage) : null
  return report ?? { ...NONE, compacted: live.usage === null }
}
