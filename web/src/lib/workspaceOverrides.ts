import type { ModelsResponse, ProviderOverrideField, ProviderOverrides, WorkspaceInfo } from './types'

// Fixed field order: JSON.stringify of the workspace form is the dirty check,
// so an override map must always serialize the same way.
const FIELDS: ProviderOverrideField[] = ['model', 'thinking', 'permission_mode', 'insights_model']

/** Keep only the four known fields as trimmed non-empty strings; sorted providers, fixed field order. */
export function normalizeOverrides(raw: unknown): ProviderOverrides {
  const out: ProviderOverrides = {}
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out
  const source = raw as Record<string, unknown>
  for (const provider of Object.keys(source).sort()) {
    const entry = source[provider]
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue
    const fields: Partial<Record<ProviderOverrideField, string>> = {}
    for (const field of FIELDS) {
      const value = (entry as Record<string, unknown>)[field]
      if (typeof value !== 'string') continue
      const trimmed = value.trim()
      if (trimmed) fields[field] = trimmed
    }
    if (Object.keys(fields).length) out[provider] = fields
  }
  return out
}

/** A new normalized map with one field set; an empty value deletes it. */
export function setOverride(
  overrides: ProviderOverrides,
  provider: string,
  field: ProviderOverrideField,
  value: string,
): ProviderOverrides {
  return normalizeOverrides({
    ...overrides,
    [provider]: { ...overrides[provider], [field]: value },
  })
}

/** The model a new chat in the workspace starts on: its override, else the engine default. */
export function effectiveWorkspaceDefaultModel(
  ws: Pick<WorkspaceInfo, 'default_provider' | 'provider_overrides'> | undefined,
  models: ModelsResponse | null,
): string {
  const provider = ws?.default_provider || ''
  return ws?.provider_overrides?.[provider]?.model
    || models?.provider_defaults?.[provider]
    || models?.default
    || ''
}
