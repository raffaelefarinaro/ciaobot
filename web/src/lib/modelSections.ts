import type { ModelsResponse, RoutineSettings } from './types'

export interface ModelSection {
  key: string
  label: string
  models: string[]
  badge?: string
  modelBadges?: Record<string, string[]>
  disabled?: boolean
  hint?: string
}

type AliasTierMap = Record<string, Record<string, string>>

const TIER_LABELS: Record<string, string> = {
  river: 'River',
  lake: 'Lake',
  sea: 'Sea',
  ocean: 'Ocean',
}

const ANTHROPIC_MODELS = ['river', 'lake', 'sea', 'ocean']
const ANTHROPIC_BADGES = {
  river: ['Haiku'],
  lake: ['Sonnet'],
  sea: ['Opus'],
  ocean: ['Fable'],
}
const CODEX_ALIAS_LABELS: Record<string, string> = {
  river: 'Luna',
  lake: 'Terra',
  sea: 'Sol',
}

export function parseModelList(raw: string): string[] {
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

export function serializeModelList(models: string[]): string {
  return parseModelList(models.join(',')).join(',')
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

function addBadge(badges: Record<string, string[]>, model: string, badge: string) {
  const key = model.trim()
  if (!key || !badge) return
  const existing = badges[key] || []
  if (!existing.includes(badge)) existing.push(badge)
  badges[key] = existing
}

export function providerModelBadges(
  provider: string,
  models: string[],
  aliasTiers: AliasTierMap | undefined | null,
  localModels: string[] = [],
): Record<string, string[]> {
  const badges: Record<string, string[]> = {}
  const available = new Set(models)
  for (const local of localModels) {
    if (available.has(local)) addBadge(badges, local, 'local')
  }
  const tiers = aliasTiers?.[provider] || {}
  for (const [tier, model] of Object.entries(tiers)) {
    if (!model || !available.has(model)) continue
    addBadge(badges, model, TIER_LABELS[tier] || tier)
  }
  return badges
}

/**
 * Build sections for the chat / schedule pickers from `/api/models`.
 */
export function sectionsFromModelsResponse(response: ModelsResponse | null): ModelSection[] {
  if (!response) return []
  const sections: ModelSection[] = []

  const ollamaModels = orderedUnique(response.ollama_models || [])
  const openrouterModels = orderedUnique(response.openrouter_models || [])

  sections.push({ key: 'anthropic', label: 'Anthropic', models: ANTHROPIC_MODELS, modelBadges: ANTHROPIC_BADGES })

  const codexTiers = response.alias_tiers?.codex || {}
  const codexModels = ['river', 'lake', 'sea'].filter((tier) => Boolean(codexTiers[tier]))
  if (codexModels.length) {
    const modelBadges: Record<string, string[]> = {}
    for (const tier of codexModels) {
      modelBadges[tier] = [CODEX_ALIAS_LABELS[tier], codexTiers[tier]].filter(Boolean)
    }
    sections.push({ key: 'codex', label: 'OpenAI Codex', models: codexModels, modelBadges })
  }

  // Ollama: cloud allowlist plus locally discovered daemon models.
  const local = orderedUnique(response.ollama_local_models || [])
  const allOllama = orderedUnique([...local, ...ollamaModels])
  if (allOllama.length) {
    sections.push({
      key: 'ollama',
      label: 'Ollama',
      models: allOllama,
      modelBadges: providerModelBadges('ollama', allOllama, response.alias_tiers, local),
    })
  }

  if (openrouterModels.length) {
    sections.push({
      key: 'openrouter',
      label: 'OpenRouter',
      models: openrouterModels,
      modelBadges: providerModelBadges('openrouter', openrouterModels, response.alias_tiers),
    })
  }

  return sections
}

/**
 * Build sections for the settings pickers from `/api/settings/routines`.
 */
export function sectionsFromModelOptions(
  options: RoutineSettings['model_options'] | undefined | null,
  backends: Record<string, boolean> = {},
  aliasTiers: AliasTierMap | undefined | null = undefined,
): ModelSection[] {
  if (!options) return []
  const sections: ModelSection[] = []

  if (options.anthropic?.length) {
    sections.push({ key: 'anthropic', label: 'Anthropic', models: ANTHROPIC_MODELS, modelBadges: ANTHROPIC_BADGES })
  }

  const ollamaAvailable = !!backends.ollama
  const local = orderedUnique(options.ollama_local || [])
  const ollama = orderedUnique([...local, ...(options.ollama_cloud || [])])
  if (ollama.length) {
    sections.push({
      key: 'ollama',
      label: 'Ollama',
      models: ollama,
      modelBadges: providerModelBadges('ollama', ollama, aliasTiers, local),
      disabled: !ollamaAvailable,
      hint: ollamaAvailable ? undefined : 'Set an Ollama API key or install local models to enable this section.',
    })
  }

  const openrouterAvailable = !!backends.openrouter
  const openrouter = orderedUnique(options.openrouter || [])
  if (openrouter.length) {
    sections.push({
      key: 'openrouter',
      label: 'OpenRouter',
      models: openrouter,
      modelBadges: providerModelBadges('openrouter', openrouter, aliasTiers),
      disabled: !openrouterAvailable,
      hint: openrouterAvailable ? undefined : 'Set OPENROUTER_API_KEY to enable OpenRouter models.',
    })
  }

  return sections
}
