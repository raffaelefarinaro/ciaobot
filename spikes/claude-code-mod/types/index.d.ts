export type Workspace = { name: string; vaultRoot: string; gwsProfile: string; color: string }

export type Project = {
  id: string
  name: string
  workspace: string
  context: string
  canonicalDoc: string
  order: number
  chatCount: number
  recentChats: string[]
}

export type Catalog = { root: string; workspaces: Workspace[]; projects: Project[]; loadedAt: string }

// One Ciao chat: a subagent of the host session the /ciaobot pane runs in.
export type Chat = {
  agentId: string
  projectId: string
  projectName: string
  workspace: string
  title: string
  lastActivity: string
  isRunning: boolean
}

export type ChatLine = { role: 'user' | 'assistant'; text: string; tools: number }

export type MemoryJob = { agentId: string; projectName: string; workspace: string; title: string; queuedAt: string }

export type View = { kind: 'home' } | { kind: 'chat'; agentId: string }

declare module 'claude-code' {
  interface PluginState {
    ciao: {
      catalog: Catalog | null
      catalogError: string
      current: Project | null
      workspace: string
      chats: Chat[]
      view: View
      lines: ChatLine[]
      expanded: string[]
      notice: string
      isAppOpen: boolean
      isRouting: boolean
      memoryQueue: MemoryJob[]
    }
  }
}
