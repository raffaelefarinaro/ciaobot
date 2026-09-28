/**
 * Display name for an MCP server as the CLIs report it: Claude Code's
 * connector servers carry a `claude_ai_` prefix (and tool names an
 * `mcp__` one), which says nothing to the reader.
 */
export function formatConnectorLabel(name: string): string {
  const clean = name.replace(/^mcp__/, '').replace(/^claude_ai_/, '')
  if (clean === 'Google_Cloud_BigQuery') return 'BigQuery'
  if (clean === 'incident_io') return 'incident.io'
  return clean
}
