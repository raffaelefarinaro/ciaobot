import { describe, expect, it } from 'vitest'
import { sharedPrompt } from './sharedContent'

describe('sharedPrompt', () => {
  it('keeps shared text and links as inert draft text', () => {
    expect(sharedPrompt({ id: 'x', createdAt: Date.now(), title: 'Article', text: 'Read later', url: 'https://example.org', files: [] }))
      .toBe('Article\n\nRead later\n\nhttps://example.org')
  })
})
