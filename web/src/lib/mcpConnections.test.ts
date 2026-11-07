import { describe, expect, it } from 'vitest'
import { groupMcpConnections } from './mcpConnections'
import type { McpConnection } from './types'

describe('groupMcpConnections', () => {
  it('groups by state in first-appearance order', () => {
    const connections: McpConnection[] = [
      { state: 'needs_auth', chat_id: 'chat-a' },
      { state: 'connected', chat_id: 'chat-b' },
      { state: 'needs_auth', chat_id: 'chat-c' },
      { state: 'connected', chat_id: 'chat-d' },
    ]
    expect(groupMcpConnections(connections)).toEqual([
      { state: 'needs_auth', chatIds: ['chat-a', 'chat-c'] },
      { state: 'connected', chatIds: ['chat-b', 'chat-d'] },
    ])
  })

  it('keeps the first non-empty error and drops duplicate chat ids', () => {
    const connections: McpConnection[] = [
      { state: 'needs_auth', chat_id: 'chat-a' },
      { state: 'needs_auth', chat_id: 'chat-b', error: 'MCP server needs authentication.' },
      { state: 'needs_auth', chat_id: 'chat-b', error: 'other' },
    ]
    expect(groupMcpConnections(connections)).toEqual([
      { state: 'needs_auth', chatIds: ['chat-a', 'chat-b'], error: 'MCP server needs authentication.' },
    ])
  })

  it('returns an empty list for no connections', () => {
    expect(groupMcpConnections([])).toEqual([])
  })
})
