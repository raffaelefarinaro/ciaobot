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

  sections.push({
    key: 'anthropic',
    label: 'Anthropic',
    models: orderedUnique(response.models || []),
  })

  const codexModels = orderedUnique(response.codex_models || response.provider_models?.codex || [])
  if (codexModels.length) {
    sections.push({
      key: 'codex',
      label: 'OpenAI Codex',
      models: codexModels,
    })
  }

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
    })
  }

  return sections
}
