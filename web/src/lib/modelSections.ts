import { modelDisplayName, modelProviderPrefix } from './modelLabels'
import type { ModelsResponse, RuntimeProvider } from './types'

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
 * Build sections for the chat / schedule pickers from `/api/models`.
 */
export function sectionsFromModelsResponse(response: ModelsResponse | null): ModelSection[] {
  if (!response) return []
  const sections: ModelSection[] = []

  // Tag each provider's default so the picker says what Automatic resolves to,
  // and name the provider namespace a model id states.
  const modelBadgesFor = (model: string | undefined, models: string[]) => {
    const badges = badgesFor(models, model)
    return Object.keys(badges).length ? { modelBadges: badges } : {}
  }

  const anthropicModels = orderedUnique(response.models || [])
  sections.push({
    key: 'anthropic',
    label: 'Anthropic',
    models: anthropicModels,
    modelLabels: labelsFor(anthropicModels),
    ...modelBadgesFor(response.provider_defaults?.claude || response.default, anthropicModels),
  })

  // opencode is bring-your-own-provider: its catalog is whatever backends the
  // user connected, already namespaced as `providerID/modelID`.
  const opencodeModels = orderedUnique(
    response.opencode_models || response.provider_models?.opencode || [],
  )
  if (opencodeModels.length) {
    sections.push({
      key: 'opencode',
      label: 'opencode',
      models: opencodeModels,
      modelLabels: labelsFor(opencodeModels),
      ...modelBadgesFor(response.provider_defaults?.opencode, opencodeModels),
    })
  }

  return sections
}
