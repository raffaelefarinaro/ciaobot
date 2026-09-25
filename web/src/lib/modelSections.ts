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

/**
 * Build sections for the chat / schedule pickers from `/api/models`.
 */
export function sectionsFromModelsResponse(response: ModelsResponse | null): ModelSection[] {
  if (!response) return []
  const sections: ModelSection[] = []

  // Tag each provider's default so the picker says what Automatic resolves to.
  const defaultBadge = (model: string | undefined, models: string[]) =>
    model && models.includes(model) ? { modelBadges: { [model]: ['Default'] } } : {}

  const anthropicModels = orderedUnique(response.models || [])
  sections.push({
    key: 'anthropic',
    label: 'Anthropic',
    models: anthropicModels,
    ...defaultBadge(response.provider_defaults?.claude || response.default, anthropicModels),
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
      ...defaultBadge(response.provider_defaults?.opencode, opencodeModels),
    })
  }

  return sections
}
