// Per-chat WebSocket reconnect policy. Pure predicates, no socket ownership:
// `stores/projects.ts` still opens, closes and books the sockets — this module
// only answers "should we?" and "how long do we wait?".

export function shouldReconnectActiveChatOnStreamingStarted(
  socket: Pick<WebSocket, 'readyState'> | undefined,
): boolean {
  // CONNECTING=0, OPEN=1. Reconnecting in either state replays the broker
  // buffer into a client that may already have consumed live deltas, which
  // duplicates streamed text chunk by chunk.
  return !socket || socket.readyState > 1
}

/** Backoff for unexpected per-chat WS drops. `attempt` is 1-based. */
export function chatWsReconnectDelayMs(attempt: number): number {
  if (attempt <= 1) return 50
  return Math.min(50 * 2 ** (attempt - 1), 2000)
}

/** Compatibility check for host proxies from before `host_unreachable` existed. */
export function isHostConnectionUnavailableMessage(message: string): boolean {
  return message.trim().toLowerCase().startsWith('host ws unreachable')
}
