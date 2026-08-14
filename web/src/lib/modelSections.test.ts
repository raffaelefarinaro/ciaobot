import { describe, expect, it } from 'vitest'
import { sectionsFromModelsResponse, sortModelsByTier } from './modelSections'
import type { ModelsResponse } from './types'

describe('sortModelsByTier', () => {
  it('orders tier-tagged models Haiku, Sonnet, Opus, Fable, keeping untagged below', () => {
    const models = ['gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.5', 'gpt-5.6-sol-ultra']
    const modelBadges = {
      'gpt-5.6-sol': ['Opus'],
      'gpt-5.6-terra': ['Sonnet'],
      'gpt-5.6-luna': ['Haiku'],
      'gpt-5.6-sol-ultra': ['Fable'],
    }
    expect(sortModelsByTier(models, modelBadges)).toEqual([
      'gpt-5.6-luna',
      'gpt-5.6-terra',
      'gpt-5.6-sol',
      'gpt-5.6-sol-ultra',
      'gpt-5.5',
    ])
  })

  it('preserves original relative order among untagged models', () => {
    const models = ['a', 'b', 'c']
    expect(sortModelsByTier(models, {})).toEqual(['a', 'b', 'c'])
  })

  it('ranks a model by its highest tier when it carries several badges', () => {
    const models = ['multi', 'plain']
    const modelBadges = { multi: ['local', 'Opus'], plain: [] }
    expect(sortModelsByTier(models, modelBadges)).toEqual(['multi', 'plain'])
  })
})

describe('modelSections', () => {
  it('sorts Codex models by tier so tagged models lead, untagged trail', () => {
    const response: ModelsResponse = {
      models: [],
      default: 'opus',
      provider_models: {},
      provider_defaults: {},
      codex_models: ['gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.5'],
      alias_tiers: {
        codex: {
          opus: 'gpt-5.6-sol',
          sonnet: 'gpt-5.6-terra',
          haiku: 'gpt-5.6-luna',
          fable: 'gpt-5.6-sol',
        },
      },
      backends: { anthropic: true },
      thinking_levels: {},
    }

    const codex = sectionsFromModelsResponse(response).find((section) => section.key === 'codex')
    expect(codex?.models).toEqual(['gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-sol-ultra', 'gpt-5.5'])
  })


  it('keeps Anthropic to fixed aliases even when /api/models has a large model list', () => {
    const response: ModelsResponse = {
      models: [
        'opus',
        'sonnet',
        'haiku',
        'claude-3-7-sonnet-20250219',
        'claude-opus-4-20250514',
      ],
      default: 'opus',
      provider_models: {
        claude: ['opus', 'sonnet', 'haiku', 'claude-3-7-sonnet-20250219'],
        codex: ['gpt-test'],
        opencode: ['anthropic/claude-sonnet-4-6'],
      },
      provider_defaults: {},
      codex_models: ['gpt-test'],
      opencode_models: ['anthropic/claude-sonnet-4-6'],
      alias_tiers: {
        codex: { haiku: 'gpt-test', sonnet: 'gpt-test', opus: 'gpt-test', fable: 'gpt-test' },
        opencode: { sonnet: 'anthropic/claude-sonnet-4-6' },
      },
      backends: { anthropic: true, codex: true, opencode: true },
      thinking_levels: {},
    }

    const sections = sectionsFromModelsResponse(response)

    expect(sections.map((section) => section.label)).toEqual([
      'Anthropic',
      'OpenAI Codex',
      'opencode',
    ])
    // The Anthropic section is the four tier aliases, never the concrete
    // `claude-*` ids that also appear in `models`.
    expect(sections.find((section) => section.key === 'anthropic')?.models).toEqual([
      'haiku',
      'sonnet',
      'opus',
      'fable',
    ])
    expect(sections.find((section) => section.key === 'codex')?.models).toEqual(['gpt-test', 'fable'])
    expect(sections.find((section) => section.key === 'codex')?.modelBadges).toEqual({
      'gpt-test': ['Haiku', 'Sonnet', 'Opus'],
      fable: ['Fable'],
    })
    expect(sections.find((section) => section.key === 'codex')?.modelLabels).toEqual({
      fable: 'gpt-test-ultra',
    })
    expect(sections.find((section) => section.key === 'opencode')?.modelBadges).toEqual({
      'anthropic/claude-sonnet-4-6': ['Sonnet'],
    })
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
    // OpenRouter used to claim this shape too, and a heuristic had to guess
    // between them. opencode is now the only provider that uses it.
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
