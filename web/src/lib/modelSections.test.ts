import { describe, expect, it } from 'vitest'
import { sectionsFromModelOptions, sectionsFromModelsResponse } from './modelSections'
import type { ModelsResponse, RoutineSettings } from './types'

describe('modelSections', () => {
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
        openrouter: ['anthropic/claude-sonnet-4.5'],
        codex: ['gpt-test'],
      },
      provider_defaults: {},
      ollama_models: ['kimi-k2.7-code:cloud'],
      ollama_local_models: [],
      openrouter_models: ['anthropic/claude-sonnet-4.5'],
      codex_models: ['gpt-test'],
      alias_tiers: {
        ollama: { sonnet: 'kimi-k2.7-code:cloud' },
        openrouter: { sonnet: 'anthropic/claude-sonnet-4.5' },
        codex: { haiku: 'gpt-test', sonnet: 'gpt-test', opus: 'gpt-test' },
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
    expect(sections.find((section) => section.key === 'ollama')?.models).toEqual(['kimi-k2.7-code:cloud'])
    expect(sections.find((section) => section.key === 'codex')?.models).toEqual(['gpt-test'])
    expect(sections.find((section) => section.key === 'codex')?.modelBadges).toEqual({
      'gpt-test': ['Haiku', 'Sonnet', 'Opus'],
    })
    expect(sections.find((section) => section.key === 'openrouter')?.models).toEqual([
      'anthropic/claude-sonnet-4.5',
    ])
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
