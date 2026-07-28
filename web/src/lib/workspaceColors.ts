/** Per-workspace accent presets (Option A: accents only). */

export type WorkspaceColorId = 'pink' | 'cyan' | 'amber' | 'emerald' | 'violet'

export const DEFAULT_WORKSPACE_COLOR: WorkspaceColorId = 'pink'

export interface WorkspaceColorPreset {
  id: WorkspaceColorId
  label: string
  /** Swatch shown in Settings (dark-theme accent). */
  swatch: string
}

export const WORKSPACE_COLOR_PRESETS: WorkspaceColorPreset[] = [
  { id: 'pink', label: 'Ciao Pink', swatch: '#ff4d6d' },
  { id: 'cyan', label: 'Terminal Cyan', swatch: '#38bdf8' },
  { id: 'amber', label: 'Console Amber', swatch: '#fb923c' },
  { id: 'emerald', label: 'Emerald Mint', swatch: '#34d399' },
  { id: 'violet', label: 'Violet Iris', swatch: '#a78bfa' },
]

const PRESET_IDS = new Set<string>(WORKSPACE_COLOR_PRESETS.map((p) => p.id))

export function normalizeWorkspaceColor(raw: string | null | undefined): WorkspaceColorId {
  const cleaned = (raw || '').trim().toLowerCase()
  if (PRESET_IDS.has(cleaned)) return cleaned as WorkspaceColorId
  return DEFAULT_WORKSPACE_COLOR
}

/** Resolve accent id for a workspace record (or pink if unknown). */
export function colorForWorkspace(
  workspace: { name?: string; color?: string | null } | null | undefined,
): WorkspaceColorId {
  return normalizeWorkspaceColor(workspace?.color)
}
