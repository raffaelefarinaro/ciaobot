import { describe, expect, it } from 'vitest'
import {
  ARCHIVE_ACTION_LABEL,
  ARCHIVE_CONFIRM_MESSAGE,
  ARCHIVE_MENU_LABEL,
} from './archiveCopy'

describe('archive copy', () => {
  it('promises the chat can be reopened', () => {
    expect(ARCHIVE_CONFIRM_MESSAGE).toBe(
      'Archive this chat? You can reopen it from the archive.',
    )
  })

  it('spells the action out on a button and keeps the menu row short', () => {
    expect(ARCHIVE_ACTION_LABEL).toBe('Archive chat')
    expect(ARCHIVE_MENU_LABEL).toBe('Archive')
  })
})
