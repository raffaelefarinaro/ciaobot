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
  /**
   * A stored value that is shown as another row of the picker: Claude Code's
   * `default` when a concrete row stands for it. The trigger names it by that
   * row and the row is highlighted.
   */
  modelAliases?: Record<string, string>
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

/** A name short enough to be a model's name rather than a sentence. */
function isShortName(text: string): boolean {
  return text.length <= 32 && text.split(/\s+/).length <= 4
}

/** The adapter's note on the `default` entry: the part before " · ", capped. */
function defaultDescriptionText(description: string | undefined): string {
  return (description || '').split(' · ')[0].trim().slice(0, 80)
}

/** A Claude model id's family and version: "claude-opus-4-5-20251101" -> Opus 4.5. */
function idFamilyVersion(text: string): { family: string; version: string } | null {
  const match = /(opus|sonnet|haiku|fable)[-_]?(\d+)(?:[-_.](\d{1,2})(?!\d))?/i.exec(text)
  if (!match) return null
  const family = match[1].charAt(0).toUpperCase() + match[1].slice(1).toLowerCase()
  return { family, version: match[3] ? `${match[2]}.${match[3]}` : match[2] }
}

function words(text: string): string[] {
  return text.toLowerCase().split(/[^a-z0-9.]+/).filter(Boolean)
}

/**
 * The model a `default` entry's description names, or ''. The adapter's
 * description is unchecked text: a display name ("Opus 4.5", "Opus"), a raw
 * model id ("claude-opus-4-5-20251101") or a sentence ("Use the default model
 * (currently Sonnet 4.5) · $3/$15 per Mtok"). Only a short name is kept; a
 * Claude id is read as family and version ("Opus 4.5"), any other id is made
 * readable; a sentence yields the model it names in "(currently …)", else
 * nothing, and the caller shows the provider alone.
 */
export function defaultDescriptionName(description: string | undefined): string {
  const text = defaultDescriptionText(description)
  if (!text) return ''
  const current = /\(currently ([^)]+)\)/i.exec(text)?.[1]?.trim()
  if (current) return isShortName(current) && !/default/i.test(current) ? current : ''
  if (/default/i.test(text)) return ''
  if (!/\s/.test(text) && /[-_/:]/.test(text)) {
    const parsed = idFamilyVersion(text)
    return parsed ? `${parsed.family} ${parsed.version}` : modelDisplayName(text)
  }
  return isShortName(text) ? text : ''
}

/** The agent's own label for one catalog entry, when it gave a usable one. */
function catalogLabel(response: ModelsResponse | null | undefined, provider: string, model: string): string {
  const label = response?.provider_catalog_evidence?.[provider]?.models
    ?.find((item) => item.id === model)?.label?.trim() || ''
  return label && label !== model && isShortName(label) && !/default/i.test(label) ? label : ''
}

/** A concrete model's readable name: the agent's label, else one made from the id. */
function concreteName(response: ModelsResponse | null | undefined, provider: string, model: string): string {
  return catalogLabel(response, provider, model) || modelDisplayName(model)
}

interface DefaultResolution {
  /** The concrete row the default stands for, '' when none fits or several do. */
  equivalent: string
  /** What the default runs, as far as the agent said: "Opus 4.5", "Sonnet", or ''. */
  name: string
}

/**
 * What a provider's `default` entry runs, matched against its concrete rows.
 * A full name ("Opus 4.5") matches a row of that name; a family alone
 * ("Opus") matches the one row whose name starts with it; a raw id
 * ("claude-opus-4-5-20251101") matches the row of that family and version.
 * When two rows fit ("Sonnet" with Sonnet 4.5 and Sonnet 4) nothing is
 * guessed: the default stays its own row, named by what the agent said.
 */
function resolveDefault(response: ModelsResponse | null | undefined, provider: string): DefaultResolution {
  const entry = response?.provider_catalog_evidence?.[provider]?.models
    ?.find((item) => item.id === AGENT_DEFAULT_MODEL)
  const name = defaultDescriptionName(entry?.description)
  if (!name) return { equivalent: '', name: '' }
  const rows = (response?.provider_models?.[provider] ?? [])
    .filter((model) => model !== AGENT_DEFAULT_MODEL)
    .map((model) => ({ model, name: concreteName(response, provider, model) }))
  const exact = rows.filter((row) => row.name.toLowerCase() === name.toLowerCase())
  if (exact.length === 1) return { equivalent: exact[0].model, name: exact[0].name }
  const wanted = words(name)
  const prefixed = rows.filter((row) => {
    const have = words(row.name)
    return wanted.length > 0 && wanted.every((word, index) => have[index] === word)
  })
  if (prefixed.length === 1) return { equivalent: prefixed[0].model, name: prefixed[0].name }
  return { equivalent: '', name }
}

/**
 * The concrete model a provider's `default` entry runs, as the agent
 * describes it ("Opus 4.5"), or '' when it has not said.
 */
export function agentDefaultModelName(response: ModelsResponse | null | undefined, provider: string): string {
  return resolveDefault(response, provider).name
}

/**
 * The catalog row the `default` entry stands for, or '' when none (or more
 * than one) fits. Pickers then list that row once, as the default, instead of
 * two look-alike rows.
 */
export function defaultEquivalent(response: ModelsResponse | null | undefined, provider: string): string {
  return resolveDefault(response, provider).equivalent
}

/**
 * The `default` row's own name when it is listed (no concrete row stands for
 * it): "Claude Code's pick (Sonnet)", or "Claude Code's pick" when the agent
 * did not say. Never the word "default".
 */
export function agentPickLabel(response: ModelsResponse | null | undefined, provider: string): string {
  const name = agentDefaultModelName(response, provider)
  const pick = `${providerShortLabel(response, provider)}'s pick`
  return name ? `${pick} (${name})` : pick
}

/**
 * The row that shows a stored model: the `default` entry (or no model) is
 * shown as its concrete equivalent when the catalog lists one.
 */
export function displayedModel(response: ModelsResponse | null | undefined, provider: string, model: string): string {
  const id = model.trim()
  if (id && id !== AGENT_DEFAULT_MODEL) return id
  return defaultEquivalent(response, provider) || id
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
  return concreteName(response, provider, id)
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
  for (const model of models) {
    labels[model] = model === AGENT_DEFAULT_MODEL
      ? agentPickLabel(response, provider)
      : modelName(response, provider, model)
  }
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
    // The `default` entry is listed once: as its concrete equivalent when
    // the catalog has one, otherwise as itself, named by the model it runs.
    const equivalent = defaultEquivalent(response, id)
    const models = orderedUnique(catalogs[id] ?? []).filter((model) => !(equivalent && model === AGENT_DEFAULT_MODEL))
    if (!models.length) continue
    const badges = badgesFor(models)
    sections.push({
      key: sectionKeyForProvider(id),
      label: providerShortLabel(response, id),
      models,
      modelLabels: labelsFor(response, id, models),
      ...(Object.keys(badges).length ? { modelBadges: badges } : {}),
      // A value stored as `default` reads, and is highlighted, as its row.
      ...(equivalent && (catalogs[id] ?? []).includes(AGENT_DEFAULT_MODEL)
        ? { modelAliases: { [AGENT_DEFAULT_MODEL]: equivalent } }
        : {}),
    })
  }
  return sections
}
