import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import type { ModelsResponse, RoutineSettings } from '../../lib/types'
import {
  DEFAULT_MODEL_SELECTION,
  DEFAULT_THINKING_SELECTION,
  useProviderChoices,
} from '../useProviderChoices'

describe('useProviderChoices', () => {
  it('builds a Default section naming the engine default', () => {
    const routines = ref({
      model_options: { anthropic: ['opus'] },
      provider_default_models: { claude: 'opus' },
    } as unknown as RoutineSettings)
    const workspaceModels = ref<ModelsResponse | null>(null)
    const { providerSectionsWithDefault, providerThinkingOptions } = useProviderChoices({
      routines,
      workspaceModels,
    })
    const sections = providerSectionsWithDefault(
      'claude',
      DEFAULT_MODEL_SELECTION,
      (effective) => `Automatic (${effective})`,
      true,
    )
    expect(sections[0].modelLabels?.[DEFAULT_MODEL_SELECTION]).toBe('Automatic (opus)')
    expect(providerThinkingOptions('claude')[0].value).toBe(DEFAULT_THINKING_SELECTION)
  })

  it("lists the agent's default once: as the Automatic entry, named by the model it runs", () => {
    const routines = ref({
      model_options: { anthropic: ['default', 'opus', 'sonnet'] },
      provider_default_models: {},
    } as unknown as RoutineSettings)
    const workspaceModels = ref({
      models: [], default: 'default',
      provider_models: { claude: ['default', 'opus', 'sonnet'] },
      provider_defaults: { claude: 'default' },
      providers: [{ id: 'claude', label: 'Anthropic (via Claude Code)', short_label: 'Claude Code', capabilities: {} }],
      provider_catalog_evidence: { claude: { status: 'observed', source: null, reason: null, models: [
        { id: 'default', label: 'Default (recommended)', description: 'Opus 4.5' },
        { id: 'opus', label: 'Opus 4.5' },
        { id: 'sonnet', label: 'Sonnet 4.5' },
      ] } },
    } as unknown as ModelsResponse)
    const { providerSectionsWithDefault, modelNameFor } = useProviderChoices({ routines, workspaceModels })
    const sections = providerSectionsWithDefault(
      'claude', DEFAULT_MODEL_SELECTION, (effective) => `Automatic (${modelNameFor('claude', effective)})`, true,
    )
    expect(sections[0].label).toBe('')
    expect(sections[0].modelLabels?.[DEFAULT_MODEL_SELECTION]).toBe('Automatic (Opus 4.5)')
    expect(sections[1].label).toBe('Claude Code')
    expect(sections[1].models).toEqual(['opus', 'sonnet'])
    expect(sections[1].modelLabels).toEqual({ opus: 'Opus 4.5', sonnet: 'Sonnet 4.5' })
    // An override stored as `default` reads, and is highlighted, as that row.
    expect(sections[1].modelAliases).toEqual({ default: 'opus' })
  })

  it('lists every provider from /api/models including codex', () => {
    const routines = ref({
      model_options: { anthropic: ['opus'] },
      provider_default_models: {},
    } as unknown as RoutineSettings)
    const descriptor = (id: string, label: string) => ({ id, label, short_label: label, capabilities: {} })
    const workspaceModels = ref({
      models: ['opus'],
      default: 'opus',
      provider_models: {
        claude: ['opus'],
        opencode: [],
        codex: ['gpt-5.5', 'gpt-5.4-mini'],
      },
      provider_defaults: {},
      providers: [
        descriptor('claude', 'Anthropic (via Claude Code)'),
        descriptor('opencode', 'opencode'),
        descriptor('codex', 'OpenAI (via Codex)'),
      ],
    } as unknown as ModelsResponse)
    const { aliasProviderSections, getProviderSection } = useProviderChoices({ routines, workspaceModels })
    expect(aliasProviderSections.value.map((section) => section.key)).toEqual(['claude', 'codex'])
    expect(getProviderSection('codex')?.label).toBe('OpenAI (via Codex)')
    expect(getProviderSection('codex')?.options).toEqual(['gpt-5.5', 'gpt-5.4-mini'])
    expect(getProviderSection('opencode')).toBeUndefined()
  })

  it('follows changes to the refs it was given, not copies of them', () => {
    const routines = ref({
      model_options: { anthropic: ['opus'] },
      provider_default_models: { claude: 'opus' },
    } as unknown as RoutineSettings)
    const workspaceModels = ref<ModelsResponse | null>(null)
    const { providerSectionsWithDefault } = useProviderChoices({ routines, workspaceModels })
    const label = () => providerSectionsWithDefault(
      'claude',
      DEFAULT_MODEL_SELECTION,
      (effective) => `Automatic (${effective})`,
      true,
    )[0].modelLabels?.[DEFAULT_MODEL_SELECTION]
    expect(label()).toBe('Automatic (opus)')
    routines.value = {
      ...routines.value,
      model_options: { anthropic: ['opus', 'sonnet'] },
      provider_default_models: { claude: 'sonnet' },
    } as unknown as RoutineSettings
    expect(label()).toBe('Automatic (sonnet)')
  })
})
