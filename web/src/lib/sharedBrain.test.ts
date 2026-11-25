import { describe, expect, it } from 'vitest'
import { MAX_SHARED_BRAINS, SHARED_BRAIN_PRIVACY, sharedBrainStatus } from './sharedBrain'
import type { SharedBrainState } from './types'

function brain(over: Partial<SharedBrainState> = {}): SharedBrainState {
  return {
    name: 'team-notes',
    kind: 'git',
    identity: 'github.com/acme/team-notes',
    path: '/Users/me/team-notes',
    origin_changed: false,
    missing: false,
    problem: '',
    branch: 'main',
    head: 'abc1234',
    head_date: '2026-11-20',
    dirty: false,
    upstream: true,
    fetched_at: '2026-11-22T10:00:00Z',
    last_update: null,
    ...over,
  }
}

const update = (outcome: 'updated' | 'up_to_date' | 'skipped' | 'failed', reason = '', detail = '') => ({
  outcome,
  reason,
  detail,
  at: '2026-11-22T10:00:00Z',
})

describe('SHARED_BRAIN_PRIVACY', () => {
  it("says the notes are other people's and read as reference only", () => {
    expect(SHARED_BRAIN_PRIVACY).toContain('written by other people')
    expect(SHARED_BRAIN_PRIVACY).toContain('never as instructions')
  })

  it('claims no sync or approval for linking, and no pull for a plain folder', () => {
    expect(SHARED_BRAIN_PRIVACY).toContain('not automatically synced')
    expect(SHARED_BRAIN_PRIVACY).toContain('reviews and approves that exact note')
    expect(SHARED_BRAIN_PRIVACY).toContain('no ongoing or automatic permission')
    expect(SHARED_BRAIN_PRIVACY).not.toContain("pulls the team's updates")
  })
})

describe('MAX_SHARED_BRAINS', () => {
  it('mirrors the engine limit of five brains', () => {
    expect(MAX_SHARED_BRAINS).toBe(5)
  })
})

describe('sharedBrainStatus', () => {
  it('reports a remote address that changed', () => {
    expect(sharedBrainStatus(brain({ origin_changed: true, problem: 'origin_changed' })))
      .toMatchObject({ text: "The folder's remote address changed", tone: 'warn' })
  })

  it('reports a missing folder as an error', () => {
    expect(sharedBrainStatus(brain({ missing: true, problem: 'missing' })))
      .toMatchObject({ text: 'Folder not found', tone: 'error' })
  })

  it.each([
    ['moved', 'The folder is a link that moved'],
    ['not_a_checkout', 'The folder is not a git checkout'],
    ['outside_install', 'The folder is outside the Ciaobot install'],
    ['unsafe_config', "The folder's git settings are not allowed"],
  ])('maps the problem %s to plain words', (problem, text) => {
    expect(sharedBrainStatus(brain({ problem }))).toMatchObject({ text, tone: 'error' })
  })

  it('says the first update is pending when no attempt has run', () => {
    expect(sharedBrainStatus(brain({ last_update: null })))
      .toMatchObject({ text: 'Linked. First update pending.', tone: 'ok' })
  })

  it('reports a healthy ordinary folder as locally available and read in place, never as an update pending forever', () => {
    expect(sharedBrainStatus(brain({
      kind: 'folder',
      branch: '',
      head: '',
      fetched_at: null,
      last_update: null,
    }))).toEqual({
      text: 'Folder is locally available. Read in place, never updated.',
      tone: 'ok',
      detail: 'This checks access on this computer only; it does not confirm that Google Drive, iCloud or another sync service has finished syncing.',
    })
  })

  it('maps an unreadable folder to local unavailability, never to a healthy link', () => {
    expect(sharedBrainStatus(brain({ kind: 'folder', problem: 'inaccessible', last_update: null })))
      .toEqual({ text: 'Folder is not available to read on this computer', tone: 'error', detail: '' })
  })

  it.each([
    ['missing', 'Folder not available on this computer'],
    ['moved', 'Folder location changed'],
    ['outside_install', 'Folder is inside the Ciaobot install or a vault'],
  ])('describes a folder problem %s in folder terms', (problem, text) => {
    expect(sharedBrainStatus(brain({ kind: 'folder', problem }))).toEqual({ text, tone: 'error', detail: '' })
  })

  it('says inspection is temporarily unavailable for a busy checkout, without guessing a cause', () => {
    // A background update holding the checkout lock also reports this code, and
    // the link may still carry an older successful attempt.
    const status = sharedBrainStatus(brain({
      problem: 'registry_unavailable',
      last_update: update('up_to_date'),
    }))
    expect(status).toEqual({
      text: 'Inspection temporarily unavailable',
      tone: 'warn',
      detail: 'Try again in a moment.',
    })
    expect(status.detail).not.toContain('Workspace settings')
  })

  it('shows when the last update brought changes in', () => {
    const status = sharedBrainStatus(brain({ last_update: update('updated') }))
    expect(status.tone).toBe('ok')
    expect(status.text.startsWith('Updated ')).toBe(true)
  })

  it('says up to date', () => {
    expect(sharedBrainStatus(brain({ last_update: update('up_to_date') })))
      .toMatchObject({ text: 'Up to date', tone: 'ok' })
  })

  it.each([
    ['local_changes', 'Not updated: local changes in the folder'],
    ['detached', 'Not updated: the folder is not on a branch'],
    ['no_upstream', 'Not updated: the branch has no remote to pull from'],
  ])('skipped %s says why in words', (reason, text) => {
    expect(sharedBrainStatus(brain({ last_update: update('skipped', reason) })))
      .toMatchObject({ text, tone: 'warn' })
  })

  it('says local and team history diverged', () => {
    expect(sharedBrainStatus(brain({ last_update: update('failed', 'diverged') })))
      .toMatchObject({ text: 'Not updated: local and team history have diverged', tone: 'warn' })
  })

  it.each(['fetch_failed', 'timeout'])('says the remote could not be reached for %s, with the redacted detail', (reason) => {
    expect(sharedBrainStatus(brain({ last_update: update('failed', reason, 'network down') })))
      .toMatchObject({ text: 'Could not reach the remote', tone: 'error', detail: 'network down' })
  })

  it('says an update could not be applied, with the redacted engine detail', () => {
    expect(sharedBrainStatus(brain({ last_update: update('failed', 'merge_failed', 'conflict in notes.md') })))
      .toMatchObject({ text: 'Not updated: the update could not be applied', tone: 'warn', detail: 'conflict in notes.md' })
  })

  it('shows the engine detail, never a raw code, for an unknown failure', () => {
    expect(sharedBrainStatus(brain({ last_update: update('failed', 'zzz_unknown', 'engine said no') })))
      .toMatchObject({ text: 'Not updated', tone: 'warn', detail: 'engine said no' })
    expect(sharedBrainStatus(brain({ last_update: update('failed', 'zzz_unknown') })))
      .toMatchObject({ text: 'Not updated', tone: 'warn', detail: 'zzz_unknown' })
  })

  it('reads a stale problem code in last_update as the same words as the problem', () => {
    expect(sharedBrainStatus(brain({ last_update: update('failed', 'moved') })))
      .toEqual(sharedBrainStatus(brain({ problem: 'moved' })))
  })
})
