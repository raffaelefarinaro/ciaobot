import type { ModelsResponse } from './types'

/** Remove catalogs whose provider auth is uncertain without touching peer providers. */
export function withoutProviderCatalogs(
  response: ModelsResponse,
  providers: readonly string[],
): ModelsResponse {
  if (!providers.length) return response
  const providerModels = { ...response.provider_models }
  const providerDefaults = { ...response.provider_defaults }
  const thinkingLevels = { ...response.thinking_levels }
  const evidence = { ...response.provider_catalog_evidence }
  const backends = { ...response.backends }
  const removedModels = new Set<string>()
  for (const provider of providers) {
    for (const model of providerModels[provider] ?? []) removedModels.add(model)
    if (provider === 'opencode') {
      for (const model of response.opencode_models ?? []) removedModels.add(model)
    }
    delete providerModels[provider]
    delete providerDefaults[provider]
    delete thinkingLevels[provider]
    delete evidence[provider]
    delete backends[provider === 'claude' ? 'anthropic' : provider]
  }
  const stillOffered = new Set(Object.values(providerModels).flat())
  const reasoningLevels = { ...response.model_reasoning_levels }
  for (const model of removedModels) {
    if (!stillOffered.has(model)) delete reasoningLevels[model]
  }
  return {
    ...response,
    ...(providers.includes('claude') ? { default: '' } : {}),
    provider_models: providerModels,
    provider_defaults: providerDefaults,
    thinking_levels: thinkingLevels,
    provider_catalog_evidence: evidence,
    backends,
    model_reasoning_levels: reasoningLevels,
    ...(providers.includes('opencode') ? { opencode_models: [] } : {}),
  }
}
