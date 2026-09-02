import { describe, expect, it } from 'vitest'
import {
  formatArtifactCommentLocation,
  isArtifactCommentEvent,
} from './artifactBridge'

describe('isArtifactCommentEvent', () => {
  it('accepts well-formed bridge messages', () => {
    expect(
      isArtifactCommentEvent({
        frame: 'ciao-artifact',
        type: 'ciao:artifact-comment',
        action: 'compose',
        selector: 'div:nth-of-type(1) > p:nth-of-type(2)',
        quote: 'hello',
        startOffset: 0,
        endOffset: 5,
        x: 10,
        y: 20,
      }),
    ).toBe(true)
    expect(
      isArtifactCommentEvent({ frame: 'ciao-artifact', type: 'ciao:artifact-comment', action: 'ready' }),
    ).toBe(true)
    expect(
      isArtifactCommentEvent({ frame: 'ciao-artifact', type: 'ciao:artifact-comment', action: 'open', id: 'c1', x: 0, y: 0 }),
    ).toBe(true)
  })

  it('rejects other frames, other types, and junk', () => {
    expect(isArtifactCommentEvent({ frame: 'other', type: 'ciao:artifact-comment', action: 'ready' })).toBe(false)
    expect(isArtifactCommentEvent({ frame: 'ciao-artifact', type: 'other', action: 'ready' })).toBe(false)
    expect(isArtifactCommentEvent(null)).toBe(false)
    expect(isArtifactCommentEvent('compose')).toBe(false)
    expect(isArtifactCommentEvent(undefined)).toBe(false)
  })
})

describe('formatArtifactCommentLocation', () => {
  it('prefers the element tag', () => {
    expect(formatArtifactCommentLocation({ elementTag: 'h2', selector: 'div > h2:nth-of-type(1)' })).toBe('h2')
  })

  it('falls back to the last selector step, stripped of nth-of-type', () => {
    expect(
      formatArtifactCommentLocation({ selector: 'div:nth-of-type(1) > section:nth-of-type(2)' }),
    ).toBe('section'),
    expect(formatArtifactCommentLocation({ selector: '' })).toBe('')
    expect(formatArtifactCommentLocation({})).toBe('')
  })
})