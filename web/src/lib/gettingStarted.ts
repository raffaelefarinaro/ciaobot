export const GETTING_STARTED_STORAGE_KEY = 'ciao-getting-started-dismissed'

/** Snapshot of the app state the checklist derives completion from. */
export type GettingStartedState = {
  /** Providers reporting ok=true in /api/setup-status. */
  providerReadyCount: number
  workspaceCount: number
  /** Projects created by the user (excludes auto/system projects). */
  userProjectCount: number
  scheduleCount: number
  /** Chats that have seen at least one message (last_activity_at set). */
  activeChatCount: number
}

export interface GettingStartedItem {
  id: string
  title: string
  body: string
  cta: string
  /** Router path the CTA deep-links to. */
  route: string
  /** The target UI lives in the sidebar, so the CTA also opens it. */
  opensSidebar?: boolean
  done: (state: GettingStartedState) => boolean
}

export const GETTING_STARTED_ITEMS: GettingStartedItem[] = [
  {
    id: 'first-chat',
    title: 'Send your first message',
    body: 'Say hello, or ask "what can Ciaobot do?" for a guided walkthrough of memory, skills, and integrations.',
    cta: 'Open a chat',
    route: '/',
    opensSidebar: true,
    done: s => s.activeChatCount > 0,
  },
  {
    id: 'project',
    title: 'Create your first project',
    body: 'Projects group related chats and inject durable context into every conversation. Use "+ Project" in the sidebar.',
    cta: 'Show the sidebar',
    route: '/',
    opensSidebar: true,
    done: s => s.userProjectCount > 0,
  },
  {
    id: 'workspace',
    title: 'Add a second workspace',
    body: 'Split life areas — personal, work, a client. Each workspace gets its own vault, projects, and default model.',
    cta: 'Open workspace settings',
    route: '/settings/workspaces',
    done: s => s.workspaceCount >= 2,
  },
  {
    id: 'schedule',
    title: 'Schedule a routine',
    body: 'Dispatch recurring prompts into a project or chat — daily reviews, weekly digests, maintenance checks.',
    cta: 'Open schedules',
    route: '/schedules',
    done: s => s.scheduleCount > 0,
  },
  {
    id: 'provider',
    title: 'Connect another provider',
    body: 'Add a second backend — Claude Code, Codex, Ollama, or OpenRouter — and switch per workspace or per chat.',
    cta: 'Open provider settings',
    route: '/settings/providers',
    done: s => s.providerReadyCount >= 2,
  },
]

/** An item with its `done` predicate resolved against the current state. */
export type GettingStartedItemStatus = Omit<GettingStartedItem, 'done'> & { done: boolean }

export type GettingStartedProgress = {
  items: GettingStartedItemStatus[]
  doneCount: number
  total: number
  allDone: boolean
}

export function gettingStartedProgress(state: GettingStartedState): GettingStartedProgress {
  const items = GETTING_STARTED_ITEMS.map(item => ({ ...item, done: item.done(state) }))
  const doneCount = items.filter(i => i.done).length
  return { items, doneCount, total: items.length, allDone: doneCount === items.length }
}

/** Defensive: /api/setup-status may be unreachable or oddly shaped. */
export function countReadyProviders(status: unknown): number {
  const providers = (status as { providers?: unknown } | null)?.providers
  if (!providers || typeof providers !== 'object') return 0
  return Object.values(providers as Record<string, unknown>).filter(
    row => !!(row as { ok?: boolean } | null)?.ok,
  ).length
}

export function isChecklistDismissed(): boolean {
  try {
    return localStorage.getItem(GETTING_STARTED_STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

export function markChecklistDismissed(): void {
  try {
    localStorage.setItem(GETTING_STARTED_STORAGE_KEY, '1')
  } catch {
    // ignore quota / private browsing
  }
}

export function clearChecklistDismissed(): void {
  try {
    localStorage.removeItem(GETTING_STARTED_STORAGE_KEY)
  } catch {
    // ignore
  }
}
