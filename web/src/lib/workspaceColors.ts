/** Per-workspace accent presets (Option A: accents only). */

export type WorkspaceColorId = 'amber' | 'pink' | 'cyan' | 'emerald' | 'violet' | 'blue' | 'teal' | 'lime' | 'fuchsia'

/**
 * What `#ciao-app` wears: a preset, or `neutral` while the active workspace's
 * own colour is not known yet, so the first paint never flashes another accent.
 */
export type WorkspaceAccent = WorkspaceColorId | 'neutral'

/** Orange, not the old pink: a pink-red primary read as an error. */
export const DEFAULT_WORKSPACE_COLOR: WorkspaceColorId = 'amber'

export interface WorkspaceColorPreset {
  id: WorkspaceColorId
  label: string
  /** Swatch shown in Settings (dark-theme accent). */
  swatch: string
  /** Hidden behind the picker's "+" until chosen or opened. */
  extra?: true
}

export const WORKSPACE_COLOR_PRESETS: WorkspaceColorPreset[] = [
  { id: 'amber', label: 'Ciao Orange', swatch: '#fb923c' },
  { id: 'pink', label: 'Rose Pink', swatch: '#ff4d6d' },
  { id: 'cyan', label: 'Terminal Cyan', swatch: '#38bdf8' },
  { id: 'emerald', label: 'Emerald Mint', swatch: '#34d399' },
  { id: 'violet', label: 'Violet Iris', swatch: '#a78bfa' },
  { id: 'blue', label: 'Sky Blue', swatch: '#60a5fa', extra: true },
  { id: 'teal', label: 'Deep Teal', swatch: '#2dd4bf', extra: true },
  { id: 'lime', label: 'Lime Pop', swatch: '#a3e635', extra: true },
  { id: 'fuchsia', label: 'Fuchsia Neon', swatch: '#e879f9', extra: true },
]

const PRESET_IDS = new Set<string>(WORKSPACE_COLOR_PRESETS.map((p) => p.id))

export function normalizeWorkspaceColor(raw: string | null | undefined): WorkspaceColorId {
  const cleaned = (raw || '').trim().toLowerCase()
  if (PRESET_IDS.has(cleaned)) return cleaned as WorkspaceColorId
  return DEFAULT_WORKSPACE_COLOR
}

/** Resolve accent id for a workspace record (or the default if unknown). */
export function colorForWorkspace(
  workspace: { name?: string; color?: string | null } | null | undefined,
): WorkspaceColorId {
  return normalizeWorkspaceColor(workspace?.color)
}

/** This browser's last-seen accent, so a reload paints it before the workspace list arrives. */
const REMEMBERED_KEY = 'ciao-workspace-color'

export function rememberWorkspaceColor(color: WorkspaceColorId): void {
  try { localStorage.setItem(REMEMBERED_KEY, color) } catch { /* storage blocked: next load is neutral */ }
}

function rememberedWorkspaceColor(): WorkspaceColorId | null {
  try {
    const raw = localStorage.getItem(REMEMBERED_KEY)
    return raw && PRESET_IDS.has(raw) ? (raw as WorkspaceColorId) : null
  } catch {
    return null
  }
}

type WorkspaceRecord = { name: string; color?: string | null }

/**
 * One workspace's accent. Once the engine's list has arrived it decides; before
 * that the colour is unknown, so the active workspace wears the colour this
 * browser last showed (neutral on a first visit) and any other one is neutral.
 */
export function workspaceAccent(
  workspaces: readonly WorkspaceRecord[],
  name: string | undefined,
  active?: string,
): WorkspaceAccent {
  if (workspaces.length) return colorForWorkspace(workspaces.find((item) => item.name === name))
  if (active === undefined || name === active) return rememberedWorkspaceColor() ?? 'neutral'
  return 'neutral'
}

/** The accent `#ciao-app` wears for the active workspace. */
export function activeWorkspaceAccent(
  workspaces: readonly WorkspaceRecord[],
  active: string,
): WorkspaceAccent {
  return workspaceAccent(workspaces, active)
}
