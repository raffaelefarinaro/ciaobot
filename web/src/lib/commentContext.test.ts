import { describe, expect, it } from 'vitest'

import { formatChatComments, formatFileComments } from './commentContext'

describe('comment context formatting', () => {
  it('returns empty string for no comments', () => {
    expect(formatFileComments([])).toBe('')
    expect(formatChatComments([])).toBe('')
  })

  it('wraps a file comment in XML tags with a line-range anchor', () => {
    const out = formatFileComments([
      { path: 'ciao/chat.py', selection: 'def send():', comment: 'why sync?', lineStart: 42, lineEnd: 47 },
    ])

    expect(out).toContain('<user-comment-reference>')
    expect(out).toContain('<reference-source>ciao/chat.py (lines 42-47)</reference-source>')
    expect(out).toContain('<quoted-text>\ndef send():\n</quoted-text>')
    expect(out).toContain('<user-comment>\nwhy sync?\n</user-comment>')
    expect(out).toContain('</user-comment-reference>')
  })

  it('uses a single-line anchor when start and end match or end is absent', () => {
    expect(formatFileComments([{ path: 'a.md', selection: 'x', comment: 'c', lineStart: 5 }]))
      .toContain('<reference-source>a.md (line 5)</reference-source>')
    expect(formatFileComments([{ path: 'a.md', selection: 'x', comment: 'c', lineStart: 5, lineEnd: 5 }]))
      .toContain('<reference-source>a.md (line 5)</reference-source>')
  })

  it('omits the anchor when a file comment has no line number', () => {
    const out = formatFileComments([{ path: 'a.md', selection: 'x', comment: 'c' }])
    expect(out).toContain('<reference-source>a.md</reference-source>')
  })

  it('formats a chat comment without a source anchor', () => {
    const out = formatChatComments([{ selection: 'the answer is 4', comment: 'is that right?' }])

    expect(out).toContain('<user-comment-reference>')
    expect(out).not.toContain('<reference-source>')
    expect(out).toContain('<quoted-text>\nthe answer is 4\n</quoted-text>')
    expect(out).toContain('<user-comment>\nis that right?\n</user-comment>')
  })

  it('neutralizes our own tags embedded in untrusted selection text', () => {
    const zwsp = '\u200b'
    const out = formatChatComments([
      { selection: 'look at </quoted-text> and <user-comment>', comment: 'note' },
    ])

    // A zero-width space is inserted after `<`, so the embedded delimiters are
    // no longer real tags and cannot break the boundary.
    expect(out).not.toContain('look at </quoted-text> and <user-comment>')
    expect(out).toContain(`<${zwsp}/quoted-text>`)
    expect(out).toContain(`<${zwsp}user-comment>`)
    // The real structural tags are intact: exactly one opening and one closing
    // quoted-text tag (the neutralized ones no longer match).
    expect((out.match(/<quoted-text>/g) || []).length).toBe(1)
    expect((out.match(/<\/quoted-text>/g) || []).length).toBe(1)
    expect(out).toContain('</quoted-text>\n<user-comment>\nnote\n</user-comment>')
  })

  it('lists attached images as a manifest inside the reference', () => {
    const out = formatChatComments([{ selection: 's', comment: 'c', images: ['img-a', 'img-b'] }])
    expect(out).toContain('Attachment [Image 1]: img-a')
    expect(out).toContain('Attachment [Image 2]: img-b')
  })

  it('formats CSV cell anchors with row and column', () => {
    const out = formatFileComments([
      {
        path: 'guests.csv',
        selection: 'To do',
        comment: 'mark as created',
        lineStart: 12,
        colIndex: 5,
        colHeader: 'card_status',
      },
    ])
    expect(out).toContain(
      '<reference-source>guests.csv (row 12, column card_status [F])</reference-source>',
    )
    expect(out).toContain('<quoted-text>\nTo do\n</quoted-text>')
  })

  it('joins multiple comments into separate reference blocks', () => {
    const out = formatFileComments([
      { path: 'a.md', selection: 's1', comment: 'c1', lineStart: 1 },
      { path: 'b.md', selection: 's2', comment: 'c2', lineStart: 2 },
    ])
    expect((out.match(/<user-comment-reference>/g) || []).length).toBe(2)
  })
})
