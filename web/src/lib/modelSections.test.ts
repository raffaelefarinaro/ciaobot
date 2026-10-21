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
    // No tier badges are produced now that tier routing is gone: the only
    // badge a model carries is the provider namespace its own id states.
    expect(sections.find((section) => section.key === 'opencode')?.modelBadges).toEqual({
      'anthropic/claude-sonnet-4-6': ['anthropic'],
    })
  })

  it('names every model for the picker and leaves the raw ids alone', () => {
    const response: ModelsResponse = {
      models: ['opus', 'sonnet', 'haiku'],
      default: 'opus',
      provider_models: { claude: ['opus', 'sonnet', 'haiku'] },
      provider_defaults: {},
      opencode_models: ['openai/gpt-5.6-luna'],
      backends: { anthropic: true, opencode: true },
      thinking_levels: {},
    }

    const sections = sectionsFromModelsResponse(response)
    const anthropic = sections.find((section) => section.key === 'anthropic')
    const opencode = sections.find((section) => section.key === 'opencode')

    expect(anthropic?.modelLabels).toEqual({
      opus: 'Opus',
      sonnet: 'Sonnet',
      haiku: 'Haiku',
    })
    expect(opencode?.modelLabels).toEqual({ 'openai/gpt-5.6-luna': 'GPT-5.6 Luna' })
    // The raw ids the engine owns are untouched by the readable name.
    expect(anthropic?.models).toEqual(['opus', 'sonnet', 'haiku'])
    expect(opencode?.models).toEqual(['openai/gpt-5.6-luna'])
  })

  it('gives a tier alias no provider badge', () => {
    const sections = sectionsFromModelsResponse({
      models: ['opus'],
      default: 'opus',
      provider_models: { claude: ['opus'] },
      provider_defaults: { claude: 'opus' },
    })

    const anthropic = sections.find((section) => section.key === 'anthropic')
    expect(anthropic?.modelBadges).toEqual({ opus: ['Default'] })
    expect(anthropic?.modelLabels).toEqual({ opus: 'Opus' })
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

  it("tags each provider's default model", () => {
    const sections = sectionsFromModelsResponse({
      ...base,
      models: ['a-model', 'b-model'],
      opencode_models: ['google/gemini-3-pro'],
      provider_defaults: { claude: 'b-model', opencode: 'google/gemini-3-pro' },
    })
    expect(sections.find((s) => s.key === 'anthropic')?.modelBadges).toEqual({ 'b-model': ['Default'] })
    // The default badge stays, and the namespace the id states rides beside it.
    expect(sections.find((s) => s.key === 'opencode')?.modelBadges).toEqual({
      'google/gemini-3-pro': ['Default', 'google'],
    })
  })
})
