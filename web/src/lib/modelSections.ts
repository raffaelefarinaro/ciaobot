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

/** The CLI's own "let me choose" entry: Claude Code lists it as `default`. */
export const AGENT_DEFAULT_MODEL = 'default'

/** The short name of a provider, the CLI the chat runs on: "Claude Code". */
export function providerShortLabel(response: ModelsResponse | null | undefined, provider: string): string {
  return response?.providers?.find((item) => item.id === provider)?.short_label || provider
}

/**
 * The concrete model a provider's `default` entry runs, as the agent
 * describes it ("Opus 4.5"), or '' when it has not said.
 */
export function agentDefaultModelName(response: ModelsResponse | null | undefined, provider: string): string {
  const entry = response?.provider_catalog_evidence?.[provider]?.models
    ?.find((item) => item.id === AGENT_DEFAULT_MODEL)
  return entry?.description?.trim() || ''
}

/**
 * The readable name of one model of one provider. The agent's `default`
 * entry is named by the model it runs, never by the word "default"; when the
 * agent has not said which model that is, the name is '' and the caller shows
 * the provider alone.
 */
export function modelName(response: ModelsResponse | null | undefined, provider: string, model: string): string {
  const id = model.trim()
  if (!id || id === AGENT_DEFAULT_MODEL) return agentDefaultModelName(response, provider)
  return modelDisplayName(id)
}

/** "Claude Code · Opus 4.5", or the provider alone when the model has no name yet. */
export function providerModelLabel(response: ModelsResponse | null | undefined, provider: string, model: string): string {
  const name = modelName(response, provider, model)
  const short = providerShortLabel(response, provider)
  return name ? `${short} · ${name}` : short
}

/** The readable name of every model in a section, keyed by its raw id. */
function labelsFor(response: ModelsResponse, provider: string, models: string[]): Record<string, string> {
  const labels: Record<string, string> = {}
  for (const model of models) labels[model] = modelName(response, provider, model) || providerShortLabel(response, provider)
  return labels
}

/**
 * The badges a model carries: the provider namespace the model's own id
 * states. The namespace is taken from the id and nowhere else, so a tier
 * alias ("opus") is never attributed to a vendor. The provider default is not
 * a badge: pickers highlight the default as the current model instead.
 */
function badgesFor(models: string[]): Record<string, string[]> {
  const badges: Record<string, string[]> = {}
  for (const model of models) {
    const provider = modelProviderPrefix(model)
    if (provider) badges[model] = [provider]
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
    const badges = badgesFor(models)
    sections.push({
      key: sectionKeyForProvider(id),
      label: providerShortLabel(response, id),
      models,
      modelLabels: labelsFor(response, id, models),
      ...(Object.keys(badges).length ? { modelBadges: badges } : {}),
    })
  }
  return sections
}
