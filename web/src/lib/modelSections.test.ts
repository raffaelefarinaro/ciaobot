import { describe, expect, it } from 'vitest'
import {
  defaultDescriptionName,
  defaultEquivalent,
  displayedModel,
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

const CLAUDE = descriptor('claude', 'Anthropic (via Claude Code)', 'Claude Code')
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
      'Claude Code',
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
    // No concrete row is Opus 4.5, so the default stays its own row, named as
    // the CLI's pick of that model.
    expect(claude?.modelLabels).toEqual({ default: "Claude Code's pick (Opus 4.5)", opus: 'Opus' })
    expect(modelName(response, 'claude', 'default')).toBe('Opus 4.5')
    expect(modelName(response, 'claude', '')).toBe('Opus 4.5')
    expect(providerModelLabel(response, 'claude', 'default')).toBe('Claude Code · Opus 4.5')
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
    expect(providerModelLabel(response, 'claude', 'default')).toBe('Claude Code')
    // The listed default row is the CLI's pick, never "Claude Code" again under
    // a "Claude Code" heading, and never the word "default".
    expect(sectionsFromModelsResponse(response)[0].modelLabels).toEqual({ default: "Claude Code's pick" })
  })
})

describe('the default entry and catalog labels', () => {
  const evidence = (models: Array<{ id: string; label: string; description?: string }>) => ({
    claude: { status: 'observed' as const, source: null, reason: null, models },
  })

  it('reads a short name, a raw id and a sentence from the adapter description', () => {
    expect(defaultDescriptionName('Opus 4.5')).toBe('Opus 4.5')
    expect(defaultDescriptionName('Opus')).toBe('Opus')
    expect(defaultDescriptionName('claude-opus-4-5-20251101')).toBe('Opus 4.5')
    expect(defaultDescriptionName('openai/gpt-5.6')).toBe('GPT-5.6')
    // The older id order, and a date that is never read as a version.
    expect(defaultDescriptionName('claude-3-5-sonnet-20241022')).toBe('Sonnet 3.5')
    expect(defaultDescriptionName('claude-3-opus-20240229')).toBe('Opus 3')
    expect(defaultDescriptionName('claude-sonnet-20241022')).toBe('Claude Sonnet-20241022')
    expect(defaultDescriptionName('Use the default model (currently Sonnet 4.5) · $3/$15 per Mtok')).toBe('Sonnet 4.5')
    expect(defaultDescriptionName('Use the default model · $3/$15 per Mtok')).toBe('')
    expect(defaultDescriptionName('The model this account is set up to use for most of its work today')).toBe('')
    expect(defaultDescriptionName('Default')).toBe('')
    expect(defaultDescriptionName('x'.repeat(200))).toBe('')
  })

  it("names concrete rows by the agent's label and lists the default once, as its concrete row", () => {
    const response = {
      models: [], default: 'default',
      providers: [descriptor('claude', 'Anthropic (via Claude Code)', 'Claude Code')],
      provider_models: { claude: ['default', 'opus', 'sonnet', 'haiku', 'sonnet[1m]'] },
      provider_defaults: { claude: 'default' },
      provider_catalog_evidence: evidence([
        { id: 'default', label: 'Default (recommended)', description: 'Opus 4.5' },
        { id: 'opus', label: 'Opus 4.5' },
        { id: 'sonnet', label: 'Sonnet 4.5' },
        { id: 'haiku', label: 'Haiku 4.5' },
        { id: 'sonnet[1m]', label: 'Sonnet 4.5 (1M context)' },
      ]),
    } as unknown as ModelsResponse
    const claude = sectionsFromModelsResponse(response)[0]
    expect(claude.models).toEqual(['opus', 'sonnet', 'haiku', 'sonnet[1m]'])
    expect(claude.modelLabels).toEqual({
      opus: 'Opus 4.5', sonnet: 'Sonnet 4.5', haiku: 'Haiku 4.5', 'sonnet[1m]': 'Sonnet 4.5 (1M context)',
    })
    expect(defaultEquivalent(response, 'claude')).toBe('opus')
    expect(displayedModel(response, 'claude', 'default')).toBe('opus')
    expect(displayedModel(response, 'claude', 'sonnet')).toBe('sonnet')
    expect(modelName(response, 'claude', 'default')).toBe('Opus 4.5')
  })

  const catalog = (description: string, rows: Array<[string, string]>) => ({
    models: [], default: 'default',
    providers: [descriptor('claude', 'Anthropic (via Claude Code)', 'Claude Code')],
    provider_models: { claude: ['default', ...rows.map(([id]) => id)] },
    provider_defaults: { claude: 'default' },
    provider_catalog_evidence: evidence([
      { id: 'default', label: 'Default (recommended)', description },
      ...rows.map(([id, label]) => ({ id, label })),
    ]),
  } as unknown as ModelsResponse)

  it("matches a family alone ('Opus') to the one row that starts with it", () => {
    const response = catalog('Opus', [['opus', 'Opus 4.5'], ['sonnet', 'Sonnet 4.5']])
    expect(defaultEquivalent(response, 'claude')).toBe('opus')
    expect(modelName(response, 'claude', 'default')).toBe('Opus 4.5')
    const claude = sectionsFromModelsResponse(response)[0]
    expect(claude.models).toEqual(['opus', 'sonnet'])
    expect(claude.modelAliases).toEqual({ default: 'opus' })
  })

  it("does not guess when two rows fit ('Sonnet' with two Sonnet rows)", () => {
    const response = catalog('Sonnet', [['sonnet', 'Sonnet 4.5'], ['claude-sonnet-4', 'Sonnet 4'], ['opus', 'Opus 4.5']])
    expect(defaultEquivalent(response, 'claude')).toBe('')
    expect(modelName(response, 'claude', 'default')).toBe('Sonnet')
    const claude = sectionsFromModelsResponse(response)[0]
    expect(claude.models).toEqual(['default', 'sonnet', 'claude-sonnet-4', 'opus'])
    expect(claude.modelLabels?.default).toBe("Claude Code's pick (Sonnet)")
    expect(claude.modelAliases).toBeUndefined()
  })

  it('matches a raw model id by family and version', () => {
    const response = catalog('claude-opus-4-5-20251101', [['opus', 'Opus 4.5'], ['claude-opus-4-1', 'Opus 4.1']])
    expect(defaultEquivalent(response, 'claude')).toBe('opus')
    expect(modelName(response, 'claude', 'default')).toBe('Opus 4.5')
  })

  it("matches the model a '(currently Sonnet 4.5)' sentence names", () => {
    const response = catalog('Use the default model (currently Sonnet 4.5) · $3/$15 per Mtok', [['opus', 'Opus 4.5'], ['sonnet', 'Sonnet 4.5']])
    expect(defaultEquivalent(response, 'claude')).toBe('sonnet')
    expect(sectionsFromModelsResponse(response)[0].models).toEqual(['opus', 'sonnet'])
  })

  it("names a rejected description's row as the CLI's pick", () => {
    for (const description of ['Default', 'gpt-5-default']) {
      const response = catalog(description, [['opus', 'Opus 4.5']])
      expect(sectionsFromModelsResponse(response)[0].modelLabels?.default).toBe("Claude Code's pick")
      expect(modelName(response, 'claude', 'default')).toBe('')
    }
  })

  it('keeps the default row when no concrete row has its name', () => {
    const response = {
      models: [], default: 'default',
      providers: [descriptor('claude', 'Anthropic (via Claude Code)', 'Claude Code')],
      provider_models: { claude: ['default', 'opus'] },
      provider_defaults: { claude: 'default' },
      provider_catalog_evidence: evidence([
        { id: 'default', label: 'Default (recommended)', description: 'Opus 4.5' },
        { id: 'opus', label: 'Opus' },
      ]),
    } as unknown as ModelsResponse
    expect(sectionsFromModelsResponse(response)[0].models).toEqual(['default', 'opus'])
    expect(displayedModel(response, 'claude', 'default')).toBe('default')
  })
})
