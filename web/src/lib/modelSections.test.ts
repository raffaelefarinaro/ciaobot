import { describe, expect, it } from 'vitest'
import { sectionsFromModelOptions, sectionsFromModelsResponse, sortModelsByTier } from './modelSections'
import type { ModelsResponse, RoutineSettings } from './types'

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
      ollama_models: [],
      ollama_local_models: [],
      openrouter_models: [],
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
    expect(codex?.models).toEqual(['gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.6-sol', 'fable', 'gpt-5.5'])
  })


  it('keeps Anthropic to fixed aliases even when /api/models has a large model list', () => {
    const response: ModelsResponse = {
      models: [
        'opus',
        'sonnet',
        'haiku',
        'claude-3-7-sonnet-20250219',
        'claude-opus-4-20250514',
        'kimi-k2.7-code:cloud',
      ],
      default: 'opus',
      provider_models: {
        claude_work: ['opus', 'sonnet', 'haiku', 'claude-3-7-sonnet-20250219'],
        claude_personal: ['kimi-k2.7-code:cloud'],
        openrouter: ['anthropic/claude-sonnet-4.5', 'anthropic/claude-fable-latest'],
        codex: ['gpt-test'],
      },
      provider_defaults: {},
      ollama_models: ['kimi-k2.7-code:cloud', 'glm-5.2:cloud'],
      ollama_local_models: [],
      openrouter_models: ['anthropic/claude-sonnet-4.5', 'anthropic/claude-fable-latest'],
      codex_models: ['gpt-test'],
      alias_tiers: {
        ollama: { sonnet: 'kimi-k2.7-code:cloud', fable: 'glm-5.2:cloud' },
        openrouter: { sonnet: 'anthropic/claude-sonnet-4.5', fable: 'anthropic/claude-fable-latest' },
        codex: { haiku: 'gpt-test', sonnet: 'gpt-test', opus: 'gpt-test', fable: 'gpt-test' },
      },
      backends: { anthropic: true, ollama: true, openrouter: true },
      thinking_levels: {},
    }

    const sections = sectionsFromModelsResponse(response)

    expect(sections.map((section) => section.label)).toEqual(['Anthropic', 'OpenAI Codex', 'Ollama', 'OpenRouter'])
    expect(sections.find((section) => section.key === 'anthropic')?.models).toEqual([
      'haiku',
      'sonnet',
      'opus',
      'fable',
    ])
    expect(sections.find((section) => section.key === 'ollama')?.modelBadges).toEqual({
      'kimi-k2.7-code:cloud': ['Sonnet'],
      'glm-5.2:cloud': ['Fable'],
    })
    expect(sections.find((section) => section.key === 'codex')?.models).toEqual(['gpt-test', 'fable'])
    expect(sections.find((section) => section.key === 'codex')?.modelBadges).toEqual({
      'gpt-test': ['Haiku', 'Sonnet', 'Opus'],
      fable: ['Fable'],
    })
    expect(sections.find((section) => section.key === 'codex')?.modelLabels).toEqual({
      fable: 'gpt-test-ultra',
    })
    expect(sections.find((section) => section.key === 'openrouter')?.models).toEqual([
      'anthropic/claude-sonnet-4.5',
      'anthropic/claude-fable-latest',
    ])
    expect(sections.find((section) => section.key === 'openrouter')?.modelBadges).toEqual({
      'anthropic/claude-sonnet-4.5': ['Sonnet'],
      'anthropic/claude-fable-latest': ['Fable'],
    })
  })

  it('keeps routine Anthropic options fixed and dynamic provider lists separate', () => {
    const options: RoutineSettings['model_options'] = {
      anthropic: ['anthropic/claude-sonnet-4.5', 'anthropic/claude-haiku-4.5'],
      ollama_cloud: ['kimi-k2.7-code:cloud'],
      ollama_local: ['llama3.1:latest'],
      openrouter: ['openai/gpt-5.1'],
    }

    const sections = sectionsFromModelOptions(options, { ollama: true, openrouter: true }, {
      ollama: { sonnet: 'kimi-k2.7-code:cloud' },
      openrouter: { opus: 'openai/gpt-5.1' },
    })

    expect(sections.map((section) => section.label)).toEqual(['Anthropic', 'Ollama', 'OpenRouter'])
    expect(sections.find((section) => section.key === 'anthropic')?.models).toEqual([
      'haiku',
      'sonnet',
      'opus',
      'fable',
    ])
    expect(sections.find((section) => section.key === 'ollama')?.modelBadges).toEqual({
      'llama3.1:latest': ['local'],
      'kimi-k2.7-code:cloud': ['Sonnet'],
    })
    expect(sections.find((section) => section.key === 'openrouter')?.modelBadges).toEqual({
      'openai/gpt-5.1': ['Opus'],
    })
  })
})
