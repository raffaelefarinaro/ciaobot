import { expect, it } from 'vitest'
import { describeInterruptedCards } from './restartBlockers'

it('is empty with no blockers', () => {
  expect(describeInterruptedCards([])).toBe('')
})

it('ignores blockers that are only working', () => {
  expect(describeInterruptedCards([{ chat_id: 'c1', reasons: ['streaming'] }])).toBe('')
})

it('names one chat with an open card', () => {
  expect(describeInterruptedCards([{ chat_id: 'c1', reasons: ['streaming', 'open_question'] }]))
    .toBe('Restarting closes the open question or approval card in 1 chat.')
})

it('counts chats with open questions or approvals', () => {
  expect(describeInterruptedCards([
    { chat_id: 'c1', reasons: ['open_question'] },
    { chat_id: 'c2', reasons: ['open_permission'] },
    { chat_id: 'c3', reasons: ['streaming'] },
  ])).toBe('Restarting closes the open question or approval cards in 2 chats.')
})
