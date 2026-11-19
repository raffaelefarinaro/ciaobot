import { describe, expect, it } from 'vitest'
import { BYPASS_NOTE, PERMISSION_DOCS, permissionDocsFor } from './permissionDocs'

describe('permissionDocs', () => {
  it('maps each provider id to its own https docs page', () => {
    for (const id of ['claude', 'opencode'] as const) {
      expect(PERMISSION_DOCS[id].href).toMatch(/^https:\/\//)
      expect(permissionDocsFor(id)).toBe(PERMISSION_DOCS[id])
    }
    expect(PERMISSION_DOCS.claude.href).toBe('https://code.claude.com/docs/en/permissions')
    expect(PERMISSION_DOCS.opencode.href).toBe('https://opencode.ai/docs/permissions/')
  })

  it('has no docs for an unknown provider', () => {
    expect(permissionDocsFor('other')).toBeUndefined()
  })

  it('states that bypass is full user access', () => {
    expect(BYPASS_NOTE).toContain('full user access')
  })
})
