import { describe, expect, test } from 'vitest'
import {
  archiveActionLabel,
  archiveConfirmMessage,
  archiveFailedToast,
  archiveMenuLabel,
  archiveStoppedToast,
} from './archiveCopy'

describe('archive confirm wording', () => {
  test('a chat with no subchats says nothing about them', () => {
    expect(archiveConfirmMessage(0)).toBe(
      'Archive this chat? You can reopen it from the archive.',
    )
  })

  test('idle subchats are counted but not described as working', () => {
    expect(archiveConfirmMessage(2)).toBe(
      'Archive this chat and 2 subchats? You can reopen them from the archive.',
    )
    expect(archiveConfirmMessage(1)).toContain('1 subchat?')
  })

  // The point of the warning: archiving does not wait for a running subchat, it
  // stops it. The user has to be able to cancel instead of finding out after.
  test('a working subchat is named, with what archiving does to it', () => {
    const message = archiveConfirmMessage(2, 1)
    expect(message).toBe(
      'Archive this chat and 2 subchats? 1 subchat is still working and will be '
      + 'stopped, so its unfinished work is lost. You can reopen them from the archive.',
    )
  })

  test('several working subchats read correctly', () => {
    expect(archiveConfirmMessage(3, 2)).toContain(
      '2 subchats are still working and will be stopped, so their unfinished work is lost.',
    )
  })

  test('the busy count never exceeds the total it is drawn from', () => {
    // busyCount === count is the common case (every subchat is working) and
    // must not read as if there were extra chats beyond the ones named.
    expect(archiveConfirmMessage(1, 1)).toBe(
      'Archive this chat and 1 subchat? 1 subchat is still working and will be '
      + 'stopped, so its unfinished work is lost. You can reopen them from the archive.',
    )
  })
})

describe('archive result wording', () => {
  test('the stopped toast says what was lost', () => {
    expect(archiveStoppedToast(1).title).toBe('Stopped 1 subchat mid-turn')
    expect(archiveStoppedToast(1).body).toContain('is not in the archive')
    expect(archiveStoppedToast(3).title).toBe('Stopped 3 subchats mid-turn')
    expect(archiveStoppedToast(3).body).toContain('those turns')
  })

  test('the failure toast says the subchats are still active', () => {
    expect(archiveFailedToast(1).body).toContain('still active')
    expect(archiveFailedToast(2).body).toContain('2 subchats could not be archived')
  })
})

describe('archive labels', () => {
  test('button and menu labels stay in step with the count', () => {
    expect(archiveActionLabel(0)).toBe('Archive chat')
    expect(archiveActionLabel(1)).toBe('Archive chat and 1 subchat')
    expect(archiveMenuLabel(0)).toBe('Archive')
    expect(archiveMenuLabel(2)).toBe('Archive (also 2 subchats)')
  })
})
