// What Permission mode "bypass" means, and where each agent CLI documents its
// own permission settings. Ciaobot enforces no file limits of its own, so the
// CLI's settings are the only boundary a user can tighten.
export const BYPASS_NOTE =
  'Bypass gives the agent your full user access: every file and command this account can reach. Ciaobot adds no file limits of its own; the agent CLI\'s permission settings apply.'

export const PERMISSION_DOCS: Record<'claude' | 'opencode', { label: string; href: string }> = {
  claude: { label: 'Claude Code permissions', href: 'https://code.claude.com/docs/en/permissions' },
  opencode: { label: 'OpenCode permissions', href: 'https://opencode.ai/docs/permissions/' },
}

/** The docs link for a provider id, or undefined for a provider with none. */
export function permissionDocsFor(provider: string): { label: string; href: string } | undefined {
  return provider === 'claude' || provider === 'opencode' ? PERMISSION_DOCS[provider] : undefined
}
