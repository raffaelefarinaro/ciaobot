import { describe, expect, it } from 'vitest'
import { effectiveWorkspaceDefaultModel, normalizeOverrides, setOverride } from './workspaceOverrides'
import type { ModelsResponse } from './types'

const models = (over: Partial<ModelsResponse> = {}): ModelsResponse => ({
  models: [],
  default: 'fallback-model',
  provider_models: {},
  provider_defaults: { claude: 'engine-claude' },
  ...over,
})

describe('normalizeOverrides', () => {
  it('drops empties, unknown keys and non-objects', () => {
    expect(normalizeOverrides(null)).toEqual({})
    expect(normalizeOverrides('x')).toEqual({})
    expect(normalizeOverrides({
      claude: { model: ' sonnet ', thinking: '', bogus: 'x', permission_mode: 7 },
      opencode: { model: '  ' },
      junk: 'nope',
    })).toEqual({ claude: { model: 'sonnet' } })
  })

  it('is order-stable so JSON.stringify can be a dirty check', () => {
    const a = normalizeOverrides({
      opencode: { insights_model: 'b', model: 'a' },
      claude: { permission_mode: 'auto', thinking: 'high' },
    })
    const b = normalizeOverrides({
      claude: { thinking: 'high', permission_mode: 'auto' },
      opencode: { model: 'a', insights_model: 'b' },
    })
    expect(JSON.stringify(a)).toBe(JSON.stringify(b))
    expect(Object.keys(a)).toEqual(['claude', 'opencode'])
    expect(Object.keys(a.claude)).toEqual(['thinking', 'permission_mode'])
  })
})

describe('setOverride', () => {
  it('sets a value without mutating the input', () => {
    const base = { claude: { model: 'sonnet' } }
    const next = setOverride(base, 'claude', 'thinking', 'high')
    expect(next).toEqual({ claude: { model: 'sonnet', thinking: 'high' } })
    expect(base).toEqual({ claude: { model: 'sonnet' } })
  })

  it('an empty value removes the field and then the emptied provider', () => {
    const one = setOverride({ claude: { model: 'sonnet', thinking: 'high' } }, 'claude', 'model', '')
    expect(one).toEqual({ claude: { thinking: 'high' } })
    expect(setOverride(one, 'claude', 'thinking', '')).toEqual({})
  })
})

describe('effectiveWorkspaceDefaultModel', () => {
  it('prefers the workspace override, then the engine default, then the catalog default', () => {
    const ws = { default_provider: 'claude', provider_overrides: { claude: { model: 'ws-model' } } }
    expect(effectiveWorkspaceDefaultModel(ws, models())).toBe('ws-model')
    expect(effectiveWorkspaceDefaultModel({ default_provider: 'claude' }, models())).toBe('engine-claude')
    expect(effectiveWorkspaceDefaultModel({ default_provider: 'opencode' }, models())).toBe('fallback-model')
    expect(effectiveWorkspaceDefaultModel(undefined, null)).toBe('')
  })

  it('keys the override by the workspace provider', () => {
    const ws = { default_provider: 'opencode', provider_overrides: { claude: { model: 'ws-model' } } }
    expect(effectiveWorkspaceDefaultModel(ws, models())).toBe('fallback-model')
  })
})
