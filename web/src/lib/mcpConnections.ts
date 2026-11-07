import type { McpConnection } from './types'

export interface McpConnectionGroup {
  state: McpConnection['state']
  chatIds: string[]
  error?: string
}

/**
 * Collapses a server's per-chat connections into one group per state, in
 * first-appearance order, so N open chats in the same state render once.
 */
export function groupMcpConnections(connections: McpConnection[]): McpConnectionGroup[] {
  const groups = new Map<McpConnection['state'], McpConnectionGroup>()
  for (const connection of connections) {
    let group = groups.get(connection.state)
    if (!group) {
      group = { state: connection.state, chatIds: [] }
      groups.set(connection.state, group)
    }
    if (!group.chatIds.includes(connection.chat_id)) group.chatIds.push(connection.chat_id)
    if (!group.error && connection.error) group.error = connection.error
  }
  return [...groups.values()]
}
