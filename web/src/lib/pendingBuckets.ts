export type PendingBuckets<T> = Record<string, T[]>

export function normalizePendingBuckets<T>(value: unknown, activeChatId?: string | null): PendingBuckets<T> {
  if (Array.isArray(value)) {
    return activeChatId && value.length ? { [activeChatId]: value as T[] } : {}
  }
  if (!value || typeof value !== 'object') return {}
  const out: PendingBuckets<T> = {}
  for (const [chatId, entries] of Object.entries(value as Record<string, unknown>)) {
    if (!chatId || !Array.isArray(entries) || entries.length === 0) continue
    out[chatId] = entries as T[]
  }
  return out
}

export function getPendingBucket<T>(buckets: PendingBuckets<T>, chatId?: string | null): T[] {
  if (!chatId) return []
  return buckets[chatId] || []
}

export function setPendingBucket<T>(buckets: PendingBuckets<T>, chatId: string, entries: T[]): void {
  if (entries.length) buckets[chatId] = entries
  else delete buckets[chatId]
}
