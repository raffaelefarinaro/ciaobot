/**
 * Start the chat that trims the two bounded regions every chat loads
 * (`ciao:memory` and `ciao:profile` in the workspace guide).
 *
 * Called by Memory's Always loaded window (MemoryGuideBudget.vue). Any other
 * surface that offers to trim the regions should call this too, so they all
 * open the same conversation. The prompt is a draft the person sends
 * (startFileDiscussion never auto-sends), the guide is pinned beside it, and the
 * prompt asks the agent to show the trimmed version and wait for approval
 * before it changes anything.
 */
import { startFileDiscussion } from './fileDiscussion'
import { fetchWorkspaceGuide } from './workspaceGuide'
import type { useProjectStore } from '../stores/projects'
import type { ChatInfo } from './types'

type ProjectStore = ReturnType<typeof useProjectStore>

export const OPTIMIZE_CHAT_TITLE = 'Trim always-loaded memory'

/** The opening draft for `guidePath`. */
export function memoryOptimizeSeed(guidePath: string): string {
  return [
    `Help me trim what every chat loads. Read the two bounded regions in \`${guidePath}\` (\`ciao:memory\` and \`ciao:profile\`) and run \`ciao memory status\`. Tell me which region is over or near its cap.`,
    '',
    'Then propose a trimmed version of each region: merge duplicates, drop entries that are expired, stale or can be found elsewhere, move facts that only matter to one project into that project\'s doc, and shorten the wording. Keep every lasting fact I would miss.',
    '',
    'Show me the before and after and wait for my approval. Only after I agree, apply it with `ciao memory update`, logging each removed or replaced entry in `Workspace/Memory-Consolidations.md` in the vault with `by: chat <this chat\'s id>`, as the undo-log rule in the memory guide says. Then run `ciao memory status` again and tell me the new percentages.',
  ].join('\n')
}

/**
 * Open the trim chat for `workspace` (the active one by default), with its
 * guide pinned. Resolves the chat, or null when there is no guide or the chat
 * could not be started (a toast says why).
 */
export async function startMemoryOptimizeChat(
  store: ProjectStore,
  options: { workspace?: string; guidePath?: string } = {},
): Promise<ChatInfo | null> {
  const workspace = options.workspace || store.activeWorkspace
  let path = options.guidePath || ''
  if (!path) {
    const guide = await fetchWorkspaceGuide(workspace)
    path = guide.path
    if (!path) {
      store.pushErrorToast('Cannot start chat', guide.error || `The ${workspace} workspace has no AGENTS.md yet.`)
      return null
    }
  }
  return startFileDiscussion(store, { path, seed: memoryOptimizeSeed(path), title: OPTIMIZE_CHAT_TITLE, workspace })
}
