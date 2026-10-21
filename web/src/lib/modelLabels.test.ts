import { describe, expect, it } from 'vitest'
import { modelDisplayName, modelProviderPrefix } from './modelLabels'

describe('modelDisplayName', () => {
  it('humanizes a tier alias without naming a vendor', () => {
    expect(modelDisplayName('sonnet')).toBe('Sonnet')
    expect(modelDisplayName('opus')).toBe('Opus')
    expect(modelDisplayName('haiku')).toBe('Haiku')
    expect(modelDisplayName('fable')).toBe('Fable')
  })

  it('drops the transport prefix before the first slash', () => {
    expect(modelDisplayName('openai/gpt-5.6-luna')).toBe('GPT-5.6 Luna')
    expect(modelDisplayName('anthropic/claude-sonnet-4-6')).toBe('Claude Sonnet-4-6')
  })

  it('names a nested id by its own segment, without repeating the vendor', () => {
    expect(modelDisplayName('openrouter/minimax/minimax-m3.1-flash:free')).toBe(
      'MiniMax M3.1 Flash :free',
    )
    expect(modelDisplayName('openrouter/anthropic/claude-sonnet-4.5')).toBe(
      'Claude Sonnet-4.5',
    )
    expect(modelDisplayName('openrouter/stealth/space-bunny-alpha')).toBe('Space Bunny Alpha')
  })

  it('keeps a version marker as a word of its own', () => {
    expect(modelDisplayName('ollama-cloud/deepseek-v4.1-flash:1017')).toBe(
      'DeepSeek V4.1 Flash :1017',
    )
  })

  it('retains the technical suffix of a tagged id', () => {
    expect(modelDisplayName('some-provider/some-model:free')).toBe('Some Model :free')
    expect(modelDisplayName('glm-5.2:cloud')).toBe('GLM-5.2 :cloud')
    expect(modelDisplayName('llama3.1:latest')).toBe('Llama3.1 :latest')
  })

  it('preserves version numbers and their punctuation', () => {
    expect(modelDisplayName('gpt-5.6')).toBe('GPT-5.6')
    expect(modelDisplayName('llama-3.3-70b-instruct')).toBe('Llama-3.3-70b Instruct')
    expect(modelDisplayName('o3-mini')).toBe('O3 Mini')
  })

  it('never states a vendor the id does not carry', () => {
    expect(modelDisplayName('sonnet')).not.toContain('Claude')
    expect(modelDisplayName('gpt-4o')).not.toContain('OpenAI')
    expect(modelDisplayName('openai/gpt-5.6-luna')).not.toContain('openai')
  })

  it('returns an empty name for an empty id', () => {
    expect(modelDisplayName('')).toBe('')
    expect(modelDisplayName('   ')).toBe('')
  })
})

describe('modelProviderPrefix', () => {
  it('reads the namespace the id states', () => {
    expect(modelProviderPrefix('openai/gpt-5.6-luna')).toBe('openai')
    expect(modelProviderPrefix('ollama-cloud/deepseek-v4.1-flash:1017')).toBe('ollama-cloud')
  })

  it('has no prefix for an id that carries none', () => {
    expect(modelProviderPrefix('sonnet')).toBe('')
    expect(modelProviderPrefix('')).toBe('')
  })
})
