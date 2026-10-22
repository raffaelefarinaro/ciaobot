import { computed, type Ref } from 'vue'
import type { ModelSection } from '../lib/modelSections'
import { modelDisplayName, modelProviderPrefix } from '../lib/modelLabels'
import type { ModelsResponse, RoutineSettings, RuntimeProvider } from '../lib/types'

// Every provider with models is a runtime provider now.
export type AliasProviderKey = RuntimeProvider

export type AliasProviderSection = {
  key: AliasProviderKey
  label: string
  options: string[]
  configurable: boolean
  // Whether the provider is signed in and reporting models. Routine selectors
  // filter to available sections.
  available: boolean
}

export const DEFAULT_MODEL_SELECTION = '__ciao_default_model__'
export const DEFAULT_THINKING_SELECTION = '__ciao_thinking_default__'
export const DEFAULT_MODE_SELECTION = '__ciao_mode_default__'
export const DEFAULT_INSIGHTS_SELECTION = '__ciao_insights_default__'

// PWA-facing names; "manual" maps to the BridgeMode "normal" on the backend.
export const PROVIDER_MODE_OPTIONS = [
  { value: DEFAULT_MODE_SELECTION, label: 'Automatic (app default)' },
  { value: 'manual', label: 'Manual — ask for every action' },
  { value: 'auto', label: 'Auto — allow safe work, ask on risky' },
  { value: 'bypass', label: 'Bypass — allow everything' },
]

/**
 * The option sources the Providers page offers: provider sections, the
 * "Default" entry, thinking levels and their labels. The Overview card reads
 * the same ones, so the two cannot drift.
 */
export function useProviderChoices(deps: {
  routines: Ref<RoutineSettings | null>
  workspaceModels: Ref<ModelsResponse | null>
}) {
  const { routines, workspaceModels } = deps

  function parseModelList(raw: string): string[] {
    const seen = new Set<string>()
    const models: string[] = []
    for (const item of raw.split(',')) {
      const model = item.trim()
      if (!model || seen.has(model)) continue
      seen.add(model)
      models.push(model)
    }
    return models
  }

  const aliasProviderSections = computed<AliasProviderSection[]>(() => {
    const settings = routines.value
    if (!settings) return []
    // Filtered to available backends — used by the routine model selectors,
    // which must not offer a backend that isn't configured.
    const sections: AliasProviderSection[] = [
      {
        key: 'claude',
        label: 'Anthropic (via Claude Code)',
        // The Anthropic tiers are real model ids, and config._operator_default_model
        // honors a `claude` entry in provider_default_models, so this picker edits
        // the same override every other provider's does.
        options: settings.model_options.anthropic || [],
        configurable: true,
        available: true,
      },
    ]
    // opencode can serve routines once its catalog is non-empty.
    const opencodeModels = parseModelList((
      workspaceModels.value?.opencode_models
      || workspaceModels.value?.provider_models?.opencode
      || []
    ).join(','))
    if (opencodeModels.length) {
      sections.push({
        key: 'opencode',
        label: 'opencode',
        options: opencodeModels,
        configurable: true,
        available: true,
      })
    }
    return sections
  })

  function getProviderSection(provider: string): AliasProviderSection | undefined {
    return aliasProviderSections.value.find((s) => s.key === provider)
  }

  function providerDefaultModelOverride(provider: AliasProviderKey): string {
    return routines.value?.provider_default_models?.[provider] || ''
  }

  // The readable name of every model in a section, keyed by its raw id, and the
  // provider namespace each id states. Both come from the shared helpers in
  // lib/modelLabels, so a model reads the same in Settings as it does in the
  // chat and schedule pickers.
  function modelLabelsFor(models: string[]): Record<string, string> {
    const labels: Record<string, string> = {}
    for (const model of models) labels[model] = modelDisplayName(model)
    return labels
  }

  function modelBadgesFor(models: string[]): Record<string, string[]> {
    const badges: Record<string, string[]> = {}
    for (const model of models) {
      // The namespace the id itself states, and nothing else: a tier alias
      // names no provider.
      const namespace = modelProviderPrefix(model)
      if (namespace) badges[model] = [namespace]
    }
    return badges
  }

  // The alias-provider section entry (models + disabled state) is identical
  // wherever a picker offers one provider's catalog; only the surrounding
  // sections (e.g. the "Default"/Automatic entry above) differ per caller.
  function aliasSectionEntry(provider: string): ModelSection {
    const section = aliasProviderSections.value.find((s) => s.key === provider)
    const models = section?.options || []
    return {
      key: provider,
      label: section?.label || provider,
      models,
      modelLabels: modelLabelsFor(models),
      modelBadges: modelBadgesFor(models),
      disabled: !section?.available,
    }
  }

  // One provider's catalog behind a single "Default" entry, which the caller
  // names (the sentinel it stores as "unset" and the label it shows). The entry
  // names the readable model Automatic resolves to, and its badge names that
  // model's provider namespace, nothing else. The raw id is not on this entry —
  // the option's own data is the sentinel — it stays on that model's entry in
  // the provider section below.
  function providerSectionsWithDefault(
    provider: AliasProviderKey,
    sentinel: string,
    label: (effective: string) => string,
    namesResolvedModel = false,
  ): ModelSection[] {
    const effective = providerDefaultModelEffective(provider)
    const namespace = modelProviderPrefix(effective)
    return [
      {
        key: 'default',
        label: 'Default',
        models: [sentinel],
        modelLabels: { [sentinel]: label(effective) },
        ...(namesResolvedModel && namespace
          ? { modelBadges: { [sentinel]: [namespace] } }
          : {}),
      },
      aliasSectionEntry(provider),
    ]
  }

  function providerDefaultModelEffective(provider: AliasProviderKey): string {
    const selected = providerDefaultModelOverride(provider)
    if (selected) return selected
    const explicit = workspaceModels.value?.provider_defaults?.[provider]
    if (explicit) return explicit
    // Fall back to the provider's first discovered model so a routine pick can
    // store a concrete id even before an operator default is configured.
    const section = aliasProviderSections.value.find((s) => s.key === provider)
    return section?.options?.[0] || ''
  }

  function providerThinkingOptions(provider: AliasProviderKey): { value: string; label: string }[] {
    const levels = workspaceModels.value?.thinking_levels?.[provider] || []
    return [
      {
        value: DEFAULT_THINKING_SELECTION,
        label: 'Automatic (provider default)',
      },
      ...levels.map((level) => ({ value: level, label: level })),
    ]
  }

  return {
    aliasProviderSections,
    getProviderSection,
    modelLabelsFor,
    modelBadgesFor,
    aliasSectionEntry,
    providerSectionsWithDefault,
    providerDefaultModelOverride,
    providerDefaultModelEffective,
    providerThinkingOptions,
  }
}
