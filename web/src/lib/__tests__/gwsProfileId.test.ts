import { describe, expect, it } from 'vitest'
import { deriveGwsProfileId, gwsStorageHint, slugifyGwsProfile } from '../gwsProfileId'

describe('gwsProfileId', () => {
  it('slugifies like the backend', () => {
    expect(slugifyGwsProfile('My Team!')).toBe('my-team')
    expect(slugifyGwsProfile('  --A_b--  ')).toBe('a_b')
    expect(slugifyGwsProfile('***')).toBe('')
  })

  it('reserved service names get a -google suffix', () => {
    expect(deriveGwsProfileId('drive', new Set())).toBe('drive-google')
    expect(deriveGwsProfileId('auth', new Set())).toBe('auth-google')
  })

  it('an id taken by another workspace gets a numeric suffix', () => {
    expect(deriveGwsProfileId('acme', new Set(['acme']))).toBe('acme-2')
    expect(deriveGwsProfileId('acme', new Set(['acme', 'acme-2']))).toBe('acme-3')
    expect(deriveGwsProfileId('', new Set())).toBe('workspace')
  })

  it('storage hint follows the legacy mapping', () => {
    expect(gwsStorageHint('personal')).toBe('secrets/gws-personal/')
    expect(gwsStorageHint('work')).toBe('secrets/gws/')
    expect(gwsStorageHint('acme')).toBe('secrets/gws-acme/')
  })
})
