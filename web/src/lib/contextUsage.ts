// Context-window occupancy for the Chat details meter. No Vue imports.

/** Token counts from a Claude compact_boundary: the window before and after compaction. */
export interface CompactTokens {
  pre: number
  post: number
}

/** The slice of a render item this helper reads. */
export interface ContextUsageItem {
  kind: string
  msg?: { origin?: string | null }
  meta?: { usage?: Record<string, unknown> | null } | null
}

export interface ContextUsage {
  /** Percent of the window in use, or null when it is unknown. */
  pct: number | null
  /** A compaction is newer than the last reported usage. */
  compacted: boolean
}

const COMPACTED_PREFIX = /^compacted context\b/i
const COMPACTED_TOKENS = /([\d,]+)\s*→\s*([\d,]+)/

/**
 * Reads the stream's status message for a finished compaction. Returns null
 * for any other status (including the "compacting" ticks while it runs).
 */
export function parseCompactedStatus(
  message: string,
): { done: true; tokens: CompactTokens | null } | null {
  if (!COMPACTED_PREFIX.test(message)) return null
  const match = COMPACTED_TOKENS.exec(message)
  if (!match) return { done: true, tokens: null }
  const pre = Number(match[1].replace(/,/g, ''))
  const post = Number(match[2].replace(/,/g, ''))
  if (!(pre > 0)) return { done: true, tokens: null }
  return { done: true, tokens: { pre, post } }
}

/**
 * The context occupancy as of the newest item. A compaction newer than the
 * last usage report makes that report stale: it is rescaled by the
 * compaction's post/pre token ratio, or hidden when those counts are unknown.
 */
export function latestContextUsage(
  items: readonly ContextUsageItem[],
  tokens: CompactTokens | null,
): ContextUsage {
  let compacted = false
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i]
    if (item.kind === 'system' && item.msg?.origin === 'compaction') {
      compacted = true
      continue
    }
    if (item.kind !== 'assistant' || !item.meta?.usage) continue
    const usage = item.meta.usage
    const pct = Number.parseFloat(String(usage.context_pct ?? usage.contextPct ?? ''))
    if (!Number.isFinite(pct) || pct <= 0) return { pct: null, compacted }
    if (!compacted) return { pct, compacted: false }
    if (!tokens) return { pct: null, compacted: true }
    return { pct: Math.min(100, (pct * tokens.post) / tokens.pre), compacted: true }
  }
  return { pct: null, compacted }
}
