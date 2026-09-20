import { describe, expect, it } from 'vitest'
import {
  assetOriginClass,
  assetOriginLabel,
  commandOrigin,
  mcpServerOrigin,
  subagentOrigin,
} from './assetOrigin'

describe('assetOriginLabel', () => {
  it('names each origin, defaulting to built-in', () => {
    expect(assetOriginLabel('custom')).toBe('Custom')
    expect(assetOriginLabel('installed')).toBe('Installed')
    expect(assetOriginLabel('global')).toBe('Global')
    expect(assetOriginLabel('builtin')).toBe('Built-in')
  })
})

describe('assetOriginClass', () => {
  it('keeps the badge utility classes and only varies the tone', () => {
    expect(assetOriginClass('custom')).toBe('badge badge--success command-source')
    expect(assetOriginClass('builtin')).toBe('badge badge--builtin command-source')
    expect(assetOriginClass('installed')).toBe('badge badge--muted command-source')
    expect(assetOriginClass('global')).toBe('badge badge--muted command-source')
  })
})

describe('commandOrigin', () => {
  // Scope is definitive because stock assets are seeded into the same editable
  // location as custom ones, so `editable` alone cannot tell them apart.
  it('trusts the scope over the editable flag', () => {
    expect(commandOrigin({ scope: 'built-in', editable: true })).toBe('builtin')
    expect(commandOrigin({ scope: 'custom', editable: false })).toBe('custom')
    expect(commandOrigin({ scope: 'global' })).toBe('global')
    expect(commandOrigin({ scope: 'installed' })).toBe('installed')
  })

  it('falls back to the editable flag only when the scope is unset', () => {
    expect(commandOrigin({ editable: true })).toBe('custom')
    expect(commandOrigin({ editable: false })).toBe('installed')
    expect(commandOrigin({})).toBe('installed')
  })

  it('classifies subagents by the same rule', () => {
    expect(subagentOrigin({ scope: 'custom' })).toBe('custom')
    expect(subagentOrigin({ editable: true })).toBe('custom')
  })
})

describe('mcpServerOrigin', () => {
  it('treats every project .mcp.json server as operator-authored', () => {
    expect(mcpServerOrigin({ name: 'notion', source: 'project' })).toBe('custom')
    expect(mcpServerOrigin({})).toBe('custom')
  })
})
