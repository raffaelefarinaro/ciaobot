import { describe, expect, it } from 'vitest'
import {
  providerForModelSection,
  sectionsFromModelsResponse,
} from './modelSections'
import type { ModelsResponse } from './types'

describe('modelSections', () => {
  it('renders each provider from its own catalog', () => {
    const response: ModelsResponse = {
      models: ['opus', 'sonnet', 'haiku'],
      default: 'opus',
      provider_models: {
        claude: ['opus', 'sonnet', 'haiku'],
        opencode: ['anthropic/claude-sonnet-4-6'],
      },
      provider_defaults: {},
      opencode_models: ['anthropic/claude-sonnet-4-6'],
      backends: { anthropic: true, opencode: true },
      thinking_levels: {},
    }

    const sections = sectionsFromModelsResponse(response)

    expect(sections.map((section) => section.label)).toEqual([
      'Anthropic',
      'opencode',
    ])
    expect(sections.find((section) => section.key === 'anthropic')?.models).toEqual([
      'opus',
      'sonnet',
      'haiku',
    ])
    expect(sections.find((section) => section.key === 'opencode')?.models).toEqual([
      'anthropic/claude-sonnet-4-6',
    ])
    // No tier badges are produced now that tier routing is gone.
    expect(sections.find((section) => section.key === 'opencode')?.modelBadges).toBeUndefined()
  })
})

describe('providerForModelSection', () => {
  it('maps the Anthropic UI section and preserves provider sections', () => {
    expect(providerForModelSection('anthropic')).toBe('claude')
    expect(providerForModelSection('opencode')).toBe('opencode')
  })
})

describe('opencode section', () => {
  const base: ModelsResponse = {
    models: [],
    default: 'opus',
    provider_models: {},
    provider_defaults: {},
  } as unknown as ModelsResponse

  it('renders opencode models as their own section', () => {
    const sections = sectionsFromModelsResponse({
      ...base,
      opencode_models: ['anthropic/claude-sonnet-4-6', 'opencode/big-pickle'],
    })
    const opencode = sections.find((s) => s.key === 'opencode')
    expect(opencode?.label).toBe('opencode')
    expect(opencode?.models).toContain('opencode/big-pickle')
  })

  it('omits the section when nothing is authenticated', () => {
    const sections = sectionsFromModelsResponse({ ...base, opencode_models: [] })
    expect(sections.find((s) => s.key === 'opencode')).toBeUndefined()
  })

  it('owns the `provider/model` id shape outright', () => {
    const sections = sectionsFromModelsResponse({
      ...base,
      opencode_models: ['anthropic/claude-sonnet-4-6', 'google/gemini-3-pro'],
    })
    const opencode = sections.find((s) => s.key === 'opencode')
    expect(opencode?.models).toEqual([
      'anthropic/claude-sonnet-4-6',
      'google/gemini-3-pro',
    ])
  })
})
