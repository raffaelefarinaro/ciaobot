import type { ChatMessage } from './types'


export type ForkSnapshot = {
  messages: ChatMessage[]
  turnIndex: number
}


export function buildForkSnapshot(
  messages: ChatMessage[],
  selected: ChatMessage,
): ForkSnapshot | null {
  if (selected.role !== 'assistant' || selected.is_error) return null
  const selectedIndex = messages.indexOf(selected)
  if (selectedIndex < 0) return null
  const snapshot = messages.slice(0, selectedIndex + 1)
  const userTurns = snapshot.filter(message => message.role === 'user').length
  if (userTurns < 1) return null
  return {
    messages: snapshot,
    turnIndex: userTurns - 1,
  }
}
