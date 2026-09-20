/**
 * Where a listed agent asset came from, and how Settings labels it.
 *
 * Skills, subagents, commands and MCP servers are all rendered as the same
 * `skill-row` disclosure in Settings, each with an origin badge. The rules
 * behind that badge were private to `SettingsView.vue`; they live here so the
 * panels split out of that view can share one copy, and so the classification
 * can be tested on its own.
 */

export type AssetOrigin = 'builtin' | 'custom' | 'installed' | 'global'

export function assetOriginLabel(origin: AssetOrigin): string {
  if (origin === 'custom') return 'Custom'
  if (origin === 'installed') return 'Installed'
  if (origin === 'global') return 'Global'
  return 'Built-in'
}

export function assetOriginClass(origin: AssetOrigin): string {
  if (origin === 'custom') return 'badge badge--success command-source'
  if (origin === 'builtin') return 'badge badge--builtin command-source'
  return 'badge badge--muted command-source'
}

export function commandOrigin(command: { editable?: boolean; scope?: string }): AssetOrigin {
  // Scope is definitive when set: stock commands/subagents are seeded into
  // the same editable location as custom ones (so users can override them
  // in place), so `editable` alone can't distinguish "built-in" from
  // "custom" — it's only a fallback for the rare case scope is unset.
  if (command.scope === 'custom') return 'custom'
  if (command.scope === 'built-in') return 'builtin'
  if (command.scope === 'global') return 'global'
  if (command.scope === 'installed') return 'installed'
  return command.editable ? 'custom' : 'installed'
}

export function subagentOrigin(agent: { editable?: boolean; scope?: string }): AssetOrigin {
  return commandOrigin(agent)
}

/** Project `.mcp.json` servers are always operator-authored. */
export function mcpServerOrigin(_srv: { name?: string; source?: string }): AssetOrigin {
  return 'custom'
}
