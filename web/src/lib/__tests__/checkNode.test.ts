import { describe, expect, it } from 'vitest'
// @ts-expect-error — plain .mjs script, no type declarations by design.
import { isSupportedVersion } from '../../../scripts/check-node.mjs'

describe('check-node version gate', () => {
  it.each([
    '20.19.0',
    '20.19.1',
    '20.20.0',
    '22.13.0',
    '22.14.2',
    '24.0.0',
    '26.5.1',
  ])('accepts %s', version => {
    expect(isSupportedVersion(version)).toBe(true)
  })

  // The range these must reject is the whole point of the file: a bare
  // `major > 20` check waves all of them through, and 22.0-22.12 in particular
  // still lacks the unflagged require(esm) that jsdom 29 depends on.
  it.each([
    '18.20.0',
    '20.18.1',
    '20.0.0',
    '21.7.3',
    '22.0.0',
    '22.12.0',
    '23.11.0',
  ])('rejects %s', version => {
    expect(isSupportedVersion(version)).toBe(false)
  })

  it('rejects an unparseable version rather than passing it through', () => {
    expect(isSupportedVersion('not-a-version')).toBe(false)
  })
})
