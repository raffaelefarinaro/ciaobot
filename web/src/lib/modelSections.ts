import { modelDisplayName, modelProviderPrefix } from './modelLabels'
import type { ModelsResponse, RuntimeProvider } from './types'

// The picker names the Claude section 'anthropic' (the models are Anthropic's,
// the runner is Claude Code). Every other provider's section key is its id.
export function sectionKeyForProvider(provider: string): string {
  return provider === 'claude' ? 'anthropic' : provider
}

export function providerForModelSection(sectionKey: string): RuntimeProvider {
  return sectionKey === 'anthropic' ? 'claude' : (sectionKey as RuntimeProvider)
}

export interface ModelSection {
  key: string
  label: string
  models: string[]
  badge?: string
  modelBadges?: Record<string, string[]>
  modelLabels?: Record<string, string>
  disabled?: boolean
  hint?: string
}

function orderedUnique(models: string[]): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  for (const raw of models) {
    const model = raw.trim()
    if (!model || seen.has(model)) continue
    seen.add(model)
    result.push(model)
  }
  return result
}

/** The readable name of every model in a section, keyed by its raw id. */
function labelsFor(models: string[]): Record<string, string> {
  const labels: Record<string, string> = {}
  for (const model of models) labels[model] = modelDisplayName(model)
  return labels
}

/**
 * The badges a model carries: the provider's default, plus the provider
 * namespace the model's own id states. The namespace is taken from the id and
 * nowhere else, so a tier alias ("opus") is never attributed to a vendor.
 */
function badgesFor(models: string[], defaultModel?: string): Record<string, string[]> {
  const badges: Record<string, string[]> = {}
  for (const model of models) {
    const list: string[] = []
    if (defaultModel && model === defaultModel) list.push('Default')
    const provider = modelProviderPrefix(model)
    if (provider) list.push(provider)
    if (list.length) badges[model] = list
  }
  return badges
}

/**
 * Build sections for the chat / schedule pickers from `/api/models`: one
 * section per provider that reports models, in registry order, labelled by
 * the registry's short label.
 */
export function sectionsFromModelsResponse(response: ModelsResponse | null): ModelSection[] {
  if (!response) return []
  const catalogs = response.provider_models ?? {}
  const registry = response.providers ?? []
  // Registry order first; the server lists ACP providers ahead of the
  // built-in ones in provider_models, so the catalog's own order is not used.
  const ids = orderedUnique([...registry.map((item) => item.id), ...Object.keys(catalogs)])
  const sections: ModelSection[] = []
  for (const id of ids) {
    const models = orderedUnique(catalogs[id] ?? [])
    if (!models.length) continue
    const defaultModel = response.provider_defaults?.[id]
    const badges = badgesFor(models, defaultModel)
    sections.push({
      key: sectionKeyForProvider(id),
      label: registry.find((item) => item.id === id)?.short_label ?? id,
      models,
      modelLabels: labelsFor(models),
      ...(Object.keys(badges).length ? { modelBadges: badges } : {}),
    })
  }
  return sections
}
