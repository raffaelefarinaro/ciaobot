import { describe, expect, it } from 'vitest'
// @ts-expect-error — plain .mjs script, no type declarations by design.
import { isSupportedVersion } from '../../../scripts/check-node.mjs'

describe('check-node version gate', () => {
  it.each([
    '22.22.2',
    '22.22.3',
    '22.23.2',
    '24.15.0',
    '24.16.1',
    '26.0.0',
    '26.5.1',
  ])('accepts %s', version => {
    expect(isSupportedVersion(version)).toBe(true)
  })

  // The range these must reject is the whole point of the file: a bare
  // `major > 22` check waves all of them through, and 22.0-22.21 in particular
  // still lacks the unflagged require(esm) that jsdom 30 depends on. The whole
  // Node 20 line is here too — jsdom 30 dropped it, so the gate must as well,
  // or an EOL Node runs a suite that silently skips every jsdom file.
  it.each([
    '18.20.0',
    '20.0.0',
    '20.19.0',
    '20.20.5',
    '21.7.3',
    '22.0.0',
    '22.12.0',
    '22.22.1',
    '23.11.0',
    '24.0.0',
    '24.14.9',
    '25.3.1',
  ])('rejects %s', version => {
    expect(isSupportedVersion(version)).toBe(false)
  })

  it('rejects an unparseable version rather than passing it through', () => {
    expect(isSupportedVersion('not-a-version')).toBe(false)
  })
})
