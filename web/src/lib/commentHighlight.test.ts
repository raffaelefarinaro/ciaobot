// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'

import {
  cleanCommentSelection,
  commentTextOccurrenceIndex,
  findCommentTextMatch,
  highlightCommentText,
} from './commentHighlight'

describe('comment text matching', () => {
  it('removes invisible format characters and maps the match to rendered offsets', () => {
    expect(cleanCommentSelection('selected\u200b text')).toBe('selected text')
    expect(cleanCommentSelection('👨‍👩‍👧‍👦')).toBe('👨‍👩‍👧‍👦')
    expect(findCommentTextMatch(
      'The selected text is visible here.',
      'selected\u200b text is visible',
    )).toEqual({ start: 4, end: 28 })
  })

  it('matches a selection newline when rendered text omits the block separator', () => {
    expect(findCommentTextMatch(
      'First paragraphSecond paragraph',
      'paragraph\nSecond paragraph',
    )).toEqual({ start: 6, end: 31 })
  })

  it('keeps duplicate quote occurrences anchored after normalization', () => {
    const source = 'repeat once. repeat twice.'
    expect(commentTextOccurrenceIndex(source, 'repeat\u200b', 13)).toBe(1)
    expect(findCommentTextMatch(source, 'repeat\u200b', 1)).toEqual({ start: 13, end: 19 })
  })

  it('accepts Unicode compatibility forms', () => {
    expect(findCommentTextMatch('Use Foo here.', 'Use Ｆｏｏ here.')).toEqual({ start: 0, end: 13 })
  })

  it('wraps a normalized match in the rendered DOM', () => {
    const root = document.createElement('div')
    root.innerHTML = '<p>The selected text</p><p>is visible</p>'

    expect(highlightCommentText(root, 'selected\u200b text\nis visible', 'comment-1')).toBe(true)
    expect(root.querySelector('.comment-highlight')?.textContent).toBe('selected text')
    expect(root.querySelectorAll('.comment-highlight')[1]?.textContent).toBe('is visible')
  })
})
