import { describe, expect, test } from 'vitest'
import type { ChatMessage } from './types'
import { buildForkSnapshot } from './chatFork'


function history(): ChatMessage[] {
  return [
    { role: 'user', content: 'First question', timestamp: '', turn_index: 0 },
    { role: 'system', content: 'Activity', timestamp: '', tool_name: '_activity' },
    { role: 'assistant', content: 'First answer', timestamp: '' },
    { role: 'user', content: 'Second question', timestamp: '', turn_index: 1 },
    { role: 'assistant', content: 'Second answer', timestamp: '' },
  ]
}


describe('buildForkSnapshot', () => {
  test('copies history only through the selected assistant answer', () => {
    const messages = history()

    const result = buildForkSnapshot(messages, messages[2])

    expect(result).toEqual({
      messages: messages.slice(0, 3),
      turnIndex: 0,
    })
  })

  test('numbers a later selected answer by its preceding user turns', () => {
    const messages = history()

    const result = buildForkSnapshot(messages, messages[4])

    expect(result?.turnIndex).toBe(1)
    expect(result?.messages).toEqual(messages)
  })

  test('rejects user rows and error bubbles', () => {
    const messages = history()
    const error: ChatMessage = {
      role: 'assistant',
      content: 'Failed',
      timestamp: '',
      is_error: true,
    }

    expect(buildForkSnapshot(messages, messages[0])).toBeNull()
    expect(buildForkSnapshot([...messages, error], error)).toBeNull()
  })
})
