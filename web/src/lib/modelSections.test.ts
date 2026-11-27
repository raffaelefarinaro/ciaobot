import { describe, expect, it } from 'vitest'
import {
  modelName,
  providerForModelSection,
  providerModelLabel,
  sectionKeyForProvider,
  sectionsFromModelsResponse,
} from './modelSections'
import type { ModelsResponse, ProviderDescriptor } from './types'

// The server's registry rows, as /api/models serves them.
function descriptor(id: string, label: string, shortLabel: string): ProviderDescriptor {
  return { id, label, short_label: shortLabel, capabilities: {} as ProviderDescriptor['capabilities'] }
}

const CLAUDE = descriptor('claude', 'Anthropic (via Claude Code)', 'Claude')
const OPENCODE = descriptor('opencode', 'OpenCode', 'OpenCode')
const CODEX = descriptor('codex', 'OpenAI (via Codex)', 'Codex')

describe('modelSections', () => {
  it('renders each provider from its own catalog', () => {
    const response: ModelsResponse = {
      models: ['opus', 'sonnet', 'haiku'],
      default: 'opus',
      providers: [CLAUDE, OPENCODE],
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
      'Claude',
      'OpenCode',
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

  it('builds a codex section from provider_models', () => {
    const sections = sectionsFromModelsResponse({
      models: ['opus'],
      default: 'opus',
      providers: [CLAUDE, OPENCODE, CODEX],
      provider_models: {
        claude: ['opus'],
        opencode: [],
        codex: ['gpt-5.5', 'gpt-5.4-mini'],
      },
      provider_defaults: { codex: 'gpt-5.5' },
    } as ModelsResponse)

    const codex = sections.find((section) => section.key === 'codex')
    expect(codex?.label).toBe('Codex')
    expect(codex?.models).toEqual(['gpt-5.5', 'gpt-5.4-mini'])
    expect(codex?.modelLabels).toEqual({ 'gpt-5.5': 'GPT-5.5', 'gpt-5.4-mini': 'GPT-5.4 Mini' })
    // The provider default is highlighted by pickers, never badged "Default".
    expect(codex?.modelBadges).toBeUndefined()
    // An empty catalog is not a section, and the registry order is kept.
    expect(sections.map((section) => section.key)).toEqual(['anthropic', 'codex'])
  })

  it('takes section order from the registry, not from the catalog object', () => {
    const sections = sectionsFromModelsResponse({
      models: ['opus'],
      default: 'opus',
      providers: [CLAUDE, CODEX],
      provider_models: { codex: ['gpt-5.5'], claude: ['opus'] },
      provider_defaults: {},
    } as ModelsResponse)

    expect(sections.map((section) => section.key)).toEqual(['anthropic', 'codex'])
  })

  it('names every model for the picker and leaves the raw ids alone', () => {
    const response: ModelsResponse = {
      models: ['opus', 'sonnet', 'haiku'],
      default: 'opus',
      providers: [CLAUDE, OPENCODE],
      provider_models: {
        claude: ['opus', 'sonnet', 'haiku'],
        opencode: ['openai/gpt-5.6-luna'],
      },
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
      providers: [CLAUDE],
      provider_models: { claude: ['opus'] },
      provider_defaults: { claude: 'opus' },
    } as ModelsResponse)

    const anthropic = sections.find((section) => section.key === 'anthropic')
    expect(anthropic?.modelBadges).toBeUndefined()
    expect(anthropic?.modelLabels).toEqual({ opus: 'Opus' })
  })
})

describe('providerForModelSection', () => {
  it('maps the Anthropic UI section and preserves provider sections', () => {
    expect(providerForModelSection('anthropic')).toBe('claude')
    expect(providerForModelSection('opencode')).toBe('opencode')
    expect(providerForModelSection('codex')).toBe('codex')
  })

  it('is the inverse of sectionKeyForProvider for every provider id', () => {
    for (const id of ['claude', 'opencode', 'codex']) {
      expect(providerForModelSection(sectionKeyForProvider(id))).toBe(id)
    }
  })
})

describe('opencode section', () => {
  const base = {
    models: [],
    default: 'opus',
    providers: [OPENCODE],
    provider_models: {},
    provider_defaults: {},
  } as unknown as ModelsResponse

  it('renders opencode models as their own section', () => {
    const sections = sectionsFromModelsResponse({
      ...base,
      provider_models: { opencode: ['anthropic/claude-sonnet-4-6', 'opencode/big-pickle'] },
    })
    const opencode = sections.find((s) => s.key === 'opencode')
    expect(opencode?.label).toBe('OpenCode')
    expect(opencode?.models).toContain('opencode/big-pickle')
  })

  it('omits the section when nothing is authenticated', () => {
    const sections = sectionsFromModelsResponse({ ...base, provider_models: { opencode: [] } })
    expect(sections.find((s) => s.key === 'opencode')).toBeUndefined()
  })

  it('owns the `provider/model` id shape outright', () => {
    const sections = sectionsFromModelsResponse({
      ...base,
      provider_models: { opencode: ['anthropic/claude-sonnet-4-6', 'google/gemini-3-pro'] },
    })
    const opencode = sections.find((s) => s.key === 'opencode')
    expect(opencode?.models).toEqual([
      'anthropic/claude-sonnet-4-6',
      'google/gemini-3-pro',
    ])
  })

  it("never badges a provider's default model with the word Default", () => {
    const sections = sectionsFromModelsResponse({
      ...base,
      providers: [CLAUDE, OPENCODE],
      models: ['a-model', 'b-model'],
      provider_models: { claude: ['a-model', 'b-model'], opencode: ['google/gemini-3-pro'] },
      provider_defaults: { claude: 'b-model', opencode: 'google/gemini-3-pro' },
    })
    expect(sections.find((s) => s.key === 'anthropic')?.modelBadges).toBeUndefined()
    // Only the namespace the id states is a badge.
    expect(sections.find((s) => s.key === 'opencode')?.modelBadges).toEqual({
      'google/gemini-3-pro': ['google'],
    })
  })

  it("names the agent's default entry by the model it runs, never 'Default'", () => {
    const response = {
      ...base,
      providers: [CLAUDE],
      models: ['default', 'opus'],
      provider_models: { claude: ['default', 'opus'] },
      provider_defaults: { claude: 'default' },
      provider_catalog_evidence: {
        claude: { status: 'observed', source: null, reason: null, models: [
          { id: 'default', label: 'Default (recommended)', description: 'Opus 4.5' },
          { id: 'opus', label: 'Opus' },
        ] },
      },
    } as ModelsResponse
    const claude = sectionsFromModelsResponse(response).find((s) => s.key === 'anthropic')
    expect(claude?.modelLabels).toEqual({ default: 'Opus 4.5', opus: 'Opus' })
    expect(modelName(response, 'claude', 'default')).toBe('Opus 4.5')
    expect(modelName(response, 'claude', '')).toBe('Opus 4.5')
    expect(providerModelLabel(response, 'claude', 'default')).toBe('Claude · Opus 4.5')
  })

  it('shows the provider alone while the default entry has no concrete name', () => {
    const response = {
      ...base,
      providers: [CLAUDE],
      models: ['default'],
      provider_models: { claude: ['default'] },
      provider_defaults: { claude: 'default' },
    } as ModelsResponse
    expect(modelName(response, 'claude', 'default')).toBe('')
    expect(providerModelLabel(response, 'claude', 'default')).toBe('Claude')
    expect(sectionsFromModelsResponse(response)[0].modelLabels).toEqual({ default: 'Claude' })
  })
})
